from __future__ import annotations

import json
from typing import Any

from app.core.schema import TaskState
from app.llm.client import LLMError, OpenAICompatibleClient


class Debugger:
    """LLM-assisted recovery planner.

    First version keeps recovery deliberately simple: inspect task_state and the
    failure, choose a rollback URL, update the global JSON, and mark work that
    should be forced back to single-step execution.
    """

    def __init__(self, client: OpenAICompatibleClient | None = None) -> None:
        self.client = client

    def recover(self, task_state: TaskState, failure: dict) -> dict:
        if self.client is not None:
            try:
                return self._recover_with_llm(task_state, failure)
            except LLMError as exc:
                fallback = self._fallback_recovery(task_state, failure)
                fallback["debugger_error"] = str(exc)[:800]
                return fallback
        return self._fallback_recovery(task_state, failure)

    def _fallback_recovery(self, task_state: TaskState, failure: dict) -> dict:
        stable = list(task_state.stable_points or [])
        rollback_url = task_state.site.get("last_stable_url") or task_state.site.get("entry_url")
        if stable:
            rollback_url = stable[-1]["url"]
        recovery_attempts = failure.get("recovery_attempts") or []
        if "refresh_page_retry" in recovery_attempts:
            strategy = "restart_browser_retry"
            repair_instruction = "刷新页面后仍然失败；关闭浏览器开启新会话后回到最近可信 URL，继续滚动 Generation。"
        else:
            strategy = "refresh_page_retry"
            repair_instruction = "先刷新最近可信 URL/当前项目页后继续滚动 Generation；如果刷新后同一目标仍失败，再升级为重启浏览器。"
        root_cause_step_id = failure.get("root_cause_step_id") or failure.get("step_id")
        category = str(failure.get("category") or "")
        if category in {"missing_prerequisite_state", "script_debugger_handoff"}:
            strategy = "rollback_and_single_step"
            repair_instruction = "脚本复现暴露前置状态缺失；回到最近可信 URL，由 Generation 单步重新完成缺失前置状态。"
        return {
            "root_cause_step_id": root_cause_step_id,
            "root_cause": "unknown_recoverable_failure",
            "strategy": strategy,
            "rollback_url": rollback_url,
            "state_update": {
                "rolling_context": {
                    "next_step": {
                        "intent": "恢复刚才失败/不确定的事项，从刷新后的页面重新单步执行。",
                        "success_condition": "失败动作以 single-step 方式完成，页面状态重新满足用户目标。",
                    }
                },
                "must_single_step_items": [failure.get("work_item_id") or failure.get("step_id")],
            },
            "repair_instruction": repair_instruction,
            "avoid": failure.get("action_summary") or failure.get("error") or "unknown failed action",
        }

    def _recover_with_llm(self, task_state: TaskState, failure: dict) -> dict:
        assert self.client is not None
        payload = {
            "task_state": task_state.to_dict(),
            "failure": failure,
            "stable_points": task_state.stable_points[-10:],
            "recent_trace": task_state.recent_trace[-12:],
        }
        data = self.client.chat_json(
            system=_DEBUGGER_SYSTEM_PROMPT,
            user=json.dumps(payload, ensure_ascii=False, indent=2),
            images=[],
            temperature=0,
            timeout=240,
        )
        recovery = data.get("recovery", data)
        if not isinstance(recovery, dict):
            raise LLMError("Debugger did not return a recovery object")
        fallback = self._fallback_recovery(task_state, failure)
        fallback.update({key: value for key, value in recovery.items() if value is not None})
        fallback.setdefault("rollback_url", task_state.site.get("last_stable_url") or task_state.site.get("entry_url"))
        fallback.setdefault("strategy", "refresh_page_retry")
        return fallback


_DEBUGGER_SYSTEM_PROMPT = """你是 Web 自动化 Agent 的 Debugger。

你的任务不是继续点击网页，而是看全局 JSON、recent_trace 和失败信息，决定：
1. 应该回退到哪个 URL。
2. 全局 JSON 应该如何更新，尤其是 rolling_context.next_step。
3. 哪些 work_item 或动作接下来必须 single-step，不能 batch。
4. 需要 avoid 的失败动作模式。
5. 如果失败上下文显示：动作链、目标元素、输入值和页面状态都没有明显错误，
   但同一目标反复失败/无进展，才可以判断 root_cause = "no_obvious_agent_error"。
   此时优先 strategy = "refresh_page_retry"，表示刷新当前可信 URL/项目页后重试。
   只有 failure.recovery_attempts 已经包含 refresh_page_retry，或者失败证据明确说明刷新无效，
   才升级 strategy = "restart_browser_retry"，表示关闭浏览器开启新会话重试。

如果 failure.source = "script_debugger"，说明 standalone 脚本复现时发现了流程/状态错误。
这时不要输出“修脚本”的建议，而要判断 Generation 应回退到哪里继续：
- missing_prerequisite_state：通常 strategy = "rollback_and_single_step"，回到最近可信项目页/工作区，
  把缺失前置目标写入 rolling_context.next_step，并把相关动作加入 must_single_step_items。
- page_internal_failure / stale_browser_session：优先 refresh_page_retry；刷新已试过再 restart_browser_retry。
- 如果 root_cause_step_id 指向早于 symptom_step 的步骤，必须以 root_cause_step_id 为准。

不要因为看到 failed/error 文案就直接重启。必须先判断是否存在更具体的 Agent 错误：
- wrong_target_or_selector：点错/选错元素。
- missing_prerequisite_state：前置状态没完成，比如资源未上传、未进入工作区。
- task_misunderstanding：任务理解错，比如调错对象。
- no_obvious_agent_error：看不出上述问题，更像浏览器会话/站点临时内部状态。

只输出 JSON：
{
  "recovery": {
    "root_cause_step_id": "...",
    "root_cause": "wrong_target_or_selector | missing_prerequisite_state | task_misunderstanding | no_obvious_agent_error | unknown_recoverable_failure",
    "strategy": "rollback_and_single_step | update_state_and_continue | refresh_page_retry | restart_browser_retry",
    "rollback_url": "https://...",
    "state_update": {
      "rolling_context": {
        "next_step": {
          "work_item_id": "...",
          "intent": "...",
          "success_condition": "...",
          "known_evidence": ["..."]
        }
      },
      "must_single_step_items": ["work_item_id or action pattern"]
    },
    "repair_instruction": "...",
    "avoid": "..."
  }
}
"""
