from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.core.schema import ActionCall, ActionDecision, PageObservation, TaskState


class OutcomeChecker:
    """Detect suspicious post-action outcomes and generation loops.

    This component is deliberately not a recovery policy. It only decides when
    the evidence should be escalated to Debugger. Debugger then decides whether
    the root cause is a wrong action, missing state, task misunderstanding, or a
    no-obvious-agent-error case that deserves a browser restart.
    """

    def check(
        self,
        *,
        task_state: TaskState,
        step_id: str,
        before: PageObservation,
        after: PageObservation,
        decision: ActionDecision,
        action: ActionCall,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        obvious_failure = _detect_obvious_failure_after_high_risk(
            step_id=step_id,
            before=before,
            after=after,
            decision=decision,
            action=action,
            result=result,
        )
        if obvious_failure:
            return obvious_failure

        processing_state = _detect_in_progress_after_finalizing_action(
            decision=decision,
            action=action,
            after=after,
        )
        if processing_state:
            return None

        loop_failure = _detect_generation_loop(
            task_state=task_state,
            step_id=step_id,
            before=before,
            after=after,
            decision=decision,
            action=action,
            result=result,
        )
        if loop_failure:
            return loop_failure
        return None

    def check_current_observation(
        self,
        *,
        task_state: TaskState,
        step_id: str,
        observation: PageObservation,
    ) -> dict[str, Any] | None:
        """Detect risky states that appear before the next decision.

        Some web apps show an error after the post-action observe already saw
        an in-progress state. In that case the next loop's initial observe sees
        the actual failure. This method turns that current-page evidence into a
        risk signal for Generation to review.
        """

        page_text = _observation_text(observation)
        failure_markers = _matched_markers(
            page_text,
            [
                "failed",
                "failure",
                "error",
                "retry",
                "unable",
                "unsuccessful",
                "try again",
                "something went wrong",
                "失败",
                "出错",
                "错误",
                "重试",
                "无法",
                "未成功",
            ],
        )
        if not failure_markers:
            return None
        if not _recent_or_previous_finalizing_action(task_state):
            return None

        repeated_count = _recent_failure_marker_count(task_state, failure_markers)
        evidence = [
            f"Current page contains failure-like markers before deciding next action: {failure_markers}.",
            "Recent/previous context indicates a finalizing action such as export/render/download/save.",
            "Generation must review this signal with DOM and screenshot before deciding next step.",
        ]
        mandatory = repeated_count >= 1
        if mandatory:
            evidence.append(
                "The same finalizing action has already failed before; after 2 explicit failures Generation must request Debugger instead of closing and retrying."
            )
        return _signal_context(
            step_id=step_id,
            category="current_page_finalizing_failure",
            observation=observation,
            evidence=evidence,
            mandatory_request_debugger=mandatory,
        )


def _detect_obvious_failure_after_high_risk(
    *,
    step_id: str,
    before: PageObservation,
    after: PageObservation,
    decision: ActionDecision,
    action: ActionCall,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    action_text = _action_text(decision, action)
    page_text = _observation_text(after)
    if not (_is_high_risk(decision, action) or _has_high_risk_intent(action_text)):
        return None
    failure_markers = _matched_markers(
        page_text,
        [
            "failed",
            "failure",
            "error",
            "retry",
            "unable",
            "unsuccessful",
            "try again",
            "something went wrong",
            "失败",
            "出错",
            "错误",
            "重试",
            "无法",
            "未成功",
        ],
    )
    if not failure_markers:
        if _looks_like_in_progress(page_text):
            return None
        return None
    return _failure_context(
        step_id=step_id,
        category="post_action_outcome_uncertain",
        before=before,
        after=after,
        decision=decision,
        action=action,
        result=result,
        evidence=[
            "High-risk or finalizing action executed without a runtime exception.",
            f"Post-action page contains failure-like markers: {failure_markers}.",
            "Detector is only escalating to Debugger; it is not deciding the root cause.",
        ],
    )


def _detect_generation_loop(
    *,
    task_state: TaskState,
    step_id: str,
    before: PageObservation,
    after: PageObservation,
    decision: ActionDecision,
    action: ActionCall,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    recent = list(task_state.recent_trace[-8:])
    if len(recent) < 4:
        return None
    current_family = _action_family(action)
    if not current_family:
        return None
    action_text = _action_text(ActionDecision(mode="single", actions=[action]), action)
    if _has_high_risk_intent(action_text) and _looks_like_in_progress(_observation_text(after)):
        return None
    same_family = [
        item
        for item in recent
        if _action_family_from_trace_item(item) == current_family
    ]
    same_url = [
        item
        for item in recent
        if _same_url_family(str(item.get("url_after") or ""), after.url)
    ]
    same_work_item = _same_work_item_repeated(task_state, recent)
    has_success_signal = _has_success_signal(task_state, current_family)
    if len(same_family) < 2 or len(same_url) < 3 or has_success_signal:
        return None
    if current_family not in {"export", "download", "render", "submit", "save"} and not same_work_item:
        return None
    return _failure_context(
        step_id=step_id,
        category="generation_loop_or_no_progress",
        before=before,
        after=after,
        decision=decision,
        action=action,
        result=result,
        evidence=[
            f"Recent trace repeatedly attempted action family: {current_family}.",
            "Recent steps stayed on the same or equivalent URL.",
            "No success signal was recorded for this action family.",
            "Escalate to Debugger to judge whether this is an agent mistake or no-obvious-agent-error.",
        ],
    )


def _detect_in_progress_after_finalizing_action(
    *,
    decision: ActionDecision,
    action: ActionCall,
    after: PageObservation,
) -> bool:
    action_text = _action_text(decision, action)
    if not (_is_high_risk(decision, action) or _has_high_risk_intent(action_text)):
        return False
    return _looks_like_in_progress(_observation_text(after))


def _failure_context(
    *,
    step_id: str,
    category: str,
    before: PageObservation,
    after: PageObservation,
    decision: ActionDecision,
    action: ActionCall,
    result: dict[str, Any],
    evidence: list[str],
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "category": category,
        "url": after.url or before.url,
        "before": _compact_observation(before),
        "after": _compact_observation(after),
        "decision": asdict(decision),
        "action": asdict(action),
        "expected_result": action.expected_result or decision.expected_result,
        "result": {key: value for key, value in result.items() if key != "action_for_state"},
        "evidence": evidence,
        "work_item_id": None,
    }


def _signal_context(
    *,
    step_id: str,
    category: str,
    observation: PageObservation,
    evidence: list[str],
    mandatory_request_debugger: bool = False,
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "category": category,
        "url": observation.url,
        "after": _compact_observation(observation),
        "evidence": evidence,
        "work_item_id": None,
        "mandatory_request_debugger": mandatory_request_debugger,
    }


def _compact_observation(observation: PageObservation) -> dict[str, Any]:
    return {
        "url": observation.url,
        "title": observation.title,
        "text_excerpt": observation.text_excerpt[:1200],
        "screenshot_path": observation.screenshot_path,
        "candidate_count": len(observation.candidates),
        "top_candidates": [
            {
                "id": item.id,
                "tag": item.tag,
                "role": item.role,
                "text": item.text,
                "accessible_name": item.accessible_name,
                "action_allowed": item.action_allowed,
            }
            for item in observation.candidates[:12]
        ],
    }


def _action_text(decision: ActionDecision, action: ActionCall) -> str:
    parts = [
        decision.reason,
        decision.expected_result,
        action.type,
        action.reason,
        action.expected_result,
        action.value,
        action.url,
    ]
    return " ".join(str(part or "") for part in parts).lower()


def _observation_text(observation: PageObservation) -> str:
    candidate_text = " ".join(
        " ".join(
            str(value or "")
            for value in [
                item.text,
                item.accessible_name,
                item.role,
                item.extra.get("context_text"),
            ]
        )
        for item in observation.candidates[:80]
    )
    return f"{observation.url} {observation.title} {observation.text_excerpt} {candidate_text}".lower()


def _is_high_risk(decision: ActionDecision, action: ActionCall) -> bool:
    return decision.risk == "high" or action.type in {"upload", "goto", "finish"}


def _has_high_risk_intent(text: str) -> bool:
    return bool(
        _matched_markers(
            text,
            [
                "export",
                "download",
                "render",
                "generate",
                "submit",
                "save",
                "导出",
                "下载",
                "渲染",
                "生成",
                "提交",
                "保存",
            ],
        )
    )


def _matched_markers(text: str, markers: list[str]) -> list[str]:
    return [marker for marker in markers if marker.lower() in text]


def _looks_like_in_progress(text: str) -> bool:
    markers = [
        "uploading",
        "processing",
        "encoding",
        "rendering",
        "exporting",
        "generating",
        "preparing",
        "compressing",
        "converting",
        "progress",
        "please wait",
        "cancel",
        "处理中",
        "上传中",
        "正在上传",
        "编码中",
        "渲染中",
        "导出中",
        "生成中",
        "转换中",
        "请稍候",
        "取消",
    ]
    if _matched_markers(text, markers):
        return True
    # Generic progress percentages should count only together with a
    # finalizing/process-ish vocabulary. Plain "50%" in an editor control is
    # not enough; "Uploading files... 50% Cancel" is.
    has_percent = "%" in text
    process_markers = [
        "upload",
        "process",
        "encod",
        "render",
        "export",
        "generat",
        "convert",
        "下载",
        "导出",
        "上传",
        "处理",
        "编码",
        "渲染",
        "生成",
        "转换",
    ]
    return has_percent and any(marker in text for marker in process_markers)


def _action_family(action: ActionCall) -> str | None:
    text = _action_text(ActionDecision(mode="single", actions=[action]), action)
    families = {
        "export": ["export", "导出"],
        "download": ["download", "下载"],
        "render": ["render", "generate", "encoding", "渲染", "生成", "编码"],
        "submit": ["submit", "提交"],
        "save": ["save", "保存"],
        "close_error": ["close", "关闭"],
    }
    for family, markers in families.items():
        if any(marker in text for marker in markers):
            return family
    return None


def _action_family_from_trace_item(item: dict[str, Any]) -> str | None:
    action = item.get("action")
    if not isinstance(action, dict):
        return None
    try:
        call = ActionCall(
            type=action.get("type") or "click",
            reason=str(action.get("reason") or ""),
            target_candidate_id=action.get("target_candidate_id"),
            selector=action.get("selector"),
            value=action.get("value"),
            path=action.get("path"),
            url=action.get("url"),
            seconds=action.get("seconds"),
            direction=action.get("direction"),
            confidence=float(action.get("confidence") or 0),
            expected_result=action.get("expected_result"),
        )
    except Exception:
        return None
    return _action_family(call)


def _same_url_family(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return _strip_hash_query(a).rstrip("/") == _strip_hash_query(b).rstrip("/")


def _strip_hash_query(url: str) -> str:
    return str(url).split("#", 1)[0].split("?", 1)[0]


def _same_work_item_repeated(task_state: TaskState, recent: list[dict[str, Any]]) -> bool:
    current = (task_state.rolling_context.get("next_step") or {}).get("work_item_id")
    if not current:
        return False
    matches = 0
    for item in recent:
        action = item.get("action") or {}
        text = " ".join(
            str(value or "")
            for value in [
                item.get("work_item_id"),
                action.get("reason") if isinstance(action, dict) else "",
                action.get("expected_result") if isinstance(action, dict) else "",
            ]
        )
        if str(current) in text:
            matches += 1
    return matches >= 2


def _has_success_signal(task_state: TaskState, family: str) -> bool:
    if family in {"export", "download", "render", "save"}:
        for resource in task_state.resources:
            status = str(resource.get("status") or "").lower()
            if status in {"exported", "downloaded", "saved", "done"}:
                return True
    return False


def _recent_or_previous_finalizing_action(task_state: TaskState) -> bool:
    previous = task_state.rolling_context.get("previous_step")
    if isinstance(previous, dict):
        text = " ".join(
            str(value or "")
            for value in [
                previous.get("action_summary"),
                previous.get("expected_result"),
                previous.get("actual_result"),
                previous.get("page_summary"),
            ]
        ).lower()
        if _has_high_risk_intent(text):
            return True
    for item in task_state.recent_trace[-6:]:
        action = item.get("action") if isinstance(item, dict) else None
        if not isinstance(action, dict):
            continue
        text = " ".join(
            str(value or "")
            for value in [
                action.get("type"),
                action.get("reason"),
                action.get("expected_result"),
                action.get("selector"),
                item.get("result"),
            ]
        ).lower()
        if _has_high_risk_intent(text):
            return True
    return False


def _recent_failure_marker_count(task_state: TaskState, markers: list[str]) -> int:
    marker_text = " ".join(str(marker).lower() for marker in markers)
    count = 0
    sources: list[str] = []
    previous = task_state.rolling_context.get("previous_step")
    if isinstance(previous, dict):
        sources.append(" ".join(str(value or "") for value in previous.values()))
    next_step = task_state.rolling_context.get("next_step")
    if isinstance(next_step, dict):
        sources.extend(str(value or "") for value in next_step.get("known_evidence") or [])
    for failure in task_state.known_failures[-8:]:
        if isinstance(failure, dict):
            sources.append(" ".join(str(value or "") for value in failure.values()))
    for item in sources:
        lowered = item.lower()
        if any(marker and marker in lowered for marker in marker_text.split()):
            count += 1
    return count
