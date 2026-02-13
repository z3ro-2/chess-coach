from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import backfill as backfill_module
import chess_review
from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION, LABEL_KEYS, validate_engine_payload
from src.commands import run_command
from src.config.provider_config import set_provider
from src.engine_traits import compute_engine_trait_scores


def _raw_game(*, game_id: int, end_time: int, result: str) -> dict:
    return {
        "url": f"https://www.chess.com/game/live/{game_id}",
        "pgn": (
            f'[Event "Live Chess"]\n'
            f'[White "logan"]\n'
            f'[Black "opponent-{game_id}"]\n'
            f'[Result "{result}"]\n'
            f"1. e4 e5 {result}\n"
        ),
        "end_time": int(end_time),
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "white": {"username": "logan", "rating": 1250},
        "black": {"username": f"opponent-{game_id}", "rating": 1230},
    }


def _oracle_v2_payload_for_game(*, game: chess_review.GameInfo) -> dict:
    player_plies = 30
    # Club-level rates: inaccuracy 0.1667, mistake 0.10, blunder 0.0667.
    player_counts = {
        "good": 20,
        "inaccuracy": 5,
        "mistake": 3,
        "blunder": 2,
        "brilliant": 0,
    }
    opponent_counts = {
        "good": player_plies,
        "inaccuracy": 0,
        "mistake": 0,
        "blunder": 0,
        "brilliant": 0,
    }
    by_side = (
        {"white": dict(player_counts), "black": dict(opponent_counts)}
        if game.your_color == "white"
        else {"white": dict(opponent_counts), "black": dict(player_counts)}
    )
    merged = {key: int(by_side["white"][key]) + int(by_side["black"][key]) for key in LABEL_KEYS}

    game_id = int(str(game.game_url).rstrip("/").split("/")[-1])
    is_win = (game.your_color == "white" and game.result == "1-0") or (game.your_color == "black" and game.result == "0-1")
    win_has_late_error = bool(is_win and (game_id % 2 == 0))
    late_a_label = "mistake" if (not is_win or win_has_late_error) else "good"
    late_b_label = "blunder" if not is_win else "good"

    key_positions = [
        {"player": "White", "move_number": 8, "label": "inaccuracy", "tactical_flag": "none", "material_change": 0},
        {"player": "White", "move_number": 15, "label": "mistake", "tactical_flag": "mate_threat", "material_change": -1},
        {"player": "White", "move_number": 23, "label": late_a_label, "tactical_flag": "none", "material_change": -1 if late_a_label != "good" else 0},
        {"player": "White", "move_number": 29, "label": late_b_label, "tactical_flag": "none", "material_change": -3 if late_b_label != "good" else 0},
    ]

    return {
        "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
        "game_summary": {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "total_plies": int(player_plies * 2),
            "total_moves": int(player_plies),
            "white_plies": int(player_plies),
            "black_plies": int(player_plies),
            "unlabeled_white_plies": 0,
            "unlabeled_black_plies": 0,
            "label_counts_total": dict(merged),
            "label_counts_white": dict(by_side["white"]),
            "label_counts_black": dict(by_side["black"]),
            "label_counts_by_side": by_side,
            "label_counts": dict(merged),
            "forced_mate_events": 0,
            "illegal_moves": 0,
        },
        "key_positions": key_positions,
    }


def _summary_args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        out=tmp_path / "output",
        username="logan",
        player_summary_every_n=10,
        player_trait_window=10,
        provider="ollama",
        ollama_url="http://127.0.0.1:11434",
        ollama_model="llama3.1:8b",
        gpt_model="gpt-4o-mini",
        timeout=5,
        max_tokens=300,
        telegram_bot_token="",
        telegram_chat_id="",
    )


def _render_summary_from_prompt(user_msg: str) -> str:
    blocks = re.findall(r"```json\n(.*?)\n```", user_msg, flags=re.DOTALL)
    if len(blocks) < 4:
        raise AssertionError("Expected authoritative JSON blocks in summary prompt.")
    summary_context = json.loads(blocks[0])
    performance = json.loads(blocks[1])
    trait_scores = json.loads(blocks[2])
    trait_window = json.loads(blocks[3])

    return (
        "---\n"
        f"date_utc: {summary_context['date_utc']}\n"
        f"your_color: {summary_context['your_color']}\n"
        f"opponent: {summary_context['opponent']}\n"
        f"result: {summary_context['result']}\n"
        f"win_pct: {performance['win_pct']}\n"
        f"loss_pct: {performance['loss_pct']}\n"
        f"draw_pct: {performance['draw_pct']}\n"
        f"trait_window_games: {trait_window['trait_window_games']}\n"
        f"trait_window_moves: {trait_window['trait_window_moves']}\n"
        f"confidence: {trait_window['confidence']}\n"
        f"trait_diagnostics: {json.dumps(trait_window.get('trait_diagnostics', {}), ensure_ascii=True, separators=(',', ':'))}\n"
        "---\n\n"
        "## Snapshot\n"
        f"- Total games: {performance['total_games']}\n"
        f"- Record: {performance['wins']}–{performance['losses']}–{performance['draws']}\n"
        f"- Win rate: {performance['win_pct']}%\n"
        f"- Trait window games: {trait_window['trait_window_games']}\n"
        f"- Trait window moves analyzed: {trait_window['trait_window_moves']}\n"
        f"- Confidence: {trait_window['confidence']}\n\n"
        "## Engine-Derived Traits\n"
        f"- Tactical Awareness: {trait_scores['tactical_awareness']}\n"
        f"- Material Discipline: {trait_scores['material_discipline']}\n"
        f"- Conversion Ability: {trait_scores['conversion_ability']}\n"
        f"- Defensive Resilience: {trait_scores['defensive_resilience']}\n"
        f"- Blunder Frequency: {trait_scores['blunder_frequency']}\n\n"
        "## Primary Weaknesses\n"
        "- Placeholder weakness text.\n\n"
        "## Training Priority\n"
        "- Review blunders.\n"
        "- Drill tactics.\n"
        "- Practice conversion.\n"
    )


def test_backfill_to_summary_integration_uses_v2_payloads_and_precomputed_metrics(monkeypatch, tmp_path) -> None:
    raw_games = [
        _raw_game(game_id=1, end_time=1_706_000_100, result="1-0"),
        _raw_game(game_id=2, end_time=1_706_000_200, result="0-1"),
        _raw_game(game_id=3, end_time=1_706_000_300, result="1/2-1/2"),
        _raw_game(game_id=4, end_time=1_706_000_400, result="1-0"),
        _raw_game(game_id=5, end_time=1_706_000_500, result="0-1"),
        _raw_game(game_id=6, end_time=1_706_000_600, result="1/2-1/2"),
        _raw_game(game_id=7, end_time=1_706_000_700, result="1-0"),
        _raw_game(game_id=8, end_time=1_706_000_800, result="0-1"),
        _raw_game(game_id=9, end_time=1_706_000_900, result="1/2-1/2"),
        _raw_game(game_id=10, end_time=1_706_001_000, result="1-0"),
    ]
    monkeypatch.setattr(backfill_module, "fetch_recent_games", lambda *_args, **_kwargs: list(raw_games))
    monkeypatch.setattr(
        backfill_module,
        "_run_stockfish_oracle",
        lambda **kwargs: _oracle_v2_payload_for_game(game=kwargs["game"]),
    )

    set_provider("ollama")
    captured: dict[str, str] = {}

    def _fake_ollama_generate(**kwargs):
        user_msg = str(kwargs.get("user_msg", ""))
        captured["user_msg"] = user_msg
        return _render_summary_from_prompt(user_msg)

    monkeypatch.setattr(chess_review, "call_ollama_generate", _fake_ollama_generate)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        result = backfill_module.backfill_recent_games(conn, username="logan", limit=10)
        assert result["games_considered"] == 10
        assert result["engine_analyses"] == 10
        assert result["stored"] == 10

        rows = conn.execute("SELECT payload_json FROM engine_payloads ORDER BY end_time DESC").fetchall()
        assert len(rows) == 10
        payloads = [json.loads(str(row[0])) for row in rows]

        for payload in payloads:
            validation = validate_engine_payload(
                payload,
                require_schema_version=True,
                require_player_fields=True,
                require_key_positions=True,
            )
            assert validation.is_valid, validation.errors

        expected_scores = compute_engine_trait_scores(payloads)
        expected_moves = sum(chess_review._payload_total_moves(payload) for payload in payloads)
        expected_confidence = chess_review._trait_confidence_from_moves(expected_moves)
        # Approximate trait envelopes for this deterministic club-level fixture.
        assert 40 <= expected_scores["tactical_awareness"] <= 85
        assert 45 <= expected_scores["material_discipline"] <= 85
        assert 50 <= expected_scores["conversion_ability"] <= 90
        assert 45 <= expected_scores["defensive_resilience"] <= 85
        assert 50 <= expected_scores["blunder_frequency"] <= 90

        command_result = run_command("summary", conn, _summary_args(tmp_path))
    finally:
        conn.close()

    text = str(command_result["text"])
    assert "Trait scores (v2 window 10/10 games):" in text
    assert f"tactical_awareness={expected_scores['tactical_awareness']}" in text
    assert f"material_discipline={expected_scores['material_discipline']}" in text
    assert f"conversion_ability={expected_scores['conversion_ability']}" in text
    assert f"defensive_resilience={expected_scores['defensive_resilience']}" in text
    assert f"blunder_frequency={expected_scores['blunder_frequency']}" in text

    summary_path = Path(str(command_result["file"]))
    assert summary_path.exists()
    markdown = summary_path.read_text(encoding="utf-8")
    assert f"trait_window_moves: {expected_moves}" in markdown
    assert f"confidence: {expected_confidence}" in markdown
    assert f"- Tactical Awareness: {expected_scores['tactical_awareness']}" in markdown
    assert f"- Material Discipline: {expected_scores['material_discipline']}" in markdown
    assert f"- Conversion Ability: {expected_scores['conversion_ability']}" in markdown
    assert f"- Defensive Resilience: {expected_scores['defensive_resilience']}" in markdown
    assert f"- Blunder Frequency: {expected_scores['blunder_frequency']}" in markdown

    user_msg = captured["user_msg"]
    assert "Do not compute, infer, or recompute any metric." in user_msg
    assert "Do not do arithmetic, percentages, ranking, or score derivation." in user_msg
    blocks = re.findall(r"```json\n(.*?)\n```", user_msg, flags=re.DOTALL)
    assert len(blocks) >= 4
    trait_scores_json = json.loads(blocks[2])
    trait_window_json = json.loads(blocks[3])
    assert trait_scores_json == expected_scores
    assert trait_window_json["trait_window_games"] == 10
    assert trait_window_json["trait_window_moves"] == expected_moves
    assert trait_window_json["confidence"] == expected_confidence
