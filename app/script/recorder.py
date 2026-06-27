from __future__ import annotations

import json
import pprint
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUCCESS_STATUSES = {
    "navigated",
    "clicked",
    "input",
    "uploaded",
    "waited",
    "scrolled",
    "hotkey",
    "batch_executed",
}

SKIPPED_ACTION_TYPES = {"finish", "assert"}


@dataclass(frozen=True)
class GeneratedScriptResult:
    path: Path
    action_count: int
    skipped_count: int


def generate_script_from_trace(
    *,
    trace_path: Path,
    output_path: Path,
    replay_output_dir: Path | None = None,
) -> GeneratedScriptResult:
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    actions, skipped_count = extract_successful_actions(trace)
    script = render_drission_replay_script(
        actions=actions,
        source_trace=trace_path,
        replay_output_dir=replay_output_dir or output_path.parent / "replay_run",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(script, encoding="utf-8")
    return GeneratedScriptResult(
        path=output_path,
        action_count=len(actions),
        skipped_count=skipped_count,
    )


def extract_successful_actions(trace: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    actions: list[dict[str, Any]] = []
    skipped_count = 0
    for event in trace:
        if event.get("event") != "action_executed":
            continue
        status = str(event.get("status") or "")
        if status not in SUCCESS_STATUSES:
            skipped_count += 1
            continue
        if event.get("mode") == "batch" and event.get("actions"):
            batch_actions, batch_skipped = _extract_batch_actions(event)
            actions.extend(batch_actions)
            skipped_count += batch_skipped
            continue
        action = _normalize_action(event.get("action") or {}, event.get("runtime_result") or {})
        if action is None:
            skipped_count += 1
            continue
        actions.append(action)
    return actions, skipped_count


def render_drission_replay_script(
    *,
    actions: list[dict[str, Any]],
    source_trace: Path,
    replay_output_dir: Path,
) -> str:
    actions_literal = pprint.pformat(actions, width=100, sort_dicts=False)
    source_trace_text = str(source_trace)
    replay_output_dir_text = str(replay_output_dir.resolve())
    template = r'''
from __future__ import annotations

import base64
import mimetypes
import os
import platform
import shutil
from pathlib import Path


from DrissionPage import ChromiumOptions, ChromiumPage


# Generated from: __SOURCE_TRACE__
# This script replays only successful browser actions captured in trace.json.
# It intentionally ignores model reasoning, failed attempts, and state-update events.
# It is intentionally standalone: it does not import this Agent project or call any LLM API.
REPLAY_ACTIONS = __ACTIONS_LITERAL__


SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv("REPLAY_OUTPUT_DIR", "__REPLAY_OUTPUT_DIR__"))
if not OUTPUT_DIR.is_absolute():
    OUTPUT_DIR = SCRIPT_DIR / OUTPUT_DIR
USER_DATA_DIR = Path(os.getenv("BROWSER_USER_DATA_DIR", str(SCRIPT_DIR / ".generated_browser_profile")))
WINDOW_SIZE = os.getenv("BROWSER_WINDOW_SIZE", "1280,850")
SCALE_FACTOR = os.getenv("BROWSER_SCALE_FACTOR", "0.8")
HEADLESS = os.getenv("BROWSER_HEADLESS", "false").lower() in {"1", "true", "yes"}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    page = create_page()
    try:
        for index, raw_action in enumerate(REPLAY_ACTIONS, start=1):
            print(f"[{index:02d}/{len(REPLAY_ACTIONS):02d}] {raw_action['type']}: {raw_action.get('reason', '')}")
            result = execute_with_retry(page, raw_action, index=index)
            print(f"    -> {result}")
            save_screenshot(page, f"step_{index:02d}.png")
        print("Replay finished.")
        save_screenshot(page, "success.png")
    except Exception:
        save_failure_artifacts(page)
        raise
    finally:
        if hasattr(page, "quit"):
            page.quit()
        elif hasattr(page, "close"):
            page.close()


def create_page():
    options = ChromiumOptions()
    if hasattr(options, "headless"):
        options.headless(HEADLESS)
    if hasattr(options, "set_argument"):
        options.set_argument(f"--window-size={WINDOW_SIZE}")
        if SCALE_FACTOR:
            options.set_argument(f"--force-device-scale-factor={SCALE_FACTOR}")
            options.set_argument("--high-dpi-support=1")
        options.set_argument("--enable-webgl")
        options.set_argument("--ignore-gpu-blocklist")
        options.set_argument("--enable-unsafe-swiftshader")
    browser_path = resolve_browser_path()
    if browser_path and hasattr(options, "set_browser_path"):
        options.set_browser_path(str(browser_path))
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if hasattr(options, "set_user_data_path"):
        options.set_user_data_path(str(USER_DATA_DIR))
    return ChromiumPage(options)


def execute_with_retry(page, action: dict, *, index: int) -> dict:
    max_attempts = int(os.getenv("REPLAY_ACTION_RETRIES", "30"))
    wait_seconds = float(os.getenv("REPLAY_RETRY_WAIT_SECONDS", "3"))
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1:
                print(f"    retry {attempt}/{max_attempts} after waiting {wait_seconds:g}s")
            return execute_action(page, action)
        except Exception as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            try:
                save_screenshot(page, f"retry_{index:02d}_{attempt:02d}.png")
            except Exception:
                pass
            try:
                page.wait(wait_seconds)
            except Exception:
                pass
    raise RuntimeError(
        f"Replay action {index} failed after {max_attempts} attempts: "
        f"{action.get('type')} {action.get('selectors') or action.get('selector') or action.get('url') or action.get('path')}; "
        f"last error: {last_error}"
    ) from last_error


def execute_action(page, action: dict) -> dict:
    action_type = action["type"]
    if action_type == "goto":
        url = action.get("url")
        if not url:
            raise ValueError("goto requires url")
        page.get(url)
        try:
            page.wait.doc_loaded(timeout=30)
        except Exception:
            pass
        page.wait(1)
        return {"status": "navigated", "url": url}
    if action_type == "click":
        if action_looks_like(action, ["download", "下载"]):
            selector = click_download_when_ready(page, action)
            return {"status": "clicked", "selector": selector}
        selector = click(page, action)
        if action_looks_like(action, ["continue", "开始导出", "export settings"]):
            wait_after_export_continue(page)
        return {"status": "clicked", "selector": selector}
    if action_type == "double_click":
        selector = click(page, action)
        page.wait(0.1)
        ele, selector = find_element(page, selectors_for(action))
        dispatch_mouse_double_click(ele)
        page.wait(1)
        return {"status": "double_clicked", "selector": selector}
    if action_type == "input":
        selector = input_text(page, action)
        return {"status": "input", "selector": selector, "value": action.get("value") or ""}
    if action_type == "upload":
        selector = upload_file(page, action)
        return {"status": "uploaded", "selector": selector, "path": str(Path(action.get("path") or "").expanduser())}
    if action_type == "wait":
        seconds = float(action.get("seconds") or 1)
        page.wait(seconds)
        return {"status": "waited", "seconds": seconds}
    if action_type == "scroll":
        direction = str(action.get("direction") or "down").lower()
        delta = {"down": 650, "up": -650, "right": 650, "left": -650}.get(direction, 650)
        if direction in {"left", "right"}:
            page.run_js("window.scrollBy(arguments[0], 0)", delta)
        else:
            page.run_js("window.scrollBy(0, arguments[0])", delta)
        page.wait(0.5)
        return {"status": "scrolled", "direction": direction}
    if action_type == "hotkey":
        value = str(action.get("value") or "")
        dispatch_key_chord(page, value)
        return {"status": "hotkey", "value": value}
    raise ValueError(f"Unsupported action type: {action_type}")


def click(page, action: dict) -> str:
    ele, selector = find_element(page, selectors_for(action))
    try:
        ele.click()
    except Exception:
        ele.click(by_js=True)
    page.wait(1)
    return selector


def click_download_when_ready(page, action: dict) -> str:
    """Wait for an export to finish, then click the Download button.

    Some editors do not enter the export-progress state even though the
    recorded Continue click returned successfully. Treat that as a recoverable
    replay condition: if the export settings dialog is still open, click
    Continue again; then wait through exporting/processing until Download is
    visible.
    """
    timeout = float(os.getenv("REPLAY_DOWNLOAD_READY_TIMEOUT", "420"))
    started = __import__("time").perf_counter()
    selectors = selectors_for(action) or ["text=Download", "text=下载"]
    last_error = None
    while __import__("time").perf_counter() - started < timeout:
        if export_settings_modal_visible(page):
            click_export_continue_fallback(page)
            page.wait(2)
            continue
        try:
            ele, selector = find_element(page, selectors, timeout=2)
            try:
                ele.click()
            except Exception:
                ele.click(by_js=True)
            page.wait(2)
            return selector
        except Exception as exc:
            last_error = exc
        text = page_text(page).lower()
        if any(marker in text for marker in ["exporting", "processing", "rendering", "encoding", "uploading", "导出中", "处理中"]):
            page.wait(3)
        else:
            page.wait(2)
    raise RuntimeError(f"Download button did not become available before timeout; last error: {last_error}")


def wait_after_export_continue(page) -> None:
    timeout = float(os.getenv("REPLAY_EXPORT_START_TIMEOUT", "45"))
    started = __import__("time").perf_counter()
    while __import__("time").perf_counter() - started < timeout:
        text = page_text(page).lower()
        if any(marker in text for marker in ["exporting", "processing", "rendering", "encoding", "download", "导出中", "处理中", "下载"]):
            return
        if not export_settings_modal_visible(page):
            return
        click_export_continue_fallback(page)
        page.wait(2)


def export_settings_modal_visible(page) -> bool:
    text = page_text(page).lower()
    return "export settings" in text and "continue" in text


def click_export_continue_fallback(page) -> bool:
    selectors = [
        "text=Continue",
        "text=继续",
        'css:button.app-button.app-button--variant-export-settings',
        'css:[class*="export-settings" i] button',
        'css:[class*="modal" i] button',
    ]
    for selector in selectors:
        try:
            ele, _ = find_element(page, [selector], timeout=1)
            try:
                ele.click()
            except Exception:
                ele.click(by_js=True)
            return True
        except Exception:
            continue
    return False


def action_looks_like(action: dict, keywords: list[str]) -> bool:
    text = " ".join(
        str(action.get(key) or "")
        for key in ["reason", "expected_result", "selector", "value"]
    )
    text += " " + " ".join(str(value or "") for value in action.get("selectors") or [])
    lowered = text.lower()
    return any(str(keyword).lower() in lowered for keyword in keywords)


def input_text(page, action: dict) -> str:
    ele, selector = find_element(page, selectors_for(action))
    value = action.get("value") or ""
    try:
        if hasattr(ele, "focus"):
            ele.focus()
        if is_contenteditable(ele):
            if not replace_contenteditable_text(ele, value):
                if hasattr(page, "_run_cdp"):
                    replace_focused_text_with_cdp(page, value)
                else:
                    ele.input(value, clear=True)
        elif is_text_form_control(ele):
            if not replace_form_control_text(ele, value):
                ele.input(value, clear=True)
        else:
            if hasattr(page, "_run_cdp"):
                replace_focused_text_with_cdp(page, value)
            else:
                ele.input(value, clear=True)
        ele.run_js(
            """
            this.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: String(arguments[0])}));
            this.dispatchEvent(new Event('change', {bubbles: true}));
            this.blur();
            """,
            value,
        )
    except Exception:
        if hasattr(page, "_run_cdp"):
            replace_focused_text_with_cdp(page, value)
        else:
            raise
    page.wait(0.5)
    return selector


def upload_file(page, action: dict) -> str:
    path = Path(action.get("path") or "").expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Upload file does not exist: {path}")
    before_text = page_text(page)
    ele, selector = find_upload_element(page, action, path)
    element_type = ""
    try:
        element_type = str(ele.attr("type") or "").lower()
    except Exception:
        pass
    install_virtual_file_picker(page, path)
    if element_type == "file":
        ele.input(str(path))
        try:
            ele.run_js(
                """
                this.dispatchEvent(new Event('input', {bubbles: true}));
                this.dispatchEvent(new Event('change', {bubbles: true}));
                """
            )
        except Exception:
            pass
        page.wait(2)
        if not upload_page_changed(page, before_text):
            click_upload_trigger_fallbacks(page, ele, path)
    else:
        page.set.upload_files(str(path))
        try:
            ele.click()
        except Exception:
            ele.click(by_js=True)
    wait_for_upload_ready(page, before_text)
    return selector


def find_upload_element(page, action: dict, path: Path):
    selectors = upload_selectors_for(action, path)
    try:
        return find_element(page, selectors, require_displayed=False, timeout=2)
    except Exception as recorded_error:
        input_result = find_file_input_by_semantics(page, path)
        if input_result is not None:
            return input_result
        trigger_result = find_upload_trigger_by_semantics(page)
        if trigger_result is not None:
            return trigger_result
        raise recorded_error


def upload_selectors_for(action: dict, path: Path) -> list[str]:
    selectors = []
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"mp4", "mov", "webm", "avi", "mkv", "m4v"}:
        selectors.extend(
            [
                'css:input[type="file"][accept*="video"]',
                'css:input[type="file"][accept*="mp4"]',
            ]
        )
    elif suffix in {"jpg", "jpeg", "png", "gif", "webp"}:
        selectors.extend(
            [
                'css:input[type="file"][accept*="image"]',
                f'css:input[type="file"][accept*="{css_attr_escape(suffix)}"]',
            ]
        )
    selectors.extend(
        [
            'css:input[type="file"]',
            'css:[data-testid*="upload" i] input[type="file"]',
            'css:[data-test*="upload" i] input[type="file"]',
            'css:[class*="upload" i] input[type="file"]',
            'css:[class*="file" i] input[type="file"]',
        ]
    )
    selectors.extend(selectors_for(action))
    return dedupe_selectors(selectors)


def find_file_input_by_semantics(page, path: Path):
    inputs = elements_for_selector(page, 'css:input[type="file"]')
    if not inputs:
        return None
    ranked = []
    suffix = path.suffix.lower().lstrip(".")
    for index, ele in enumerate(inputs):
        score = 0
        attrs = element_attr_text(ele, ["accept", "name", "id", "class", "aria-label", "title"]).lower()
        context = element_context_text(ele).lower()
        combined = f"{attrs} {context}"
        if "video" in attrs:
            score += 8
        if suffix and suffix in attrs:
            score += 6
        if "image" in attrs and suffix in {"jpg", "jpeg", "png", "gif", "webp"}:
            score += 6
        if any(marker in combined for marker in ["upload", "add file", "add files", "choose file", "browse", "drag and drop", "drop files"]):
            score += 5
        if any(marker in attrs for marker in ["file", "media", "asset"]):
            score += 2
        ranked.append((score, -index, ele))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    best_score, _, best = ranked[0]
    if best_score <= 0 and len(ranked) > 1:
        return None
    return best, "semantic:input[type=file]"


def find_upload_trigger_by_semantics(page):
    selectors = [
        "text=Add files",
        "text=Add file",
        "text=Upload",
        "text=Click to upload",
        "text=Choose file",
        "text=Browse",
        'css:[role="button"][aria-label*="upload" i]',
        'css:button[aria-label*="upload" i]',
        'css:[class*="upload" i]',
        'css:[class*="add-file" i]',
        'css:[class*="dropzone" i]',
    ]
    for selector in selectors:
        runtime_selector = normalize_selector(selector)
        try:
            ele = page.ele(runtime_selector, timeout=1)
            if ele:
                return ele, f"semantic:{selector}"
        except Exception:
            continue
    return None


def elements_for_selector(page, selector: str):
    try:
        return list(page.eles(normalize_selector(selector), timeout=1) or [])
    except TypeError:
        try:
            return list(page.eles(normalize_selector(selector)) or [])
        except Exception:
            return []
    except Exception:
        return []


def element_attr_text(ele, names: list[str]) -> str:
    values = []
    for name in names:
        try:
            value = ele.attr(name)
        except Exception:
            value = None
        if value:
            values.append(str(value))
    return " ".join(values)


def element_context_text(ele) -> str:
    try:
        return str(
            ele.run_js(
                """
                const node = this;
                const parts = [];
                const collect = (item) => {
                  if (!item) return;
                  const text = (item.innerText || item.textContent || '').trim();
                  if (text) parts.push(text.slice(0, 300));
                };
                collect(node);
                collect(node.closest('label'));
                collect(node.parentElement);
                collect(node.closest('[role="button"], button, label, [class*="upload" i], [class*="file" i], [class*="drop" i]'));
                return parts.join(' ');
                """
            )
            or ""
        )
    except Exception:
        return ""


def page_text(page) -> str:
    try:
        return str(page.run_js("return document.body ? document.body.innerText : ''") or "")
    except Exception:
        return ""


def upload_page_changed(page, before_text: str) -> bool:
    current = page_text(page)
    if not current:
        return False
    return current != before_text


def wait_for_upload_ready(page, before_text: str) -> None:
    timeout = float(os.getenv("REPLAY_UPLOAD_READY_TIMEOUT", "60"))
    started = __import__("time").perf_counter()
    last_text = before_text
    while __import__("time").perf_counter() - started < timeout:
        page.wait(1)
        text = page_text(page)
        lowered = text.lower()
        if text and text != before_text:
            # While progress/processing text is visible, keep waiting. Once the
            # page has changed and no generic busy marker is present, let the
            # next replay action verify its own target.
            if any(marker in lowered for marker in ["uploading", "processing", "encoding", "rendering"]):
                last_text = text
                continue
            return
        last_text = text or last_text
    # Do not fail here: the next concrete action will produce a better error
    # and screenshot. This wait is a readiness helper, not a task assertion.


def click_upload_trigger_fallbacks(page, file_input, path: Path) -> None:
    selectors = []
    try:
        input_id = file_input.attr("id")
    except Exception:
        input_id = None
    if input_id:
        selectors.append(f'css:label[for="{css_attr_escape(input_id)}"]')
    selectors.extend(
        [
            "text=Upload",
            "text=Click to upload",
            "text=Add files",
            "text=Add file",
            "text=Choose file",
            "text=Browse",
        ]
    )
    try:
        page.set.upload_files(str(path))
    except Exception:
        pass
    for selector in selectors:
        try:
            ele = page.ele(normalize_selector(selector), timeout=1)
            if not ele:
                continue
            try:
                ele.click()
            except Exception:
                ele.click(by_js=True)
            page.wait(2)
            return
        except Exception:
            continue


def css_attr_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def dedupe_selectors(selectors: list[str]) -> list[str]:
    result = []
    seen = set()
    for selector in selectors:
        clean = str(selector or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def find_element(page, selectors: list[str], *, require_displayed: bool = True, timeout: float = 4):
    if not selectors:
        raise RuntimeError("No selectors supplied")
    errors = []
    for selector in selectors:
        runtime_selector = normalize_selector(selector)
        try:
            if require_displayed:
                ok = page.wait.ele_displayed(runtime_selector, timeout=timeout, raise_err=False)
                if not ok:
                    errors.append(f"{selector}: not displayed")
                    continue
            ele = page.ele(runtime_selector, timeout=timeout)
            if ele:
                return ele, selector
        except Exception as exc:
            errors.append(f"{selector}: {type(exc).__name__}: {exc}")
    for selector in selectors:
        text = text_from_selector(selector)
        if not text:
            continue
        fallback = find_visible_element_by_text(page, text)
        if fallback is not None:
            ele, fallback_selector = fallback
            return ele, fallback_selector
    raise RuntimeError("Element lookup failed: " + " | ".join(errors))


def selectors_for(action: dict) -> list[str]:
    raw = list(action.get("selectors") or [])
    if action.get("selector"):
        raw.append(action["selector"])
    result = []
    seen = set()
    for selector in raw:
        selector = str(selector or "").strip()
        if not selector or selector in seen:
            continue
        seen.add(selector)
        result.append(selector)
    return result


def normalize_selector(selector: str) -> str:
    clean = selector.strip()
    if clean.startswith("text="):
        return "text:" + clean[len("text="):]
    return clean


def text_from_selector(selector: str) -> str | None:
    clean = selector.strip()
    if clean.startswith("text="):
        return clean[len("text="):].strip()
    if clean.startswith("text:"):
        return clean[len("text:"):].strip()
    return None


def find_visible_element_by_text(page, text: str):
    wanted = " ".join(str(text or "").split()).strip()
    if not wanted:
        return None
    token = f"replay_text_fallback_{int(__import__('time').time() * 1000)}"
    ok = False
    try:
        ok = bool(
            page.run_js(
                """
                const wanted = String(arguments[0] || '').trim().toLowerCase().replace(/\s+/g, ' ');
                const token = String(arguments[1]);
                const visible = (el) => {
                  const style = getComputedStyle(el);
                  if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                };
                const label = (el) => [
                  el.innerText,
                  el.textContent,
                  el.getAttribute('aria-label'),
                  el.getAttribute('title'),
                  el.getAttribute('placeholder'),
                  el.value,
                ].filter(Boolean).join(' ').trim().toLowerCase().replace(/\s+/g, ' ');
                const actionableAncestor = (el) => el.closest('button, a, [role="button"], label, input, textarea, select, [tabindex], [class*="button" i], [class*="btn" i], [class*="item" i], [class*="tab" i], [class*="collapse" i]');
                const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], label, input, textarea, select, [tabindex], div, span'));
                const scored = [];
                for (const el of candidates) {
                  if (!visible(el)) continue;
                  const text = label(el);
                  if (!text) continue;
                  const exact = text === wanted;
                  const contains = !exact && text.includes(wanted);
                  if (!exact && !contains) continue;
                  const target = actionableAncestor(el) || el;
                  if (!visible(target)) continue;
                  const rect = target.getBoundingClientRect();
                  const area = rect.width * rect.height;
                  scored.push({target, score: (exact ? 1000 : 500) - Math.min(area / 1000, 250)});
                }
                scored.sort((a, b) => b.score - a.score);
                if (!scored.length) return false;
                scored[0].target.setAttribute('data-replay-text-fallback', token);
                return true;
                """,
                wanted,
                token,
            )
        )
    except Exception:
        ok = False
    if not ok:
        return None
    selector = f'css:[data-replay-text-fallback="{css_attr_escape(token)}"]'
    try:
        ele = page.ele(selector, timeout=1)
        if ele:
            return ele, f"semantic:text={wanted}"
    except Exception:
        return None
    return None


def is_contenteditable(ele) -> bool:
    try:
        return str(ele.attr("contenteditable") or "").lower() == "true"
    except Exception:
        return False


def replace_contenteditable_text(ele, value: str) -> bool:
    try:
        return bool(
            ele.run_js(
                """
                this.focus();
                const value = String(arguments[0]);
                const selection = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(this);
                selection.removeAllRanges();
                selection.addRange(range);
                let ok = false;
                try {
                  ok = document.execCommand('insertText', false, value);
                } catch (e) {
                  ok = false;
                }
                if (!ok) {
                  this.textContent = value;
                }
                this.dispatchEvent(new InputEvent('input', {
                  bubbles: true,
                  inputType: 'insertText',
                  data: value
                }));
                this.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
                """,
                value,
            )
        )
    except Exception:
        return False


def replace_form_control_text(ele, value: str) -> bool:
    try:
        return bool(
            ele.run_js(
                """
                this.focus();
                const value = String(arguments[0]);
                const proto = this instanceof HTMLTextAreaElement
                  ? HTMLTextAreaElement.prototype
                  : HTMLInputElement.prototype;
                const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
                if (descriptor && descriptor.set) {
                  descriptor.set.call(this, '');
                  this.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    inputType: 'deleteContentBackward',
                    data: null
                  }));
                  descriptor.set.call(this, value);
                } else {
                  this.value = value;
                }
                this.dispatchEvent(new InputEvent('input', {
                  bubbles: true,
                  inputType: 'insertText',
                  data: value
                }));
                this.dispatchEvent(new Event('change', {bubbles: true}));
                this.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'Enter'}));
                return this.value === value;
                """,
                value,
            )
        )
    except Exception:
        return False


def is_text_form_control(ele) -> bool:
    try:
        tag = str(ele.tag or "").lower()
    except Exception:
        tag = ""
    if tag == "textarea":
        return True
    if tag != "input":
        return False
    try:
        input_type = str(ele.attr("type") or "text").lower()
    except Exception:
        input_type = "text"
    return input_type not in {
        "button",
        "submit",
        "reset",
        "checkbox",
        "radio",
        "file",
        "image",
        "range",
        "color",
    }


def replace_focused_text_with_cdp(page, value: str) -> None:
    dispatch_key_chord(page, "CTRL+A")
    try:
        page._run_cdp("Input.dispatchKeyEvent", type="keyDown", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
        page._run_cdp("Input.dispatchKeyEvent", type="keyUp", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
    except Exception:
        pass
    page._run_cdp("Input.insertText", text=value)


def dispatch_mouse_double_click(ele) -> None:
    ele.click()
    ele.run_js(
        """
        const rect = this.getBoundingClientRect();
        this.dispatchEvent(new MouseEvent('dblclick', {
          bubbles: true,
          cancelable: true,
          view: window,
          detail: 2,
          clientX: rect.left + rect.width / 2,
          clientY: rect.top + rect.height / 2,
        }));
        """
    )


def dispatch_key_chord(page, value: str) -> None:
    normalized = value.strip().upper().replace("CONTROL+", "CTRL+")
    if "+" not in normalized:
        page.actions.key_down(normalized).key_up(normalized)
        return
    modifier, key = normalized.split("+", 1)
    if hasattr(page, "_run_cdp"):
        modifiers = 2 if modifier == "CTRL" else 4 if modifier in {"META", "CMD", "COMMAND"} else 0
        page._run_cdp("Input.dispatchKeyEvent", type="rawKeyDown", key=key.lower(), code=f"Key{key}", modifiers=modifiers)
        page._run_cdp("Input.dispatchKeyEvent", type="keyUp", key=key.lower(), code=f"Key{key}", modifiers=modifiers)
    else:
        page.actions.key_down(modifier).key_down(key).key_up(key).key_up(modifier)


def install_virtual_file_picker(page, path: Path) -> None:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    page.run_js(
        """
        const encoded = arguments[0];
        const fileName = arguments[1];
        const mimeType = arguments[2];
        const binary = atob(encoded);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
        const file = new File([bytes], fileName, {type: mimeType, lastModified: Date.now()});
        const handle = {
          kind: 'file',
          name: fileName,
          getFile: async () => file,
          isSameEntry: async (other) => Boolean(other && other.name === fileName),
          queryPermission: async () => 'granted',
          requestPermission: async () => 'granted',
        };
        globalThis.showOpenFilePicker = async () => [handle];
        return {name: file.name, size: file.size, type: file.type};
        """,
        encoded,
        path.name,
        mime_type,
    )


def save_screenshot(page, name: str) -> None:
    screenshots = OUTPUT_DIR / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    try:
        page.get_screenshot(path=str(screenshots / name))
    except TypeError:
        page.get_screenshot(str(screenshots / name))


def save_failure_artifacts(page) -> None:
    save_screenshot(page, "failure.png")
    try:
        (OUTPUT_DIR / "failure.html").write_text(str(page.html or ""), encoding="utf-8")
    except Exception:
        pass
    try:
        state = {
            "url": getattr(page, "url", ""),
            "title": getattr(page, "title", ""),
        }
        (OUTPUT_DIR / "failure_state.json").write_text(str(state), encoding="utf-8")
    except Exception:
        pass


def resolve_browser_path() -> Path | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    for candidate in [
        Path.home() / ".local/bin/google-chrome",
        Path.home() / ".local/opt/google-chrome-deb/opt/google/chrome/google-chrome",
    ]:
        if candidate.exists():
            return candidate.resolve()
    if platform.system() == "Windows":
        for candidate in [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ]:
            if candidate.exists():
                return candidate
    return None


if __name__ == "__main__":
    main()
'''
    return (
        template.replace("__SOURCE_TRACE__", source_trace_text)
        .replace("__ACTIONS_LITERAL__", actions_literal)
        .replace("__REPLAY_OUTPUT_DIR__", replay_output_dir_text)
    )


def _extract_batch_actions(event: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    raw_actions = list(event.get("actions") or [])
    sub_results = list(event.get("sub_results") or [])
    actions: list[dict[str, Any]] = []
    skipped_count = 0
    for raw_action, result in zip(raw_actions, sub_results):
        if str(result.get("status") or "") not in SUCCESS_STATUSES:
            skipped_count += 1
            continue
        action = _normalize_action(raw_action, result)
        if action is None:
            skipped_count += 1
            continue
        actions.append(action)
    if len(sub_results) < len(raw_actions):
        skipped_count += len(raw_actions) - len(sub_results)
    return actions, skipped_count


def _normalize_action(raw_action: dict[str, Any], runtime_result: dict[str, Any]) -> dict[str, Any] | None:
    action_type = str(raw_action.get("type") or "")
    if not action_type or action_type in SKIPPED_ACTION_TYPES:
        return None
    selector = runtime_result.get("selector") or raw_action.get("selector")
    selectors = _selector_candidates(raw_action, runtime_result, selector)
    normalized = {
        "type": action_type,
        "reason": str(raw_action.get("reason") or f"Replay {action_type}"),
        # Candidate ids are intentionally dropped: they are observation-local
        # and may point to a different element during replay.
        "target_candidate_id": None,
        "selector": selector,
        "selectors": selectors,
        "value": raw_action.get("value"),
        "path": raw_action.get("path") or runtime_result.get("path"),
        "url": runtime_result.get("url") or raw_action.get("url"),
        "seconds": raw_action.get("seconds"),
        "direction": raw_action.get("direction"),
        "confidence": float(raw_action.get("confidence") or 0.0),
        "expected_result": raw_action.get("expected_result"),
    }
    if action_type in {"click", "double_click", "input", "upload"} and not (
        normalized["selector"] or normalized["selectors"]
    ):
        return None
    if action_type == "goto" and not normalized["url"]:
        return None
    if action_type == "upload" and not normalized["path"]:
        return None
    if action_type == "wait" and normalized["seconds"] is None:
        normalized["seconds"] = 1
    return normalized


def _selector_candidates(
    raw_action: dict[str, Any],
    runtime_result: dict[str, Any],
    selector: str | None,
) -> list[str]:
    raw_values = []
    raw_values.extend(raw_action.get("selector_candidates") or [])
    raw_values.extend(raw_action.get("selectors") or [])
    raw_values.extend(runtime_result.get("selector_candidates") or [])
    if selector:
        raw_values.append(selector)
    result = []
    seen = set()
    for value in raw_values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
