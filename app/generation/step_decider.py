from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.schema import ActionCall, ActionDecision, PageObservation, TaskState
from app.llm.client import OpenAICompatibleClient


class StepDecider:
    """Multimodal rolling decision maker.

    It does not plan the full website path. It answers one question:
    based on previous step, current page and next intent, what is the safest
    minimal next tool call?
    """

    def __init__(self, client: OpenAICompatibleClient) -> None:
        self.client = client

    def decide(self, *, task_state: TaskState, observation: PageObservation) -> ActionDecision:
        result = self.client.chat_json(
            system=_SYSTEM_PROMPT,
            user=_build_user_prompt(task_state, observation),
            images=[Path(observation.screenshot_path)] if observation.screenshot_path else [],
            temperature=0.1,
            timeout=240,
        )
        return _parse_decision(result)


_SYSTEM_PROMPT = """你是浏览器自动化 Agent 的多模态 Step Decider。

你每次输出一个安全的下一步决策，可以是 single，也可以是小批次 batch。
你必须综合 rolling_context、DOM candidates、当前 URL/title/text 和截图。
rolling_context.next_step 是当前最重要的待办事项；goal.work_items 和 resources 是长期记忆。
不要重复执行 status=done 的 work_item。
必须按 goal.work_items 的顺序推进：只有前面的 work_item 都是 status=done，才可以执行后面的 work_item。
特别是 Export、导出、Continue、下载、提交等最终动作，只有上传、调速、加文字、调透明度等前置编辑事项已经明确 done 后才能执行。
如果当前 next_step/当前未完成 work_item 不是最终导出事项，即使页面上能看到 Export 按钮，也不要提前点击 Export。
如果 resources 里某个文件 status=uploaded，说明 runtime 已确认上传成功；除非 known_failures 明确说明上传失败，否则不要再次上传同一资源。
选择目标规则：
1. 目标元素在 dom_candidates 中时，必须输出 target_candidate_id。
2. selector 只能逐字复制该 candidate 提供的 selector/selectors，不能自己创造 selector。
3. 不要输出 Playwright 专属 selector，例如 :has-text(...)、:text(...)。
4. 如果截图上看得到目标但 dom_candidates 中没有对应元素，不要编 selector；输出 wait/scroll/单步重新观察，或说明需要 debugger/visual grounding。
5. Export、导出、登录、删除、支付、提交等中高风险动作必须从现有 DOM candidate 中选择；没有 candidate_id 时不要执行。
当前第一版不要输出坐标。
如果页面还没有打开，第一步使用 goto。
如果需要上传文件，优先选择 action_allowed 包含 upload 的 candidate，并输出 upload + path。
如果目标控件是当前 DOM 中已经存在的 input/textarea/contenteditable，优先直接输出 input 动作；不要先 click 再 input。
如果滑块、旋钮、拖拽条旁边有可编辑的数值输入框，优先使用数值输入框，因为它更可验证、更可 replay。
Speed、Opacity、字号、音量、时间等数值型目标：如果输入框已在 DOM 中，直接 input 目标值。
如果需要编辑画布中文字，优先先 click/double_click 文字对象或 contenteditable，再 input。
添加标题文字时，如果页面已有默认占位文字（例如 Title text、Sample Text、Text），目标是把占位文字完整替换为用户要求的文字；不要把新文字追加到占位文字后面。
对画布/预览区里的已有文字执行 input 时，应理解为“先全选原文字再输入新文字”；expected_result 也要写成“文字被替换为 xxx”，不要写成“追加 xxx”。
如果任务要求设置字体颜色，例如红色，必须在文字对象被选中后找到颜色/Color/Fill/文字颜色控件并设置为目标颜色；不要只输入文字就把文字样式事项标记完成。
如果当前动作完成了全部目标，输出 finish。

risk_signal 规则：
1. rolling_context.risk_signal 只是规则层传感器给出的异常信号，不是最终裁决。
2. 你必须结合截图、DOM、全局 JSON、前后页面记录自己判断。
3. 如果 risk_signal 其实是正常处理中，例如导出/上传/渲染进度还在变化，输出 wait 或下一步观察动作，不要 request_debugger。
4. 如果你判断当前状态确实无法安全继续，例如明显失败、状态自相矛盾、前面某步可能错了、连续无进展，设置 request_debugger=true，并说明 debugger_reason。
5. request_debugger=true 时不会执行 actions；actions 可填一个 wait 占位，但原因必须说明为什么需要 Debugger 回退/校准。
6. 硬纪律：同一个高风险最终动作（Export/导出/Render/Generate/Save/Submit/Download）已经出现 2 次明确失败
   （例如 Encoding failed、Export failed、Render failed、error、retry、失败、出错）时，必须 request_debugger=true。
   不要再输出 Close 错误弹窗后继续重试，也不要改参数后继续重试；应交给 Debugger 判断刷新、重启或回退。
7. 如果 rolling_context.risk_signal.mandatory_request_debugger=true，必须 request_debugger=true。

batch 规则：
1. 只有同一页面、同一局部面板、低风险连续动作才允许 batch。
2. batch 最多 3 个动作。
3. batch 内每个动作仍必须绑定 target_candidate_id 或 selector。
4. goto、upload、finish、assert、登录、导出、删除、支付、提交等高风险动作必须 single。
5. 如果一个动作会导致页面跳转、打开文件选择器、开始导出或登录，不要和后续动作合并。
6. 低风险且目标已经在当前 DOM candidates 中可见时，应优先 batch；不要保守地拆成多个 single。
7. 如果后续目标要等前一个动作后才出现在 DOM 中，除非你有稳定 selector，否则不要 batch 到那个未来目标。
8. 如果目标 input 已经可见，通常不要 batch 成「click input -> input 值」，而是直接 single input。
9. 适合 single input 的例子：Speed 面板已展开且速度输入框已在 DOM 中：直接 input 1.5；Opacity 面板已展开且百分比输入框已在 DOM 中：直接 input 50。
10. 适合 batch 的例子：多个目标都已经在当前 DOM 中可见，且不是一个 input 动作就能完成，例如勾选多个低风险选项。
11. 如果你输出 single 且 risk=low，请在 reason 中说明为什么不能安全 batch。

只输出 JSON：
{
  "mode": "single|batch",
  "risk": "low|medium|high",
  "reason": "...",
  "why_batch_is_safe": "... or null",
  "expected_result": "...",
  "request_debugger": false,
  "debugger_reason": "... or null",
  "actions": [
    {
      "type": "goto|click|double_click|input|upload|wait|scroll|hotkey|assert|finish",
      "reason": "...",
      "target_candidate_id": "cand_x or null",
      "selector": "optional stable selector",
      "value": "optional text",
      "path": "optional file path",
      "url": "optional url",
      "seconds": 1,
      "direction": "down|up|left|right|null",
      "confidence": 0.0,
      "expected_result": "..."
    }
  ],
  "commit_after": true,
  "stop_if_any_action_fails": true
}
"""


def _parse_decision(result: dict[str, Any]) -> ActionDecision:
    if "action" in result and "actions" not in result:
        action = _parse_action(result["action"])
        return ActionDecision(
            mode="single",
            actions=[action],
            risk="medium",
            reason=action.reason,
            expected_result=action.expected_result,
        )

    raw_actions = result.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raw_actions = [result]
    actions = [_parse_action(item) for item in raw_actions[:3] if isinstance(item, dict)]
    if not actions:
        actions = [_parse_action(result)]

    mode = str(result.get("mode") or ("batch" if len(actions) > 1 else "single")).lower()
    risk = str(result.get("risk") or "medium").lower()
    decision = ActionDecision(
        mode="batch" if mode == "batch" and len(actions) > 1 else "single",
        actions=actions,
        risk=risk if risk in {"low", "medium", "high"} else "medium",
        reason=str(result.get("reason") or actions[0].reason),
        why_batch_is_safe=result.get("why_batch_is_safe"),
        expected_result=result.get("expected_result") or actions[-1].expected_result,
        commit_after=bool(result.get("commit_after", True)),
        stop_if_any_action_fails=bool(result.get("stop_if_any_action_fails", True)),
        request_debugger=bool(result.get("request_debugger", False)),
        debugger_reason=result.get("debugger_reason"),
    )
    return _sanitize_decision(decision)


def _parse_action(raw: dict[str, Any]) -> ActionCall:
    allowed = set(ActionCall.__dataclass_fields__)
    data = {key: value for key, value in raw.items() if key in allowed}
    data.setdefault("reason", "Model selected this action.")
    data.setdefault("type", "wait")
    return ActionCall(**data)


def _sanitize_decision(decision: ActionDecision) -> ActionDecision:
    high_risk_types = {"goto", "upload", "finish", "assert"}
    risky_keywords = ["export", "导出", "login", "sign in", "delete", "删除", "pay", "支付", "submit", "提交"]
    has_high_risk_type = any(action.type in high_risk_types for action in decision.actions)
    has_risky_text = any(
        any(keyword in " ".join([action.reason or "", action.expected_result or "", action.selector or ""]).lower() for keyword in risky_keywords)
        for action in decision.actions
    )
    if decision.risk != "low" or has_high_risk_type or has_risky_text:
        first = decision.actions[0]
        return ActionDecision(
            mode="single",
            actions=[first],
            risk="high" if has_high_risk_type or has_risky_text else decision.risk,
            reason=decision.reason,
            why_batch_is_safe=None,
            expected_result=first.expected_result,
            request_debugger=decision.request_debugger,
            debugger_reason=decision.debugger_reason,
        )
    return decision


def _build_user_prompt(task_state: TaskState, observation: PageObservation) -> str:
    compact_candidates = [
        {
            "id": c.id,
            "tag": c.tag,
            "role": c.role,
            "text": c.text,
            "accessible_name": c.accessible_name,
            "selector": c.selector,
            "selectors": c.selectors[:3],
            "rect": c.rect,
            "action_allowed": c.action_allowed,
            "extra": {
                key: c.extra.get(key)
                for key in [
                    "type",
                    "placeholder",
                    "aria_label",
                    "label_text",
                    "context_text",
                    "css_path",
                    "value",
                    "href",
                ]
                if c.extra.get(key)
            },
        }
        for c in observation.candidates
    ]
    payload = {
        "rolling_context": task_state.rolling_context,
        "current_milestone_id": task_state.current_milestone_id,
        "milestones": task_state.milestones,
        "goal": task_state.goal,
        "resources": task_state.resources,
        "site": task_state.site,
        "avoid": task_state.avoid,
        "known_failures": task_state.known_failures[-5:],
        "observation": {
            "url": observation.url,
            "title": observation.title,
            "text_excerpt": observation.text_excerpt[:3000],
            "candidate_count": len(observation.candidates),
            "dom_candidates": compact_candidates,
        },
    }
    return (
        "基于下面 JSON 和截图，判断下一步最小安全动作。\n"
        "重点回答：上一步结果是什么、当前页面在哪里、下一步 intent 是什么。\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
