from __future__ import annotations

from app.core.schema import ActionCall, PageObservation
from app.runtime.base import BrowserRuntime


class UnsafeActionError(RuntimeError):
    pass


class ResilienceExecutor:
    """Validate and execute one tool call.

    First version is intentionally small: validate action shape, prefer
    candidate_id/selector for UI actions, execute through runtime, return a
    structured result for trace/debugger.
    """

    def __init__(self, runtime: BrowserRuntime) -> None:
        self.runtime = runtime

    def run(self, action: ActionCall, observation: PageObservation) -> dict:
        self._validate(action, observation)
        before_url = observation.url
        result = self.runtime.execute(action)
        after = self.runtime.observe()
        return {
            "status": result.get("status", "executed"),
            "before_url": before_url,
            "after_url": after.url,
            "action": action.__dict__,
            "runtime_result": result,
        }

    def _validate(self, action: ActionCall, observation: PageObservation) -> None:
        if action.type in {"click", "double_click", "input", "upload"} and not (action.target_candidate_id or action.selector):
            raise UnsafeActionError(f"{action.type} requires target_candidate_id or selector")
        if action.target_candidate_id:
            ids = {c.id for c in observation.candidates}
            if ids and action.target_candidate_id not in ids:
                raise UnsafeActionError(f"Unknown target_candidate_id: {action.target_candidate_id}")
        if action.type in {"click", "double_click", "input", "upload"} and action.selector:
            self._validate_selector_source(action, observation)
        if action.type == "goto" and not action.url:
            raise UnsafeActionError("goto requires url")
        if action.type == "upload" and not action.path:
            raise UnsafeActionError("upload requires path")

    def _validate_selector_source(self, action: ActionCall, observation: PageObservation) -> None:
        if _looks_like_generated_selector(action.selector or ""):
            raise UnsafeActionError(f"Unsupported or generated selector syntax: {action.selector}")
        candidate = None
        if action.target_candidate_id:
            candidate = next((item for item in observation.candidates if item.id == action.target_candidate_id), None)
        if candidate is not None:
            allowed = set(candidate.selectors or [])
            if candidate.selector:
                allowed.add(candidate.selector)
            if action.selector in allowed:
                return
            raise UnsafeActionError(
                f"Selector was not copied from target candidate {candidate.id}: {action.selector}"
            )
        all_candidate_selectors = set()
        for item in observation.candidates:
            all_candidate_selectors.update(item.selectors or [])
            if item.selector:
                all_candidate_selectors.add(item.selector)
        if all_candidate_selectors and action.selector not in all_candidate_selectors:
            raise UnsafeActionError(
                f"Selector was not copied from current DOM candidates: {action.selector}"
            )


def _looks_like_generated_selector(selector: str) -> bool:
    lowered = str(selector or "").lower()
    forbidden = [":has-text", ":text(", ">>", "xpath=", "css="]
    if "," in lowered:
        return True
    return any(marker in lowered for marker in forbidden)
