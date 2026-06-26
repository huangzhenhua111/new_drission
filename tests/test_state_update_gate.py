from app.core.schema import ActionCall, ActionDecision, PageObservation
from app.generation.agent_loop import _state_update_gate


def _decision(action: ActionCall, *, risk: str = "low", mode: str = "single") -> ActionDecision:
    return ActionDecision(mode=mode, actions=[action], risk=risk)  # type: ignore[arg-type]


def test_state_update_gate_skips_low_risk_local_input() -> None:
    action = ActionCall(
        type="input",
        reason="set opacity value",
        value="50",
        expected_result="Opacity input changes to 50",
    )

    gate = _state_update_gate(
        step_number=12,
        steps_since_model_state_update=1,
        has_model_client=True,
        decision=_decision(action),
        action=action,
        before=PageObservation(url="https://example.com/project", text_excerpt="Opacity 100"),
        after=PageObservation(url="https://example.com/project", text_excerpt="Opacity 50"),
        result={"status": "input"},
    )

    assert gate["call_model"] is False
    assert gate["reason"] == "low_risk_local_action_deterministic_update_is_enough"


def test_state_update_gate_calls_model_for_upload_and_export() -> None:
    upload = ActionCall(type="upload", reason="upload resource", path="/tmp/a.mp4")
    upload_gate = _state_update_gate(
        step_number=3,
        steps_since_model_state_update=0,
        has_model_client=True,
        decision=_decision(upload, risk="medium"),
        action=upload,
        before=PageObservation(url="https://example.com/project"),
        after=PageObservation(url="https://example.com/project"),
        result={"status": "uploaded"},
    )
    assert upload_gate["call_model"] is True
    assert upload_gate["reason"] == "resource_upload_changes_task_state"

    export = ActionCall(
        type="click",
        reason="click Export button",
        expected_result="start video export and download",
    )
    export_gate = _state_update_gate(
        step_number=20,
        steps_since_model_state_update=0,
        has_model_client=True,
        decision=_decision(export, risk="medium"),
        action=export,
        before=PageObservation(url="https://example.com/project"),
        after=PageObservation(url="https://example.com/project"),
        result={"status": "clicked"},
    )
    assert export_gate["call_model"] is True
    assert export_gate["reason"] == "finalizing_or_external_action_requires_semantic_state_update"


def test_state_update_gate_calls_periodic_checkpoint() -> None:
    action = ActionCall(type="click", reason="open local panel")

    gate = _state_update_gate(
        step_number=9,
        steps_since_model_state_update=4,
        has_model_client=True,
        decision=_decision(action),
        action=action,
        before=PageObservation(url="https://example.com/project", text_excerpt="Panel A"),
        after=PageObservation(url="https://example.com/project", text_excerpt="Panel A"),
        result={"status": "clicked"},
    )

    assert gate["call_model"] is True
    assert gate["reason"] == "periodic_state_summary_checkpoint"


def test_state_update_gate_skips_when_no_model_client() -> None:
    action = ActionCall(type="click", reason="click Export button")

    gate = _state_update_gate(
        step_number=1,
        steps_since_model_state_update=10,
        has_model_client=False,
        decision=_decision(action, risk="high"),
        action=action,
        before=PageObservation(url="https://example.com"),
        after=PageObservation(url="https://example.com/export"),
        result={"status": "clicked"},
    )

    assert gate["call_model"] is False
    assert gate["reason"] == "no_state_update_llm_configured"
