from __future__ import annotations

from typing import Protocol

from app.core.schema import ActionCall, PageObservation


class BrowserRuntime(Protocol):
    def observe(self) -> PageObservation:
        ...

    def execute(self, action: ActionCall) -> dict:
        ...

    def close(self) -> None:
        ...

