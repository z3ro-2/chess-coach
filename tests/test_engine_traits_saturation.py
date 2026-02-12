from __future__ import annotations

from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION
from src.engine_traits import compute_engine_trait_scores


def _strict_mode_saturation_payload() -> dict:
    return {
        "game_summary": {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "your_color": "black",
            "result": "0-1",
            "total_plies": 82,
            "total_moves": 41,
            "player_total_plies": 41,
            "player_total_moves": 41,
            "player_label_counts": {
                "good": 30,
                "inaccuracy": 6,
                "mistake": 3,
                "blunder": 2,
                "brilliant": 0,
            },
            "label_counts_by_side": {
                "white": {
                    "good": 41,
                    "inaccuracy": 0,
                    "mistake": 0,
                    "blunder": 0,
                    "brilliant": 0,
                },
                "black": {
                    "good": 30,
                    "inaccuracy": 6,
                    "mistake": 3,
                    "blunder": 2,
                    "brilliant": 0,
                },
            },
            "label_counts": {
                "good": 71,
                "inaccuracy": 6,
                "mistake": 3,
                "blunder": 2,
                "brilliant": 0,
            },
        },
        # Strict mode carries exactly four key positions.
        "key_positions": [
            {"player": "Black", "move_number": 6, "label": "blunder", "tactical_flag": "hanging_piece", "material_change": -2},
            {"player": "Black", "move_number": 14, "label": "mistake", "tactical_flag": "tactical_miss", "material_change": -1},
            {"player": "Black", "move_number": 22, "label": "inaccuracy", "tactical_flag": "none", "material_change": 0},
            {"player": "Black", "move_number": 34, "label": "blunder", "tactical_flag": "hanging_piece", "material_change": -3},
        ],
    }


def test_saturation_repro_with_player_counts_and_four_positions() -> None:
    payload = _strict_mode_saturation_payload()
    assert int(payload["game_summary"]["total_plies"]) > 60
    assert len(payload["key_positions"]) == 4
    label_counts = payload["game_summary"]["player_label_counts"]
    assert int(label_counts["inaccuracy"]) > 0
    assert int(label_counts["mistake"]) > 0
    assert int(label_counts["blunder"]) > 0

    scores = compute_engine_trait_scores([payload])

    assert scores["tactical_awareness"] < 80
    assert scores["material_discipline"] < 85
    assert scores["defensive_resilience"] < 90


def test_traits_debug_output_is_disabled_by_default(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TRAITS_DEBUG", raising=False)
    compute_engine_trait_scores([_strict_mode_saturation_payload()])
    captured = capsys.readouterr()
    assert "[traits-debug]" not in captured.err


def test_traits_debug_output_emits_components_when_enabled(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRAITS_DEBUG", "1")
    compute_engine_trait_scores([_strict_mode_saturation_payload()])
    captured = capsys.readouterr()

    assert "[traits-debug]" in captured.err
    assert '"total_plies": 82' in captured.err
    assert '"player_total_plies": 41' in captured.err
    assert '"player_label_counts": {"blunder": 2, "brilliant": 0, "good": 30, "inaccuracy": 6, "mistake": 3}' in captured.err
    assert '"key_positions_count": 4' in captured.err
    assert '"tactical_awareness_components"' in captured.err
    assert '"material_discipline_components"' in captured.err
    assert '"conversion_ability_components"' in captured.err
    assert '"defensive_resilience_components"' in captured.err
    assert '"blunder_frequency_components"' in captured.err
    assert '"coverage":' in captured.err
    assert '"total_errors":' in captured.err
    assert '"primary_games":' in captured.err
    assert '"total_player_plies":' in captured.err
    assert '"max_allowed_score":' in captured.err
