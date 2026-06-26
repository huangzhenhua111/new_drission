from app.core.schema import ActionCall, ActionDecision
from app.generation.agent_loop import _enforce_must_single
from app.generation.task_parser import TaskParser


def test_debugger_must_single_item_forces_batch_to_single() -> None:
    state = TaskParser(None).parse(
        request="调整速度并导出",
        entry_url="https://example.com",
        resources=[],
    )
    state.rolling_context["next_step"] = {"work_item_id": "w2", "intent": "调整速度"}
    state.goal["must_single_step_items"] = ["w2"]
    decision = ActionDecision(
        mode="batch",
        risk="low",
        reason="same panel edit",
        actions=[
            ActionCall(type="click", reason="open panel", target_candidate_id="cand_1"),
            ActionCall(type="input", reason="set value", target_candidate_id="cand_2", value="1.5"),
        ],
    )

    enforced = _enforce_must_single(decision, state)

    assert enforced.mode == "single"
    assert len(enforced.actions) == 1
