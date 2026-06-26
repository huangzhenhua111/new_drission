from __future__ import annotations

from app.core.schema import ActionCall, PageObservation


class StaticRuntime:
    """Dry-run runtime used before the Drission adapter is implemented."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.actions: list[dict] = []

    def observe(self) -> PageObservation:
        return PageObservation(url=self.url, title="Dry Run", text_excerpt="Static dry-run observation.")

    def execute(self, action: ActionCall) -> dict:
        self.actions.append(action.__dict__)
        if action.type == "goto" and action.url:
            self.url = action.url
        return {"status": "dry_run", "action": action.__dict__}

    def close(self) -> None:
        return None

