from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import get_settings
from app.core.schema import ActionCall
from app.core.schema import TaskState
from app.debugger.rollback import Debugger
from app.generation.agent_loop import AgentLoop
from app.generation.step_decider import StepDecider
from app.generation.state_updater import TaskStateUpdater
from app.generation.task_parser import TaskParser
from app.llm.client import OpenAICompatibleClient
from app.runtime.drission import DrissionRuntime
from app.runtime.static import StaticRuntime
from app.script.debugger import ScriptDebugger
from app.script.recorder import generate_script_from_trace
from app.script.runner import run_script
from app.trace.recorder import TraceRecorder


def main() -> None:
    parser = argparse.ArgumentParser(prog="new-drission")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-task", help="Create task_state.json from user request.")
    init.add_argument("--request", required=True)
    init.add_argument("--entry-url", required=True)
    init.add_argument("--resource", action="append", default=[])
    init.add_argument("--output", default="outputs/task_state.json")
    init.add_argument("--no-llm", action="store_true", help="Use deterministic fallback parser.")

    dry = sub.add_parser("dry-run", help="Load task_state and write an initial trace.")
    dry.add_argument("--task-state", required=True)
    dry.add_argument("--output-dir", default="outputs/dry_run")

    run = sub.add_parser("run-task", help="Run real browser rolling multimodal agent.")
    run.add_argument("--request", required=True)
    run.add_argument("--entry-url", required=True)
    run.add_argument("--resource", action="append", default=[])
    run.add_argument("--output-dir", default="outputs/run")
    run.add_argument("--max-steps", type=int, default=20)
    run.add_argument("--init-no-llm", action="store_true")

    script = sub.add_parser("generate-script", help="Generate replay script from a trace.json.")
    script.add_argument("--trace", required=True)
    script.add_argument("--output", default=None)
    script.add_argument("--replay-output-dir", default=None)

    verify = sub.add_parser("verify-script", help="Run a generated standalone script and capture logs.")
    verify.add_argument("--script", required=True)
    verify.add_argument("--output-dir", default=None)
    verify.add_argument("--timeout-seconds", type=int, default=1800)

    debug_script = sub.add_parser("debug-script", help="Run and LLM-patch a generated standalone script.")
    debug_script.add_argument("--script", required=True)
    debug_script.add_argument("--output-dir", default=None)
    debug_script.add_argument("--max-attempts", type=int, default=3)
    debug_script.add_argument("--timeout-seconds", type=int, default=1800)
    debug_script.add_argument("--no-llm", action="store_true")
    debug_script.add_argument(
        "--task-state",
        default=None,
        help="Optional task_state.json. When script debugging finds a flow/state error, ask the flow Debugger for a recovery plan.",
    )
    debug_script.add_argument(
        "--resume-max-steps",
        type=int,
        default=20,
        help="Max Generation steps after a script-debugger handoff.",
    )
    debug_script.add_argument(
        "--no-resume-generation",
        action="store_true",
        help="Only write flow_recovery_plan_XX.json; do not automatically resume Generation.",
    )

    args = parser.parse_args()
    if args.command == "init-task":
        _init_task(args)
    elif args.command == "dry-run":
        _dry_run(args)
    elif args.command == "run-task":
        _run_task(args)
    elif args.command == "generate-script":
        _generate_script(args)
    elif args.command == "verify-script":
        _verify_script(args)
    elif args.command == "debug-script":
        _debug_script(args)


def _init_task(args: argparse.Namespace) -> None:
    settings = get_settings()
    client = None if args.no_llm else OpenAICompatibleClient(settings.text)
    state = TaskParser(client).parse(request=args.request, entry_url=args.entry_url, resources=args.resource)
    output = Path(args.output)
    state.save(output)
    print(f"Wrote task state: {output}")
    print(f"Text model: {settings.text.model}; multimodal model: {settings.multimodal.model}")


def _dry_run(args: argparse.Namespace) -> None:
    state = TaskState.load(Path(args.task_state))
    runtime = StaticRuntime(state.site["current_url"])
    recorder = TraceRecorder(Path(args.output_dir))
    obs = runtime.observe()
    recorder.add(
        {
            "event": "observe",
            "url": obs.url,
            "title": obs.title,
            "rolling_context": state.rolling_context,
        }
    )
    print(f"Dry run trace written to {Path(args.output_dir) / 'trace.json'}")


def _run_task(args: argparse.Namespace) -> None:
    settings = get_settings()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    task_state_path = output_dir / "task_state.json"

    text_client = None if args.init_no_llm else OpenAICompatibleClient(settings.text)
    state = TaskParser(text_client).parse(
        request=args.request,
        entry_url=args.entry_url,
        resources=args.resource,
    )
    state.save(task_state_path)

    runtime = DrissionRuntime(browser=settings.browser, output_dir=output_dir)
    multimodal_client = OpenAICompatibleClient(settings.multimodal)
    decider = StepDecider(multimodal_client)
    state_updater = TaskStateUpdater(multimodal_client)
    debugger = Debugger(multimodal_client)
    loop = AgentLoop(
        task_state=state,
        task_state_path=task_state_path,
        runtime=runtime,
        decider=decider,
        output_dir=output_dir,
        state_updater=state_updater,
        debugger=debugger,
        max_steps=args.max_steps,
    )
    result = loop.run()
    script_result = generate_script_from_trace(
        trace_path=output_dir / "trace.json",
        output_path=output_dir / "generated_script.py",
        replay_output_dir=output_dir / "replay_run",
    )
    print(f"Run result: {result}")
    print(f"Task state: {task_state_path}")
    print(f"Trace: {output_dir / 'trace.json'}")
    print(
        "Generated script: "
        f"{script_result.path} ({script_result.action_count} actions, "
        f"{script_result.skipped_count} skipped)"
    )


def _generate_script(args: argparse.Namespace) -> None:
    trace_path = Path(args.trace)
    output_path = Path(args.output) if args.output else trace_path.parent / "generated_script.py"
    replay_output_dir = Path(args.replay_output_dir) if args.replay_output_dir else output_path.parent / "replay_run"
    result = generate_script_from_trace(
        trace_path=trace_path,
        output_path=output_path,
        replay_output_dir=replay_output_dir,
    )
    print(
        "Generated script: "
        f"{result.path} ({result.action_count} actions, {result.skipped_count} skipped)"
    )


def _verify_script(args: argparse.Namespace) -> None:
    script_path = Path(args.script)
    output_dir = Path(args.output_dir) if args.output_dir else script_path.parent / "script_verify"
    result = run_script(
        script_path=script_path,
        output_dir=output_dir,
        timeout_seconds=args.timeout_seconds,
    )
    print(f"Script success: {result.success}")
    print(f"Exit code: {result.exit_code}")
    print(f"Run JSON: {result.run_json}")
    print(f"Stdout: {output_dir / 'script_run.stdout.log'}")
    print(f"Stderr: {output_dir / 'script_run.stderr.log'}")


def _debug_script(args: argparse.Namespace) -> None:
    settings = get_settings()
    script_path = Path(args.script)
    output_dir = Path(args.output_dir) if args.output_dir else script_path.parent / "script_debug"
    client = None if args.no_llm else OpenAICompatibleClient(settings.multimodal)
    result = ScriptDebugger(client).debug(
        script_path=script_path,
        output_dir=output_dir,
        max_attempts=args.max_attempts,
        timeout_seconds=args.timeout_seconds,
    )
    flow_recovery_plan_path = None
    resumed_generation_dir = None
    resumed_script_path = None
    if result.status == "handoff_to_flow_debugger" and result.handoff_path and args.task_state:
        state = TaskState.load(Path(args.task_state))
        handoff = json.loads(result.handoff_path.read_text(encoding="utf-8"))
        failure = _script_handoff_to_flow_failure(handoff)
        recovery = Debugger(client).recover(state, failure)
        flow_recovery_plan_path = output_dir / f"flow_recovery_plan_{result.attempts:02d}.json"
        flow_recovery_plan_path.write_text(
            json.dumps(
                {
                    "source": "script_debugger_handoff",
                    "handoff_path": str(result.handoff_path),
                    "task_state_path": str(Path(args.task_state)),
                    "failure": failure,
                    "recovery": recovery,
                    "next_instruction": (
                        "Resume Generation from this recovery plan: navigate/refresh/restart according to "
                        "strategy, apply state_update to task_state, force listed items to single-step."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if not args.no_resume_generation:
            resumed = _resume_generation_from_recovery(
                task_state=state,
                original_task_state_path=Path(args.task_state),
                output_dir=output_dir,
                recovery=recovery,
                failure=failure,
                max_steps=args.resume_max_steps,
                client=client,
            )
            resumed_generation_dir = resumed["output_dir"]
            resumed_script_path = resumed.get("generated_script")
    print(f"Debug status: {result.status}")
    print(f"Final/current script: {result.final_script}")
    print(f"Attempts: {result.attempts}")
    print(f"Log: {result.log_path}")
    if result.handoff_path:
        print(f"Flow handoff: {result.handoff_path}")
    if flow_recovery_plan_path:
        print(f"Flow recovery plan: {flow_recovery_plan_path}")
    if resumed_generation_dir:
        print(f"Resumed Generation: {resumed_generation_dir}")
    if resumed_script_path:
        print(f"Resumed generated script: {resumed_script_path}")


def _script_handoff_to_flow_failure(handoff: dict) -> dict:
    reason = str(handoff.get("reason") or handoff.get("debugger_notes") or "")
    category = str(handoff.get("category") or "script_debugger_handoff")
    symptom_step = handoff.get("symptom_step") or handoff.get("root_cause_step")
    return {
        "source": "script_debugger",
        "category": category,
        "step_id": symptom_step,
        "root_cause_step_id": handoff.get("root_cause_step"),
        "work_item_id": symptom_step,
        "error": reason,
        "action_summary": reason or f"Script replay failed with {category}",
        "screenshot": handoff.get("screenshot_path"),
        "failure_context": {
            "script_path": handoff.get("script_path"),
            "run_json": handoff.get("run_json"),
            "failure_html_path": handoff.get("failure_html_path"),
            "failure_state_path": handoff.get("failure_state_path"),
            "stdout_path": handoff.get("stdout_path"),
            "stderr_path": handoff.get("stderr_path"),
        },
        "evidence": [
            "Standalone script Debugger classified this as a flow/state error, not a mechanical script patch.",
            reason,
        ],
    }


def _resume_generation_from_recovery(
    *,
    task_state: TaskState,
    original_task_state_path: Path,
    output_dir: Path,
    recovery: dict,
    failure: dict,
    max_steps: int,
    client: OpenAICompatibleClient | None,
) -> dict:
    if client is None:
        raise RuntimeError("Cannot resume Generation without an LLM client.")
    settings = get_settings()
    resume_dir = output_dir / "resumed_generation"
    resume_dir.mkdir(parents=True, exist_ok=True)
    task_state_path = resume_dir / "task_state.json"
    _apply_flow_recovery_to_task_state(task_state, recovery, failure)
    rollback_url = str(
        recovery.get("rollback_url")
        or task_state.site.get("last_stable_url")
        or task_state.site.get("current_url")
        or task_state.site.get("entry_url")
        or ""
    )
    if rollback_url:
        task_state.site["current_url"] = rollback_url
        task_state.site["last_stable_url"] = rollback_url
    task_state.save(task_state_path)

    runtime = DrissionRuntime(browser=settings.browser, output_dir=resume_dir)
    if rollback_url:
        runtime.execute(
            ActionCall(
                type="goto",
                reason="Resume Generation from script-debugger flow recovery.",
                url=rollback_url,
                expected_result="Recovered page is loaded for continued Generation.",
            )
        )
    loop = AgentLoop(
        task_state=task_state,
        task_state_path=task_state_path,
        runtime=runtime,
        decider=StepDecider(client),
        output_dir=resume_dir,
        state_updater=TaskStateUpdater(client),
        debugger=Debugger(client),
        max_steps=max_steps,
    )
    result = loop.run()
    summary = {
        "status": "generation_resumed",
        "original_task_state_path": str(original_task_state_path),
        "task_state_path": str(task_state_path),
        "rollback_url": rollback_url,
        "failure": failure,
        "recovery": recovery,
        "generation_result": result,
    }
    (resume_dir / "resume_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    trace_path = resume_dir / "trace.json"
    generated_script = None
    if trace_path.exists():
        if rollback_url:
            _prepend_recovery_navigation_to_trace(
                trace_path=trace_path,
                rollback_url=rollback_url,
                reason="Open recovered rollback URL before replaying resumed Generation actions.",
            )
        script_result = generate_script_from_trace(
            trace_path=trace_path,
            output_path=resume_dir / "generated_script.py",
            replay_output_dir=resume_dir / "replay_run",
        )
        generated_script = script_result.path
        summary["generated_script"] = str(script_result.path)
        summary["generated_script_action_count"] = script_result.action_count
        summary["generated_script_skipped_count"] = script_result.skipped_count
        (resume_dir / "resume_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return {"output_dir": resume_dir, "generated_script": generated_script}


def _prepend_recovery_navigation_to_trace(
    *,
    trace_path: Path,
    rollback_url: str,
    reason: str,
) -> None:
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    if trace and trace[0].get("event") == "action_executed":
        action = trace[0].get("action") or {}
        if action.get("type") == "goto" and action.get("url") == rollback_url:
            return
    event = {
        "step_id": "recovery_goto",
        "event": "action_executed",
        "status": "navigated",
        "mode": "single",
        "elapsed_seconds": 0,
        "action": {
            "type": "goto",
            "reason": reason,
            "url": rollback_url,
            "expected_result": "Recovered rollback URL is loaded before replaying resumed Generation.",
        },
        "runtime_result": {
            "status": "navigated",
            "url": rollback_url,
        },
    }
    trace_path.write_text(
        json.dumps([event, *trace], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _apply_flow_recovery_to_task_state(
    task_state: TaskState,
    recovery: dict,
    failure: dict,
) -> None:
    task_state.known_failures.append(failure)
    strategy = str(recovery.get("strategy") or recovery.get("recommended_strategy") or "").lower()
    if strategy:
        attempts = task_state.goal.setdefault("recovery_attempts", [])
        if strategy not in attempts:
            attempts.append(strategy)
        del attempts[:-10]
    avoid = recovery.get("avoid")
    if avoid:
        task_state.avoid.append(str(avoid))
        task_state.avoid = task_state.avoid[-20:]
    state_update = recovery.get("state_update") or {}
    if isinstance(state_update, dict):
        rolling_context = state_update.get("rolling_context")
        if isinstance(rolling_context, dict):
            for key, value in rolling_context.items():
                task_state.rolling_context[key] = value
        must_single = state_update.get("must_single_step_items")
        if isinstance(must_single, list):
            existing = task_state.goal.setdefault("must_single_step_items", [])
            for item in must_single:
                if item and item not in existing:
                    existing.append(item)
    task_state.rolling_context.pop("risk_signal", None)


if __name__ == "__main__":
    main()
