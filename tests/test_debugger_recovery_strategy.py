from app.debugger.rollback import Debugger
from app.generation.task_parser import TaskParser


def test_debugger_fallback_prefers_refresh_before_restart() -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    state.site["last_stable_url"] = "https://example.com/project"
    state.stable_points.append(
        {
            "id": "sp_project",
            "url": "https://example.com/project",
            "reason": "project page",
            "after_step_id": "s8",
        }
    )

    recovery = Debugger(None).recover(
        state,
        {
            "step_id": "s9",
            "category": "generation_loop_or_no_progress",
            "work_item_id": "w_export",
        },
    )

    assert recovery["strategy"] == "refresh_page_retry"
    assert recovery["rollback_url"] == "https://example.com/project"
    assert "刷新" in recovery["repair_instruction"]


def test_debugger_fallback_escalates_to_restart_after_refresh_attempt() -> None:
    state = TaskParser(None).parse(
        request="导出视频",
        entry_url="https://example.com",
        resources=[],
    )
    state.site["last_stable_url"] = "https://example.com/project"
    state.stable_points.append(
        {
            "id": "sp_project",
            "url": "https://example.com/project",
            "reason": "project page",
            "after_step_id": "s9",
        }
    )

    recovery = Debugger(None).recover(
        state,
        {
            "step_id": "s10",
            "category": "generation_loop_or_no_progress",
            "work_item_id": "w_export",
            "recovery_attempts": ["refresh_page_retry"],
        },
    )

    assert recovery["strategy"] == "restart_browser_retry"
    assert recovery["rollback_url"] == "https://example.com/project"
    assert "关闭浏览器" in recovery["repair_instruction"]
