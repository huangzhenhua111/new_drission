from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

from app.core.schema import ActionCall, ActionDecision, PageObservation, TaskState
from app.debugger.rollback import Debugger
from app.generation.step_decider import StepDecider
from app.generation.state_updater import TaskStateUpdater
from app.llm.client import LLMError
from app.recovery.outcome_checker import OutcomeChecker
from app.resilience.executor import ResilienceExecutor
from app.runtime.base import BrowserRuntime
from app.trace.recorder import TraceRecorder


class AgentLoop:
    def __init__(
        self,
        *,
        task_state: TaskState,
        task_state_path: Path,
        runtime: BrowserRuntime,
        decider: StepDecider,
        output_dir: Path,
        state_updater: TaskStateUpdater | None = None,
        debugger: Debugger | None = None,
        outcome_checker: OutcomeChecker | None = None,
        max_steps: int = 20,
    ) -> None:
        self.task_state = task_state
        self.task_state_path = task_state_path
        self.runtime = runtime
        self.decider = decider
        self.state_updater = state_updater or TaskStateUpdater(None)
        self.debugger = debugger or Debugger(None)
        self.outcome_checker = outcome_checker or OutcomeChecker()
        self.executor = ResilienceExecutor(runtime)
        self.output_dir = output_dir
        self.max_steps = max_steps
        self.trace = TraceRecorder(output_dir)
        self._steps_since_model_state_update = 0

    def run(self) -> dict:
        try:
            for step_number in range(1, self.max_steps + 1):
                started = perf_counter()
                observation = self.runtime.observe()
                self._update_current_page(observation)
                pre_decision_signal = self.outcome_checker.check_current_observation(
                    task_state=self.task_state,
                    step_id=f"s{step_number}",
                    observation=observation,
                )
                if pre_decision_signal is not None:
                    pre_decision_signal["work_item_id"] = (
                        self.task_state.rolling_context.get("next_step") or {}
                    ).get("work_item_id")
                    self._record_risk_signal(pre_decision_signal)
                    self.trace.add(
                        {
                            "step_id": f"s{step_number}",
                            "event": "risk_signal_detected",
                            "phase": "before_decision",
                            "risk_signal": pre_decision_signal,
                        }
                    )
                self.task_state.save(self.task_state_path)

                try:
                    decision = self.decider.decide(task_state=self.task_state, observation=observation)
                except LLMError as exc:
                    failure = {
                        "step_id": f"s{step_number}",
                        "event": "action_decision_failed",
                        "phase": "step_decider",
                        "status": "llm_failed",
                        "reason": str(exc)[:800],
                    }
                    self.trace.add(failure)
                    self.task_state.known_failures.append(
                        {
                            "step_id": f"s{step_number}",
                            "category": "step_decider_failed",
                            "error": str(exc)[:800],
                        }
                    )
                    self.task_state.save(self.task_state_path)
                    return {
                        "status": "step_decider_failed",
                        "steps": step_number,
                        "reason": str(exc)[:800],
                    }
                decision = _enforce_mandatory_debugger_signal(decision, self.task_state)
                decision = _block_premature_finalizing_action(decision, self.task_state)
                event = {
                    "step_id": f"s{step_number}",
                    "event": "action_decided",
                    "url_before": observation.url,
                    "decision": asdict(decision),
                    "action": asdict(decision.actions[0]),
                    "candidate_count": len(observation.candidates),
                    "screenshot_path": observation.screenshot_path,
                }
                self.trace.add(event)

                if decision.request_debugger:
                    recovery_result = self._recover_from_generation_request(
                        step_number=step_number,
                        observation=observation,
                        decision=decision,
                    )
                    self.task_state.save(self.task_state_path)
                    if recovery_result.get("recovered"):
                        continue
                    return {
                        "status": "debugger_requested",
                        "steps": step_number,
                        "reason": decision.debugger_reason or decision.reason,
                    }

                if len(decision.actions) == 1 and decision.actions[0].type == "finish":
                    action = decision.actions[0]
                    self._record_previous_step(step_number, observation, action, {"status": "finished"})
                    self.task_state.save(self.task_state_path)
                    return {"status": "finished", "steps": step_number}

                try:
                    result = self._execute_decision(decision, observation)
                except Exception as exc:
                    result = {
                        "status": "action_failed",
                        "mode": "single",
                        "before_url": observation.url,
                        "after_url": observation.url,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "action": asdict(decision.actions[0]),
                        "action_for_state": decision.actions[0],
                    }
                action = result["action_for_state"]
                public_result = {
                    key: value for key, value in result.items() if key != "action_for_state"
                }
                elapsed = round(perf_counter() - started, 3)
                result_event = {
                    "step_id": f"s{step_number}",
                    "event": "action_executed",
                    "elapsed_seconds": elapsed,
                    **public_result,
                }
                _attach_selector_candidates(result_event, decision, observation)
                self.trace.add(result_event)
                if result.get("status") in {"batch_failed", "action_failed"}:
                    recovery_result = self._recover_from_action_failure(
                        step_number=step_number,
                        observation=observation,
                        decision=decision,
                        result=result,
                    )
                    self.task_state.save(self.task_state_path)
                    if not recovery_result.get("recovered"):
                        return {
                            "status": result.get("status"),
                            "steps": step_number,
                            "failed_action_index": result.get("failed_action_index"),
                            "reason": result.get("error"),
                        }
                    continue

                after = self.runtime.observe()
                self._record_previous_step(step_number, after, action, public_result)
                self._record_recent_trace(step_number, observation, after, action, public_result)
                risk_signal = self.outcome_checker.check(
                    task_state=self.task_state,
                    step_id=f"s{step_number}",
                    before=observation,
                    after=after,
                    decision=decision,
                    action=action,
                    result=public_result,
                )
                if risk_signal is not None:
                    risk_signal["work_item_id"] = (
                        self.task_state.rolling_context.get("next_step") or {}
                    ).get("work_item_id")
                    self._record_risk_signal(risk_signal)
                    self.trace.add(
                        {
                            "step_id": f"s{step_number}",
                            "event": "risk_signal_detected",
                            "risk_signal": risk_signal,
                        }
                    )
                    self.task_state.save(self.task_state_path)
                else:
                    self._clear_resolved_risk_signal()
                state_update_started = perf_counter()
                self.state_updater.apply_deterministic_after_step(
                    task_state=self.task_state,
                    step_id=f"s{step_number}",
                    before=observation,
                    after=after,
                    action=action,
                    result=public_result,
                )
                self.task_state.save(self.task_state_path)
                state_update_gate = _state_update_gate(
                    step_number=step_number,
                    steps_since_model_state_update=self._steps_since_model_state_update,
                    has_model_client=getattr(self.state_updater, "client", None) is not None,
                    decision=decision,
                    action=action,
                    before=observation,
                    after=after,
                    result=public_result,
                )
                if not state_update_gate["call_model"]:
                    self._steps_since_model_state_update += 1
                    self.trace.add(
                        {
                            "step_id": f"s{step_number}",
                            "event": "state_updated",
                            "status": "deterministic_only",
                            "model_update_skipped": True,
                            "skip_reason": state_update_gate["reason"],
                            "steps_since_model_state_update": self._steps_since_model_state_update,
                            "elapsed_seconds": round(perf_counter() - state_update_started, 3),
                        }
                    )
                    self.task_state.save(self.task_state_path)
                    continue

                try:
                    state_update_result = self.state_updater.update_after_step(
                        task_state=self.task_state,
                        step_id=f"s{step_number}",
                        before=observation,
                        after=after,
                        action=action,
                        result=public_result,
                        apply_deterministic=False,
                    )
                    self._steps_since_model_state_update = 0
                except LLMError as exc:
                    state_update_result = {
                        "status": "state_update_failed",
                        "phase": "state_updater",
                        "reason": str(exc)[:800],
                    }
                    self.trace.add(
                        {
                            "step_id": f"s{step_number}",
                            "event": "state_updated",
                            **state_update_result,
                        }
                    )
                    self.task_state.known_failures.append(
                        {
                            "step_id": f"s{step_number}",
                            "category": "state_update_failed",
                            "error": str(exc)[:800],
                        }
                    )
                    self.task_state.save(self.task_state_path)
                    return {
                        "status": "state_update_failed",
                        "steps": step_number,
                        "reason": str(exc)[:800],
                    }
                self.trace.add(
                    {
                        "step_id": f"s{step_number}",
                        "event": "state_updated",
                        "model_update_reason": state_update_gate["reason"],
                        **state_update_result,
                    }
                )
                self.task_state.save(self.task_state_path)

            return {"status": "max_steps_reached", "steps": self.max_steps}
        finally:
            self.runtime.close()

    def _recover_from_action_failure(
        self,
        *,
        step_number: int,
        observation: PageObservation,
        decision: ActionDecision,
        result: dict,
    ) -> dict:
        failure = {
            "step_id": f"s{step_number}",
            "category": result.get("status", "action_failed"),
            "url": observation.url,
            "work_item_id": (self.task_state.rolling_context.get("next_step") or {}).get("work_item_id"),
            "error": result.get("error"),
            "error_type": result.get("error_type"),
            "failed_action_index": result.get("failed_action_index"),
            "decision": asdict(decision),
            "sub_results": result.get("sub_results", []),
        }
        return self._recover_from_failure_context(step_number=step_number, failure=failure)

    def _recover_from_generation_request(
        self,
        *,
        step_number: int,
        observation: PageObservation,
        decision: ActionDecision,
    ) -> dict:
        risk_signal = self.task_state.rolling_context.get("risk_signal")
        failure = {
            "step_id": f"s{step_number}",
            "category": "generation_requested_debugger",
            "url": observation.url,
            "work_item_id": (self.task_state.rolling_context.get("next_step") or {}).get("work_item_id"),
            "reason": decision.debugger_reason or decision.reason,
            "decision": asdict(decision),
            "risk_signal": risk_signal if isinstance(risk_signal, dict) else None,
            "observation": {
                "url": observation.url,
                "title": observation.title,
                "text_excerpt": observation.text_excerpt[:1200],
                "screenshot_path": observation.screenshot_path,
                "candidate_count": len(observation.candidates),
            },
            "evidence": [
                "Generation model reviewed risk_signal, DOM and screenshot, then explicitly requested Debugger.",
                decision.debugger_reason or decision.reason,
            ],
        }
        return self._recover_from_failure_context(step_number=step_number, failure=failure)

    def _recover_from_failure_context(
        self,
        *,
        step_number: int,
        failure: dict[str, Any],
    ) -> dict:
        if "recovery_attempts" not in failure:
            attempts = []
            if isinstance(self.task_state.goal, dict):
                attempts = list(self.task_state.goal.get("recovery_attempts") or [])
            failure["recovery_attempts"] = attempts
        self.task_state.known_failures.append(failure)
        recovery = self.debugger.recover(self.task_state, failure)
        self._apply_recovery(recovery)
        self.trace.add(
            {
                "step_id": f"s{step_number}",
                "event": "debugger_recovery",
                "failure": failure,
                "recovery": recovery,
            }
        )
        rollback_url = recovery.get("rollback_url")
        strategy = str(recovery.get("strategy") or recovery.get("recommended_strategy") or "").lower()
        before_recovery = self.runtime.observe()
        if strategy == "restart_browser_retry":
            self.runtime.close()
        if rollback_url:
            self.runtime.execute(
                ActionCall(
                    type="goto",
                    reason=_recovery_goto_reason(strategy),
                    url=str(rollback_url),
                    expected_result="Recovered page is loaded for single-step generation.",
                )
            )
            after_recovery = self.runtime.observe()
            recovery_update = self._update_state_after_recovery_navigation(
                step_number=step_number,
                strategy=strategy,
                failure=failure,
                recovery=recovery,
                before_recovery=before_recovery,
                after_recovery=after_recovery,
            )
            return {
                "recovered": True,
                "rollback_url": rollback_url,
                "strategy": strategy or "rollback_and_continue",
                "state_update": recovery_update,
            }
        return {"recovered": True, "strategy": strategy or "continue"}

    def _update_state_after_recovery_navigation(
        self,
        *,
        step_number: int,
        strategy: str,
        failure: dict[str, Any],
        recovery: dict[str, Any],
        before_recovery: PageObservation,
        after_recovery: PageObservation,
    ) -> dict[str, Any]:
        step_id = f"s{step_number}"
        started = perf_counter()
        self._update_current_page(after_recovery)
        if strategy == "restart_browser_retry":
            old_path = self.task_state_path
            new_state, result = self.state_updater.rebuild_after_browser_restart(
                old_task_state=self.task_state,
                step_id=step_id,
                failure=failure,
                recovery=recovery,
                after_recovery=after_recovery,
            )
            self.task_state = new_state
            self.task_state_path = _restart_task_state_path(old_path)
            self._steps_since_model_state_update = 0
            result = {
                **result,
                "mode": "restart_browser_retry",
                "old_task_state_path": str(old_path),
                "new_task_state_path": str(self.task_state_path),
            }
            self.trace.add(
                {
                    "step_id": step_id,
                    "event": "recovery_state_updated",
                    **result,
                    "elapsed_seconds_total": round(perf_counter() - started, 3),
                }
            )
            self.task_state.save(self.task_state_path)
            return result

        result = self.state_updater.update_after_recovery_refresh(
            task_state=self.task_state,
            step_id=step_id,
            failure=failure,
            recovery=recovery,
            before_recovery=before_recovery,
            after_recovery=after_recovery,
        )
        self._steps_since_model_state_update = 0
        result = {
            **result,
            "mode": strategy or "rollback_and_continue",
            "task_state_path": str(self.task_state_path),
        }
        self.trace.add(
            {
                "step_id": step_id,
                "event": "recovery_state_updated",
                **result,
                "elapsed_seconds_total": round(perf_counter() - started, 3),
            }
        )
        self.task_state.save(self.task_state_path)
        return result

    def _apply_recovery(self, recovery: dict[str, Any]) -> None:
        strategy = str(recovery.get("strategy") or recovery.get("recommended_strategy") or "").lower()
        if strategy:
            attempts = self.task_state.goal.setdefault("recovery_attempts", [])
            if strategy not in attempts:
                attempts.append(strategy)
            del attempts[:-10]
        avoid = recovery.get("avoid")
        if avoid:
            self.task_state.avoid.append(str(avoid))
            self.task_state.avoid = self.task_state.avoid[-20:]
        state_update = recovery.get("state_update") or {}
        if not isinstance(state_update, dict):
            return
        rolling_context = state_update.get("rolling_context")
        if isinstance(rolling_context, dict):
            for key, value in rolling_context.items():
                self.task_state.rolling_context[key] = value
        must_single = state_update.get("must_single_step_items")
        if isinstance(must_single, list):
            existing = self.task_state.goal.setdefault("must_single_step_items", [])
            for item in must_single:
                if item and item not in existing:
                    existing.append(item)
        self.task_state.rolling_context.pop("risk_signal", None)

    def _record_risk_signal(self, risk_signal: dict[str, Any]) -> None:
        self.task_state.rolling_context["risk_signal"] = {
            **risk_signal,
            "instruction": (
                "This is a sensor signal, not a final verdict. StepDecider must inspect "
                "the current screenshot/DOM/global JSON and either continue safely or set "
                "request_debugger=true."
            ),
        }
        self.task_state.known_failures.append(
            {
                "step_id": risk_signal.get("step_id"),
                "category": risk_signal.get("category"),
                "status": "risk_signal_pending_generation_review",
                "evidence": risk_signal.get("evidence", [])[:3],
            }
        )
        self.task_state.known_failures = self.task_state.known_failures[-10:]

    def _clear_resolved_risk_signal(self) -> None:
        signal = self.task_state.rolling_context.get("risk_signal")
        if not isinstance(signal, dict):
            return
        # Keep the signal for one model turn. If Generation chose a normal
        # browser action and the following observation no longer triggers the
        # checker, treat it as reviewed/resolved and remove it.
        self.task_state.rolling_context["last_resolved_risk_signal"] = {
            "step_id": signal.get("step_id"),
            "category": signal.get("category"),
            "resolved_by": "generation_continued_without_debugger",
        }
        self.task_state.rolling_context.pop("risk_signal", None)

    def _execute_decision(self, decision: ActionDecision, observation: PageObservation) -> dict:
        decision = _enforce_must_single(decision, self.task_state)
        if decision.mode != "batch" or len(decision.actions) == 1:
            action = decision.actions[0]
            result = self.executor.run(action, observation)
            result["mode"] = "single"
            result["action_for_state"] = action
            return result

        sub_results = []
        current_observation = observation
        before_url = observation.url
        last_action = decision.actions[-1]
        for index, action in enumerate(decision.actions, start=1):
            try:
                sub_result = self.executor.run(action, current_observation)
            except Exception as exc:
                return {
                    "status": "batch_failed",
                    "mode": "batch",
                    "before_url": before_url,
                    "after_url": current_observation.url,
                    "failed_action_index": index,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "sub_results": sub_results,
                    "action": asdict(action),
                    "action_for_state": action,
                }
            sub_result["action_index"] = index
            sub_results.append(sub_result)
            current_observation = self.runtime.observe()

        return {
            "status": "batch_executed",
            "mode": "batch",
            "before_url": before_url,
            "after_url": current_observation.url,
            "sub_results": sub_results,
            "action": asdict(last_action),
            "actions": [asdict(action) for action in decision.actions],
            "batch_reason": decision.reason,
            "why_batch_is_safe": decision.why_batch_is_safe,
            "runtime_result": {"status": "batch_executed", "count": len(decision.actions)},
            "action_for_state": _batch_summary_action(decision),
        }

    def _update_current_page(self, observation: PageObservation) -> None:
        self.task_state.site["current_url"] = observation.url
        if observation.url and not _looks_like_auth_or_error(observation):
            self.task_state.site["last_stable_url"] = observation.url
        self.task_state.rolling_context["current_page"] = {
            "url": observation.url,
            "title": observation.title,
            "page_summary": observation.text_excerpt[:600],
            "goal_relevant_observations": _candidate_summaries(observation),
            "page_state_confidence": 0.5,
        }

    def _record_previous_step(
        self,
        step_number: int,
        observation: PageObservation,
        action: ActionCall,
        result: dict,
    ) -> None:
        status = result.get("status", "executed")
        self.task_state.rolling_context["previous_step"] = {
            "step_id": f"s{step_number}",
            "page_url": observation.url,
            "page_summary": observation.text_excerpt[:500],
            "action_summary": _action_summary(action),
            "expected_result": action.expected_result,
            "actual_result": str(result)[:600],
            "status": status,
        }

    def _record_recent_trace(
        self,
        step_number: int,
        before: PageObservation,
        after: PageObservation,
        action: ActionCall,
        result: dict,
    ) -> None:
        item = {
            "step_id": f"s{step_number}",
            "url_before": before.url,
            "url_after": after.url,
            "action": asdict(action),
            "expected_result": action.expected_result,
            "result": result.get("status", "executed"),
        }
        self.task_state.recent_trace.append(item)
        self.task_state.recent_trace = self.task_state.recent_trace[-12:]
        if after.url and not _looks_like_auth_or_error(after):
            self.task_state.stable_points.append(
                {
                    "id": f"sp{len(self.task_state.stable_points) + 1}",
                    "url": after.url,
                    "reason": f"After successful step s{step_number}",
                    "after_step_id": f"s{step_number}",
                }
            )
            self.task_state.stable_points = self.task_state.stable_points[-10:]


def _candidate_summaries(observation: PageObservation) -> list[str]:
    summaries = []
    for candidate in observation.candidates[:20]:
        label = candidate.accessible_name or candidate.text or candidate.extra.get("context_text") or candidate.selector
        if not label:
            continue
        summaries.append(f"{candidate.id}: {candidate.tag}/{candidate.role or ''} {label} {candidate.action_allowed}")
    return summaries[:12]


def _action_summary(action: ActionCall) -> str:
    target = action.target_candidate_id or action.selector or action.url or ""
    value = action.value or action.path or action.direction or ""
    return f"{action.type} {target} {value}".strip()


def _batch_summary_action(decision: ActionDecision) -> ActionCall:
    last = decision.actions[-1]
    return ActionCall(
        type=last.type,
        reason=decision.reason or "Batch action sequence executed.",
        target_candidate_id=last.target_candidate_id,
        selector=last.selector,
        value=last.value,
        path=last.path,
        url=last.url,
        seconds=last.seconds,
        direction=last.direction,
        confidence=min((action.confidence for action in decision.actions), default=0.0),
        expected_result=decision.expected_result or last.expected_result,
    )


def _attach_selector_candidates(
    event: dict[str, Any],
    decision: ActionDecision,
    observation: PageObservation,
) -> None:
    if event.get("mode") == "batch" and event.get("actions"):
        event["selector_candidates_by_index"] = [
            _selector_candidates_for_action(action, observation)
            for action in decision.actions
        ]
        for raw_action, selectors in zip(event.get("actions") or [], event["selector_candidates_by_index"]):
            if selectors:
                raw_action["selector_candidates"] = selectors
        return
    if not decision.actions:
        return
    selectors = _selector_candidates_for_action(decision.actions[0], observation)
    if not selectors:
        return
    event["selector_candidates"] = selectors
    if isinstance(event.get("action"), dict):
        event["action"]["selector_candidates"] = selectors


def _selector_candidates_for_action(
    action: ActionCall,
    observation: PageObservation,
) -> list[str]:
    values: list[str] = []
    if action.selector:
        values.append(action.selector)
    if action.target_candidate_id:
        candidate = next(
            (item for item in observation.candidates if item.id == action.target_candidate_id),
            None,
        )
        if candidate:
            values.extend(candidate.selectors or [])
            if candidate.selector:
                values.append(candidate.selector)
    result = []
    seen = set()
    for value in values:
        clean = str(value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _recovery_goto_reason(strategy: str) -> str:
    if strategy == "refresh_page_retry":
        return "Debugger refresh-page retry to recovery URL."
    if strategy == "restart_browser_retry":
        return "Debugger restart-browser retry to recovery URL."
    return "Debugger rollback to recovery URL."


def _restart_task_state_path(path: Path) -> Path:
    parent = path.parent
    stem = path.stem
    suffix = path.suffix or ".json"
    for index in range(1, 1000):
        candidate = parent / f"{stem}_restart_{index:02d}{suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem}_restart{suffix}"


def _enforce_must_single(decision: ActionDecision, task_state: TaskState) -> ActionDecision:
    if decision.mode != "batch" or len(decision.actions) <= 1:
        return decision
    current_work_item = (task_state.rolling_context.get("next_step") or {}).get("work_item_id")
    must_single_items = task_state.goal.get("must_single_step_items", []) if isinstance(task_state.goal, dict) else []
    patterns = [str(item).lower() for item in must_single_items]
    joined = " ".join(
        [
            str(current_work_item or ""),
            decision.reason or "",
            decision.expected_result or "",
            " ".join(action.reason or "" for action in decision.actions),
            " ".join(action.expected_result or "" for action in decision.actions),
        ]
    ).lower()
    if current_work_item in must_single_items or any(pattern and pattern in joined for pattern in patterns):
        first = decision.actions[0]
        return ActionDecision(
            mode="single",
            actions=[first],
            risk="medium",
            reason=f"Forced single-step by debugger recovery: {decision.reason}",
            expected_result=first.expected_result,
            request_debugger=decision.request_debugger,
            debugger_reason=decision.debugger_reason,
        )
    return decision


def _enforce_mandatory_debugger_signal(
    decision: ActionDecision,
    task_state: TaskState,
) -> ActionDecision:
    signal = task_state.rolling_context.get("risk_signal")
    if not isinstance(signal, dict) or not signal.get("mandatory_request_debugger"):
        return decision
    if decision.request_debugger:
        return decision
    reason = (
        "Mandatory debugger handoff: current risk_signal says the same high-risk "
        "finalizing action has failed at least twice. Do not close the error dialog "
        "and retry again."
    )
    return ActionDecision(
        mode="single",
        actions=[
            ActionCall(
                type="wait",
                reason="Placeholder action; AgentLoop will hand off to Debugger before executing.",
                seconds=0,
                expected_result="Debugger receives repeated finalizing-action failure context.",
            )
        ],
        risk="high",
        reason=reason,
        expected_result="Debugger decides rollback/recovery instead of another blind retry.",
        request_debugger=True,
        debugger_reason=reason,
    )


def _block_premature_finalizing_action(
    decision: ActionDecision,
    task_state: TaskState,
) -> ActionDecision:
    if not decision.actions:
        return decision
    action = decision.actions[0]
    if not _looks_like_finalizing_or_external_action(action, decision):
        return decision
    first_unfinished = _first_unfinished_work_item(task_state)
    if first_unfinished is None or _work_item_is_finalizing(first_unfinished):
        return decision

    instruction = (
        "Do not execute Export/Continue/Download/Submit until this first unfinished "
        "work item is completed and marked done."
    )
    blocked_action = _action_summary(action)
    prior_block_count = _recent_premature_block_count(
        task_state,
        work_item_id=str(first_unfinished.get("id") or ""),
    )
    _rollback_later_finalizing_work_items(task_state, first_unfinished)
    task_state.rolling_context["premature_finalizing_action_blocked"] = {
        "blocked_action": blocked_action,
        "reason": "premature_finalizing_action_blocked",
        "first_unfinished_work_item": first_unfinished,
        "instruction": instruction,
        "block_count": prior_block_count + 1,
    }
    task_state.rolling_context["next_step"] = {
        "work_item_id": first_unfinished.get("id"),
        "intent": first_unfinished.get("title"),
        "success_condition": first_unfinished.get("success_condition")
        or f"完成事项：{first_unfinished.get('title')}",
        "known_evidence": first_unfinished.get("evidence", []),
        "guard_note": instruction,
    }
    task_state.known_failures.append(
        {
            "category": "premature_finalizing_action_blocked",
            "blocked_action": blocked_action,
            "work_item_id": first_unfinished.get("id"),
            "message": instruction,
            "block_count": prior_block_count + 1,
        }
    )
    task_state.known_failures = task_state.known_failures[-12:]

    if prior_block_count >= 2:
        reason = (
            "Premature finalizing action was blocked repeatedly for the same unfinished "
            f"work item ({first_unfinished.get('title')}). Hand off to Debugger to close "
            "the modal/rollback state and resume the prerequisite step."
        )
        return ActionDecision(
            mode="single",
            actions=[
                ActionCall(
                    type="wait",
                    reason="Placeholder action; AgentLoop will hand off to Debugger before executing.",
                    seconds=0,
                    confidence=1.0,
                    expected_result="Debugger receives repeated premature-finalizing context.",
                )
            ],
            risk="high",
            reason=reason,
            expected_result="Debugger rolls back the premature finalizing state.",
            commit_after=True,
            stop_if_any_action_fails=True,
            request_debugger=True,
            debugger_reason=reason,
        )

    recovery_action = ActionCall(
        type="hotkey",
        value="ESC",
        reason=(
            "Guard blocked a premature finalizing action. Press Escape to close any "
            "Export/Continue modal, then observe again and continue the unfinished item: "
            f"{first_unfinished.get('title')}"
        ),
        confidence=1.0,
        expected_result=(
            "Any premature finalizing dialog is closed; the editor is visible again so "
            "the unfinished prerequisite can continue."
        ),
    )
    return ActionDecision(
        mode="single",
        actions=[recovery_action],
        risk="medium",
        reason=recovery_action.reason,
        expected_result=recovery_action.expected_result,
        commit_after=True,
        stop_if_any_action_fails=True,
    )


def _recent_premature_block_count(task_state: TaskState, *, work_item_id: str) -> int:
    count = 0
    for failure in reversed(task_state.known_failures[-8:]):
        if not isinstance(failure, dict):
            continue
        if failure.get("category") != "premature_finalizing_action_blocked":
            continue
        if str(failure.get("work_item_id") or "") != work_item_id:
            continue
        count += 1
    return count


def _rollback_later_finalizing_work_items(
    task_state: TaskState,
    first_unfinished: dict[str, Any],
) -> None:
    items = task_state.goal.get("work_items") if isinstance(task_state.goal, dict) else []
    if not isinstance(items, list):
        return
    seen_unfinished = False
    for item in items:
        if not isinstance(item, dict):
            continue
        if item is first_unfinished or item.get("id") == first_unfinished.get("id"):
            seen_unfinished = True
            if item.get("status") != "done":
                item["status"] = "in_progress"
            continue
        if not seen_unfinished:
            continue
        if not _work_item_is_finalizing(item):
            continue
        if item.get("status") == "done":
            item["status"] = "pending"
            item.setdefault("evidence", []).append(
                "Rolled back by premature-finalizing guard because an earlier prerequisite is unfinished."
            )


def _first_unfinished_work_item(task_state: TaskState) -> dict[str, Any] | None:
    goal = task_state.goal if isinstance(task_state.goal, dict) else {}
    items = goal.get("work_items")
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("status") != "done":
            return item
    return None


def _work_item_is_finalizing(item: dict[str, Any]) -> bool:
    text = str(item.get("title") or "").lower()
    return any(
        marker in text
        for marker in [
            "export",
            "导出",
            "continue",
            "download",
            "下载",
            "submit",
            "提交",
            "save",
            "保存",
        ]
    )


def _state_update_gate(
    *,
    step_number: int,
    steps_since_model_state_update: int,
    has_model_client: bool,
    decision: ActionDecision,
    action: ActionCall,
    before: PageObservation,
    after: PageObservation,
    result: dict,
) -> dict[str, Any]:
    """Decide whether the expensive LLM StateUpdater is worth calling.

    Every step has already applied deterministic facts before this gate runs.
    The model updater is reserved for moments where global progress may need a
    semantic rewrite: resource transitions, high-risk/finalizing actions,
    large page changes, or periodic summary checkpoints. Plain goto is kept
    deterministic because the next observe already captures the new page.
    """

    if not has_model_client:
        return {
            "call_model": False,
            "reason": "no_state_update_llm_configured",
        }

    status = str(result.get("status") or "")
    if status in {"action_failed", "batch_failed"}:
        return {
            "call_model": True,
            "reason": "action_failure_requires_semantic_state_update",
        }

    if action.type in {"finish", "assert"}:
        return {
            "call_model": True,
            "reason": f"{action.type}_action_requires_goal_state_update",
        }

    if action.type == "goto":
        return {
            "call_model": False,
            "reason": "navigation_observe_on_next_step_is_enough",
        }

    if _url_context_changed(before.url, after.url):
        return {
            "call_model": True,
            "reason": "url_context_changed",
        }

    if action.type == "upload" or _result_contains_upload(status, result):
        return {
            "call_model": True,
            "reason": "resource_upload_changes_task_state",
        }

    if decision.risk == "high":
        return {
            "call_model": True,
            "reason": "high_risk_decision_requires_semantic_state_update",
        }

    if _looks_like_finalizing_or_external_action(action, decision):
        return {
            "call_model": True,
            "reason": "finalizing_or_external_action_requires_semantic_state_update",
        }

    if decision.mode == "batch" and len(decision.actions) > 1:
        return {
            "call_model": True,
            "reason": "batch_commit_requires_single_semantic_summary",
        }

    if (
        decision.risk == "low"
        and action.type in {"click", "double_click", "input", "hotkey", "scroll"}
        and not _looks_like_finalizing_or_external_action(action, decision)
        and not _url_context_changed(before.url, after.url)
        and steps_since_model_state_update < 4
    ):
        return {
            "call_model": False,
            "reason": "low_risk_local_action_deterministic_update_is_enough",
        }

    if _page_text_changed_substantially(before.text_excerpt, after.text_excerpt):
        return {
            "call_model": True,
            "reason": "page_text_changed_substantially",
        }

    if steps_since_model_state_update >= 4:
        return {
            "call_model": True,
            "reason": "periodic_state_summary_checkpoint",
        }

    return {
        "call_model": False,
        "reason": "low_risk_local_action_deterministic_update_is_enough",
    }


def _url_context_changed(before_url: str, after_url: str) -> bool:
    if not before_url or not after_url or before_url == after_url:
        return False
    before = urlparse(before_url)
    after = urlparse(after_url)
    return (before.scheme, before.netloc, before.path) != (
        after.scheme,
        after.netloc,
        after.path,
    )


def _result_contains_upload(status: str, result: dict) -> bool:
    if status == "uploaded":
        return True
    runtime_result = result.get("runtime_result")
    if isinstance(runtime_result, dict) and runtime_result.get("status") == "uploaded":
        return True
    return False


def _looks_like_finalizing_or_external_action(
    action: ActionCall,
    decision: ActionDecision,
) -> bool:
    text = " ".join(
        [
            action.type,
            action.reason or "",
            action.expected_result or "",
            decision.reason or "",
            decision.expected_result or "",
            action.value or "",
            action.url or "",
        ]
    ).lower()
    markers = [
        "export",
        "download",
        "render",
        "encoding",
        "submit",
        "generate",
        "save",
        "publish",
        "login",
        "sign in",
        "delete",
        "remove",
        "pay",
        "checkout",
        "导出",
        "下载",
        "渲染",
        "编码",
        "提交",
        "生成",
        "保存",
        "发布",
        "登录",
        "删除",
        "支付",
    ]
    return any(marker in text for marker in markers)


def _page_text_changed_substantially(before_text: str, after_text: str) -> bool:
    before = " ".join(str(before_text or "").split())
    after = " ".join(str(after_text or "").split())
    if not before or not after:
        return False
    if before == after:
        return False
    before_words = set(before.lower().split())
    after_words = set(after.lower().split())
    if not before_words or not after_words:
        return False
    overlap = len(before_words & after_words)
    larger = max(len(before_words), len(after_words))
    return larger >= 30 and overlap / larger < 0.55


def _looks_like_auth_or_error(observation: PageObservation) -> bool:
    text = f"{observation.url} {observation.title} {observation.text_excerpt}".lower()
    markers = [
        "login",
        "sign in",
        "accounts.google.com",
        "oauth",
        "verify you are human",
        "cloudflare",
        "404",
        "not found",
    ]
    return any(marker in text for marker in markers)
