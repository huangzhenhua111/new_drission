from __future__ import annotations

import difflib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.llm.client import LLMError, OpenAICompatibleClient
from app.script.runner import ScriptRunResult
from app.script.runner import run_script


@dataclass(frozen=True)
class ScriptDebugResult:
    status: str
    final_script: Path
    attempts: int
    log_path: Path
    handoff_path: Path | None = None
    flow_recovery_plan_path: Path | None = None


class ScriptDebugger:
    def __init__(self, client: OpenAICompatibleClient | None = None) -> None:
        self.client = client

    def debug(
        self,
        *,
        script_path: Path,
        output_dir: Path,
        max_attempts: int = 3,
        timeout_seconds: int = 1800,
    ) -> ScriptDebugResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path = output_dir / "debug.log"
        current_script = script_path.resolve()
        log_lines: list[str] = []

        for attempt in range(1, max_attempts + 1):
            attempt_dir = output_dir / f"attempt_{attempt:02d}"
            run = run_script(
                script_path=current_script,
                output_dir=attempt_dir,
                timeout_seconds=timeout_seconds,
                run_name=f"script_run_{attempt:02d}",
            )
            log_lines.append(
                f"[Debug] attempt {attempt}: exit={run.exit_code}, elapsed={run.elapsed_seconds}s, script={current_script}"
            )
            if run.success:
                final_script = output_dir / "script_final.py"
                shutil.copy2(current_script, final_script)
                log_lines.append(f"[Debug] success: final script saved to {final_script}")
                log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                return ScriptDebugResult("success", final_script, attempt, log_path)

            if attempt >= max_attempts:
                break
            if self.client is None:
                log_lines.append("[Debug] no LLM client configured; cannot patch script.")
                break

            try:
                diagnosis = self._diagnose_with_llm(
                    script_path=current_script,
                    run=run,
                    attempt_dir=attempt_dir,
                    attempt=attempt,
                )
            except Exception as exc:
                log_lines.append(f"[Debug] diagnosis failed: {type(exc).__name__}: {exc}")
                break
            strategy = str(diagnosis.get("strategy") or "patch_script")
            if strategy == "handoff_to_flow_debugger":
                handoff_path = self._write_handoff(
                    output_dir=output_dir,
                    attempt=attempt,
                    script_path=current_script,
                    run=run,
                    diagnosis=diagnosis,
                )
                log_lines.append(
                    "[Debug] handoff to flow debugger: "
                    f"{handoff_path} ({diagnosis.get('category') or 'unknown'})"
                )
                log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                return ScriptDebugResult(
                    "handoff_to_flow_debugger",
                    current_script,
                    attempt,
                    log_path,
                    handoff_path=handoff_path,
                )
            if strategy == "stop":
                handoff_path = self._write_handoff(
                    output_dir=output_dir,
                    attempt=attempt,
                    script_path=current_script,
                    run=run,
                    diagnosis=diagnosis,
                )
                log_lines.append(
                    "[Debug] stopped by diagnosis: "
                    f"{handoff_path} ({diagnosis.get('category') or 'unknown'})"
                )
                log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                return ScriptDebugResult(
                    "stopped",
                    current_script,
                    attempt,
                    log_path,
                    handoff_path=handoff_path,
                )

            patched = diagnosis.get("patched_script") or diagnosis.get("script") or diagnosis.get("code")
            if not isinstance(patched, str) or not patched.strip():
                log_lines.append("[Debug] diagnosis selected patch_script but did not return patched_script.")
                break
            patched = _strip_code_fence(patched)
            patched_path = output_dir / f"patched_script_{attempt:02d}.py"
            patched_path.write_text(patched, encoding="utf-8")
            diff = "\n".join(
                difflib.unified_diff(
                    current_script.read_text(encoding="utf-8").splitlines(),
                    patched.splitlines(),
                    fromfile=str(current_script),
                    tofile=str(patched_path),
                    lineterm="",
                )
            )
            (output_dir / f"patch_{attempt:02d}.diff").write_text(diff, encoding="utf-8")
            log_lines.append(f"[Debug] patched attempt {attempt}: {patched_path}")
            current_script = patched_path

        log_lines.append("[Debug] failed: max attempts reached or patch unavailable.")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return ScriptDebugResult("failed", current_script, max_attempts, log_path)

    def _diagnose_with_llm(
        self,
        *,
        script_path: Path,
        run: ScriptRunResult,
        attempt_dir: Path,
        attempt: int,
    ) -> dict:
        assert self.client is not None
        browser_dir = run.output_dir / "browser"
        failure_html = _read_text(browser_dir / "failure.html", limit=12000)
        failure_state = _read_text(browser_dir / "failure_state.json", limit=2000)
        screenshot = browser_dir / "screenshots" / "failure.png"
        images = [screenshot] if screenshot.exists() else []
        payload = {
            "attempt": attempt,
            "script_path": str(script_path),
            "script": script_path.read_text(encoding="utf-8"),
            "run": run.to_dict(),
            "stdout_tail": run.stdout[-8000:],
            "stderr_tail": run.stderr[-12000:],
            "failure_state": failure_state,
            "failure_html_excerpt": failure_html,
            "instruction": (
                "First diagnose whether this is a mechanical standalone-script problem or a flow/state problem. "
                "If it is mechanical, return strategy=patch_script and a complete patched standalone script. "
                "If a prerequisite state is missing, the generated flow was stale/wrong, the page internal state "
                "looks corrupted, or the browser session likely needs refresh/restart, return "
                "strategy=handoff_to_flow_debugger instead of patching around it."
            ),
        }
        context_path = attempt_dir / f"failure_context_{attempt:02d}.json"
        context_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        data = self.client.chat_json(
            system=_SCRIPT_DEBUGGER_SYSTEM_PROMPT,
            user=json.dumps(payload, ensure_ascii=False, indent=2),
            images=images,
            temperature=0,
            timeout=240,
        )
        diagnosis = data.get("diagnosis", data)
        if not isinstance(diagnosis, dict):
            raise LLMError("Script debugger did not return a diagnosis object")
        diagnosis.setdefault("strategy", "patch_script")
        diagnosis.setdefault("category", "mechanical_script_error")
        return diagnosis

    def _write_handoff(
        self,
        *,
        output_dir: Path,
        attempt: int,
        script_path: Path,
        run: ScriptRunResult,
        diagnosis: dict,
    ) -> Path:
        browser_dir = run.output_dir / "browser"
        handoff = {
            "source": "script_debugger",
            "attempt": attempt,
            "category": diagnosis.get("category") or "unknown",
            "strategy": diagnosis.get("strategy") or "handoff_to_flow_debugger",
            "root_cause_step": diagnosis.get("root_cause_step"),
            "symptom_step": diagnosis.get("symptom_step"),
            "reason": diagnosis.get("reason") or diagnosis.get("root_cause") or "",
            "script_path": str(script_path),
            "run": run.to_dict(),
            "stdout_path": str(run.output_dir / f"script_run_{attempt:02d}.stdout.log"),
            "stderr_path": str(run.output_dir / f"script_run_{attempt:02d}.stderr.log"),
            "run_json": str(run.run_json),
            "screenshot_path": str(browser_dir / "screenshots" / "failure.png"),
            "failure_html_path": str(browser_dir / "failure.html"),
            "failure_state_path": str(browser_dir / "failure_state.json"),
            "debugger_notes": diagnosis.get("debugger_notes") or diagnosis.get("notes"),
        }
        path = output_dir / f"script_failure_handoff_{attempt:02d}.json"
        path.write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def _read_text(path: Path, *, limit: int) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def _strip_code_fence(text: str) -> str:
    clean = text.strip()
    match = re.match(r"^```(?:python)?\s*(.*?)\s*```$", clean, flags=re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    return clean + "\n"


_SCRIPT_DEBUGGER_SYSTEM_PROMPT = """你是 DrissionPage 自动化脚本 Debugger。

你的输入包含一个 standalone Python 脚本、运行 stdout/stderr、失败页面 HTML 摘要和可选截图。
你的任务是先诊断，再决定是修脚本，还是把控制权交给流程 Debugger 回到 Generation。

你必须区分两类问题：

A. mechanical_script_error：脚本本身的机械问题，可以局部修复。
例如 selector 失效/非唯一、等待不够、input/change 事件没触发、弹窗按钮 selector 不稳。
这种情况下 strategy = "patch_script"，并返回完整 patched_script。

B. 流程/状态问题：不应该靠脚本硬补。
例如前置状态没完成、上传动作看似执行但素材没有进入工作区/时间线、脚本继续执行时页面仍停在上传入口、
Generation 记录的页面状态和 replay 状态明显不一致、页面内部处理失败且没有明显 Agent 误点、
浏览器会话/站点状态疑似需要刷新或重启。
这种情况下 strategy = "handoff_to_flow_debugger"，不要返回 patched_script。

硬性规则：
1. 只输出 JSON。
2. 如果 strategy = "patch_script"，字段必须包含 patched_script。
3. patched_script 必须是完整 Python 脚本，不是 diff。
4. patched_script 只能依赖 DrissionPage/Python 标准库；禁止调用 LLM/API；禁止 import app.*。
5. 优先做通用修复：增加 selector fallback、增加智能等待/重试、修正明显失效 selector、修正输入事件、等待上传/导出/弹窗状态。
6. 不要改变用户任务目标；不要新增下载/删除/支付等用户没要求的高风险动作。
7. 如果失败根因是 missing_prerequisite_state / page_internal_failure / stale_browser_session / task_unsatisfied_or_ambiguous，
   必须 handoff_to_flow_debugger，不能通过脚本硬绕。

输出格式：
{
  "diagnosis": {
    "category": "mechanical_script_error | missing_prerequisite_state | page_internal_failure | stale_browser_session | task_unsatisfied_or_ambiguous",
    "strategy": "patch_script | handoff_to_flow_debugger | stop",
    "root_cause_step": "可选，最早出错或前置缺失的步骤",
    "symptom_step": "可选，当前报错/暴露症状的步骤",
    "reason": "为什么这样分类",
    "patched_script": "仅 strategy=patch_script 时返回完整 Python 代码"
  }
}
"""
