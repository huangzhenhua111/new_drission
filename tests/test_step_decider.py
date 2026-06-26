import json

from app.core.schema import Candidate, PageObservation
from app.generation.step_decider import _SYSTEM_PROMPT, _build_user_prompt, _parse_decision
from app.generation.task_parser import TaskParser


def test_batch_allowed_only_when_low_risk() -> None:
    decision = _parse_decision(
        {
            "mode": "batch",
            "risk": "low",
            "reason": "same panel edits",
            "actions": [
                {"type": "click", "reason": "open speed", "target_candidate_id": "cand_1"},
                {"type": "click", "reason": "focus input", "target_candidate_id": "cand_2"},
                {"type": "input", "reason": "set speed", "target_candidate_id": "cand_2", "value": "1.5"},
            ],
        }
    )
    assert decision.mode == "batch"
    assert len(decision.actions) == 3


def test_medium_risk_batch_is_forced_to_single() -> None:
    decision = _parse_decision(
        {
            "mode": "batch",
            "risk": "medium",
            "reason": "not sure",
            "actions": [
                {"type": "click", "reason": "open speed", "target_candidate_id": "cand_1"},
                {"type": "input", "reason": "set speed", "target_candidate_id": "cand_2", "value": "1.5"},
            ],
        }
    )
    assert decision.mode == "single"
    assert len(decision.actions) == 1


def test_export_batch_is_forced_to_single_even_if_model_says_low_risk() -> None:
    decision = _parse_decision(
        {
            "mode": "batch",
            "risk": "low",
            "reason": "export then finish",
            "actions": [
                {"type": "click", "reason": "click Export", "target_candidate_id": "cand_1", "expected_result": "导出开始"},
                {"type": "finish", "reason": "done"},
            ],
        }
    )
    assert decision.mode == "single"
    assert len(decision.actions) == 1


def test_prompt_prefers_direct_input_for_visible_numeric_fields() -> None:
    assert "已经存在的 input/textarea/contenteditable" in _SYSTEM_PROMPT
    assert "直接 input 1.5" in _SYSTEM_PROMPT
    assert "直接 input 50" in _SYSTEM_PROMPT
    assert "不要先 click 再 input" in _SYSTEM_PROMPT


def test_prompt_allows_generation_to_request_debugger_after_reviewing_risk_signal() -> None:
    assert "risk_signal" in _SYSTEM_PROMPT
    assert "request_debugger=true" in _SYSTEM_PROMPT
    assert "不是最终裁决" in _SYSTEM_PROMPT
    assert "同一个高风险最终动作" in _SYSTEM_PROMPT
    assert "mandatory_request_debugger=true" in _SYSTEM_PROMPT


def test_parse_decision_preserves_request_debugger_flag() -> None:
    decision = _parse_decision(
        {
            "mode": "single",
            "risk": "high",
            "request_debugger": True,
            "debugger_reason": "Export failed after reviewing screenshot and DOM.",
            "actions": [
                {"type": "wait", "reason": "placeholder while handing off to Debugger"}
            ],
        }
    )

    assert decision.request_debugger is True
    assert "Export failed" in str(decision.debugger_reason)


def test_prompt_forbids_invented_selectors() -> None:
    assert "必须输出 target_candidate_id" in _SYSTEM_PROMPT
    assert "不能自己创造 selector" in _SYSTEM_PROMPT
    assert "不要编 selector" in _SYSTEM_PROMPT


def test_user_prompt_includes_all_candidates_not_first_80() -> None:
    state = TaskParser(None).parse(
        request="点击导出按钮",
        entry_url="https://example.com",
        resources=[],
    )
    candidates = [
        Candidate(id=f"cand_{index}", tag="button", text=f"Button {index}", selector=f"css:#b{index}")
        for index in range(1, 122)
    ]
    observation = PageObservation(url="https://example.com", candidates=candidates)

    prompt = _build_user_prompt(state, observation)
    data = json.loads(prompt[prompt.index("{") :])

    assert data["observation"]["candidate_count"] == 121
    assert len(data["observation"]["dom_candidates"]) == 121
    assert data["observation"]["dom_candidates"][-1]["id"] == "cand_121"
