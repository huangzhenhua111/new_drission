from app.core.schema import ActionCall, PageObservation
from app.generation.state_updater import TaskStateUpdater
from app.generation.task_parser import TaskParser


def test_state_updater_marks_uploaded_resource_and_advances_next_step() -> None:
    state = TaskParser(None).parse(
        request="打开视频编辑器，上传 /tmp/sample.mp4；然后剪掉开头 2 秒；最后导出",
        entry_url="https://example.com",
        resources=["/tmp/sample.mp4"],
    )
    updater = TaskStateUpdater(None)
    before = PageObservation(url="https://example.com", title="home")
    after = PageObservation(url="https://example.com/editor", title="editor", text_excerpt="00:26.63")
    action = ActionCall(
        type="upload",
        reason="upload sample",
        path="/tmp/sample.mp4",
        target_candidate_id="cand_1",
        expected_result="video appears in timeline",
    )
    result = {
        "status": "uploaded",
        "runtime_result": {"status": "uploaded", "path": "/tmp/sample.mp4"},
    }

    updater.update_after_step(
        task_state=state,
        step_id="s2",
        before=before,
        after=after,
        action=action,
        result=result,
    )

    assert state.resources[0]["status"] == "uploaded"
    assert state.resources[0]["uploaded_at_step"] == "s2"
    upload_items = [item for item in state.goal["work_items"] if "上传" in item["title"]]
    assert upload_items[0]["status"] == "done"
    assert state.rolling_context["next_step"]["work_item_id"] != upload_items[0]["id"]
