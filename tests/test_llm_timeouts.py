from pathlib import Path


def test_runtime_llm_timeouts_are_240_seconds() -> None:
    root = Path(__file__).resolve().parents[1]
    assert "timeout=240" in (root / "app/generation/step_decider.py").read_text()
    assert "timeout=240" in (root / "app/generation/state_updater.py").read_text()
    assert "timeout=240" in (root / "app/debugger/rollback.py").read_text()
