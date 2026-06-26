from app.generation.task_parser import TaskParser


def test_fallback_task_parser_has_rolling_context() -> None:
    state = TaskParser(None).parse(
        request="上传 sample.mp4 并添加标题",
        entry_url="https://example.com",
        resources=["/tmp/sample.mp4"],
    )
    assert state.site["entry_url"] == "https://example.com"
    assert state.resources[0]["type"] == "video"
    assert state.rolling_context["previous_step"] is None
    assert "current_page" in state.rolling_context
    assert "next_step" in state.rolling_context


def test_fallback_task_parser_creates_work_items() -> None:
    state = TaskParser(None).parse(
        request="打开网站，上传 sample.mp4；然后添加标题；最后导出",
        entry_url="https://example.com",
        resources=["/tmp/sample.mp4"],
    )
    work_items = state.goal["work_items"]
    assert len(work_items) >= 3
    assert work_items[0]["status"] == "pending"
    assert any("上传" in item["title"] for item in work_items)
