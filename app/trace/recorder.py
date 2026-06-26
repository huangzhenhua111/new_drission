from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TraceRecorder:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []

    def add(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self.flush()

    def flush(self) -> None:
        (self.output_dir / "trace.json").write_text(
            json.dumps(self.events, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

