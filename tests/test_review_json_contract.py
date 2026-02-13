from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import analysis_pipeline as pipeline_module
from analysis_pipeline import (
    LLMFormatViolationError,
    format_game_review_markdown,
    run_analysis_pipeline,
    validate_game_review_json,
)
from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION


class _Game:
    game_url = "https://www.chess.com/game/live/777"
    pgn = '[Event "Live Chess"]\n[Result "1-0"]\n1. e4 e5 1-0\n'
    your_color = "white"
    opponent = "opponent"
    result = "1-0"
    time_control = "600"
    rated = True
    rules = "chess"

    @property
    def end_dt_utc(self):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(1_706_000_000, tz=timezone.utc)


def _v2_summary() -> dict[str, object]:
    return {
        "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
        "engine_depth": 15,
        "result": "1-0",
        "total_plies": 4,
        "total_moves": 2,
        "white_plies": 2,
        "black_plies": 2,
        "unlabeled_white_plies": 0,
        "unlabeled_black_plies": 0,
        "label_counts_total": {"good": 3, "inaccuracy": 1, "mistake": 0, "blunder": 0, "brilliant": 0},
        "label_counts_white": {"good": 1, "inaccuracy": 1, "mistake": 0, "blunder": 0, "brilliant": 0},
        "label_counts_black": {"good": 2, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
        "label_counts_by_side": {
            "white": {"good": 1, "inaccuracy": 1, "mistake": 0, "blunder": 0, "brilliant": 0},
            "black": {"good": 2, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
        },
        "label_counts": {"good": 3, "inaccuracy": 1, "mistake": 0, "blunder": 0, "brilliant": 0},
        "forced_mate_events": 0,
        "illegal_moves": 0,
    }


def _four_positions() -> list[dict[str, object]]:
    return [
        {"move_number": 1, "player": "White", "label": "mistake", "material_change": 0, "mate_threat": False, "forcing": False, "tactical_flag": "tactical_miss", "played_san": "e4", "best_san": "Nf3"},
        {"move_number": 1, "player": "Black", "label": "inaccuracy", "material_change": 0, "mate_threat": False, "forcing": False, "tactical_flag": "none", "played_san": "e5", "best_san": "c5"},
        {"move_number": 2, "player": "White", "label": "blunder", "material_change": -3, "mate_threat": False, "forcing": True, "tactical_flag": "hanging_piece", "played_san": "Qh5", "best_san": "Nc3"},
        {"move_number": 2, "player": "Black", "label": "good", "material_change": 0, "mate_threat": False, "forcing": False, "tactical_flag": "none", "played_san": "Nc6", "best_san": "Nc6"},
    ]


def _trace_for_positions(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    swings = (4.0, 3.0, 2.0, 1.0)
    output: list[dict[str, object]] = []
    for idx, row in enumerate(rows):
        output.append(
            {
                "move_number": row["move_number"],
                "player": row["player"],
                "label": row["label"],
                "material_change": row["material_change"],
                "mate_threat": row["mate_threat"],
                "forcing": row["forcing"],
                "tactical_flag": row["tactical_flag"],
                "played_san": row["played_san"],
                "best_san": row["best_san"],
                "eval_before": 0.0,
                "played_eval": 0.0,
                "best_eval": 0.0,
                "eval_loss": 0.0,
                "abs_eval_swing": float(swings[idx]),
            }
        )
    return output


def _valid_review_json() -> dict[str, object]:
    return {
        "game_overview": "You played actively but missed tactical precision in critical moments.",
        "critical_mistakes": [
            {
                "move_number": 12,
                "description": "You allowed a tactical shot on your king side.",
                "why_it_matters": "It gave up initiative and pressure.",
                "improvement_tip": "Check forcing captures and checks before committing.",
            }
        ],
        "strengths": ["You developed pieces quickly.", "You stayed alert to threats."],
        "training_focus": ["Practice forcing-move scans.", "Rehearse king-safety motifs."],
        "confidence": "MEDIUM",
    }


def test_validate_game_review_json_valid_passes() -> None:
    assert validate_game_review_json(dict(_valid_review_json())) is True


def test_validate_game_review_json_missing_field_fails() -> None:
    bad = dict(_valid_review_json())
    bad.pop("training_focus")
    assert validate_game_review_json(bad) is False


def test_validate_game_review_json_invalid_type_fails() -> None:
    bad = dict(_valid_review_json())
    bad["critical_mistakes"] = [{"move_number": "12", "description": "x", "why_it_matters": "y", "improvement_tip": "z"}]
    assert validate_game_review_json(bad) is False


@pytest.mark.parametrize(
    "raw_output",
    [
        "```json\n{\"game_overview\":\"x\"}\n```",  # markdown-wrapped
        "Here is your review:\n{\"game_overview\":\"x\"}",  # commentary before JSON
        "{\"game_overview\":\"x\"",  # partial JSON
    ],
)
def test_structure_drift_non_compliant_outputs_are_rejected(raw_output: str) -> None:
    with pytest.raises(LLMFormatViolationError):
        pipeline_module._parse_game_review_json_or_raise(raw_output)  # type: ignore[attr-defined]


def test_structure_drift_valid_json_passes_parser() -> None:
    parsed = pipeline_module._parse_game_review_json_or_raise(  # type: ignore[attr-defined]
        (
            '{"game_overview":"Valid.","critical_mistakes":[],"strengths":["S1"],'
            '"training_focus":["T1"],"confidence":"LOW"}'
        )
    )
    assert parsed["game_overview"] == "Valid."


def test_run_analysis_pipeline_two_invalid_outputs_trigger_deterministic_fallback(monkeypatch) -> None:
    key_positions = _four_positions()
    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": _v2_summary(),
            "key_positions": key_positions,
            "all_positions": _trace_for_positions(key_positions),
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: None)
    output = run_analysis_pipeline(
        game=_Game(),
        args=SimpleNamespace(enable_engine=True),
        llm_runner=lambda _sys, _user: "Not JSON output",
        logger=logging.getLogger("test"),
    )
    assert "Deterministic fallback review:" in output
    assert "Blunder count target:" in output


def test_run_analysis_pipeline_fallback_is_deterministic(monkeypatch) -> None:
    key_positions = _four_positions()
    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": _v2_summary(),
            "key_positions": key_positions,
            "all_positions": _trace_for_positions(key_positions),
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: None)
    runner = lambda _sys, _user: "Not JSON output"
    out1 = run_analysis_pipeline(
        game=_Game(),
        args=SimpleNamespace(enable_engine=True),
        llm_runner=runner,
        logger=logging.getLogger("test"),
    )
    out2 = run_analysis_pipeline(
        game=_Game(),
        args=SimpleNamespace(enable_engine=True),
        llm_runner=runner,
        logger=logging.getLogger("test"),
    )
    assert out1 == out2


def test_self_critique_yes_passes_without_generation_retry(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_LLM_SELF_CRITIQUE", "1")
    key_positions = _four_positions()
    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": _v2_summary(),
            "key_positions": key_positions,
            "all_positions": _trace_for_positions(key_positions),
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: None)
    calls: list[str] = []

    def _llm_runner(_system: str, user: str) -> str:
        calls.append(user)
        if "Does this advice strictly match the engine facts? YES or NO." in user:
            return "YES"
        return (
            '{"game_overview":"Critique passed.","critical_mistakes":[],"strengths":["S1"],'
            '"training_focus":["T1"],"confidence":"MEDIUM"}'
        )

    output = run_analysis_pipeline(
        game=_Game(),
        args=SimpleNamespace(enable_engine=True),
        llm_runner=_llm_runner,
        logger=logging.getLogger("test"),
    )
    assert "Critique passed." in output
    assert len(calls) == 2


def test_self_critique_no_triggers_generation_retry(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_LLM_SELF_CRITIQUE", "1")
    key_positions = _four_positions()
    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": _v2_summary(),
            "key_positions": key_positions,
            "all_positions": _trace_for_positions(key_positions),
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: None)
    generation_count = {"value": 0}
    calls: list[str] = []

    def _llm_runner(_system: str, user: str) -> str:
        calls.append(user)
        if "Does this advice strictly match the engine facts? YES or NO." in user:
            return "NO" if generation_count["value"] == 1 else "YES"
        generation_count["value"] += 1
        if generation_count["value"] == 1:
            return (
                '{"game_overview":"First generation.","critical_mistakes":[],"strengths":["S1"],'
                '"training_focus":["T1"],"confidence":"MEDIUM"}'
            )
        return (
            '{"game_overview":"Second generation.","critical_mistakes":[],"strengths":["S2"],'
            '"training_focus":["T2"],"confidence":"MEDIUM"}'
        )

    output = run_analysis_pipeline(
        game=_Game(),
        args=SimpleNamespace(enable_engine=True),
        llm_runner=_llm_runner,
        logger=logging.getLogger("test"),
    )
    assert "Second generation." in output
    assert "First generation." not in output
    assert len(calls) == 4


def test_run_analysis_pipeline_retries_once_on_format_violation(monkeypatch) -> None:
    key_positions = _four_positions()
    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": _v2_summary(),
            "key_positions": key_positions,
            "all_positions": _trace_for_positions(key_positions),
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: None)
    calls: list[str] = []

    def _llm_runner(_system: str, user: str) -> str:
        calls.append(user)
        if len(calls) == 1:
            return "invalid-json"
        return (
            '{"game_overview":"Retry succeeded.","critical_mistakes":[],"strengths":["S1"],'
            '"training_focus":["T1"],"confidence":"MEDIUM"}'
        )

    output = run_analysis_pipeline(
        game=_Game(),
        args=SimpleNamespace(enable_engine=True),
        llm_runner=_llm_runner,
        logger=logging.getLogger("test"),
    )
    assert len(calls) == 2
    assert "Your previous output violated format. Output ONLY valid JSON. No commentary. No markdown. No explanation." in calls[1]
    assert "Retry succeeded." in output


def test_structure_drift_retry_then_valid_output_passes(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_LLM_SELF_CRITIQUE", raising=False)
    key_positions = _four_positions()
    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": _v2_summary(),
            "key_positions": key_positions,
            "all_positions": _trace_for_positions(key_positions),
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: None)
    calls: list[str] = []

    def _llm_runner(_system: str, user: str) -> str:
        calls.append(user)
        if len(calls) == 1:
            return "Here is your review:\n{\"game_overview\":\"x\"}"
        return (
            '{"game_overview":"Recovered from drift.","critical_mistakes":[],"strengths":["S1"],'
            '"training_focus":["T1"],"confidence":"LOW"}'
        )

    output = run_analysis_pipeline(
        game=_Game(),
        args=SimpleNamespace(enable_engine=True),
        llm_runner=_llm_runner,
        logger=logging.getLogger("test"),
    )
    assert len(calls) == 2
    assert "Recovered from drift." in output


def test_llm_retry_logic_on_epoch_change(monkeypatch) -> None:
    key_positions = _four_positions()
    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": _v2_summary(),
            "key_positions": key_positions,
            "all_positions": _trace_for_positions(key_positions),
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: None)
    hash_calls = {"n": 0}

    def _changing_prompt_hash(*_args, **_kwargs) -> str:
        hash_calls["n"] += 1
        return f"epoch-hash-{hash_calls['n']}"

    monkeypatch.setattr(pipeline_module, "prompt_hash", _changing_prompt_hash)

    calls: list[str] = []

    def _llm_runner(_system: str, user: str) -> str:
        calls.append(user)
        if len(calls) == 1:
            return "invalid-json"
        return (
            '{"game_overview":"Epoch retry succeeded.","critical_mistakes":[],"strengths":["S1"],'
            '"training_focus":["T1"],"confidence":"LOW"}'
        )

    output = run_analysis_pipeline(
        game=_Game(),
        args=SimpleNamespace(enable_engine=True),
        llm_runner=_llm_runner,
        logger=logging.getLogger("test"),
    )

    assert len(calls) == 2
    assert hash_calls["n"] >= 2
    assert "Epoch retry succeeded." in output


def test_fallback_counter_increments_when_triggered(monkeypatch) -> None:
    key_positions = _four_positions()
    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": _v2_summary(),
            "key_positions": key_positions,
            "all_positions": _trace_for_positions(key_positions),
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: None)
    pipeline_module.reset_fallback_usage_count()
    before = pipeline_module.get_fallback_usage_count()

    output = run_analysis_pipeline(
        game=_Game(),
        args=SimpleNamespace(enable_engine=True),
        llm_runner=lambda _sys, _user: "Not JSON output",
        logger=logging.getLogger("test"),
    )

    after = pipeline_module.get_fallback_usage_count()
    assert "Deterministic fallback review:" in output
    assert after == before + 1


def test_two_violations_increment_fallback_counter(monkeypatch) -> None:
    key_positions = _four_positions()
    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": _v2_summary(),
            "key_positions": key_positions,
            "all_positions": _trace_for_positions(key_positions),
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: None)
    pipeline_module.reset_llm_attempt_counters()
    before = pipeline_module.get_llm_attempt_counters()

    output = run_analysis_pipeline(
        game=_Game(),
        args=SimpleNamespace(enable_engine=True),
        llm_runner=lambda _sys, _user: "not-json",
        logger=logging.getLogger("test"),
    )

    after = pipeline_module.get_llm_attempt_counters()
    assert "Deterministic fallback review:" in output
    assert int(after["llm_total_attempts"]) == int(before["llm_total_attempts"]) + 1
    assert int(after["llm_fallback_count"]) == int(before["llm_fallback_count"]) + 1


def test_structure_drift_two_non_compliant_outputs_trigger_fallback(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_LLM_SELF_CRITIQUE", raising=False)
    key_positions = _four_positions()
    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": _v2_summary(),
            "key_positions": key_positions,
            "all_positions": _trace_for_positions(key_positions),
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: None)
    calls: list[str] = []

    def _llm_runner(_system: str, user: str) -> str:
        calls.append(user)
        return "```json\n{\"game_overview\":\"x\"}\n```" if len(calls) == 1 else "{\"game_overview\":\"x\""

    output = run_analysis_pipeline(
        game=_Game(),
        args=SimpleNamespace(enable_engine=True),
        llm_runner=_llm_runner,
        logger=logging.getLogger("test"),
    )
    assert len(calls) == 2
    assert "Deterministic fallback review:" in output


def test_format_game_review_markdown_matches_template() -> None:
    review = {
        "game_overview": "Clear overview.",
        "critical_mistakes": [
            {
                "move_number": 14,
                "description": "Missed tactic.",
                "why_it_matters": "Lost initiative.",
                "improvement_tip": "Scan forcing lines.",
            }
        ],
        "strengths": ["Fast development."],
        "training_focus": ["Daily tactic reps."],
        "confidence": "HIGH",
    }
    expected = (
        "# Game Review\n"
        "\n"
        "## Summary\n"
        "Clear overview.\n"
        "\n"
        "## Critical Mistakes\n"
        "### 1. Move 14\n"
        "- Description: Missed tactic.\n"
        "- Why It Matters: Lost initiative.\n"
        "- Improvement Tip: Scan forcing lines.\n"
        "\n"
        "## Strengths\n"
        "- Fast development.\n"
        "\n"
        "## Training Focus\n"
        "- Daily tactic reps.\n"
        "\n"
        "## Confidence\n"
        "- HIGH\n"
    )
    assert format_game_review_markdown(review) == expected


def test_format_game_review_markdown_preserves_semantics() -> None:
    overview = "Exact wording: keep symbols !? and spacing."
    description = "Description with exact token A->B."
    why = "Why text keeps punctuation: ;,:"
    tip = "Tip keeps casing MiXeD."
    strength = "Strength text unchanged."
    focus = "Focus text unchanged."
    review = {
        "game_overview": overview,
        "critical_mistakes": [
            {
                "move_number": 7,
                "description": description,
                "why_it_matters": why,
                "improvement_tip": tip,
            }
        ],
        "strengths": [strength],
        "training_focus": [focus],
        "confidence": "MEDIUM",
    }
    output = format_game_review_markdown(review)
    for value in (overview, description, why, tip, strength, focus, "MEDIUM"):
        assert value in output
