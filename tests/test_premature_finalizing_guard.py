from app.core.schema import ActionCall, ActionDecision
from app.generation.agent_loop import _block_premature_finalizing_action
from app.generation.task_parser import TaskParser


def test_premature_export_is_blocked_before_unfinished_editing_items() -> None:
    state = TaskParser(None).parse(
        request="上传 sample.mp4，把视频速度调整为 1.5 倍，最后点击 Export",
        entry_url="https://example.com",
        resources=["/tmp/sample.mp4"],
    )
    state.goal["work_items"][0]["status"] = "done"
    state.goal["work_items"][1]["status"] = "in_progress"
    export = ActionCall(
        type="click",
        reason="Click Export button",
        selector="css:button.export",
        expected_result="Export dialog opens",
    )
    decision = ActionDecision(mode="single", actions=[export], risk="high")

    guarded = _block_premature_finalizing_action(decision, state)

    assert guarded.actions[0].type == "hotkey"
    assert guarded.actions[0].value == "ESC"
    assert state.rolling_context["next_step"]["work_item_id"] == state.goal["work_items"][1]["id"]
    assert state.known_failures[-1]["category"] == "premature_finalizing_action_blocked"


def test_export_is_allowed_when_first_unfinished_item_is_export() -> None:
    state = TaskParser(None).parse(
        request="上传 sample.mp4，最后点击 Export",
        entry_url="https://example.com",
        resources=["/tmp/sample.mp4"],
    )
    state.goal["work_items"][0]["status"] = "done"
    state.goal["work_items"][1]["status"] = "in_progress"
    export = ActionCall(
        type="click",
        reason="Click Export button",
        selector="css:button.export",
        expected_result="Export dialog opens",
    )
    decision = ActionDecision(mode="single", actions=[export], risk="high")

    guarded = _block_premature_finalizing_action(decision, state)

    assert guarded.actions[0].type == "click"


def test_premature_export_rolls_back_later_export_done_state() -> None:
    state = TaskParser(None).parse(
        request="上传 sample.mp4，把视频不透明度调低一点，最后点击 Export",
        entry_url="https://example.com",
        resources=["/tmp/sample.mp4"],
    )
    state.goal["work_items"][0]["status"] = "done"
    state.goal["work_items"][1]["status"] = "in_progress"
    state.goal["work_items"][2]["status"] = "done"
    export = ActionCall(
        type="click",
        reason="Click Continue",
        selector="css:button.continue",
        expected_result="Export starts",
    )
    decision = ActionDecision(mode="single", actions=[export], risk="high")

    guarded = _block_premature_finalizing_action(decision, state)

    assert guarded.actions[0].type == "hotkey"
    assert state.goal["work_items"][1]["status"] == "in_progress"
    assert state.goal["work_items"][2]["status"] == "pending"


def test_repeated_premature_export_hands_off_to_debugger() -> None:
    state = TaskParser(None).parse(
        request="上传 sample.mp4，把视频不透明度调低一点，最后点击 Export",
        entry_url="https://example.com",
        resources=["/tmp/sample.mp4"],
    )
    state.goal["work_items"][0]["status"] = "done"
    state.goal["work_items"][1]["status"] = "in_progress"
    for _ in range(2):
        state.known_failures.append(
            {
                "category": "premature_finalizing_action_blocked",
                "work_item_id": state.goal["work_items"][1]["id"],
            }
        )
    export = ActionCall(
        type="click",
        reason="Click Export",
        selector="css:button.export",
        expected_result="Export dialog opens",
    )
    decision = ActionDecision(mode="single", actions=[export], risk="high")

    guarded = _block_premature_finalizing_action(decision, state)

    assert guarded.request_debugger is True
    assert guarded.actions[0].type == "wait"
