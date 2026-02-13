from __future__ import annotations

import json
import os

import pytest
import requests

import chess_review
from analysis_pipeline import load_prompt_file, validate_game_review_json


def _ollama_url() -> str:
    return str(os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") or "http://127.0.0.1:11434").strip()


def _ollama_timeout() -> int:
    raw = str(os.environ.get("OLLAMA_INTEGRATION_TIMEOUT", "20") or "20").strip()
    try:
        value = int(raw)
    except Exception:
        value = 20
    return max(1, value)


def _model_available(base_url: str, model_name: str) -> bool:
    url = base_url.rstrip("/") + "/api/tags"
    try:
        resp = requests.get(url, timeout=2)
    except Exception:
        return False
    if resp.status_code >= 400:
        return False
    try:
        payload = resp.json()
    except Exception:
        return False
    models = payload.get("models")
    if not isinstance(models, list):
        return False
    names = {str(item.get("name", "") or "") for item in models if isinstance(item, dict)}
    return model_name in names


@pytest.mark.integration
def test_ollama_review_generation_integration_json_contract() -> None:
    base_url = _ollama_url()
    model_name = "qwen2.5:14b-instruct"
    if not _model_available(base_url, model_name):
        pytest.skip(f"Ollama not reachable or model missing: {model_name} at {base_url}")

    payload = {
        "schema_version": 2,
        "context": {
            "schema_version": 2,
            "engine_depth": 15,
            "date_utc": "2026-02-13",
            "your_color": "white",
            "opponent": "opponent",
            "result": "1-0",
            "time_control": "600",
            "rated": True,
            "rules": "chess",
            "url": "https://www.chess.com/game/live/777",
        },
        "distilled_insights": {
            "player_plies_analyzed": 24,
            "top_3_worst_moves": [
                {
                    "move_number": 12,
                    "label": "blunder",
                    "played_san": "Qh5",
                    "best_san": "Nc3",
                    "tactical_flag": "hanging_piece",
                    "material_change": -3.0,
                    "abs_eval_swing": 2.4,
                    "phase": "middlegame",
                }
            ],
            "largest_eval_swing": {
                "move_number": 12,
                "label": "blunder",
                "played_san": "Qh5",
                "best_san": "Nc3",
                "tactical_flag": "hanging_piece",
                "material_change": -3.0,
                "abs_eval_swing": 2.4,
                "phase": "middlegame",
            },
            "tactical_error_summary": {
                "total_tactical_errors": 1,
                "by_flag": {"hanging_piece": 1},
                "blunder_like_events": 1,
            },
            "material_loss_summary": {
                "material_loss_events": 1,
                "total_material_loss": 3.0,
                "largest_single_material_loss": 3.0,
            },
            "phase_error_distribution": {
                "opening": 0,
                "middlegame": 1,
                "endgame": 0,
                "total_error_moves": 1,
            },
        },
    }

    system_template = load_prompt_file("review_system.md")
    user_template = load_prompt_file("review_user_strict.md")
    user_prompt = user_template.replace("{payload}", json.dumps(payload, ensure_ascii=True, separators=(",", ":")))

    output = chess_review.call_ollama_generate(
        base_url=base_url,
        model=model_name,
        system_msg=system_template,
        user_msg=user_prompt,
        timeout=_ollama_timeout(),
    )

    parsed = json.loads(str(output))
    assert isinstance(parsed, dict)
    assert validate_game_review_json(parsed) is True
    assert str(parsed.get("confidence", "")).strip() in {"LOW", "MEDIUM", "HIGH"}
    assert isinstance(parsed.get("critical_mistakes"), list)
    assert isinstance(parsed.get("strengths"), list)
    assert isinstance(parsed.get("training_focus"), list)
