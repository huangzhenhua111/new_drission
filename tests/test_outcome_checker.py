from app.core.schema import ActionCall, ActionDecision, PageObservation
from app.generation.task_parser import TaskParser
from app.recovery.outcome_checker import OutcomeChecker


def test_high_risk_failure_text_escalates_to_debugger_context() -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    before = PageObservation(url="https://example.com/editor", text_excerpt="Ready to export")
    after = PageObservation(url="https://example.com/editor", text_excerpt="Encoding failed. Try again.")
    action = ActionCall(
        type="click",
        reason="Click Export",
        target_candidate_id="cand_export",
        expected_result="Start exporting and download the result",
    )
    decision = ActionDecision(mode="single", risk="high", actions=[action])

    failure = OutcomeChecker().check(
        task_state=state,
        step_id="s9",
        before=before,
        after=after,
        decision=decision,
        action=action,
        result={"status": "clicked"},
    )

    assert failure is not None
    assert failure["category"] == "post_action_outcome_uncertain"
    assert "failed" in " ".join(failure["evidence"]).lower()


def test_high_risk_processing_progress_does_not_escalate_to_debugger() -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    before = PageObservation(url="https://example.com/editor", text_excerpt="Ready to export")
    after = PageObservation(
        url="https://example.com/editor",
        text_excerpt="Uploading files... 50% Cancel",
    )
    action = ActionCall(
        type="click",
        reason="Click Export",
        target_candidate_id="cand_export",
        expected_result="Start exporting and download the result",
    )
    decision = ActionDecision(mode="single", risk="high", actions=[action])

    failure = OutcomeChecker().check(
        task_state=state,
        step_id="s9",
        before=before,
        after=after,
        decision=decision,
        action=action,
        result={"status": "clicked"},
    )

    assert failure is None


def test_repeated_export_family_on_same_url_escalates_generation_loop() -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    state.recent_trace = [
        _trace_item("s1", "Click Export"),
        _trace_item("s2", "Close failed dialog"),
        _trace_item("s3", "Choose 720p export quality"),
        _trace_item("s4", "Click Export again"),
    ]
    before = PageObservation(url="https://example.com/project/1", text_excerpt="Export panel")
    after = PageObservation(url="https://example.com/project/1", text_excerpt="Export panel")
    action = ActionCall(
        type="click",
        reason="Click Export again",
        target_candidate_id="cand_export",
        expected_result="Start export",
    )
    decision = ActionDecision(mode="single", risk="high", actions=[action])

    failure = OutcomeChecker().check(
        task_state=state,
        step_id="s5",
        before=before,
        after=after,
        decision=decision,
        action=action,
        result={"status": "clicked"},
    )

    assert failure is not None
    assert failure["category"] == "generation_loop_or_no_progress"


def test_current_page_encoding_failed_after_export_creates_risk_signal() -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    state.rolling_context["previous_step"] = {
        "action_summary": "click cand_export",
        "expected_result": "Start export",
        "actual_result": "clicked final Export button",
    }
    observation = PageObservation(
        url="https://example.com/project/1",
        text_excerpt="Encoding failed Close",
    )

    signal = OutcomeChecker().check_current_observation(
        task_state=state,
        step_id="s10",
        observation=observation,
    )

    assert signal is not None
    assert signal["category"] == "current_page_finalizing_failure"
    assert signal["mandatory_request_debugger"] is False
    assert "failed" in " ".join(signal["evidence"]).lower()


def test_current_page_second_encoding_failed_requires_debugger_signal() -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    state.rolling_context["previous_step"] = {
        "action_summary": "click cand_export",
        "expected_result": "Start export",
        "actual_result": "clicked final Export button",
    }
    state.rolling_context["next_step"] = {
        "work_item_id": "w1",
        "known_evidence": ["s8: previous export attempt showed Encoding failed"],
    }
    observation = PageObservation(
        url="https://example.com/project/1",
        text_excerpt="Encoding failed Close",
    )

    signal = OutcomeChecker().check_current_observation(
        task_state=state,
        step_id="s10",
        observation=observation,
    )

    assert signal is not None
    assert signal["mandatory_request_debugger"] is True


def _trace_item(step_id: str, reason: str) -> dict:
    return {
        "step_id": step_id,
        "url_before": "https://example.com/project/1",
        "url_after": "https://example.com/project/1",
        "action": {
            "type": "click",
            "reason": reason,
            "expected_result": reason,
        },
        "result": "clicked",
    }
