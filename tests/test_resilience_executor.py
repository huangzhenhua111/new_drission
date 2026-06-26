import pytest

from app.core.schema import ActionCall, Candidate, PageObservation
from app.resilience.executor import ResilienceExecutor, UnsafeActionError
from app.runtime.static import StaticRuntime


def test_click_requires_target_or_selector() -> None:
    runtime = StaticRuntime("https://example.com")
    executor = ResilienceExecutor(runtime)
    obs = PageObservation(url="https://example.com")
    with pytest.raises(UnsafeActionError):
        executor.run(ActionCall(type="click", reason="test"), obs)


def test_click_known_candidate_passes() -> None:
    runtime = StaticRuntime("https://example.com")
    executor = ResilienceExecutor(runtime)
    obs = PageObservation(
        url="https://example.com",
        candidates=[Candidate(id="cand_1", tag="button", text="Start", action_allowed=["click"])],
    )
    result = executor.run(ActionCall(type="click", reason="test", target_candidate_id="cand_1"), obs)
    assert result["status"] == "dry_run"


def test_selector_must_come_from_current_candidate() -> None:
    runtime = StaticRuntime("https://example.com")
    executor = ResilienceExecutor(runtime)
    obs = PageObservation(
        url="https://example.com",
        candidates=[
            Candidate(
                id="cand_1",
                tag="button",
                text="Export",
                selector="css:button.export",
                selectors=["css:button.export", "text=Export"],
                action_allowed=["click"],
            )
        ],
    )

    result = executor.run(
        ActionCall(type="click", reason="test", selector="css:button.export"),
        obs,
    )
    assert result["status"] == "dry_run"


def test_generated_playwright_selector_is_rejected() -> None:
    runtime = StaticRuntime("https://example.com")
    executor = ResilienceExecutor(runtime)
    obs = PageObservation(
        url="https://example.com",
        candidates=[
            Candidate(
                id="cand_1",
                tag="button",
                text="Export",
                selector="css:button.export",
                selectors=["css:button.export"],
                action_allowed=["click"],
            )
        ],
    )
    with pytest.raises(UnsafeActionError):
        executor.run(
            ActionCall(
                type="click",
                reason="test",
                selector="css:button.ve-btn.btn-yellow:has-text('Export'), .export-modal button",
            ),
            obs,
        )
