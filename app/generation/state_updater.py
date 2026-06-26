from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any

from app.core.schema import ActionCall, PageObservation, TaskState
from app.llm.client import LLMError, OpenAICompatibleClient


class TaskStateUpdater:
    """Keeps the global task JSON aligned with observed execution facts.

    Deterministic runtime facts are applied first. The model is then allowed to
    summarize progress and choose the next work item, but it should not invent
    or overwrite hard facts such as local resource paths or successful uploads.
    """

    def __init__(self, client: OpenAICompatibleClient | None = None) -> None:
        self.client = client

    def update_after_step(
        self,
        *,
        task_state: TaskState,
        step_id: str,
        before: PageObservation,
        after: PageObservation,
        action: ActionCall,
        result: dict,
        apply_deterministic: bool = True,
    ) -> dict[str, Any]:
        started = perf_counter()
        if apply_deterministic:
            self.apply_deterministic_after_step(
                task_state=task_state,
                step_id=step_id,
                before=before,
                after=after,
                action=action,
                result=result,
            )
        if self.client is None:
            return {"status": "deterministic_only", "elapsed_seconds": round(perf_counter() - started, 3)}
        model_update = self._ask_model_for_state_update(
            task_state=task_state,
            step_id=step_id,
            before=before,
            after=after,
            action=action,
            result=result,
        )
        self._merge_model_update(task_state, model_update)
        self._apply_deterministic_facts(
            task_state=task_state,
            step_id=step_id,
            before=before,
            after=after,
            action=action,
            result=result,
        )
        self._ensure_work_items(task_state)
        self._update_next_step_fallback(task_state)
        self._normalize_work_item_progress(task_state)
        return {"status": "model_updated", "elapsed_seconds": round(perf_counter() - started, 3)}

    def apply_deterministic_after_step(
        self,
        *,
        task_state: TaskState,
        step_id: str,
        before: PageObservation,
        after: PageObservation,
        action: ActionCall,
        result: dict,
    ) -> None:
        self._apply_deterministic_facts(
            task_state=task_state,
            step_id=step_id,
            before=before,
            after=after,
            action=action,
            result=result,
        )
        self._ensure_work_items(task_state)
        self._normalize_work_item_progress(task_state)
        self._update_next_step_fallback(task_state)

    def update_after_recovery_refresh(
        self,
        *,
        task_state: TaskState,
        step_id: str,
        failure: dict[str, Any],
        recovery: dict[str, Any],
        before_recovery: PageObservation,
        after_recovery: PageObservation,
    ) -> dict[str, Any]:
        """Recalibrate the same global JSON after a page refresh/rollback.

        A refresh can keep the project URL but silently roll the application
        state back. The expensive model update is appropriate here because it
        may need to move some previously-completed work_items back to
        in_progress/pending based on the fresh page.
        """

        started = perf_counter()
        if self.client is not None:
            update = self._ask_model_for_recovery_update(
                task_state=task_state,
                step_id=step_id,
                mode="refresh_page_retry",
                failure=failure,
                recovery=recovery,
                before_recovery=before_recovery,
                after_recovery=after_recovery,
            )
            self._merge_model_update(task_state, update)
            self._ensure_work_items(task_state)
            self._normalize_work_item_progress(task_state)
            self._update_next_step_fallback(task_state)
            return {"status": "model_updated", "elapsed_seconds": round(perf_counter() - started, 3)}

        self._fallback_recalibrate_after_refresh(
            task_state=task_state,
            step_id=step_id,
            failure=failure,
            after_recovery=after_recovery,
        )
        return {"status": "deterministic_recovery_update", "elapsed_seconds": round(perf_counter() - started, 3)}

    def rebuild_after_browser_restart(
        self,
        *,
        old_task_state: TaskState,
        step_id: str,
        failure: dict[str, Any],
        recovery: dict[str, Any],
        after_recovery: PageObservation,
    ) -> tuple[TaskState, dict[str, Any]]:
        """Create a fresh task JSON after restarting the browser.

        The old JSON is used only as evidence to produce replay hints. The new
        state does not carry over current-session completion flags, because the
        browser/application session has been reset.
        """

        started = perf_counter()
        if self.client is not None:
            update = self._ask_model_for_restart_rebuild(
                old_task_state=old_task_state,
                step_id=step_id,
                failure=failure,
                recovery=recovery,
                after_recovery=after_recovery,
            )
            new_state = self._task_state_from_restart_update(
                old_task_state=old_task_state,
                update=update,
                after_recovery=after_recovery,
            )
            return new_state, {"status": "model_rebuilt", "elapsed_seconds": round(perf_counter() - started, 3)}

        new_state = _fallback_restart_task_state(
            old_task_state=old_task_state,
            step_id=step_id,
            failure=failure,
            recovery=recovery,
            after_recovery=after_recovery,
        )
        return new_state, {"status": "deterministic_restart_rebuild", "elapsed_seconds": round(perf_counter() - started, 3)}

    def _apply_deterministic_facts(
        self,
        *,
        task_state: TaskState,
        step_id: str,
        before: PageObservation,
        after: PageObservation,
        action: ActionCall,
        result: dict,
    ) -> None:
        status = str(result.get("status") or "")
        if action.type == "goto" and status == "navigated":
            _mark_matching_work_item(
                task_state,
                ["打开", "进入", "网站", "页面", "url", "视频编辑器"],
                step_id,
                f"Navigated to {after.url}",
            )
        if action.type == "upload" and status == "uploaded":
            uploaded_path = str((result.get("runtime_result") or {}).get("path") or action.path or "")
            for resource in task_state.resources:
                path = str(resource.get("path") or "")
                if not path:
                    continue
                if path == action.path or Path(path).name == Path(uploaded_path).name or Path(path).name == Path(action.path or "").name:
                    resource["status"] = "uploaded"
                    resource["uploaded_at_step"] = step_id
                    resource["runtime_path"] = uploaded_path
                    evidence = resource.setdefault("evidence", [])
                    _append_unique(evidence, f"Runtime upload succeeded at {step_id}")
            _mark_matching_work_item(
                task_state,
                ["上传", "导入", "素材", "时间线", "文件"],
                step_id,
                f"Runtime upload succeeded for {action.path}",
            )
        if action.type in {"click", "double_click", "input", "hotkey"} and status in {"clicked", "input", "hotkey"}:
            _mark_open_work_item_in_progress(
                task_state,
                step_id,
                f"{action.type} executed: {action.expected_result or action.reason}",
            )
        if action.type == "finish" or status == "finished":
            for item in _work_items(task_state):
                if item.get("status") != "done":
                    item["status"] = "done"
                    _append_unique(item.setdefault("evidence", []), f"Finished at {step_id}")

    def _ask_model_for_state_update(
        self,
        *,
        task_state: TaskState,
        step_id: str,
        before: PageObservation,
        after: PageObservation,
        action: ActionCall,
        result: dict,
    ) -> dict[str, Any]:
        assert self.client is not None
        payload = {
            "task_state": {
                "original_user_request": task_state.original_user_request,
                "site": task_state.site,
                "resources": task_state.resources,
                "goal": task_state.goal,
                "milestones": task_state.milestones,
                "current_milestone_id": task_state.current_milestone_id,
                "rolling_context": task_state.rolling_context,
                "recent_trace": task_state.recent_trace[-8:],
                "known_failures": task_state.known_failures[-5:],
            },
            "executed_step": {
                "step_id": step_id,
                "action": asdict(action),
                "result": result,
                "before": _compact_observation(before),
                "after": _compact_observation(after),
            },
        }
        data = self.client.chat_json(
            system=_STATE_UPDATER_SYSTEM_PROMPT,
            user=json.dumps(payload, ensure_ascii=False, indent=2),
            images=[],
            temperature=0,
            timeout=240,
        )
        return data.get("state_update", data)

    def _ask_model_for_recovery_update(
        self,
        *,
        task_state: TaskState,
        step_id: str,
        mode: str,
        failure: dict[str, Any],
        recovery: dict[str, Any],
        before_recovery: PageObservation,
        after_recovery: PageObservation,
    ) -> dict[str, Any]:
        assert self.client is not None
        payload = {
            "mode": mode,
            "instruction": (
                "页面已按 Debugger 建议刷新/回退。请根据刷新前失败记录和刷新后的页面，"
                "重新判断哪些 work_items 仍然成立，哪些需要从 done 回退到 in_progress/pending。"
            ),
            "task_state": task_state.to_dict(),
            "failure": failure,
            "recovery": recovery,
            "before_recovery": _compact_observation(before_recovery),
            "after_recovery": _compact_observation(after_recovery),
        }
        data = self.client.chat_json(
            system=_RECOVERY_REFRESH_SYSTEM_PROMPT,
            user=json.dumps(payload, ensure_ascii=False, indent=2),
            images=[],
            temperature=0,
            timeout=240,
        )
        return data.get("state_update", data)

    def _ask_model_for_restart_rebuild(
        self,
        *,
        old_task_state: TaskState,
        step_id: str,
        failure: dict[str, Any],
        recovery: dict[str, Any],
        after_recovery: PageObservation,
    ) -> dict[str, Any]:
        assert self.client is not None
        payload = {
            "mode": "restart_browser_retry",
            "instruction": (
                "浏览器已经重启，这是一个新会话。请基于旧 JSON 的成功证据生成新的 task_state。"
                "旧会话中成功过的动作只能变成 replay_hints，不能继续作为当前会话 done 状态。"
            ),
            "old_task_state": old_task_state.to_dict(),
            "failure": failure,
            "recovery": recovery,
            "after_recovery": _compact_observation(after_recovery),
        }
        data = self.client.chat_json(
            system=_RESTART_REBUILD_SYSTEM_PROMPT,
            user=json.dumps(payload, ensure_ascii=False, indent=2),
            images=[],
            temperature=0,
            timeout=240,
        )
        return data.get("task_state", data.get("state_update", data))

    def _task_state_from_restart_update(
        self,
        *,
        old_task_state: TaskState,
        update: dict[str, Any],
        after_recovery: PageObservation,
    ) -> TaskState:
        base = _fallback_restart_task_state(
            old_task_state=old_task_state,
            step_id="restart_rebuild",
            failure={},
            recovery={},
            after_recovery=after_recovery,
        )
        if isinstance(update, dict):
            for key in [
                "site",
                "resources",
                "goal",
                "milestones",
                "current_milestone_id",
                "rolling_context",
                "stable_points",
                "recent_trace",
                "known_failures",
                "avoid",
            ]:
                if key in update:
                    setattr(base, key, update[key])
        self._ensure_work_items(base)
        self._normalize_work_item_progress(base)
        self._update_next_step_fallback(base)
        return base

    def _merge_model_update(self, task_state: TaskState, update: dict[str, Any]) -> None:
        if not isinstance(update, dict):
            return
        hard_uploaded = {
            str(resource.get("path")): dict(resource)
            for resource in task_state.resources
            if resource.get("status") == "uploaded"
        }

        resources = update.get("resources")
        if isinstance(resources, list):
            merged_resources = []
            by_path = {str(item.get("path")): item for item in task_state.resources}
            for item in resources:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "")
                if not path:
                    continue
                original = dict(by_path.get(path, {}))
                original.update(item)
                if path in hard_uploaded:
                    original.update(hard_uploaded[path])
                merged_resources.append(original)
            if merged_resources:
                existing_paths = {str(item.get("path")) for item in merged_resources}
                for item in task_state.resources:
                    if str(item.get("path")) not in existing_paths:
                        merged_resources.append(item)
                task_state.resources = merged_resources

        goal = update.get("goal")
        if isinstance(goal, dict):
            old_goal = dict(task_state.goal)
            old_goal.update({k: v for k, v in goal.items() if k not in {"summary", "success_criteria"}})
            task_state.goal = old_goal

        milestones = update.get("milestones")
        if isinstance(milestones, list) and milestones:
            task_state.milestones = [item for item in milestones if isinstance(item, dict)]

        current_milestone_id = update.get("current_milestone_id")
        if isinstance(current_milestone_id, str) and current_milestone_id:
            task_state.current_milestone_id = current_milestone_id

        rolling_context = update.get("rolling_context")
        if isinstance(rolling_context, dict):
            for key in ["next_step", "task_progress_summary"]:
                if key in rolling_context:
                    task_state.rolling_context[key] = rolling_context[key]

    def _ensure_work_items(self, task_state: TaskState) -> None:
        if not isinstance(task_state.goal, dict):
            task_state.goal = {"summary": task_state.original_user_request}
        items = task_state.goal.get("work_items")
        if isinstance(items, list) and items:
            for index, item in enumerate(items, start=1):
                if isinstance(item, dict):
                    item.setdefault("id", f"w{index}")
                    item.setdefault("status", "pending")
                    item.setdefault("evidence", [])
            return
        task_state.goal["work_items"] = [
            {
                "id": "w1",
                "title": task_state.original_user_request,
                "status": "pending",
                "evidence": [],
            }
        ]

    def _update_next_step_fallback(self, task_state: TaskState) -> None:
        items = _work_items(task_state)
        current = next((item for item in items if item.get("status") in {"in_progress", "blocked"}), None)
        if current is None:
            current = next((item for item in items if item.get("status") != "done"), None)
        if current is None:
            task_state.rolling_context["next_step"] = {
                "work_item_id": None,
                "intent": "验证所有用户目标是否已经完成；如果已完成，输出 finish。",
                "success_condition": "所有 work_items 均为 done，并且最终页面状态满足用户需求。",
            }
            return
        task_state.rolling_context["next_step"] = {
            "work_item_id": current.get("id"),
            "intent": current.get("title"),
            "success_condition": current.get("success_condition") or f"完成事项：{current.get('title')}",
            "known_evidence": current.get("evidence", []),
        }

    def _normalize_work_item_progress(self, task_state: TaskState) -> None:
        """Keep a single active work item.

        The model may optimistically mark future items in_progress when it sees
        related UI. That makes the next-step signal fuzzy, so only the first
        not-done item may remain in_progress; later items are reset to pending
        unless blocked/done.
        """
        seen_active = False
        for item in _work_items(task_state):
            status = item.get("status")
            if status == "done":
                continue
            if status == "blocked":
                continue
            if not seen_active and status == "in_progress":
                seen_active = True
                continue
            if not seen_active and status == "pending":
                continue
            if seen_active and status == "in_progress":
                item["status"] = "pending"

    def _fallback_recalibrate_after_refresh(
        self,
        *,
        task_state: TaskState,
        step_id: str,
        failure: dict[str, Any],
        after_recovery: PageObservation,
    ) -> None:
        self._ensure_work_items(task_state)
        failed_id = failure.get("work_item_id")
        if not failed_id:
            failed_id = (task_state.rolling_context.get("next_step") or {}).get("work_item_id")
        seen_failed = False
        for item in _work_items(task_state):
            if failed_id and item.get("id") == failed_id:
                seen_failed = True
                item["status"] = "in_progress"
                _append_unique(
                    item.setdefault("evidence", []),
                    f"{step_id}: refreshed page requires revalidation from {after_recovery.url}",
                )
                continue
            if seen_failed and item.get("status") == "done":
                item["status"] = "pending"
                _append_unique(
                    item.setdefault("evidence", []),
                    f"{step_id}: reset after refresh because later progress may no longer be valid",
                )
        task_state.rolling_context["recovery_note"] = {
            "mode": "refresh_page_retry",
            "step_id": step_id,
            "after_url": after_recovery.url,
            "instruction": "刷新可能导致页面状态回退；接下来必须重新观察并验证当前 work_item 是否仍成立。",
        }
        self._normalize_work_item_progress(task_state)
        self._update_next_step_fallback(task_state)


_STATE_UPDATER_SYSTEM_PROMPT = """你是 Web 自动化 Agent 的全局 JSON 状态更新器。

你只负责根据刚执行的一步、前后页面和截图，更新“做到哪了”和“下一步要做什么”。
不要生成浏览器动作。

硬约束：
1. 不要修改 original_user_request。
2. 不要删除资源；不要改写本地资源路径。
3. runtime 已成功的事实优先于视觉猜测。比如 result.status=uploaded 表示该资源已上传，不要因为界面上仍有上传按钮就改回 pending。
4. 如果不确定某个 work_item 是否完成，标成 in_progress，不要标 done。
5. next_step 必须指向第一个 pending/in_progress 的用户事项，而不是回到已经完成的事项。

只输出 JSON：
{
  "state_update": {
    "resources": [...],
    "goal": {
      "work_items": [
        {"id": "w1", "title": "...", "status": "pending|in_progress|done|blocked", "evidence": ["..."]}
      ]
    },
    "milestones": [...],
    "current_milestone_id": "...",
    "rolling_context": {
      "task_progress_summary": "...",
      "next_step": {
        "work_item_id": "w2",
        "intent": "...",
        "success_condition": "...",
        "known_evidence": ["..."]
      }
    }
  }
}
"""


_RECOVERY_REFRESH_SYSTEM_PROMPT = """你是 Web 自动化 Agent 的恢复态 JSON 校准器。

当前发生的是 refresh_page_retry：浏览器没有重启，但页面已经刷新/回退到 recovery URL。
刷新可能导致网页内部状态回退，比如弹窗关闭、导出中断、未保存编辑丢失、某些已完成操作需要重做。

你的任务：
1. 根据 failure/recovery/before_recovery/after_recovery 判断哪些 work_items 仍可信。
2. 如果刷新后页面证据不能支持某个 done 状态，把它改成 in_progress 或 pending。
3. 不要因为旧 JSON 写着 done 就盲目保留 done；done 必须能被刷新后的页面或硬事实支持。
4. runtime 硬事实仍然优先：本地资源路径不能改；上传成功这类 runtime 事实可以作为证据，但刷新后是否仍在当前项目里要看页面。
5. next_step 指向刷新后第一个需要继续/重做/验证的事项。

只输出 JSON：
{
  "state_update": {
    "resources": [...],
    "goal": {
      "work_items": [
        {"id": "w1", "title": "...", "status": "pending|in_progress|done|blocked", "evidence": ["..."]}
      ]
    },
    "milestones": [...],
    "current_milestone_id": "...",
    "rolling_context": {
      "task_progress_summary": "...",
      "next_step": {
        "work_item_id": "...",
        "intent": "...",
        "success_condition": "...",
        "known_evidence": ["..."]
      },
      "recovery_note": "..."
    }
  }
}
"""


_RESTART_REBUILD_SYSTEM_PROMPT = """你是 Web 自动化 Agent 的重启恢复 JSON 重建器。

当前发生的是 restart_browser_retry：浏览器已经关闭并重新打开，这是一个新会话。
旧 JSON 不能继续作为当前完成状态使用。

你的任务：
1. 生成一个新的 task_state JSON。
2. 保留 original_user_request、入口 URL、本地资源路径、用户目标。
3. 旧会话里成功过的 work_items/动作只能写入 replay_hints，表示“可以照着快速重放的经验”。
4. 新会话的 work_items 默认应该是 pending/in_progress，而不是沿用旧会话的 done。
5. 如果 after_recovery 页面已经自然满足某些目标，可以把对应 work_item 标 done，但必须写清页面证据。
6. rolling_context.next_step 应指向新会话接下来要正式执行的第一件事。

只输出完整 JSON task_state：
{
  "original_user_request": "...",
  "site": {...},
  "resources": [...],
  "goal": {
    "summary": "...",
    "success_criteria": [...],
    "work_items": [...],
    "replay_hints": [
      {
        "source_work_item_id": "...",
        "title": "...",
        "previous_evidence": [...],
        "recommended_fast_replay": "..."
      }
    ]
  },
  "milestones": [...],
  "current_milestone_id": "...",
  "rolling_context": {...},
  "stable_points": [...],
  "recent_trace": [],
  "known_failures": [...],
  "avoid": [...]
}
"""


def _compact_observation(observation: PageObservation) -> dict[str, Any]:
    return {
        "url": observation.url,
        "title": observation.title,
        "text_excerpt": observation.text_excerpt[:1600],
        "candidate_count": len(observation.candidates),
        "candidate_summaries": [
            {
                "id": candidate.id,
                "tag": candidate.tag,
                "role": candidate.role,
                "text": candidate.text,
                "accessible_name": candidate.accessible_name,
                "action_allowed": candidate.action_allowed,
                "extra": {
                    key: candidate.extra.get(key)
                    for key in ["type", "value", "context_text", "label_text"]
                    if candidate.extra.get(key)
                },
            }
            for candidate in observation.candidates[:40]
        ],
    }


def _work_items(task_state: TaskState) -> list[dict[str, Any]]:
    goal = task_state.goal if isinstance(task_state.goal, dict) else {}
    items = goal.get("work_items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _mark_matching_work_item(
    task_state: TaskState,
    keywords: list[str],
    step_id: str,
    evidence: str,
) -> None:
    lowered_keywords = [keyword.lower() for keyword in keywords]
    for item in _work_items(task_state):
        title = str(item.get("title") or "").lower()
        if item.get("status") == "done":
            continue
        if any(keyword in title for keyword in lowered_keywords):
            item["status"] = "done"
            _append_unique(item.setdefault("evidence", []), f"{step_id}: {evidence}")
            return
    _mark_open_work_item_in_progress(task_state, step_id, evidence)


def _mark_open_work_item_in_progress(task_state: TaskState, step_id: str, evidence: str) -> None:
    for item in _work_items(task_state):
        if item.get("status") == "pending":
            item["status"] = "in_progress"
            _append_unique(item.setdefault("evidence", []), f"{step_id}: {evidence}")
            return


def _append_unique(items: list, value: str) -> None:
    if value not in items:
        items.append(value)


def _fallback_restart_task_state(
    *,
    old_task_state: TaskState,
    step_id: str,
    failure: dict[str, Any],
    recovery: dict[str, Any],
    after_recovery: PageObservation,
) -> TaskState:
    replay_hints = []
    new_work_items = []
    for item in _work_items(old_task_state):
        old_status = item.get("status")
        new_item = {
            "id": item.get("id"),
            "title": item.get("title"),
            "status": "pending",
            "evidence": [],
            "previous_session_status": old_status,
        }
        if old_status == "done":
            replay_hints.append(
                {
                    "source_work_item_id": item.get("id"),
                    "title": item.get("title"),
                    "previous_evidence": item.get("evidence", []),
                    "recommended_fast_replay": (
                        "旧会话中此事项成功过；新会话不要直接视为 done，"
                        "但可以让 Generation 参考证据快速重放并重新验证。"
                    ),
                }
            )
        new_work_items.append(new_item)
    if new_work_items:
        new_work_items[0]["status"] = "in_progress"

    resources = []
    for resource in old_task_state.resources:
        item = dict(resource)
        if item.get("status") == "uploaded":
            item["previous_session_status"] = "uploaded"
            item["status"] = "available"
            evidence = item.setdefault("evidence", [])
            _append_unique(
                evidence,
                f"{step_id}: browser restarted; upload must be revalidated/replayed in the new session",
            )
        resources.append(item)

    entry_url = old_task_state.site.get("entry_url") or after_recovery.url
    current_url = after_recovery.url or recovery.get("rollback_url") or entry_url
    goal = {
        "summary": old_task_state.goal.get("summary", old_task_state.original_user_request),
        "success_criteria": old_task_state.goal.get("success_criteria", [old_task_state.original_user_request]),
        "work_items": new_work_items,
        "replay_hints": replay_hints,
        "restart_recovery": {
            "from_step_id": failure.get("step_id"),
            "reason": failure.get("category") or failure.get("error") or "restart_browser_retry",
            "old_recovery_attempts": old_task_state.goal.get("recovery_attempts", []),
        },
    }
    return TaskState(
        original_user_request=old_task_state.original_user_request,
        site={
            "entry_url": entry_url,
            "current_url": current_url,
            "last_stable_url": current_url,
            "previous_session_url": old_task_state.site.get("current_url"),
        },
        resources=resources,
        goal=goal,
        milestones=[
            {
                "id": "m_restart_replay",
                "title": "根据旧会话成功经验，在新浏览器会话中快速重放并重新验证",
                "status": "in_progress",
                "evidence": [f"Created after restart recovery at {step_id}"],
            },
            {
                "id": "m_finish",
                "title": "完成剩余用户目标并验证最终结果",
                "status": "pending",
                "evidence": [],
            },
        ],
        current_milestone_id="m_restart_replay",
        rolling_context={
            "previous_step": {
                "step_id": step_id,
                "action_summary": "restart_browser_retry",
                "actual_result": f"New browser session at {current_url}",
                "status": "recovered",
            },
            "current_page": {
                "url": current_url,
                "title": after_recovery.title,
                "page_summary": after_recovery.text_excerpt[:600],
                "goal_relevant_observations": [],
                "page_state_confidence": 0.5,
            },
            "next_step": {
                "work_item_id": new_work_items[0].get("id") if new_work_items else None,
                "intent": "新浏览器会话已启动；参考 replay_hints 快速重放旧会话中验证过的成功动作，并重新验证当前页面事实。",
                "success_condition": "旧会话成功经验被重新执行/验证，随后继续剩余用户目标。",
                "known_evidence": [hint["recommended_fast_replay"] for hint in replay_hints[:3]],
            },
            "recovery_note": {
                "mode": "restart_browser_retry",
                "instruction": "这是新 JSON；旧 JSON 只提供 replay_hints，不再作为当前完成状态。",
            },
        },
        stable_points=[
            {
                "id": "sp_restart",
                "url": current_url,
                "reason": "New browser session after restart recovery",
                "after_step_id": step_id,
            }
        ],
        recent_trace=[],
        known_failures=[
            {
                "step_id": failure.get("step_id"),
                "category": failure.get("category"),
                "recovered_by": "restart_browser_retry",
            }
        ],
        avoid=list(old_task_state.avoid[-10:]),
    )
