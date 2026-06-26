from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


ActionType = Literal[
    "goto",
    "click",
    "double_click",
    "input",
    "upload",
    "wait",
    "scroll",
    "hotkey",
    "assert",
    "finish",
]


@dataclass
class Candidate:
    id: str
    tag: str | None = None
    role: str | None = None
    text: str | None = None
    accessible_name: str | None = None
    selector: str | None = None
    selectors: list[str] = field(default_factory=list)
    rect: dict[str, float] = field(default_factory=dict)
    action_allowed: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PageObservation:
    url: str
    title: str = ""
    text_excerpt: str = ""
    screenshot_path: str | None = None
    candidates: list[Candidate] = field(default_factory=list)


@dataclass
class ActionCall:
    type: ActionType
    reason: str
    target_candidate_id: str | None = None
    selector: str | None = None
    value: str | None = None
    path: str | None = None
    url: str | None = None
    seconds: float | None = None
    direction: str | None = None
    confidence: float = 0.0
    expected_result: str | None = None


@dataclass
class ActionDecision:
    mode: Literal["single", "batch"]
    actions: list[ActionCall]
    risk: Literal["low", "medium", "high"] = "medium"
    reason: str = ""
    why_batch_is_safe: str | None = None
    expected_result: str | None = None
    commit_after: bool = True
    stop_if_any_action_fails: bool = True
    request_debugger: bool = False
    debugger_reason: str | None = None


@dataclass
class TaskState:
    original_user_request: str
    site: dict[str, Any]
    resources: list[dict[str, Any]]
    goal: dict[str, Any]
    milestones: list[dict[str, Any]]
    current_milestone_id: str
    rolling_context: dict[str, Any]
    stable_points: list[dict[str, Any]] = field(default_factory=list)
    recent_trace: list[dict[str, Any]] = field(default_factory=list)
    known_failures: list[dict[str, Any]] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "TaskState":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)
