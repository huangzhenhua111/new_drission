from app.core.schema import ActionCall, PageObservation
from app.generation.state_updater import TaskStateUpdater
from app.generation.task_parser import TaskParser


class _OptimisticDoneClient:
    def chat_json(self, **kwargs):  # noqa: ANN003, ANN201
        return {
            "state_update": {
                "goal": {
                    "work_items": [
                        {
                            "id": "w1",
                            "title": "上传 /tmp/sample.mp4，把视频速度调整为 1.5 倍，添加标题文字“短视频测试”，把视频不透明度调低一点",
                            "status": "done",
                            "evidence": ["s2: Runtime upload succeeded for /tmp/sample.mp4"],
                        },
                        {
                            "id": "w2",
                            "title": "最后点击 Export",
                            "status": "pending",
                            "evidence": [],
                        },
                    ]
                },
                "rolling_context": {
                    "next_step": {
                        "work_item_id": "w2",
                        "intent": "最后点击 Export",
                    }
                },
            }
        }


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


def test_goto_does_not_mark_complex_work_item_done() -> None:
    state = TaskParser(None).parse(
        request="打开 123Apps Video Editor，上传 /tmp/sample.mp4，剪掉开头 2 秒，调速 1.5，添加标题并下载",
        entry_url="https://123apps.com/",
        resources=["/tmp/sample.mp4"],
    )
    updater = TaskStateUpdater(None)
    before = PageObservation(url="chrome://newtab/", title="new tab")
    after = PageObservation(url="https://123apps.com/", title="123Apps", text_excerpt="Video Editor")
    action = ActionCall(
        type="goto",
        reason="open site",
        url="https://123apps.com/",
        expected_result="home page loads",
    )

    updater.update_after_step(
        task_state=state,
        step_id="s1",
        before=before,
        after=after,
        action=action,
        result={"status": "navigated"},
    )

    assert any(item.get("status") == "done" for item in state.goal["work_items"] if "打开" in item["title"])
    assert all(
        item.get("status") != "done"
        for item in state.goal["work_items"]
        if any(marker in item["title"] for marker in ["上传", "剪", "调速", "标题", "下载"])
    )
    assert state.rolling_context["next_step"]["work_item_id"] != state.goal["work_items"][0]["id"]


def test_state_updater_downgrades_done_when_evidence_misses_editing_requirements() -> None:
    state = TaskParser(None).parse(
        request="上传 /tmp/sample.mp4，把视频速度调整为 1.5 倍，添加标题文字“短视频测试”，把视频不透明度调低一点，最后点击 Export",
        entry_url="https://example.com",
        resources=["/tmp/sample.mp4"],
    )
    updater = TaskStateUpdater(_OptimisticDoneClient())
    before = PageObservation(url="https://example.com/editor", title="editor")
    after = PageObservation(url="https://example.com/editor", title="editor")
    action = ActionCall(
        type="upload",
        reason="upload sample",
        path="/tmp/sample.mp4",
        expected_result="video appears in timeline",
    )

    updater.update_after_step(
        task_state=state,
        step_id="s2",
        before=before,
        after=after,
        action=action,
        result={"status": "uploaded", "runtime_result": {"status": "uploaded", "path": "/tmp/sample.mp4"}},
    )

    first = state.goal["work_items"][0]
    assert first["status"] == "in_progress"
    assert "Export" not in state.rolling_context["next_step"]["intent"]


def test_deterministic_update_marks_speed_done_only_after_value_selected() -> None:
    state = TaskParser(None).parse(
        request="打开编辑器，上传 sample.mp4，把视频速度调整为 1.5 倍，最后点击 Export",
        entry_url="https://example.com",
        resources=["/tmp/sample.mp4"],
    )
    for item in state.goal["work_items"]:
        if "速度" in item["title"]:
            break
        item["status"] = "done"
        if "上传" in item["title"]:
            item["evidence"] = ["s2: Runtime upload succeeded for /tmp/sample.mp4"]
    speed_item = next(item for item in state.goal["work_items"] if "速度" in item["title"])
    speed_item["status"] = "in_progress"
    updater = TaskStateUpdater(None)
    before = PageObservation(url="https://example.com/editor", title="editor")
    after = PageObservation(url="https://example.com/editor", title="editor")

    updater.apply_deterministic_after_step(
        task_state=state,
        step_id="s3",
        before=before,
        after=after,
        action=ActionCall(
            type="click",
            reason="点击 Speed 标签切换到速度调整面板，为下一步设置 1.5 倍速做准备",
            expected_result="Speed settings panel opens",
        ),
        result={"status": "clicked"},
    )

    assert speed_item["status"] == "in_progress"

    updater.apply_deterministic_after_step(
        task_state=state,
        step_id="s4",
        before=before,
        after=after,
        action=ActionCall(
            type="click",
            reason="点击 1.5 倍速预设按钮",
            expected_result="视频速度被设置为 1.5 倍",
        ),
        result={"status": "clicked"},
    )

    assert speed_item["status"] == "done"


def test_deterministic_update_marks_text_done_only_after_input_value() -> None:
    state = TaskParser(None).parse(
        request="打开编辑器，添加标题文字“短视频测试”，最后点击 Export",
        entry_url="https://example.com",
        resources=[],
    )
    for item in state.goal["work_items"]:
        if "标题" in item["title"] or "文字" in item["title"]:
            break
        item["status"] = "done"
    text_item = next(item for item in state.goal["work_items"] if "标题" in item["title"] or "文字" in item["title"])
    text_item["status"] = "in_progress"
    updater = TaskStateUpdater(None)
    before = PageObservation(url="https://example.com/editor", title="editor")
    after = PageObservation(url="https://example.com/editor", title="editor")

    updater.apply_deterministic_after_step(
        task_state=state,
        step_id="s5",
        before=before,
        after=after,
        action=ActionCall(
            type="click",
            reason="点击左侧边栏的 Text 按钮，打开文字添加面板，以便添加标题文字“短视频测试”。",
            expected_result="Text panel opens",
        ),
        result={"status": "clicked"},
    )

    assert text_item["status"] == "in_progress"

    updater.apply_deterministic_after_step(
        task_state=state,
        step_id="s6",
        before=before,
        after=after,
        action=ActionCall(
            type="input",
            reason="输入标题文字",
            value="短视频测试",
            expected_result="标题文字变为短视频测试",
        ),
        result={"status": "input"},
    )

    assert text_item["status"] == "done"


def test_deterministic_update_does_not_mark_text_done_when_placeholder_was_appended() -> None:
    state = TaskParser(None).parse(
        request="打开编辑器，添加标题文字“短视频测试”，最后点击 Export",
        entry_url="https://example.com",
        resources=[],
    )
    for item in state.goal["work_items"]:
        if "标题" in item["title"] or "文字" in item["title"]:
            text_item = item
            text_item["status"] = "in_progress"
            break
        item["status"] = "done"
    else:
        raise AssertionError("text work item not found")

    updater = TaskStateUpdater(None)
    before = PageObservation(url="https://example.com/editor", title="editor")
    after = PageObservation(
        url="https://example.com/editor",
        title="editor",
        text_excerpt="Canvas shows Title text短视频测试 on the preview.",
    )

    updater.apply_deterministic_after_step(
        task_state=state,
        step_id="s6",
        before=before,
        after=after,
        action=ActionCall(
            type="input",
            reason="输入标题文字",
            value="短视频测试",
            expected_result="文字被替换为短视频测试",
        ),
        result={"status": "input"},
    )

    assert text_item["status"] == "in_progress"


def test_deterministic_update_marks_color_done_only_after_red_color_evidence() -> None:
    state = TaskParser(None).parse(
        request="打开编辑器，添加标题文字“短视频测试”，把标题字体颜色改成红色，最后点击 Export",
        entry_url="https://example.com",
        resources=[],
    )
    for item in state.goal["work_items"]:
        if "颜色" in item["title"] or "红色" in item["title"]:
            color_item = item
            color_item["status"] = "in_progress"
            break
        item["status"] = "done"
    else:
        raise AssertionError("color work item not found")

    updater = TaskStateUpdater(None)
    before = PageObservation(url="https://example.com/editor", title="editor")
    after = PageObservation(url="https://example.com/editor", title="editor")

    updater.apply_deterministic_after_step(
        task_state=state,
        step_id="s7",
        before=before,
        after=after,
        action=ActionCall(
            type="input",
            reason="输入标题文字",
            value="短视频测试",
            expected_result="标题文字变为短视频测试",
        ),
        result={"status": "input"},
    )

    assert color_item["status"] in {"pending", "in_progress"}

    updater.apply_deterministic_after_step(
        task_state=state,
        step_id="s8",
        before=before,
        after=after,
        action=ActionCall(
            type="click",
            reason="选择红色文字颜色",
            expected_result="标题字体颜色设置为红色",
        ),
        result={"status": "clicked"},
    )

    assert color_item["status"] == "done"
