from __future__ import annotations

import json
from pathlib import Path

from app.script.recorder import extract_successful_actions
from app.script.recorder import generate_script_from_trace


def test_extract_successful_actions_uses_runtime_selector_and_drops_candidate_id() -> None:
    trace = [
        {
            "event": "action_executed",
            "status": "clicked",
            "action": {
                "type": "click",
                "reason": "Click Export",
                "target_candidate_id": "cand_15",
                "selector": "css:old",
                "confidence": 0.9,
            },
            "runtime_result": {"status": "clicked", "selector": "css:button.export"},
        },
        {
            "event": "action_executed",
            "status": "action_failed",
            "action": {"type": "click", "reason": "Failed"},
        },
        {
            "event": "action_decided",
            "action": {"type": "click", "reason": "Not executed"},
        },
    ]

    actions, skipped = extract_successful_actions(trace)

    assert skipped == 1
    assert actions == [
        {
            "type": "click",
            "reason": "Click Export",
            "target_candidate_id": None,
            "selector": "css:button.export",
            "selectors": ["css:button.export"],
            "value": None,
            "path": None,
            "url": None,
            "seconds": None,
            "direction": None,
            "confidence": 0.9,
            "expected_result": None,
        }
    ]


def test_extract_successful_batch_actions() -> None:
    trace = [
        {
            "event": "action_executed",
            "status": "batch_executed",
            "mode": "batch",
            "actions": [
                {"type": "click", "reason": "Open panel", "selector": "css:.panel"},
                {"type": "input", "reason": "Set value", "selector": "css:input", "value": "50"},
            ],
            "sub_results": [
                {"status": "clicked", "selector": "css:.panel"},
                {"status": "input", "selector": "css:input", "value": "50"},
            ],
        }
    ]

    actions, skipped = extract_successful_actions(trace)

    assert skipped == 0
    assert [action["type"] for action in actions] == ["click", "input"]
    assert actions[1]["value"] == "50"


def test_generate_script_from_trace_writes_replay_file(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    output_path = tmp_path / "generated_script.py"
    trace_path.write_text(
        json.dumps(
            [
                {
                    "event": "action_executed",
                    "status": "navigated",
                    "action": {"type": "goto", "reason": "Open", "url": "https://example.com"},
                    "runtime_result": {"status": "navigated", "url": "https://example.com"},
                },
                {
                    "event": "action_executed",
                    "status": "finished",
                    "action": {"type": "finish", "reason": "Done"},
                },
            ]
        ),
        encoding="utf-8",
    )

    result = generate_script_from_trace(trace_path=trace_path, output_path=output_path)

    assert result.action_count == 1
    assert result.skipped_count == 1
    content = output_path.read_text(encoding="utf-8")
    assert "REPLAY_ACTIONS" in content
    assert "https://example.com" in content
    assert "from DrissionPage import ChromiumOptions, ChromiumPage" in content
    assert "from app." not in content
    assert "execute_with_retry" in content
    assert "REPLAY_ACTION_RETRIES" in content


def test_generated_upload_script_has_semantic_fallbacks(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    output_path = tmp_path / "generated_script.py"
    trace_path.write_text(
        json.dumps(
            [
                {
                    "event": "action_executed",
                    "status": "uploaded",
                    "action": {
                        "type": "upload",
                        "reason": "Upload video",
                        "path": "/tmp/sample.mp4",
                        "selector_candidates": [
                            "css:div.upload-wrapper.small > div:nth-of-type(1) input"
                        ],
                    },
                    "runtime_result": {
                        "status": "uploaded",
                        "selector": "css:div.upload-wrapper.small > div:nth-of-type(1) input",
                        "path": "/tmp/sample.mp4",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    result = generate_script_from_trace(trace_path=trace_path, output_path=output_path)

    assert result.action_count == 1
    content = output_path.read_text(encoding="utf-8")
    assert "def find_upload_element" in content
    assert 'css:input[type="file"]' in content
    assert "semantic:input[type=file]" in content
    assert "text=Add files" in content
    assert "recorded_error" in content
