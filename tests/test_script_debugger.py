from __future__ import annotations

import sys
from pathlib import Path

from app.script.debugger import ScriptDebugger
from app.script.runner import run_script


class FakeScriptDebugClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = 0

    def chat_json(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        return self.response


def test_run_script_captures_success(tmp_path: Path) -> None:
    script = tmp_path / "ok.py"
    script.write_text("print('hello replay')\n", encoding="utf-8")

    result = run_script(script_path=script, output_dir=tmp_path / "run")

    assert result.success is True
    assert result.exit_code == 0
    assert "hello replay" in result.stdout
    assert result.run_json.exists()
    assert (tmp_path / "run" / "script_run.stdout.log").exists()


def test_script_debugger_without_llm_records_failure(tmp_path: Path) -> None:
    script = tmp_path / "bad.py"
    script.write_text(
        "import sys\nprint('[01/01] click: fail')\nprint('boom', file=sys.stderr)\nsys.exit(2)\n",
        encoding="utf-8",
    )

    result = ScriptDebugger(None).debug(
        script_path=script,
        output_dir=tmp_path / "debug",
        max_attempts=2,
        timeout_seconds=30,
    )

    assert result.status == "failed"
    assert result.log_path.exists()
    text = result.log_path.read_text(encoding="utf-8")
    assert "no LLM client configured" in text
    assert "exit=2" in text


def test_script_debugger_can_patch_mechanical_script_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import sys\nprint('broken')\nsys.exit(2)\n", encoding="utf-8")
    client = FakeScriptDebugClient(
        {
            "diagnosis": {
                "category": "mechanical_script_error",
                "strategy": "patch_script",
                "reason": "test patch",
                "patched_script": "print('fixed')\n",
            }
        }
    )

    result = ScriptDebugger(client).debug(
        script_path=bad,
        output_dir=tmp_path / "debug_patch",
        max_attempts=2,
        timeout_seconds=30,
    )

    assert result.status == "success"
    assert client.calls == 1
    assert (tmp_path / "debug_patch" / "patched_script_01.py").exists()
    assert (tmp_path / "debug_patch" / "patch_01.diff").exists()


def test_script_debugger_hands_flow_state_error_to_flow_debugger(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import sys\nprint('upload did not reach timeline')\nsys.exit(2)\n", encoding="utf-8")
    client = FakeScriptDebugClient(
        {
            "diagnosis": {
                "category": "missing_prerequisite_state",
                "strategy": "handoff_to_flow_debugger",
                "root_cause_step": "upload",
                "symptom_step": "click Speed",
                "reason": "File input accepted a path, but the editor still shows the upload entry.",
            }
        }
    )

    result = ScriptDebugger(client).debug(
        script_path=bad,
        output_dir=tmp_path / "debug_handoff",
        max_attempts=2,
        timeout_seconds=30,
    )

    assert result.status == "handoff_to_flow_debugger"
    assert result.handoff_path is not None
    assert result.handoff_path.exists()
    assert not (tmp_path / "debug_handoff" / "patched_script_01.py").exists()
    handoff = result.handoff_path.read_text(encoding="utf-8")
    assert "missing_prerequisite_state" in handoff
    assert "click Speed" in handoff
