from __future__ import annotations

import base64
import mimetypes
import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any

from app.config import BrowserConfig
from app.core.schema import ActionCall, Candidate, PageObservation


DEFAULT_WINDOW_SIZE = "1280,850"
DEFAULT_SCALE_FACTOR = "0.8"


class DrissionRuntime:
    def __init__(self, *, browser: BrowserConfig, output_dir: Path) -> None:
        self.browser = browser
        self.output_dir = output_dir
        self.page: Any | None = None
        self._last_candidates: dict[str, Candidate] = {}

    def start(self) -> None:
        if self.page is not None:
            return
        try:
            from DrissionPage import ChromiumOptions, ChromiumPage
        except ModuleNotFoundError as exc:
            raise RuntimeError("DrissionPage is not installed. Run: pip install -r requirements.txt") from exc

        options = ChromiumOptions()
        if hasattr(options, "headless"):
            options.headless(self.browser.headless)
        if hasattr(options, "set_argument"):
            options.set_argument(f"--window-size={os.getenv('BROWSER_WINDOW_SIZE', DEFAULT_WINDOW_SIZE)}")
            scale_factor = os.getenv("BROWSER_SCALE_FACTOR", DEFAULT_SCALE_FACTOR).strip()
            if scale_factor:
                options.set_argument(f"--force-device-scale-factor={scale_factor}")
                options.set_argument("--high-dpi-support=1")
            options.set_argument("--enable-webgl")
            options.set_argument("--ignore-gpu-blocklist")
            options.set_argument("--enable-unsafe-swiftshader")
        browser_path = _resolve_browser_path()
        if browser_path and hasattr(options, "set_browser_path"):
            options.set_browser_path(str(browser_path))
        self.browser.user_data_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(options, "set_user_data_path"):
            options.set_user_data_path(str(self.browser.user_data_dir))
        self.page = ChromiumPage(options)

    def observe(self) -> PageObservation:
        page = self._require_page()
        screenshot = self.output_dir / "screenshots" / "current.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        try:
            page.get_screenshot(path=str(screenshot))
        except TypeError:
            page.get_screenshot(str(screenshot))

        state = self._state()
        candidates = self._candidates()
        self._last_candidates = {candidate.id: candidate for candidate in candidates}
        return PageObservation(
            url=state["url"],
            title=state["title"],
            text_excerpt=state["text_excerpt"],
            screenshot_path=str(screenshot),
            candidates=candidates,
        )

    def execute(self, action: ActionCall) -> dict:
        self.start()
        if action.type == "goto":
            return self._goto(action.url or "")
        if action.type == "click":
            return self._click(action)
        if action.type == "double_click":
            result = self._click(action)
            page = self._require_page()
            page.wait(0.1)
            result2 = self._click(action, double=True)
            result["double_click"] = result2
            return result
        if action.type == "input":
            return self._input(action)
        if action.type == "upload":
            return self._upload(action)
        if action.type == "wait":
            seconds = float(action.seconds or 1)
            self._require_page().wait(seconds)
            return {"status": "waited", "seconds": seconds}
        if action.type == "scroll":
            return self._scroll(action)
        if action.type == "hotkey":
            return self._hotkey(action.value or "")
        if action.type == "assert":
            return {"status": "assertion_requested", "expected_result": action.expected_result}
        if action.type == "finish":
            return {"status": "finished", "reason": action.reason}
        raise ValueError(f"Unsupported action type: {action.type}")

    def close(self) -> None:
        if self.page is None:
            return
        if hasattr(self.page, "quit"):
            self.page.quit()
        elif hasattr(self.page, "close"):
            self.page.close()
        self.page = None

    def _goto(self, url: str) -> dict:
        if not url:
            raise ValueError("goto requires url")
        page = self._require_page()
        page.get(url)
        page.wait.doc_loaded(timeout=30)
        page.wait(1)
        return {"status": "navigated", "url": url}

    def _click(self, action: ActionCall, *, double: bool = False) -> dict:
        ele, selector = self._find_element(action)
        if double:
            _dispatch_mouse_double_click(ele)
        else:
            try:
                ele.click()
            except Exception:
                ele.click(by_js=True)
        self._require_page().wait(1)
        return {"status": "clicked", "selector": selector}

    def _input(self, action: ActionCall) -> dict:
        ele, selector = self._find_element(action)
        value = action.value or ""
        try:
            if hasattr(ele, "focus"):
                ele.focus()
            if _is_contenteditable(ele):
                page = self._require_page()
                if hasattr(page, "_run_cdp"):
                    _dispatch_key_chord(page, "CTRL", "A")
                    page._run_cdp("Input.insertText", text=value)
                else:
                    ele.input(value, clear=True)
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
            page = self._require_page()
            if hasattr(page, "_run_cdp"):
                _dispatch_key_chord(page, "CTRL", "A")
                page._run_cdp("Input.insertText", text=value)
            else:
                raise
        self._require_page().wait(0.5)
        return {"status": "input", "selector": selector, "value": value}

    def _upload(self, action: ActionCall) -> dict:
        path = Path(action.path or "").expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Upload file does not exist: {path}")
        ele, selector = self._find_element(action, require_displayed=False)
        element_type = ""
        try:
            element_type = str(ele.attr("type") or "").lower()
        except Exception:
            pass
        if element_type == "file":
            ele.input(str(path))
        else:
            page = self._require_page()
            _install_virtual_file_picker(page, path)
            page.set.upload_files(str(path))
            try:
                ele.click()
            except Exception:
                ele.click(by_js=True)
            page.wait(1)
        return {"status": "uploaded", "selector": selector, "path": str(path)}

    def _scroll(self, action: ActionCall) -> dict:
        direction = (action.direction or "down").lower()
        delta = {"down": 650, "up": -650, "right": 650, "left": -650}.get(direction, 650)
        if direction in {"left", "right"}:
            self._require_page().run_js("window.scrollBy(arguments[0], 0)", delta)
        else:
            self._require_page().run_js("window.scrollBy(0, arguments[0])", delta)
        self._require_page().wait(0.5)
        return {"status": "scrolled", "direction": direction}

    def _hotkey(self, value: str) -> dict:
        page = self._require_page()
        normalized = value.strip().upper()
        if "+" in normalized and hasattr(page, "_run_cdp"):
            modifier, key = normalized.replace("CONTROL+", "CTRL+").split("+", 1)
            _dispatch_key_chord(page, modifier, key)
        else:
            page.actions.key_down(normalized).key_up(normalized)
        return {"status": "hotkey", "value": value}

    def _find_element(self, action: ActionCall, *, require_displayed: bool = True) -> tuple[Any, str]:
        candidate = self._last_candidates.get(action.target_candidate_id or "")
        selectors = []
        if action.selector:
            selectors.append(action.selector)
        if candidate:
            selectors.extend(candidate.selectors or [])
            if candidate.selector:
                selectors.append(candidate.selector)
        selectors = _dedupe([selector for selector in selectors if selector])
        if not selectors:
            raise RuntimeError(f"No selector for action: {action}")
        page = self._require_page()
        errors = []
        for selector in selectors:
            runtime_selector = _normalize_selector(selector)
            try:
                if require_displayed:
                    ok = page.wait.ele_displayed(runtime_selector, timeout=4, raise_err=False)
                    if not ok:
                        errors.append(f"{selector}: not displayed")
                        continue
                ele = page.ele(runtime_selector, timeout=4)
                if ele:
                    return ele, selector
            except Exception as exc:
                errors.append(f"{selector}: {type(exc).__name__}: {exc}")
        raise RuntimeError("Element lookup failed: " + " | ".join(errors))

    def _state(self) -> dict:
        page = self._require_page()
        text = ""
        html = ""
        for _ in range(3):
            try:
                text = page.run_js("return document.body ? document.body.innerText : ''") or ""
                html = page.html or ""
                break
            except Exception:
                page.wait(0.5)
        return {
            "url": getattr(page, "url", ""),
            "title": getattr(page, "title", ""),
            "text_excerpt": str(text)[:5000],
            "html_excerpt": str(html)[:3000],
        }

    def _candidates(self) -> list[Candidate]:
        page = self._require_page()
        raw_candidates = page.run_js(CANDIDATE_JS) or []
        candidates: list[Candidate] = []
        for index, raw in enumerate(raw_candidates[:250], start=1):
            selectors = _build_selectors(raw)
            if not selectors:
                continue
            action_allowed = _action_allowed(raw)
            candidates.append(
                Candidate(
                    id=f"cand_{index}",
                    tag=raw.get("tag"),
                    role=raw.get("role"),
                    text=_compact(raw.get("text"), 180),
                    accessible_name=_compact(raw.get("accessible_name"), 180),
                    selector=selectors[0],
                    selectors=selectors,
                    rect=raw.get("rect") or {},
                    action_allowed=action_allowed,
                    extra={
                        "type": raw.get("type"),
                        "placeholder": raw.get("placeholder"),
                        "aria_label": raw.get("aria_label"),
                        "label_text": _compact(raw.get("label_text"), 160),
                        "context_text": _compact(raw.get("context_text"), 220),
                        "css_path": raw.get("css_path"),
                        "value": _compact(raw.get("value"), 120),
                        "href": raw.get("href"),
                        "is_visible": raw.get("is_visible"),
                    },
                )
            )
        return candidates

    def _require_page(self) -> Any:
        if self.page is None:
            self.start()
        if self.page is None:
            raise RuntimeError("Failed to start browser")
        return self.page


def _resolve_browser_path() -> Path | None:
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


def _build_selectors(raw: dict) -> list[str]:
    tag = raw.get("tag") or "*"
    result: list[str] = []
    id_value = raw.get("id")
    if id_value:
        result.append(f"css:{tag}#{_css_escape(id_value)}")
        result.append(f"css:#{_css_escape(id_value)}")
    name = raw.get("name")
    if name:
        result.append(f"css:{tag}[name=\"{_attr_escape(name)}\"]")
    aria = raw.get("aria_label")
    if aria:
        result.append(f"css:{tag}[aria-label=\"{_attr_escape(aria)}\"]")
    placeholder = raw.get("placeholder")
    if placeholder:
        result.append(f"css:{tag}[placeholder=\"{_attr_escape(placeholder)}\"]")
    css_path = raw.get("css_path")
    if css_path:
        result.append(f"css:{css_path}")
    text = _compact(raw.get("text"), 80)
    if text and tag in {"button", "a", "label"}:
        result.append(f"text={text}")
    return _dedupe(result)


def _action_allowed(raw: dict) -> list[str]:
    tag = (raw.get("tag") or "").lower()
    type_value = (raw.get("type") or "").lower()
    role = (raw.get("role") or "").lower()
    contenteditable = str(raw.get("contenteditable") or "").lower() == "true"
    if tag == "input" and type_value == "file":
        return ["upload"]
    if contenteditable:
        return ["click", "double_click", "input"]
    if tag in {"input", "textarea"} and type_value not in {"button", "submit", "checkbox", "radio", "file"}:
        return ["click", "input"]
    if tag == "select":
        return ["click"]
    if tag in {"button", "a", "label"} or role in {"button", "link", "tab", "menuitem"} or raw.get("cursor_pointer"):
        return ["click"]
    return ["inspect"]


def _install_virtual_file_picker(page: Any, path: Path) -> None:
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


def _dispatch_mouse_double_click(ele: Any) -> None:
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


def _dispatch_key_chord(page: Any, modifier: str, key: str) -> None:
    modifier = modifier.upper()
    key = key.upper()
    modifiers = 2 if modifier == "CTRL" else 4 if modifier in {"META", "CMD", "COMMAND"} else 0
    page._run_cdp("Input.dispatchKeyEvent", type="rawKeyDown", key=key.lower(), code=f"Key{key}", modifiers=modifiers)
    page._run_cdp("Input.dispatchKeyEvent", type="keyUp", key=key.lower(), code=f"Key{key}", modifiers=modifiers)


def _is_contenteditable(ele: Any) -> bool:
    try:
        return str(ele.attr("contenteditable") or "").lower() == "true"
    except Exception:
        return False


def _normalize_selector(selector: str) -> str:
    clean = selector.strip()
    if clean.startswith("text="):
        return "text:" + clean[len("text=") :]
    return clean


def _compact(value: object, max_length: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_length].strip()


def _dedupe(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _css_escape(value: str) -> str:
    return re.sub(r"([^a-zA-Z0-9_-])", lambda m: "\\" + m.group(1), value)


def _attr_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


CANDIDATE_JS = r"""
return (() => {
  const interactiveTags = new Set(['a', 'button', 'input', 'textarea', 'select', 'option', 'label']);
  const interactiveAttrs = ['role', 'onclick', 'tabindex', 'contenteditable'];
  const skippedTags = new Set(['script', 'style', 'noscript', 'template', 'meta', 'link']);

  function attr(el, name) {
    const value = el.getAttribute(name);
    return value === null ? null : value;
  }
  function textOf(el) {
    const text = el.innerText || el.textContent || '';
    return text.replace(/\s+/g, ' ').trim();
  }
  function compact(value, maxLength = 220) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    return text.length > maxLength ? text.slice(0, maxLength).trim() : text;
  }
  function isVisible(el) {
    if (el.hidden) return false;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && (attr(el, 'type') || '').toLowerCase() === 'hidden') return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    return !!(rect.width || rect.height || el.getClientRects().length);
  }
  function rectOf(el) {
    const rect = el.getBoundingClientRect();
    return {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)};
  }
  function cssPath(el) {
    const parts = [];
    let current = el;
    while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
      const tag = current.tagName.toLowerCase();
      const id = current.getAttribute('id');
      if (id) {
        parts.unshift(`${tag}#${CSS.escape(id)}`);
        break;
      }
      const stableClasses = Array.from(current.classList || [])
        .filter(name => name && !/^(active|selected|open|show|disabled|focus|hover)$/.test(name))
        .slice(0, 2);
      if (stableClasses.length) {
        const classSelector = `${tag}.${stableClasses.map(name => CSS.escape(name)).join('.')}`;
        try {
          if (document.querySelectorAll(classSelector).length === 1) {
            parts.unshift(classSelector);
            break;
          }
        } catch (_) {}
      }
      let index = 1;
      let prev = current.previousElementSibling;
      while (prev) {
        if (prev.tagName.toLowerCase() === tag) index += 1;
        prev = prev.previousElementSibling;
      }
      parts.unshift(`${tag}:nth-of-type(${index})`);
      current = current.parentElement;
    }
    return parts.join(' > ');
  }
  function labelText(el) {
    const id = el.getAttribute('id');
    if (id) {
      const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (label) return textOf(label);
    }
    const closest = el.closest('label');
    return closest ? textOf(closest) : '';
  }
  function accessibleName(el) {
    return attr(el, 'aria-label') || labelText(el) || textOf(el) || attr(el, 'title') || '';
  }
  function contextText(el) {
    const pieces = [];
    let current = el;
    let depth = 0;
    while (current && current !== document.body && depth < 5) {
      const text = compact(textOf(current), 160);
      const aria = attr(current, 'aria-label');
      if (text) pieces.push(text);
      if (aria) pieces.push(aria);
      current = current.parentElement;
      depth += 1;
    }
    return compact(Array.from(new Set(pieces)).join(' | '), 240);
  }
  function isCandidate(el) {
    const tag = el.tagName.toLowerCase();
    if (skippedTags.has(tag)) return false;
    if (interactiveTags.has(tag)) return true;
    if (interactiveAttrs.some(name => el.hasAttribute(name))) return true;
    const style = window.getComputedStyle(el);
    const text = textOf(el);
    if (style.cursor === 'pointer' && text.length <= 180) return true;
    return false;
  }

  return Array.from(document.querySelectorAll('*'))
    .map((el, index) => ({el, index}))
    .filter(({el}) => isCandidate(el))
    .filter(({el}) => isVisible(el) || (el.tagName.toLowerCase() === 'input' && (attr(el, 'type') || '').toLowerCase() === 'file'))
    .map(({el, index}) => {
      const tag = el.tagName.toLowerCase();
      return {
        dom_index: index,
        tag,
        text: compact(textOf(el), 220),
        id: attr(el, 'id'),
        name: attr(el, 'name'),
        type: attr(el, 'type'),
        role: attr(el, 'role'),
        aria_label: attr(el, 'aria-label'),
        placeholder: attr(el, 'placeholder'),
        value: ['input', 'textarea', 'select'].includes(tag) ? String(el.value || '') : attr(el, 'value'),
        href: attr(el, 'href'),
        contenteditable: attr(el, 'contenteditable'),
        tabindex: attr(el, 'tabindex'),
        cursor_pointer: window.getComputedStyle(el).cursor === 'pointer',
        is_visible: isVisible(el),
        rect: rectOf(el),
        css_path: cssPath(el),
        accessible_name: compact(accessibleName(el), 180),
        label_text: compact(labelText(el), 160),
        context_text: contextText(el)
      };
    });
})();
"""
