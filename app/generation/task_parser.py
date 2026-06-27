from __future__ import annotations

from pathlib import Path
import re

from app.core.schema import TaskState
from app.llm.client import LLMError, OpenAICompatibleClient


class TaskParser:
    def __init__(self, client: OpenAICompatibleClient | None = None) -> None:
        self.client = client

    def parse(self, *, request: str, entry_url: str, resources: list[str]) -> TaskState:
        if self.client is not None:
            try:
                return self._parse_with_llm(request=request, entry_url=entry_url, resources=resources)
            except LLMError:
                # The skeleton must still be usable without network/model access.
                pass
        return self._fallback_parse(request=request, entry_url=entry_url, resources=resources)

    def _parse_with_llm(self, *, request: str, entry_url: str, resources: list[str]) -> TaskState:
        assert self.client is not None
        data = self.client.chat_json(
            system=(
                "你是 Web 自动化 Agent 的任务状态初始化器。"
                "只输出 JSON。不要生成固定点击步骤，只生成 milestone 和关键 task_state。"
            ),
            user=(
                "根据用户需求生成 task_state JSON。必须包含 original_user_request, site, resources, "
                "goal, milestones, current_milestone_id, rolling_context, stable_points, recent_trace, "
                f"known_failures, avoid。\n用户需求：{request}\n入口 URL：{entry_url}\n资源：{resources}"
            ),
        )
        return TaskState(**data)

    def _fallback_parse(self, *, request: str, entry_url: str, resources: list[str]) -> TaskState:
        resource_items = [{"type": _guess_resource_type(path), "path": path, "status": "available"} for path in resources]
        work_items = _split_request_into_work_items(request)
        milestones = [
            {"id": "m1", "title": "进入目标网站并理解当前页面", "status": "pending", "evidence": []},
            {"id": "m2", "title": "完成用户要求的主要编辑/操作任务", "status": "pending", "evidence": []},
            {"id": "m3", "title": "验证结果并完成导出或最终提交", "status": "pending", "evidence": []},
        ]
        return TaskState(
            original_user_request=request,
            site={"entry_url": entry_url, "current_url": entry_url, "last_stable_url": entry_url},
            resources=resource_items,
            goal={
                "summary": request,
                "success_criteria": [request],
                "work_items": work_items,
            },
            milestones=milestones,
            current_milestone_id="m1",
            rolling_context={
                "previous_step": None,
                "current_page": {"url": entry_url, "title": "", "page_summary": "尚未打开或观察页面", "goal_relevant_observations": []},
                "next_step": {
                    "milestone_id": "m1",
                    "milestone_title": milestones[0]["title"],
                    "intent": "打开入口 URL，观察页面并找到推进用户目标的最小下一步",
                    "success_condition": "页面可访问，能看到与任务相关的入口或工作区",
                    "not_yet_needed": [],
                },
            },
            stable_points=[{"id": "sp0", "url": entry_url, "reason": "用户提供的入口 URL", "after_step_id": None}],
        )


def _split_request_into_work_items(request: str) -> list[dict]:
    text = " ".join(str(request or "").split())
    if not text:
        return []
    parts = re.split(r"(?:；|;|，然后|然后|，最后|最后|，再|再|并且|以及|，|,)", text)
    items: list[dict] = []
    for index, part in enumerate(parts, start=1):
        item = part.strip(" ，,。.;；")
        if len(item) < 2:
            continue
        items.append(
            {
                "id": f"w{len(items) + 1}",
                "title": item,
                "status": "pending",
                "evidence": [],
            }
        )
    if not items:
        items.append({"id": "w1", "title": text, "status": "pending", "evidence": []})
    return items


def _guess_resource_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in {".mp4", ".mov", ".avi", ".webm", ".mkv"}:
        return "video"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image"
    return "file"
