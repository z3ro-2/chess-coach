from __future__ import annotations

import json
import re
from types import SimpleNamespace

import chess_review
from src.config.provider_config import set_provider


def test_build_performance_summary_computes_percentages() -> None:
    recent_meta = [
        (1_706_000_100, "1-0", "white", 1200, "https://www.chess.com/game/live/1"),  # win
        (1_706_000_200, "0-1", "white", 1201, "https://www.chess.com/game/live/2"),  # loss
        (1_706_000_300, "1/2-1/2", "black", 1202, "https://www.chess.com/game/live/3"),  # draw
    ]

    summary = chess_review._build_performance_summary(recent_meta)

    assert summary == {
        "total_games": 3,
        "wins": 1,
        "losses": 1,
        "draws": 1,
        "win_pct": 33.3,
        "loss_pct": 33.3,
        "draw_pct": 33.3,
    }


def test_build_performance_summary_handles_zero_games() -> None:
    summary = chess_review._build_performance_summary([])

    assert summary == {
        "total_games": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "win_pct": 0.0,
        "loss_pct": 0.0,
        "draw_pct": 0.0,
    }


def test_generate_player_summary_prompt_uses_precomputed_percentages(monkeypatch, tmp_path) -> None:
    set_provider("ollama")
    captured_user_msg: dict[str, str] = {}

    def _fake_ollama_generate(**kwargs):
        captured_user_msg["text"] = str(kwargs.get("user_msg", ""))
        return json.dumps(
            {
                "overall_profile": "Stable practical player with moderate tactical volatility.",
                "strengths": ["Maintains playable structures.", "Finds active piece play."],
                "weaknesses": ["Misses tactical resources under pressure."],
                "improvement_priorities": [
                    "Run daily forcing-move checks.",
                    "Review blunder motifs from recent games.",
                    "Practice conversion when better.",
                ],
                "style_assessment": "Balanced style with occasional tactical overreach.",
                "confidence": "MEDIUM",
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )

    monkeypatch.setattr(chess_review, "call_ollama_generate", _fake_ollama_generate)

    stats_path = tmp_path / "player_stats.md"
    stats_path.write_text("# stats", encoding="utf-8")
    recent_meta = [
        (1_706_000_100, "1-0", "white", 1200, "https://www.chess.com/game/live/1"),  # win
        (1_706_000_200, "1-0", "black", 1201, "https://www.chess.com/game/live/2"),  # loss
        (1_706_000_300, "1/2-1/2", "white", 1202, "https://www.chess.com/game/live/3"),  # draw
    ]
    args = SimpleNamespace(
        username="logan",
        gpt_model="gpt-4o-mini",
        ollama_model="llama3.1:8b",
        ollama_url="http://127.0.0.1:11434",
        timeout=5,
        max_tokens=200,
    )
    trait_scores = {
        "tactical_awareness": 92,
        "material_discipline": 87,
        "conversion_ability": 78,
        "defensive_resilience": 88,
        "blunder_frequency": 95,
    }
    summary_context = {
        "date_utc": "2026-02-12",
        "your_color": "white",
        "opponent": "opponent",
        "result": "1-0",
    }

    out = chess_review._generate_player_summary_markdown(
        args,
        processed_count=3,
        cadence=3,
        stats_path=stats_path,
        recent_meta=recent_meta,
        trait_scores=trait_scores,
        trait_window_size=20,
        trait_window_moves=420,
        trait_confidence="MEDIUM",
        summary_context=summary_context,
    )

    assert "# Game Review" not in out
    assert out.startswith("---\n")
    assert "## Snapshot" in out
    user_msg = captured_user_msg["text"]
    assert "Format the deterministic player summary." in user_msg
    assert "Authoritative summary_context JSON" in user_msg
    assert "Authoritative performance_summary JSON" in user_msg
    assert "Authoritative trait_scores JSON" in user_msg
    assert "Authoritative trait_window JSON" in user_msg
    assert "Do not compute, infer, or recompute any metric." in user_msg
    assert "Do not do arithmetic, percentages, ranking, or score derivation." in user_msg
    assert "Return raw JSON only." in user_msg
    assert "markdown" not in user_msg.lower()
    assert "overall_profile" in user_msg
    assert "## Trends" not in user_msg
    assert "wins/total_games" not in user_msg
    assert "round(" not in user_msg

    matches = re.findall(r"```json\n(.*?)\n```", user_msg, flags=re.DOTALL)
    assert len(matches) >= 5
    context_summary = json.loads(matches[0])
    performance_summary = json.loads(matches[1])
    trait_summary = json.loads(matches[2])
    trait_window = json.loads(matches[3])
    weakness_summary = json.loads(matches[4])
    assert context_summary == summary_context
    assert performance_summary == {
        "total_games": 3,
        "wins": 1,
        "losses": 1,
        "draws": 1,
        "win_pct": 33.3,
        "loss_pct": 33.3,
        "draw_pct": 33.3,
    }
    assert trait_summary == trait_scores
    assert trait_window == {
        "trait_window_games": 20,
        "trait_window_moves": 420,
        "confidence": "MEDIUM",
        "trait_diagnostics": {},
    }
    assert weakness_summary["trait_name"] == "Conversion Ability"
    assert weakness_summary["score"] == 78


def test_validate_player_summary_json_contract() -> None:
    valid = {
        "overall_profile": "profile",
        "strengths": ["a"],
        "weaknesses": ["b"],
        "improvement_priorities": ["c"],
        "style_assessment": "style",
        "confidence": "HIGH",
    }
    assert chess_review.validate_player_summary_json(valid) is True

    missing = dict(valid)
    missing.pop("style_assessment")
    assert chess_review.validate_player_summary_json(missing) is False

    invalid_type = dict(valid)
    invalid_type["strengths"] = "not-a-list"
    assert chess_review.validate_player_summary_json(invalid_type) is False

    extra = dict(valid)
    extra["extra_key"] = "nope"
    assert chess_review.validate_player_summary_json(extra) is False


def test_format_player_summary_markdown_matches_template() -> None:
    payload = {
        "summary_context": {"date_utc": "2026-02-12", "your_color": "white", "opponent": "opponent", "result": "1-0"},
        "performance_summary": {
            "total_games": 4,
            "wins": 2,
            "losses": 1,
            "draws": 1,
            "win_pct": 50.0,
            "loss_pct": 25.0,
            "draw_pct": 25.0,
        },
        "trait_scores": {
            "tactical_awareness": 70,
            "material_discipline": 68,
            "conversion_ability": 62,
            "defensive_resilience": 66,
            "blunder_frequency": 72,
        },
        "trait_window": {"trait_window_games": 20, "trait_window_moves": 420, "confidence": "MEDIUM", "trait_diagnostics": {}},
        "summary_json": {
            "overall_profile": "Stable profile.",
            "strengths": ["Fast development."],
            "weaknesses": ["Conversion inconsistency."],
            "improvement_priorities": ["Conversion checklist."],
            "style_assessment": "Practical style.",
            "confidence": "MEDIUM",
        },
    }
    expected = (
        "---\n"
        "date_utc: 2026-02-12\n"
        "your_color: white\n"
        "opponent: opponent\n"
        "result: 1-0\n"
        "win_pct: 50.0\n"
        "loss_pct: 25.0\n"
        "draw_pct: 25.0\n"
        "trait_window_games: 20\n"
        "trait_window_moves: 420\n"
        "confidence: MEDIUM\n"
        "trait_diagnostics: {}\n"
        "---\n"
        "\n"
        "## Snapshot\n"
        "- Total games: 4\n"
        "- Record: 2–1–1\n"
        "- Win rate: 50.0%\n"
        "- Trait window games: 20\n"
        "- Trait window moves analyzed: 420\n"
        "- Confidence: MEDIUM\n"
        "- Overall profile: Stable profile.\n"
        "- Style assessment: Practical style.\n"
        "\n"
        "## Engine-Derived Traits\n"
        "- Tactical Awareness: 70\n"
        "- Material Discipline: 68\n"
        "- Conversion Ability: 62\n"
        "- Defensive Resilience: 66\n"
        "- Blunder Frequency: 72\n"
        "\n"
        "## Primary Weaknesses\n"
        "- Conversion inconsistency.\n"
        "\n"
        "## Training Priority\n"
        "- Conversion checklist.\n"
    )
    assert chess_review.format_player_summary_markdown(payload) == expected


def test_format_player_summary_markdown_preserves_semantics() -> None:
    profile = "Exact profile text A->B"
    style = "Exact style text !?"
    weakness = "Exact weakness ;,:"
    priority = "Exact priority MiXeD"
    payload = {
        "summary_context": {"date_utc": "2026-02-12", "your_color": "black", "opponent": "rival", "result": "0-1"},
        "performance_summary": {"total_games": 1, "wins": 0, "losses": 1, "draws": 0, "win_pct": 0.0, "loss_pct": 100.0, "draw_pct": 0.0},
        "trait_scores": {
            "tactical_awareness": 40,
            "material_discipline": 50,
            "conversion_ability": 45,
            "defensive_resilience": 48,
            "blunder_frequency": 52,
        },
        "trait_window": {"trait_window_games": 5, "trait_window_moves": 90, "confidence": "LOW", "trait_diagnostics": {}},
        "summary_json": {
            "overall_profile": profile,
            "strengths": ["unused in markdown body"],
            "weaknesses": [weakness],
            "improvement_priorities": [priority],
            "style_assessment": style,
            "confidence": "LOW",
        },
    }
    output = chess_review.format_player_summary_markdown(payload)
    for value in (profile, style, weakness, priority):
        assert value in output


def test_player_summary_retry_on_first_format_violation(monkeypatch, tmp_path) -> None:
    set_provider("ollama")
    calls: list[str] = []

    def _fake_ollama_generate(**kwargs):
        user_msg = str(kwargs.get("user_msg", ""))
        calls.append(user_msg)
        if len(calls) == 1:
            return "not-json"
        return json.dumps(
            {
                "overall_profile": "Recovered after retry.",
                "strengths": ["S1"],
                "weaknesses": ["W1"],
                "improvement_priorities": ["P1"],
                "style_assessment": "Style.",
                "confidence": "MEDIUM",
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )

    monkeypatch.setattr(chess_review, "call_ollama_generate", _fake_ollama_generate)

    stats_path = tmp_path / "player_stats.md"
    stats_path.write_text("# stats", encoding="utf-8")
    out = chess_review._generate_player_summary_markdown(
        SimpleNamespace(
            username="logan",
            gpt_model="gpt-4o-mini",
            ollama_model="llama3.1:8b",
            ollama_url="http://127.0.0.1:11434",
            timeout=5,
            max_tokens=200,
        ),
        processed_count=3,
        cadence=3,
        stats_path=stats_path,
        recent_meta=[(1_706_000_100, "1-0", "white", 1200, "https://www.chess.com/game/live/1")],
        trait_scores={
            "tactical_awareness": 70,
            "material_discipline": 68,
            "conversion_ability": 62,
            "defensive_resilience": 66,
            "blunder_frequency": 72,
        },
        trait_window_size=20,
        trait_window_moves=420,
        trait_confidence="MEDIUM",
        summary_context={"date_utc": "2026-02-12", "your_color": "white", "opponent": "opponent", "result": "1-0"},
    )
    assert len(calls) == 2
    assert "Your previous output violated format. Output ONLY valid JSON. No commentary. No markdown. No explanation." in calls[1]
    assert "Recovered after retry." in out


def test_player_summary_two_format_violations_trigger_fallback(monkeypatch, tmp_path) -> None:
    set_provider("ollama")
    calls: list[str] = []

    def _fake_ollama_generate(**kwargs):
        calls.append(str(kwargs.get("user_msg", "")))
        return "still-not-json"

    monkeypatch.setattr(chess_review, "call_ollama_generate", _fake_ollama_generate)

    stats_path = tmp_path / "player_stats.md"
    stats_path.write_text("# stats", encoding="utf-8")
    out = chess_review._generate_player_summary_markdown(
        SimpleNamespace(
            username="logan",
            gpt_model="gpt-4o-mini",
            ollama_model="llama3.1:8b",
            ollama_url="http://127.0.0.1:11434",
            timeout=5,
            max_tokens=200,
        ),
        processed_count=3,
        cadence=3,
        stats_path=stats_path,
        recent_meta=[(1_706_000_100, "1-0", "white", 1200, "https://www.chess.com/game/live/1")],
        trait_scores={
            "tactical_awareness": 70,
            "material_discipline": 68,
            "conversion_ability": 62,
            "defensive_resilience": 66,
            "blunder_frequency": 72,
        },
        trait_window_size=20,
        trait_window_moves=420,
        trait_confidence="MEDIUM",
        summary_context={"date_utc": "2026-02-12", "your_color": "white", "opponent": "opponent", "result": "1-0"},
    )
    assert len(calls) == 2
    assert "Deterministic fallback summary from engine traits only." in out
    assert "Conversion Ability: 62/100" in out
    assert "Practice conversion drills from +2 to +5 advantage positions." in out


def test_player_summary_fallback_is_deterministic(monkeypatch, tmp_path) -> None:
    set_provider("ollama")

    def _fake_ollama_generate(**_kwargs):
        return "still-not-json"

    monkeypatch.setattr(chess_review, "call_ollama_generate", _fake_ollama_generate)
    stats_path = tmp_path / "player_stats.md"
    stats_path.write_text("# stats", encoding="utf-8")
    kwargs = dict(
        args=SimpleNamespace(
            username="logan",
            gpt_model="gpt-4o-mini",
            ollama_model="llama3.1:8b",
            ollama_url="http://127.0.0.1:11434",
            timeout=5,
            max_tokens=200,
        ),
        processed_count=3,
        cadence=3,
        stats_path=stats_path,
        recent_meta=[(1_706_000_100, "1-0", "white", 1200, "https://www.chess.com/game/live/1")],
        trait_scores={
            "tactical_awareness": 70,
            "material_discipline": 68,
            "conversion_ability": 62,
            "defensive_resilience": 66,
            "blunder_frequency": 72,
        },
        trait_window_size=20,
        trait_window_moves=420,
        trait_confidence="MEDIUM",
        summary_context={"date_utc": "2026-02-12", "your_color": "white", "opponent": "opponent", "result": "1-0"},
    )
    out1 = chess_review._generate_player_summary_markdown(**kwargs)
    out2 = chess_review._generate_player_summary_markdown(**kwargs)
    assert out1 == out2


def test_trait_confidence_tiers_are_deterministic() -> None:
    assert chess_review._trait_confidence_from_moves(0) == "LOW"
    assert chess_review._trait_confidence_from_moves(199) == "LOW"
    assert chess_review._trait_confidence_from_moves(200) == "MEDIUM"
    assert chess_review._trait_confidence_from_moves(599) == "MEDIUM"
    assert chess_review._trait_confidence_from_moves(600) == "HIGH"


def test_trait_diagnostics_contains_deterministic_explainability_fields() -> None:
    final_scores = {
        "tactical_awareness": 43,
        "material_discipline": 58,
        "conversion_ability": 80,
        "defensive_resilience": 61,
        "blunder_frequency": 70,
    }
    aggregate_components = {
        "guardrails": {
            "max_allowed_score": 88,
            "error_cap_applied": True,
            "error_rate_strict_cap_applied": False,
            "low_volume_cap_applied": False,
        },
        "tactical_awareness_components": {
            "mistake_rate": 0.11,
            "blunder_rate": 0.07,
            "brilliant_rate": 0.01,
            "mate_threat_rate_per_position": 0.14,
            "mate_threat_penalty": 1.68,
            "raw_before_clamp": 43.4,
        },
        "material_discipline_components": {
            "weighted_error_rate": 0.17,
            "severe_material_rate_per_position": 0.08,
            "severe_material_penalty": 0.96,
            "raw_before_clamp": 58.2,
        },
        "conversion_ability_components": {
            "win_late_error_rate": 0.10,
            "raw_before_clamp": 80.0,
        },
        "defensive_resilience_components": {
            "pressure_rate": 0.13,
            "non_win_mate_threat_rate": 0.20,
            "non_win_mate_threat_penalty": 2.0,
            "raw_before_clamp": 61.1,
        },
        "blunder_frequency_components": {
            "blunder_rate": 0.07,
            "raw_before_clamp": 70.0,
        },
    }

    diagnostics = chess_review._trait_diagnostics_from_aggregate(
        final_scores=final_scores,
        aggregate_components=aggregate_components,
    )

    assert set(diagnostics.keys()) == set(final_scores.keys())
    for trait_name, detail in diagnostics.items():
        assert set(detail.keys()) == {
            "rate_inputs",
            "raw_score",
            "penalties_applied",
            "guardrail_applied",
            "final_clamp",
        }
        assert isinstance(detail["rate_inputs"], dict)
        assert isinstance(detail["penalties_applied"], dict)
        assert isinstance(detail["raw_score"], float)
        assert detail["guardrail_applied"]["max_allowed_score"] == 88
        assert "error_presence_cap" in detail["guardrail_applied"]["reasons"]
        assert detail["final_clamp"]["final_score"] == final_scores[trait_name]
