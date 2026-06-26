import json

from app.core.schema import ActionCall


def test_public_result_excludes_action_for_state_object() -> None:
    action = ActionCall(type="click", reason="x", target_candidate_id="cand_1")
    result = {
        "status": "clicked",
        "action_for_state": action,
        "runtime_result": {"status": "clicked"},
    }
    public_result = {key: value for key, value in result.items() if key != "action_for_state"}

    json.dumps(public_result)
    assert "action_for_state" not in public_result
