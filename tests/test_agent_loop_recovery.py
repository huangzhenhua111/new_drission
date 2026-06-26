from pathlib import Path

from app.core.schema import ActionCall, ActionDecision, PageObservation
from app.generation.agent_loop import AgentLoop, _enforce_mandatory_debugger_signal
from app.generation.step_decider import StepDecider
from app.generation.task_parser import TaskParser


class RestartDebugger:
    def recover(self, task_state, failure):  # noqa: ANN001
        return {
            "root_cause_step_id": failure["step_id"],
            "root_cause": "no_obvious_agent_error",
            "strategy": "restart_browser_retry",
            "rollback_url": "https://example.com",
            "state_update": {
                "rolling_context": {
                    "next_step": {
                        "intent": "Retry after restarting browser",
                        "success_condition": "Continue from a clean browser session",
                    }
                },
                "must_single_step_items": ["export"],
            },
            "avoid": "do not blindly repeat the failed export loop",
        }


class RefreshDebugger:
    def recover(self, task_state, failure):  # noqa: ANN001
        return {
            "root_cause_step_id": failure["step_id"],
            "root_cause": "no_obvious_agent_error",
            "strategy": "refresh_page_retry",
            "rollback_url": "https://example.com/project",
            "state_update": {
                "rolling_context": {
                    "next_step": {
                        "intent": "Retry after refreshing the current project page",
                        "success_condition": "Continue from a refreshed page",
                    }
                },
                "must_single_step_items": ["export"],
            },
            "avoid": "do not repeat the same failed export loop",
        }


class FakeRuntime:
    def __init__(self) -> None:
        self.closed = 0
        self.executed = []

    def observe(self) -> PageObservation:
        return PageObservation(url="https://example.com/project", title="Project")

    def execute(self, action: ActionCall) -> dict:
        self.executed.append(action)
        return {"status": "navigated" if action.type == "goto" else "clicked"}

    def close(self) -> None:
        self.closed += 1


class FakeDecider:
    def __init__(self, decisions: list[ActionDecision]) -> None:
        self.decisions = list(decisions)

    def decide(self, *, task_state, observation):  # noqa: ANN001
        if self.decisions:
            return self.decisions.pop(0)
        return ActionDecision(
            mode="single",
            actions=[ActionCall(type="finish", reason="done")],
            risk="low",
        )


class FakeOutcomeChecker:
    def check_current_observation(self, **kwargs):  # noqa: ANN003
        return None

    def check(self, **kwargs):  # noqa: ANN003
        return {
            "step_id": kwargs["step_id"],
            "category": "post_action_outcome_uncertain",
            "url": kwargs["after"].url,
            "evidence": ["Synthetic risk signal for Generation review."],
        }


class CountingDebugger(RefreshDebugger):
    def __init__(self) -> None:
        self.calls = 0

    def recover(self, task_state, failure):  # noqa: ANN001
        self.calls += 1
        return super().recover(task_state, failure)


def test_restart_browser_strategy_closes_then_gotos_recovery_url(tmp_path: Path) -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    runtime = FakeRuntime()
    loop = AgentLoop(
        task_state=state,
        task_state_path=tmp_path / "task_state.json",
        runtime=runtime,
        decider=StepDecider(None),
        output_dir=tmp_path,
        debugger=RestartDebugger(),
    )

    result = loop._recover_from_failure_context(
        step_number=7,
        failure={
            "step_id": "s7",
            "category": "generation_loop_or_no_progress",
            "url": "https://example.com/project",
            "evidence": ["No obvious agent error."],
        },
    )

    assert result["recovered"] is True
    assert result["strategy"] == "restart_browser_retry"
    assert runtime.closed == 1
    assert runtime.executed[-1].type == "goto"
    assert runtime.executed[-1].url == "https://example.com"
    assert loop.task_state_path.name == "task_state_restart_01.json"
    assert loop.task_state_path.exists()
    assert loop.task_state is not state
    assert "replay_hints" in loop.task_state.goal
    assert all(
        item.get("status") != "done"
        for item in loop.task_state.goal.get("work_items", [])
    )
    assert result["state_update"]["mode"] == "restart_browser_retry"


def test_refresh_strategy_gotos_recovery_url_without_closing_browser(tmp_path: Path) -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    runtime = FakeRuntime()
    loop = AgentLoop(
        task_state=state,
        task_state_path=tmp_path / "task_state.json",
        runtime=runtime,
        decider=StepDecider(None),
        output_dir=tmp_path,
        debugger=RefreshDebugger(),
    )

    result = loop._recover_from_failure_context(
        step_number=7,
        failure={
            "step_id": "s7",
            "category": "generation_loop_or_no_progress",
            "url": "https://example.com/project",
            "evidence": ["No obvious agent error."],
        },
    )

    assert result["recovered"] is True
    assert result["strategy"] == "refresh_page_retry"
    assert runtime.closed == 0
    assert runtime.executed[-1].type == "goto"
    assert runtime.executed[-1].url == "https://example.com/project"
    assert runtime.executed[-1].reason == "Debugger refresh-page retry to recovery URL."
    assert "export" in state.goal["must_single_step_items"]
    assert "refresh_page_retry" in state.goal["recovery_attempts"]
    assert result["state_update"]["mode"] == "refresh_page_retry"
    assert loop.task_state is state
    assert loop.task_state_path.name == "task_state.json"


def test_risk_signal_does_not_directly_enter_debugger(tmp_path: Path) -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    runtime = FakeRuntime()
    debugger = CountingDebugger()
    decider = FakeDecider(
        [
            ActionDecision(
                mode="single",
                risk="high",
                actions=[
                    ActionCall(
                        type="click",
                        reason="Click Export",
                        target_candidate_id="cand_export",
                        expected_result="Start export",
                    )
                ],
            ),
            ActionDecision(
                mode="single",
                risk="low",
                actions=[ActionCall(type="finish", reason="Generation judged it can continue")],
            ),
        ]
    )
    loop = AgentLoop(
        task_state=state,
        task_state_path=tmp_path / "task_state.json",
        runtime=runtime,
        decider=decider,
        output_dir=tmp_path,
        debugger=debugger,
        outcome_checker=FakeOutcomeChecker(),
        max_steps=2,
    )

    result = loop.run()

    assert result["status"] == "finished"
    assert debugger.calls == 0
    trace = (tmp_path / "trace.json").read_text(encoding="utf-8")
    assert "risk_signal_detected" in trace


def test_generation_request_debugger_enters_debugger(tmp_path: Path) -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    state.rolling_context["risk_signal"] = {
        "category": "post_action_outcome_uncertain",
        "evidence": ["Encoding failed"],
    }
    runtime = FakeRuntime()
    debugger = CountingDebugger()
    decider = FakeDecider(
        [
            ActionDecision(
                mode="single",
                risk="high",
                request_debugger=True,
                debugger_reason="Generation reviewed risk_signal and found export failed.",
                actions=[ActionCall(type="wait", reason="debugger placeholder")],
            )
        ]
    )
    loop = AgentLoop(
        task_state=state,
        task_state_path=tmp_path / "task_state.json",
        runtime=runtime,
        decider=decider,
        output_dir=tmp_path,
        debugger=debugger,
        max_steps=1,
    )

    result = loop.run()

    assert result["status"] == "max_steps_reached"
    assert debugger.calls == 1
    trace = (tmp_path / "trace.json").read_text(encoding="utf-8")
    assert "debugger_recovery" in trace


def test_mandatory_risk_signal_forces_generation_debugger_request() -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    state.rolling_context["risk_signal"] = {
        "category": "current_page_finalizing_failure",
        "mandatory_request_debugger": True,
        "evidence": ["Encoding failed twice"],
    }
    model_decision = ActionDecision(
        mode="single",
        risk="low",
        actions=[
            ActionCall(
                type="click",
                reason="Close Encoding failed dialog and retry export",
                target_candidate_id="cand_close",
            )
        ],
    )

    decision = _enforce_mandatory_debugger_signal(model_decision, state)

    assert decision.request_debugger is True
    assert decision.actions[0].type == "wait"
    assert "failed at least twice" in str(decision.debugger_reason)
