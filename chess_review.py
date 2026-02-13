#!/usr/bin/env python3
"""chess_review.py

Chess.com -> LLM -> Markdown coaching notes (daemon/poller).

What it does:
- Polls the Chess.com public API for your account's games.
- Detects new (unprocessed) games using a local SQLite state DB.
- For each new game, generates a Markdown coaching review via:
    - OpenAI Chat Completions API (provider=gpt), OR
    - Ollama local endpoint (provider=ollama)
  Switchable with a single flag.
- Saves both the raw PGN and the generated Markdown to disk with collision-proof names.
- Optionally maintains an index.md.

Dependencies:
- Python 3.10+ recommended
- pip install requests

Examples:
  # Run forever, poll every 5 minutes, use Ollama locally
  python3 chess_review.py --username LoganChess --provider ollama --ollama-model llama3.1:8b --poll-seconds 300 --update-index

  # Run once, backfill last 7 days, use GPT
  export OPENAI_API_KEY="..."
  python3 chess_review.py --username LoganChess --provider gpt --gpt-model gpt-4o-mini --once --lookback-days 7 --update-index

  # Custom output + state location
  python3 chess_review.py --username LoganChess --out ./reviews --state-db ./state.sqlite --provider ollama

Notes:
- Chess.com API provides raw PGN/metadata only (no engine eval). This script uses the LLM to produce human-coachable notes.
- For best coaching quality, consider adding an optional Stockfish pass later to detect eval swings and feed only critical moments.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple



import requests
from analysis_pipeline import run_analysis_pipeline
from engine.payload_schema import (
    ENGINE_PAYLOAD_SCHEMA_VERSION,
    enrich_summary_with_player_fields,
    validate_engine_payload,
)
from src.commands import list_command_names, run_command
from src.config.provider_config import get_provider, set_provider
from src.config.output_paths import get_output_root
from src.db.bootstrap import ensure_bootstrap
from src.db.ingest_check import close_ingest_db_check, is_game_ingested_in_db
from src.db.player_metrics import record_player_rating_for_game
from src.db.runtime_updates import fetch_player_runtime_snapshot, sync_game_record_and_traits
from src.db.schema import ensure_postgres_core_schema
from src.telegram_commands import poll_telegram_commands
from src.utils.timezone import get_display_timezone

CHESSCOM_GAMES_URL = "https://api.chess.com/pub/player/{username}/games/{year}/{month:02d}"
DEFAULT_TIMEOUT = 120  # seconds (local Ollama cold starts can be slow)
USER_AGENT = "chess-review-daemon/0.1 (+https://chess.com/pubapi)"
logger = logging.getLogger(__name__)
TRAIT_SCORE_KEYS: tuple[str, ...] = (
    "tactical_awareness",
    "material_discipline",
    "conversion_ability",
    "defensive_resilience",
    "blunder_frequency",
)

# -----------------------------
# Telegram notifications
# -----------------------------
class TelegramError(RuntimeError):
    pass


def _telegram_api_base(bot_token: str) -> str:
    return f"https://api.telegram.org/bot{bot_token.strip()}"


def send_telegram_document(
    bot_token: str,
    chat_id: str,
    file_path: Path,
    caption: str,
    timeout: int = DEFAULT_TIMEOUT,
    disable_notification: bool = False,
) -> None:
    """Send a document (file) to Telegram via a bot.

    Uses: https://api.telegram.org/bot<TOKEN>/sendDocument
    """
    if not bot_token:
        raise TelegramError("Missing bot_token")
    if not chat_id:
        raise TelegramError("Missing chat_id")
    if not file_path.exists():
        raise TelegramError(f"File not found: {file_path}")

    url = _telegram_api_base(bot_token) + "/sendDocument"
    data = {
        "chat_id": chat_id,
        "caption": caption[:1024],  # Telegram caption limit (safe clamp)
        "disable_notification": disable_notification,
        "parse_mode": "Markdown",
    }

    # Use multipart/form-data
    with file_path.open("rb") as f:
        files = {"document": (file_path.name, f, "text/markdown")}
        resp = requests.post(url, data=data, files=files, timeout=timeout)

    if resp.status_code >= 400:
        raise TelegramError(f"Telegram API error {resp.status_code}: {resp.text[:800]}")

    try:
        payload = resp.json()
    except Exception:
        raise TelegramError(f"Telegram returned non-JSON: {resp.text[:800]}")

    if not payload.get("ok", False):
        raise TelegramError(f"Telegram sendDocument failed: {str(payload)[:800]}")


def send_telegram_message(
    message: str,
    *,
    bot_token: str,
    chat_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    disable_notification: bool = False,
) -> None:
    """Send a plain text message to Telegram via a bot."""
    if not bot_token:
        raise TelegramError("Missing bot_token")
    if not chat_id:
        raise TelegramError("Missing chat_id")

    url = _telegram_api_base(bot_token) + "/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": str(message or "")[:4096],
        "disable_notification": disable_notification,
    }
    resp = requests.post(url, data=payload, timeout=timeout)
    if resp.status_code >= 400:
        raise TelegramError(f"Telegram API error {resp.status_code}: {resp.text[:800]}")

    try:
        body = resp.json()
    except Exception:
        raise TelegramError(f"Telegram returned non-JSON: {resp.text[:800]}")
    if not body.get("ok", False):
        raise TelegramError(f"Telegram sendMessage failed: {str(body)[:800]}")


def build_telegram_caption(game: "GameInfo") -> str:
    """Short caption for the Telegram message."""
    # Keep it compact and readable.
    dt = game.end_dt_utc.strftime("%Y-%m-%d %H:%M UTC")
    who = "White" if game.your_color == "white" else "Black"
    return (
        f"Chess review ready — {dt}\n"
        f"You: {who} vs {game.opponent}\n"
        f"Result: {game.result} | TC: {game.time_control}\n"
        f"{game.game_url}"
    )

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None  # type: ignore


# -----------------------------
# Data model
# -----------------------------
@dataclasses.dataclass
class GameInfo:
    game_url: str
    pgn: str
    end_time: int  # epoch seconds (UTC)
    time_control: str
    rated: bool
    rules: str
    white_username: str
    black_username: str
    white_rating: Optional[int]
    black_rating: Optional[int]
    result: str  # "1-0", "0-1", "1/2-1/2", "*"
    your_color: str  # "white" or "black"
    opponent: str

    @property
    def end_dt_utc(self) -> datetime:
        return datetime.fromtimestamp(self.end_time, tz=timezone.utc)


# -----------------------------
# SQLite state
# -----------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS processed_games (
  game_url TEXT PRIMARY KEY,
  end_time INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  md_path TEXT NOT NULL,
  pgn_path TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_processed_end_time ON processed_games(end_time);

CREATE TABLE IF NOT EXISTS processed_game_meta (
  game_url TEXT PRIMARY KEY,
  end_time INTEGER NOT NULL,
  result TEXT NOT NULL,
  player_color TEXT NOT NULL,
  player_rating INTEGER,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_processed_meta_end_time ON processed_game_meta(end_time);

CREATE TABLE IF NOT EXISTS summary_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_summary_processed_count INTEGER NOT NULL DEFAULT 0,
  last_summary_end_time INTEGER,
  updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

INSERT OR IGNORE INTO summary_state (id, last_summary_processed_count, last_summary_end_time, updated_at)
VALUES (1, 0, NULL, strftime('%s','now'));

CREATE TABLE IF NOT EXISTS engine_payloads (
  game_url TEXT PRIMARY KEY,
  end_time INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  engine_depth INTEGER NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_engine_payloads_end_time ON engine_payloads(end_time);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        db_path.touch(exist_ok=True)
    except Exception:
        pass
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def is_processed(conn: sqlite3.Connection, game_url: str) -> bool:
    if is_game_ingested_in_db(game_url):
        return True

    cur = conn.execute("SELECT 1 FROM processed_games WHERE game_url = ?", (game_url,))
    return cur.fetchone() is not None


def mark_processed(
    conn: sqlite3.Connection,
    game_url: str,
    end_time: int,
    md_path: Path,
    pgn_path: Path,
    provider: str,
    model: str,
    content_hash: str,
) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT OR REPLACE INTO processed_games
          (game_url, end_time, created_at, md_path, pgn_path, provider, model, hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (game_url, end_time, now, str(md_path), str(pgn_path), provider, model, content_hash),
    )
    conn.commit()


def _engine_payload_exists(conn: sqlite3.Connection, game_url: str) -> bool:
    row = conn.execute("SELECT 1 FROM engine_payloads WHERE game_url = ?", (game_url,)).fetchone()
    return row is not None


def _load_engine_payload(conn: sqlite3.Connection, game_url: str) -> Dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_json FROM engine_payloads WHERE game_url = ?",
        (str(game_url),),
    ).fetchone()
    if row is None:
        return None
    try:
        loaded = json.loads(str(row[0]))
    except Exception:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _stored_engine_payload_is_reusable(conn: sqlite3.Connection, game_url: str) -> bool:
    loaded = _load_engine_payload(conn, game_url)
    if loaded is None:
        return False
    validation = validate_engine_payload(
        loaded,
        require_schema_version=True,
        require_player_fields=True,
        require_key_positions=False,
    )
    if validation.is_valid:
        return True
    logger.warning(
        "Re-deriving engine payload for %s: schema=%s expected=%s errors=%s",
        str(game_url),
        int(validation.schema_version),
        int(ENGINE_PAYLOAD_SCHEMA_VERSION),
        ",".join(validation.errors),
    )
    return False


def _store_engine_payload(
    conn: sqlite3.Connection,
    *,
    game_url: str,
    end_time: int,
    engine_depth: int,
    payload: Mapping[str, Any],
) -> bool:
    payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    existing_row = conn.execute(
        "SELECT payload_json FROM engine_payloads WHERE game_url = ?",
        (str(game_url),),
    ).fetchone()
    if existing_row is not None and str(existing_row[0]) == payload_json:
        conn.commit()
        return False
    conn.execute(
        """
        INSERT OR REPLACE INTO engine_payloads
          (game_url, end_time, created_at, engine_depth, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (game_url, int(end_time), int(time.time()), int(engine_depth), payload_json),
    )
    conn.commit()
    return True


def _upsert_backfill_processed_records(conn: sqlite3.Connection, *, game: GameInfo, args: argparse.Namespace) -> None:
    provider = "stockfish"
    model = f"stockfish-depth-{int(getattr(args, 'engine_depth', 15) or 15)}"
    h = compute_content_hash(game, provider, model)
    mark_processed(
        conn=conn,
        game_url=game.game_url,
        end_time=game.end_time,
        md_path=Path("__backfill__/no_markdown.md"),
        pgn_path=Path("__backfill__/inline_pgn"),
        provider=provider,
        model=model,
        content_hash=h,
    )
    _record_processed_game_meta(conn, game)


def _player_rating_for_game(game: GameInfo) -> Optional[int]:
    return game.white_rating if game.your_color == "white" else game.black_rating


def _record_processed_game_meta(conn: sqlite3.Connection, game: GameInfo) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO processed_game_meta
          (game_url, end_time, result, player_color, player_rating, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            game.game_url,
            game.end_time,
            game.result,
            game.your_color,
            _player_rating_for_game(game),
            int(time.time()),
        ),
    )
    conn.commit()


def _performance_counts_from_rows(rows: List[Tuple[str, str]]) -> Tuple[int, int, int]:
    wins = 0
    losses = 0
    draws = 0
    for result, player_color in rows:
        color = (player_color or "").lower()
        if result == "1/2-1/2":
            draws += 1
            continue
        if color == "white":
            if result == "1-0":
                wins += 1
            elif result == "0-1":
                losses += 1
        elif color == "black":
            if result == "0-1":
                wins += 1
            elif result == "1-0":
                losses += 1
    return wins, losses, draws


def _fetch_recent_performance_sqlite(conn: sqlite3.Connection, limit: int) -> Tuple[int, int, int]:
    cur = conn.execute(
        """
        SELECT result, player_color
        FROM processed_game_meta
        ORDER BY end_time DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    )
    rows = [(str(r[0]), str(r[1])) for r in cur.fetchall()]
    return _performance_counts_from_rows(rows)


def _fetch_recent_ratings_sqlite(conn: sqlite3.Connection, limit: int) -> List[int]:
    cur = conn.execute(
        """
        SELECT player_rating
        FROM processed_game_meta
        WHERE player_rating IS NOT NULL
        ORDER BY end_time DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    )
    return [int(r[0]) for r in cur.fetchall() if r[0] is not None]


def _rating_trend_text(ratings: List[int]) -> str:
    if not ratings:
        return "No rating data yet."
    latest = ratings[0]
    oldest = ratings[-1]
    delta = latest - oldest
    if len(ratings) == 1:
        return f"Latest observed rating: {latest}"
    direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return f"Recent trend: {latest} ({direction} {delta:+d} across {len(ratings)} games)"


def _safe_short_text(value: Any, max_len: int = 120) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _write_player_stats_markdown(
    conn: sqlite3.Connection,
    *,
    out_dir: Path,
    username: str,
    recent_games: int = 20,
) -> Path:
    runtime = fetch_player_runtime_snapshot(player_username=username, recent_games=recent_games, trait_limit=10)
    performance = runtime.get("performance") if runtime.get("available") else None
    if isinstance(performance, dict):
        wins = int(performance.get("wins", 0) or 0)
        losses = int(performance.get("losses", 0) or 0)
        draws = int(performance.get("draws", 0) or 0)
    else:
        wins, losses, draws = _fetch_recent_performance_sqlite(conn, recent_games)

    ratings = []
    if runtime.get("available"):
        ratings = [int(r.get("rating")) for r in runtime.get("ratings", []) if r.get("rating") is not None]
    if not ratings:
        ratings = _fetch_recent_ratings_sqlite(conn, recent_games)

    trait_rows = runtime.get("traits", []) if runtime.get("available") else []
    lines: List[str] = [
        "# Player Stats",
        "",
        f"- Updated (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Player: {username}",
        f"- Source window: last {recent_games} processed games",
        "",
        "## Rating Trend",
        _rating_trend_text(ratings),
        "",
        "## Recent Performance",
        f"- Wins: {wins}",
        f"- Losses: {losses}",
        f"- Draws: {draws}",
        "",
        "## Current Trait Scores",
    ]

    if trait_rows:
        for trait in trait_rows[:10]:
            name = _safe_short_text(trait.get("name") or trait.get("key") or "Unknown trait", 64)
            category = _safe_short_text(trait.get("category"), 20)
            desc = _safe_short_text(trait.get("description"), 110)
            confidence = float(trait.get("confidence", 0.0) or 0.0)
            trend = float(trait.get("trend_ema", 0.0) or 0.0)
            lines.append(
                f"- **{name}** ({category}): confidence {confidence:.2f}, trend {trend:+.2f}. {desc}"
            )
    else:
        lines.append("- No trait scores available yet.")

    lines.append("")
    path = out_dir / "player_stats.md"
    write_text(path, "\n".join(lines))
    return path


def _load_recent_game_meta_for_summary(conn: sqlite3.Connection, limit: int) -> List[Tuple[int, str, str, Optional[int], str]]:
    cur = conn.execute(
        """
        SELECT end_time, result, player_color, player_rating, game_url
        FROM processed_game_meta
        ORDER BY end_time DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    )
    rows = cur.fetchall()
    return [
        (int(r[0]), str(r[1]), str(r[2]), int(r[3]) if r[3] is not None else None, str(r[4]))
        for r in rows
    ]


def _load_recent_game_reviews_for_traits(
    conn: sqlite3.Connection,
    limit: int,
) -> List[Dict[str, Any]]:
    target = max(1, int(limit))
    cur = conn.execute(
        """
        SELECT game_url, payload_json
        FROM engine_payloads
        ORDER BY end_time DESC, game_url DESC
        """
    )
    payloads: List[Dict[str, Any]] = []
    skipped_legacy = 0
    for game_url, payload_json in cur:
        try:
            loaded = json.loads(str(payload_json))
        except Exception:
            continue
        if not isinstance(loaded, dict):
            continue
        validation = validate_engine_payload(
            loaded,
            require_schema_version=True,
            require_player_fields=True,
            require_key_positions=False,
        )
        if not validation.is_valid:
            skipped_legacy += 1
            logger.error(
                "Ignoring stale/invalid engine payload for trait window game_url=%s schema=%s errors=%s",
                str(game_url),
                int(validation.schema_version),
                ",".join(validation.errors),
            )
            continue
        payloads.append(loaded)
        if len(payloads) >= target:
            break
    if skipped_legacy > 0:
        logger.warning(
            "Trait window skipped %s legacy/stale payloads while collecting schema v2 payloads.",
            int(skipped_legacy),
        )
    return payloads


def _load_recent_engine_payloads_from_db(
    conn: sqlite3.Connection,
    limit: int,
) -> List[Dict[str, Any]]:
    return _load_recent_game_reviews_for_traits(conn, limit)


def _analyze_game_with_stockfish(*, game: GameInfo, args: argparse.Namespace) -> Dict[str, Any]:
    from engine.stockfish_oracle import StockfishOracle

    oracle = StockfishOracle(
        stockfish_path=str(getattr(args, "stockfish_path", "") or ""),
        depth=int(getattr(args, "engine_depth", 15) or 15),
    )
    engine_output = oracle.analyze_game(str(getattr(game, "pgn", "") or ""))
    game_summary = enrich_summary_with_player_fields(
        dict(engine_output.get("game_summary") or {}),
        your_color=str(game.your_color),
    )
    game_summary["result"] = game.result
    payload = {
        "schema_version": int(engine_output.get("schema_version", ENGINE_PAYLOAD_SCHEMA_VERSION) or ENGINE_PAYLOAD_SCHEMA_VERSION),
        "game_summary": game_summary,
        "key_positions": list(engine_output.get("key_positions") or []),
    }
    validation = validate_engine_payload(
        payload,
        require_schema_version=True,
        require_player_fields=True,
        require_key_positions=True,
    )
    if not validation.is_valid:
        raise RuntimeError(f"Stockfish payload invariants failed: {';'.join(validation.errors)}")
    return payload


def _load_engine_payloads_for_trait_window(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    *,
    window_size: int,
) -> List[Dict[str, Any]]:
    _ = args
    return _load_recent_game_reviews_for_traits(conn, window_size)


def _compute_trait_scores_for_window(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    *,
    window_size: int,
) -> Dict[str, int]:
    return dict(_compute_trait_scores_and_window_metrics(conn, args, window_size=window_size)["scores"])


def _payload_total_moves(payload: Mapping[str, Any]) -> int:
    summary = payload.get("game_summary")
    if not isinstance(summary, Mapping):
        return 0
    player_total_plies = 0
    try:
        player_total_plies = int(summary.get("player_total_plies", 0) or 0)
    except Exception:
        player_total_plies = 0
    if player_total_plies > 0:
        return player_total_plies
    player_total_moves = 0
    try:
        player_total_moves = int(summary.get("player_total_moves", 0) or 0)
    except Exception:
        player_total_moves = 0
    if player_total_moves > 0:
        return player_total_moves
    total_moves = 0
    try:
        total_moves = int(summary.get("total_moves", 0) or 0)
    except Exception:
        total_moves = 0
    if total_moves > 0:
        return total_moves
    total_plies = 0
    try:
        total_plies = int(summary.get("total_plies", 0) or 0)
    except Exception:
        total_plies = 0
    if total_plies > 0:
        return max(1, int(round(total_plies / 2.0)))
    return 0


def _trait_confidence_from_moves(total_moves: int) -> str:
    moves = max(0, int(total_moves or 0))
    if moves >= 600:
        return "HIGH"
    if moves >= 200:
        return "MEDIUM"
    return "LOW"


def _normalized_trait_scores(raw_scores: Mapping[str, Any]) -> Dict[str, int]:
    assert isinstance(raw_scores, Mapping)
    assert all(key in raw_scores for key in TRAIT_SCORE_KEYS)
    normalized: Dict[str, int] = {}
    for key in TRAIT_SCORE_KEYS:
        normalized[key] = int(raw_scores[key])
    return normalized


def _traits_debug_enabled() -> bool:
    return str(os.environ.get("TRAITS_DEBUG", "")).strip() == "1"


def _compute_trait_scores_and_window_metrics(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    *,
    window_size: int,
) -> Dict[str, Any]:
    from src.engine_traits import compute_engine_trait_scores, get_last_aggregate_scores_after_clamp

    payloads = _load_engine_payloads_for_trait_window(conn, args, window_size=window_size)
    target_window = max(1, int(window_size))
    total_moves = sum(_payload_total_moves(payload) for payload in payloads)
    v2_payload_count = len(payloads)
    trait_scores = compute_engine_trait_scores(payloads)
    assert isinstance(trait_scores, dict)
    assert all(k in trait_scores for k in TRAIT_SCORE_KEYS)
    normalized_scores = _normalized_trait_scores(trait_scores)
    debug_scores_after_clamp = get_last_aggregate_scores_after_clamp()
    confidence = _trait_confidence_from_moves(total_moves)
    confidence_reason = ""
    if v2_payload_count < target_window:
        confidence = "LOW"
        confidence_reason = f"insufficient v2 payloads ({int(v2_payload_count)}/{int(target_window)})"
        logger.warning(
            "Trait window confidence forced LOW: insufficient v2 payloads (%s/%s).",
            int(v2_payload_count),
            int(target_window),
        )
    return {
        "scores": dict(normalized_scores),
        "scores_after_clamp": dict(debug_scores_after_clamp) if isinstance(debug_scores_after_clamp, Mapping) else None,
        "trait_window_games": int(v2_payload_count),
        "trait_window_requested_games": int(target_window),
        "trait_window_moves": max(0, int(total_moves)),
        "confidence": str(confidence),
        "confidence_reason": str(confidence_reason),
    }


def _load_latest_summary_context(conn: sqlite3.Connection) -> Dict[str, str]:
    cur = conn.execute(
        """
        SELECT
            pg.end_time,
            COALESCE(meta.player_color, '') AS player_color,
            COALESCE(meta.result, '*') AS result,
            pg.pgn_path
        FROM processed_games AS pg
        LEFT JOIN processed_game_meta AS meta ON meta.game_url = pg.game_url
        ORDER BY pg.end_time DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if row is None:
        return {
            "date_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "your_color": "unknown",
            "opponent": "unknown",
            "result": "*",
        }

    end_time = int(row[0] or int(time.time()))
    your_color = str(row[1] or "").strip().lower()
    if your_color not in {"white", "black"}:
        your_color = "unknown"
    result = str(row[2] or "*").strip() or "*"
    opponent = "unknown"

    pgn_path = Path(str(row[3] or "").strip())
    if pgn_path.exists():
        try:
            pgn_text = pgn_path.read_text(encoding="utf-8")
            white_name = _normalize_username(str(extract_pgn_header(pgn_text, "White") or ""))
            black_name = _normalize_username(str(extract_pgn_header(pgn_text, "Black") or ""))
            if your_color == "white" and black_name:
                opponent = black_name
            elif your_color == "black" and white_name:
                opponent = white_name
        except Exception:
            pass

    return {
        "date_utc": datetime.fromtimestamp(end_time, tz=timezone.utc).strftime("%Y-%m-%d"),
        "your_color": your_color,
        "opponent": opponent,
        "result": result,
    }


def _primary_weakness_from_trait_scores(trait_scores: Mapping[str, Any]) -> Dict[str, Any]:
    labels = {
        "tactical_awareness": "Tactical Awareness",
        "material_discipline": "Material Discipline",
        "conversion_ability": "Conversion Ability",
        "defensive_resilience": "Defensive Resilience",
        "blunder_frequency": "Blunder Frequency",
    }
    scores = _normalized_trait_scores(trait_scores)
    scored = [(key, int(scores[key])) for key in TRAIT_SCORE_KEYS]
    scored.sort(key=lambda item: (item[1], TRAIT_SCORE_KEYS.index(item[0])))
    weakest_key, weakest_score = scored[0]
    return {
        "trait_key": weakest_key,
        "trait_name": labels[weakest_key],
        "score": weakest_score,
        "reason": f"Lowest deterministic engine-derived score in the rolling window ({weakest_score}/100).",
    }


def _summary_state(conn: sqlite3.Connection) -> Tuple[int, Optional[int]]:
    cur = conn.execute(
        "SELECT last_summary_processed_count, last_summary_end_time FROM summary_state WHERE id = 1"
    )
    row = cur.fetchone()
    if row is None:
        conn.execute(
            """
            INSERT OR IGNORE INTO summary_state
              (id, last_summary_processed_count, last_summary_end_time, updated_at)
            VALUES (1, 0, NULL, ?)
            """,
            (int(time.time()),),
        )
        conn.commit()
        return 0, None
    return int(row[0] or 0), int(row[1]) if row[1] is not None else None


def _set_summary_state(conn: sqlite3.Connection, processed_count: int, last_end_time: int) -> None:
    conn.execute(
        """
        UPDATE summary_state
        SET last_summary_processed_count = ?, last_summary_end_time = ?, updated_at = ?
        WHERE id = 1
        """,
        (int(processed_count), int(last_end_time), int(time.time())),
    )
    conn.commit()


def _build_performance_summary(
    recent_meta: List[Tuple[int, str, str, Optional[int], str]],
) -> Dict[str, Any]:
    rows = [(str(result), str(color)) for _, result, color, _, _ in recent_meta]
    wins, losses, draws = _performance_counts_from_rows(rows)
    total_games = wins + losses + draws
    if total_games <= 0:
        return {
            "total_games": 0,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "win_pct": 0.0,
            "loss_pct": 0.0,
            "draw_pct": 0.0,
        }

    return {
        "total_games": total_games,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "win_pct": round(wins / total_games * 100, 1),
        "loss_pct": round(losses / total_games * 100, 1),
        "draw_pct": round(draws / total_games * 100, 1),
    }


def _build_player_summary_prompt(
    *,
    summary_context: Mapping[str, Any],
    performance_summary: Mapping[str, Any],
    trait_scores: Mapping[str, Any],
    trait_window: Mapping[str, Any],
    primary_weakness: Mapping[str, Any],
) -> Tuple[str, str]:
    system_msg = (
        "You are a strict chess summary formatter. "
        "Use only provided JSON values and return Markdown in the exact requested structure."
    )
    normalized_trait_scores = _normalized_trait_scores(trait_scores)
    summary_context_json = json.dumps(
        {
            "date_utc": str(summary_context.get("date_utc", "unknown") or "unknown"),
            "your_color": str(summary_context.get("your_color", "unknown") or "unknown"),
            "opponent": str(summary_context.get("opponent", "unknown") or "unknown"),
            "result": str(summary_context.get("result", "*") or "*"),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    performance_summary_json = json.dumps(
        {
            "total_games": int(performance_summary.get("total_games", 0) or 0),
            "wins": int(performance_summary.get("wins", 0) or 0),
            "losses": int(performance_summary.get("losses", 0) or 0),
            "draws": int(performance_summary.get("draws", 0) or 0),
            "win_pct": float(performance_summary.get("win_pct", 0.0) or 0.0),
            "loss_pct": float(performance_summary.get("loss_pct", 0.0) or 0.0),
            "draw_pct": float(performance_summary.get("draw_pct", 0.0) or 0.0),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    trait_scores_json = json.dumps(
        {
            "tactical_awareness": int(normalized_trait_scores["tactical_awareness"]),
            "material_discipline": int(normalized_trait_scores["material_discipline"]),
            "conversion_ability": int(normalized_trait_scores["conversion_ability"]),
            "defensive_resilience": int(normalized_trait_scores["defensive_resilience"]),
            "blunder_frequency": int(normalized_trait_scores["blunder_frequency"]),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    trait_window_json = json.dumps(
        {
            "trait_window_games": int(trait_window.get("trait_window_games", 0) or 0),
            "trait_window_moves": int(trait_window.get("trait_window_moves", 0) or 0),
            "confidence": str(trait_window.get("confidence", "LOW") or "LOW"),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    primary_weakness_json = json.dumps(
        {
            "trait_name": str(primary_weakness.get("trait_name", "Unknown trait") or "Unknown trait"),
            "score": int(primary_weakness["score"]),
            "reason": str(primary_weakness.get("reason", "") or ""),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )

    user_msg = f"""Format the deterministic player summary.

Authoritative summary_context JSON (use exact values):
```json
{summary_context_json}
```

Authoritative performance_summary JSON (use exact values):
```json
{performance_summary_json}
```

Authoritative trait_scores JSON (use exact values):
```json
{trait_scores_json}
```

Authoritative trait_window JSON (use exact values):
```json
{trait_window_json}
```

Authoritative primary_weakness JSON (use exact values):
```json
{primary_weakness_json}
```

Output constraints:
- Return Markdown only.
- Use exactly this YAML + section structure and headings.
- Do not add any headings besides:
  - `## Snapshot`
  - `## Engine-Derived Traits`
  - `## Primary Weaknesses`
  - `## Training Priority`
- Do not compute, infer, or recompute any metric.
- Do not do arithmetic, percentages, ranking, or score derivation.
- Copy numeric values exactly from provided JSON.

Use this exact template:

---
date_utc: <summary_context.date_utc>
your_color: <summary_context.your_color>
opponent: <summary_context.opponent>
result: <summary_context.result>
win_pct: <performance_summary.win_pct>
loss_pct: <performance_summary.loss_pct>
draw_pct: <performance_summary.draw_pct>
trait_window_games: <trait_window.trait_window_games>
trait_window_moves: <trait_window.trait_window_moves>
confidence: <trait_window.confidence>
---

## Snapshot
- Total games: <performance_summary.total_games>
- Record: <performance_summary.wins>–<performance_summary.losses>–<performance_summary.draws>
- Win rate: <performance_summary.win_pct>%
- Trait window games: <trait_window.trait_window_games>
- Trait window moves analyzed: <trait_window.trait_window_moves>
- Confidence: <trait_window.confidence>

## Engine-Derived Traits
- Tactical Awareness: <trait_scores.tactical_awareness>
- Material Discipline: <trait_scores.material_discipline>
- Conversion Ability: <trait_scores.conversion_ability>
- Defensive Resilience: <trait_scores.defensive_resilience>
- Blunder Frequency: <trait_scores.blunder_frequency>

## Primary Weaknesses
- <primary_weakness.trait_name>: <primary_weakness.reason>

## Training Priority
- <action 1>
- <action 2>
- <action 3>
"""
    return system_msg, user_msg


def _generate_player_summary_markdown(
    args: argparse.Namespace,
    *,
    processed_count: int,
    cadence: int,
    stats_path: Path,
    recent_meta: List[Tuple[int, str, str, Optional[int], str]],
    trait_scores: Mapping[str, Any],
    trait_window_size: int,
    trait_window_moves: int,
    trait_confidence: str,
    summary_context: Mapping[str, Any],
) -> str:
    _ = processed_count
    _ = cadence
    _ = stats_path
    _ = recent_meta
    _ = trait_window_size
    trait_scores = _normalized_trait_scores(trait_scores)
    performance_summary = _build_performance_summary(recent_meta)
    primary_weakness = _primary_weakness_from_trait_scores(trait_scores)
    trait_window = {
        "trait_window_games": int(trait_window_size),
        "trait_window_moves": int(trait_window_moves),
        "confidence": str(trait_confidence),
    }
    system_msg, user_msg = _build_player_summary_prompt(
        summary_context=summary_context,
        performance_summary=performance_summary,
        trait_scores=trait_scores,
        trait_window=trait_window,
        primary_weakness=primary_weakness,
    )
    provider = get_provider()
    if provider == "gpt":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set but --provider gpt was selected.")
        return call_openai_chat(
            api_key=api_key,
            model=args.gpt_model,
            system_msg=system_msg,
            user_msg=user_msg,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
        )
    return call_ollama_generate(
        base_url=args.ollama_url,
        model=args.ollama_model,
        system_msg=system_msg,
        user_msg=user_msg,
        timeout=args.timeout,
    )


def _maybe_generate_player_summary(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    *,
    latest_game: GameInfo,
    stats_path: Path,
) -> Optional[Path]:
    cadence = max(1, int(args.player_summary_every_n))
    cur = conn.execute("SELECT COUNT(*) FROM processed_games")
    processed_count = int(cur.fetchone()[0] or 0)
    last_summary_count, _last_summary_end_time = _summary_state(conn)
    if processed_count == 0 or processed_count % cadence != 0:
        return None
    if last_summary_count >= processed_count:
        return None

    recent_meta = _load_recent_game_meta_for_summary(conn, cadence)
    trait_window_size = max(1, int(getattr(args, "player_trait_window", 20) or 20))
    trait_window_metrics = _compute_trait_scores_and_window_metrics(
        conn,
        args,
        window_size=trait_window_size,
    )
    trait_scores = _normalized_trait_scores(trait_window_metrics.get("scores") or {})
    trait_window_games = int(trait_window_metrics.get("trait_window_games", 0) or 0)
    trait_window_moves = int(trait_window_metrics.get("trait_window_moves", 0) or 0)
    trait_confidence = str(trait_window_metrics.get("confidence", "LOW") or "LOW")
    trait_confidence_reason = str(trait_window_metrics.get("confidence_reason", "") or "").strip()
    if trait_confidence_reason:
        logger.warning("Player summary trait window note: %s", trait_confidence_reason)
    summary_context = _load_latest_summary_context(conn)
    summary_md = _generate_player_summary_markdown(
        args,
        processed_count=processed_count,
        cadence=cadence,
        stats_path=stats_path,
        recent_meta=recent_meta,
        trait_scores=trait_scores,
        trait_window_size=trait_window_games,
        trait_window_moves=trait_window_moves,
        trait_confidence=trait_confidence,
        summary_context=summary_context,
    )
    summary_path = args.out / "player_summary.md"
    write_text(summary_path, summary_md)
    _set_summary_state(conn, processed_count, latest_game.end_time)

    if getattr(args, "telegram_bot_token", None) and getattr(args, "telegram_chat_id", None):
        try:
            send_telegram_document(
                bot_token=args.telegram_bot_token,
                chat_id=args.telegram_chat_id,
                file_path=summary_path,
                caption=f"Player summary ready — last {processed_count} games",
                timeout=args.timeout,
                disable_notification=getattr(args, "telegram_disable_notification", False),
            )
        except Exception as e:
            print(f"[warn] Telegram summary notification failed: {e}", file=sys.stderr)

    return summary_path


# -----------------------------
# Chess.com API
# -----------------------------
class ChessComError(RuntimeError):
    pass


def _http_get_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _month_range_for_lookback(now_utc: datetime, lookback_days: int) -> List[Tuple[int, int]]:
    """Return list of (year, month) covering now-lookback_days through now, inclusive."""
    start = now_utc - timedelta(days=lookback_days)

    months: List[Tuple[int, int]] = []
    y, m = start.year, start.month
    end_y, end_m = now_utc.year, now_utc.month

    while (y, m) <= (end_y, end_m):
        months.append((y, m))
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    return months


def fetch_recent_games(username: str, lookback_days: int) -> List[Dict[str, Any]]:
    now_utc = datetime.now(timezone.utc)
    months = _month_range_for_lookback(now_utc, lookback_days)

    games: List[Dict[str, Any]] = []
    for y, m in months:
        url = CHESSCOM_GAMES_URL.format(username=username.lower(), year=y, month=m)
        try:
            data = _http_get_json(url)
            month_games = data.get("games", [])
            if isinstance(month_games, list):
                games.extend(month_games)
        except requests.HTTPError as e:
            # 404 is normal if no games that month
            if getattr(e.response, "status_code", None) == 404:
                continue
            raise ChessComError(f"Chess.com API error for {url}: {e}")
    return games


def _fetch_backfill_candidates(
    *,
    username: str,
    lookback_days: int,
    rules_filter: Optional[str],
    target_valid_games: int,
) -> Tuple[List[GameInfo], int]:
    """
    Fetch candidate games for backfill, stopping once enough valid games are collected.

    Returns:
      - parsed candidate games passing username/rules filters
      - number of valid games considered before final top-N selection
    """
    now_utc = datetime.now(timezone.utc)
    months = _month_range_for_lookback(now_utc, lookback_days)
    valid_games_target = max(1, int(target_valid_games))

    parsed: List[GameInfo] = []
    considered_count = 0
    for y, m in reversed(months):
        url = CHESSCOM_GAMES_URL.format(username=username.lower(), year=y, month=m)
        try:
            data = _http_get_json(url)
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) == 404:
                continue
            raise ChessComError(f"Chess.com API error for {url}: {e}")

        month_games = data.get("games", [])
        if not isinstance(month_games, list):
            continue

        for raw in month_games:
            game = parse_game(raw, username)
            if game is None:
                continue
            if rules_filter and game.rules != rules_filter:
                continue
            parsed.append(game)
            considered_count += 1

        # Stop only after finishing the current month to keep month-level determinism.
        if considered_count >= valid_games_target:
            break

    return parsed, considered_count


def _normalize_username(u: str) -> str:
    return (u or "").strip().lower()


PGN_HEADER_RE = re.compile(r'^\[(?P<key>[A-Za-z0-9_]+)\s+"(?P<val>.*)"\]\s*$')


def extract_pgn_header(pgn: str, key: str) -> Optional[str]:
    wanted = key.strip()
    for line in pgn.splitlines():
        m = PGN_HEADER_RE.match(line.strip())
        if not m:
            # headers end at first non-header line
            if line.strip() and not line.startswith("["):
                break
            continue
        if m.group("key") == wanted:
            return m.group("val")
    return None


def parse_game(raw: Dict[str, Any], username: str) -> Optional[GameInfo]:
    game_url = raw.get("url") or ""
    pgn = raw.get("pgn") or ""
    end_time = raw.get("end_time")
    if not game_url or not pgn or not isinstance(end_time, int):
        return None

    rules = raw.get("rules") or "chess"
    time_control = raw.get("time_control") or ""
    rated = bool(raw.get("rated", False))

    white = raw.get("white") or {}
    black = raw.get("black") or {}

    white_u = _normalize_username(white.get("username", ""))
    black_u = _normalize_username(black.get("username", ""))
    if not white_u or not black_u:
        return None

    user_u = _normalize_username(username)
    if user_u not in (white_u, black_u):
        return None

    your_color = "white" if user_u == white_u else "black"
    opponent = black_u if your_color == "white" else white_u

    def _maybe_int(x: Any) -> Optional[int]:
        try:
            return int(x)
        except Exception:
            return None

    white_r = _maybe_int(white.get("rating"))
    black_r = _maybe_int(black.get("rating"))

    result = extract_pgn_header(pgn, "Result") or "*"

    return GameInfo(
        game_url=game_url,
        pgn=pgn,
        end_time=end_time,
        time_control=time_control,
        rated=rated,
        rules=rules,
        white_username=white_u,
        black_username=black_u,
        white_rating=white_r,
        black_rating=black_r,
        result=result,
        your_color=your_color,
        opponent=opponent,
    )


def safe_filename(s: str, max_len: int = 60) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len] if len(s) > max_len else s


def short_id_from_url(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return h[:10]


# -----------------------------
# LLM providers
# -----------------------------
class LLMError(RuntimeError):
    pass


def call_openai_chat(
    api_key: str,
    model: str,
    system_msg: str,
    user_msg: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_tokens: int = 1400,
) -> str:
    """OpenAI Chat Completions REST call."""
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    payload = {
        "model": model,
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
    }

    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout)
    if resp.status_code >= 400:
        raise LLMError(f"OpenAI API error {resp.status_code}: {resp.text[:800]}")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise LLMError(f"Unexpected OpenAI response shape: {str(data)[:800]}")


def call_ollama_generate(
    base_url: str,
    model: str,
    system_msg: str,
    user_msg: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Ollama /api/generate call (non-streaming)."""
    url = base_url.rstrip("/") + "/api/generate"
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}

    # Some Ollama models support system prompts via the prompt itself; keep it simple.
    prompt = f"{system_msg}\n\n{user_msg}"

    payload = {"model": model, "prompt": prompt, "stream": False}
    resp = requests.post(
        url,
        headers=headers,
        data=json.dumps(payload),
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise LLMError(f"Ollama error {resp.status_code}: {resp.text[:800]}")

    data = resp.json()
    if "response" not in data:
        raise LLMError(f"Unexpected Ollama response: {str(data)[:800]}")
    return data["response"]


# -----------------------------
# Output / indexing
# -----------------------------
def ensure_dirs(out_dir: Path) -> None:
    (out_dir / "md").mkdir(parents=True, exist_ok=True)
    (out_dir / "pgn").mkdir(parents=True, exist_ok=True)
    (out_dir / "logs").mkdir(parents=True, exist_ok=True)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_pgn_once(pgn_path: Path, pgn: str) -> None:
    if pgn_path.exists():
        logger.debug("PGN already exists, skipping write: %s", pgn_path)
        return
    pgn_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(pgn_path, "x", encoding="utf-8") as file_handle:
            file_handle.write(pgn)
    except FileExistsError:
        logger.debug("PGN already exists, skipping write: %s", pgn_path)


def format_local_dt(dt_utc: datetime, tz_name: str) -> datetime:
    if not tz_name or ZoneInfo is None:
        return dt_utc
    try:
        return dt_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        return dt_utc


def build_paths(out_dir: Path, game: GameInfo, tz_name: str) -> Tuple[Path, Path]:
    dt_local = format_local_dt(game.end_dt_utc, tz_name)
    stamp = dt_local.strftime("%Y-%m-%d_%H%M%S")
    gid = short_id_from_url(game.game_url)

    opp = safe_filename(game.opponent)
    color = safe_filename(game.your_color)

    # sanitize result for filenames
    result = game.result.replace("/", "half")
    result = safe_filename(result)

    base = f"{stamp}_{color}_vs_{opp}_{result}_{gid}"
    md_path = out_dir / "md" / f"{base}.md"
    pgn_path = out_dir / "pgn" / f"{base}.pgn"
    return md_path, pgn_path


def compute_content_hash(game: GameInfo, provider: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(game.game_url.encode("utf-8"))
    h.update(str(game.end_time).encode("utf-8"))
    h.update(provider.encode("utf-8"))
    h.update(model.encode("utf-8"))
    h.update(hashlib.sha256(game.pgn.encode("utf-8")).digest())
    return h.hexdigest()


def update_index(out_dir: Path, limit: int = 5000) -> None:
    md_dir = out_dir / "md"
    files = sorted(md_dir.glob("*.md"), key=lambda p: p.name, reverse=True)

    lines: List[str] = ["# Chess Game Reviews", ""]
    for p in files[:limit]:
        rel = p.relative_to(out_dir)
        title = p.stem
        lines.append(f"- [{title}]({rel.as_posix()})")

    lines.append("")
    write_text(out_dir / "index.md", "\n".join(lines))


# -----------------------------
# Main processing loop
# -----------------------------
def process_game(conn: sqlite3.Connection, args: argparse.Namespace, game: GameInfo) -> Optional[Path]:
    if is_processed(conn, game.game_url):
        return None

    ensure_dirs(args.out)
    md_path, pgn_path = build_paths(args.out, game, args.timezone)

    # Always archive raw PGN first.
    write_pgn_once(pgn_path, game.pgn)

    provider = get_provider()
    used_model = args.ollama_model if provider == "ollama" else args.gpt_model
    try:
        review_md = run_analysis_pipeline(
            game=game,
            args=args,
            llm_runner=lambda system_msg, user_msg: _call_selected_llm_backend(
                args=args,
                system_msg=system_msg,
                user_msg=user_msg,
            ),
            logger=logger,
        )
    except Exception as exc:
        logger.error("Stockfish failure: %s", exc)
        try:
            send_telegram_message(
                f"❌ Engine failure for {game.game_url}\nStockfish could not produce analysis.\nReason: {str(exc)[:300]}",
                bot_token=str(getattr(args, "telegram_bot_token", "") or ""),
                chat_id=str(getattr(args, "telegram_chat_id", "") or ""),
                timeout=int(getattr(args, "timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT),
                disable_notification=bool(getattr(args, "telegram_disable_notification", False)),
            )
        except Exception as notify_exc:
            logger.error("Telegram engine-failure notification failed: %s", notify_exc)
        return None

    write_text(md_path, review_md)

    # Optional: Telegram notification with the generated Markdown attached
    if getattr(args, "telegram_bot_token", None) and getattr(args, "telegram_chat_id", None):
        try:
            caption = build_telegram_caption(game)
            send_telegram_document(
                bot_token=args.telegram_bot_token,
                chat_id=args.telegram_chat_id,
                file_path=md_path,
                caption=caption,
                timeout=args.timeout,
                disable_notification=getattr(args, "telegram_disable_notification", False),
            )
        except Exception as e:
            # Do not fail the whole pipeline if Telegram is down/misconfigured.
            print(f"[warn] Telegram notification failed: {e}", file=sys.stderr)

    h = compute_content_hash(game, provider, used_model)
    mark_processed(
        conn=conn,
        game_url=game.game_url,
        end_time=game.end_time,
        md_path=md_path,
        pgn_path=pgn_path,
        provider=provider,
        model=used_model,
        content_hash=h,
    )
    _record_processed_game_meta(conn, game)

    runtime_sync = sync_game_record_and_traits(
        player_username=args.username,
        game_payload={
            "game_url": game.game_url,
            "pgn": game.pgn,
            "end_time": game.end_time,
            "time_control": game.time_control,
            "rated": game.rated,
            "rules": game.rules,
            "result": game.result,
            "white_username": game.white_username,
            "black_username": game.black_username,
            "white_rating": game.white_rating,
            "black_rating": game.black_rating,
            "player_color": game.your_color,
        },
    )
    if not runtime_sync.get("available"):
        logger.debug("Runtime Postgres sync skipped: %s", runtime_sync.get("reason"))

    record_player_rating_for_game(
        player_username=args.username,
        game_url=game.game_url,
        end_time=game.end_dt_utc,
        player_color=game.your_color,
        pgn=game.pgn,
        time_control=game.time_control,
        rated=game.rated,
    )
    stats_path = _write_player_stats_markdown(
        conn,
        out_dir=args.out,
        username=args.username,
        recent_games=max(20, int(args.player_summary_every_n)),
    )
    try:
        _maybe_generate_player_summary(conn, args, latest_game=game, stats_path=stats_path)
    except Exception as e:
        # Summary generation is best effort and must not block per-game review output.
        print(f"[warn] Player summary generation failed: {e}", file=sys.stderr)

    if args.update_index:
        update_index(args.out)

    return md_path


def _select_backfill_games(args: argparse.Namespace) -> Tuple[List[GameInfo], int]:
    lookback_days = max(int(getattr(args, "lookback_days", 10) or 10), 3650)
    backfill_n = max(1, int(getattr(args, "backfill", 0) or 0))
    scan_target = backfill_n + 25
    parsed, considered_count = _fetch_backfill_candidates(
        username=str(args.username),
        lookback_days=lookback_days,
        rules_filter=str(args.rules_filter) if getattr(args, "rules_filter", None) else None,
        target_valid_games=scan_target,
    )
    parsed.sort(key=lambda g: (g.end_time, g.game_url), reverse=True)
    return parsed[:backfill_n], considered_count


def run_backfill(conn: sqlite3.Connection, args: argparse.Namespace) -> Dict[str, Any]:
    requested_count = max(1, int(getattr(args, "backfill", 0) or 0))
    if requested_count > 200:
        raise ValueError("Backfill limit exceeded: max 200 games at once.")

    games, fetched_count = _select_backfill_games(args)
    processed_games = 0
    engine_analyses = 0
    stale_payloads_rederived = 0
    rebuild_payloads = bool(getattr(args, "rebuild_payloads", False))

    for game in games:
        payload_exists = _engine_payload_exists(conn, game.game_url)
        payload_reusable = (not rebuild_payloads) and _stored_engine_payload_is_reusable(conn, game.game_url)
        if payload_reusable:
            _upsert_backfill_processed_records(conn, game=game, args=args)
            processed_games += 1
            continue
        if payload_exists:
            stale_payloads_rederived += 1
        payload = _analyze_game_with_stockfish(game=game, args=args)
        inserted = _store_engine_payload(
            conn,
            game_url=game.game_url,
            end_time=game.end_time,
            engine_depth=int(getattr(args, "engine_depth", 15) or 15),
            payload=payload,
        )
        _upsert_backfill_processed_records(conn, game=game, args=args)
        processed_games += 1
        engine_analyses += 1
        if not inserted:
            logger.debug("Engine payload already present with identical content for %s", game.game_url)

    trait_scores = _compute_trait_scores_for_window(
        conn,
        args,
        window_size=max(1, int(getattr(args, "player_trait_window", 20) or 20)),
    )
    assert isinstance(trait_scores, dict)
    assert all(k in trait_scores for k in TRAIT_SCORE_KEYS)
    return {
        "total_games_requested": requested_count,
        "games_fetched_from_chess_com": fetched_count,
        "games_analyzed_with_stockfish": engine_analyses,
        "games_processed": processed_games,
        "engine_analyses": engine_analyses,
        "stale_payloads_rederived": stale_payloads_rederived,
        "trait_scores": trait_scores,
    }


def _print_backfill_summary(result: Mapping[str, Any]) -> None:
    scores = _normalized_trait_scores(result.get("trait_scores") or {})
    if _traits_debug_enabled():
        from src.engine_traits import get_last_traits_debug_aggregate

        aggregate = get_last_traits_debug_aggregate()
        if not isinstance(aggregate, Mapping):
            raise RuntimeError("Trait propagation mismatch detected")
        expected_raw = aggregate.get("scores_after_clamp")
        if not isinstance(expected_raw, Mapping):
            raise RuntimeError("Trait propagation mismatch detected")
        expected = _normalized_trait_scores(expected_raw)
        if scores != expected:
            raise RuntimeError("Trait propagation mismatch detected")
    print("Backfill Summary:")
    print(f"- total games requested: {int(result.get('total_games_requested', 0) or 0)}")
    print(f"- games fetched from chess.com: {int(result.get('games_fetched_from_chess_com', 0) or 0)}")
    print(f"- games analyzed with Stockfish: {int(result.get('games_analyzed_with_stockfish', 0) or 0)}")
    print("- traits (post-backfill):")
    print(f"  tactical_awareness: {int(scores['tactical_awareness'])}")
    print(f"  material_discipline: {int(scores['material_discipline'])}")
    print(f"  conversion_ability: {int(scores['conversion_ability'])}")
    print(f"  defensive_resilience: {int(scores['defensive_resilience'])}")
    print(f"  blunder_frequency: {int(scores['blunder_frequency'])}")


def poll_once(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    raw_games = fetch_recent_games(args.username, args.lookback_days)

    parsed: List[GameInfo] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.lookback_days)

    for rg in raw_games:
        gi = parse_game(rg, args.username)
        if gi is None:
            continue

        if args.rules_filter and gi.rules != args.rules_filter:
            continue

        if gi.end_dt_utc < cutoff:
            continue

        parsed.append(gi)

    parsed.sort(key=lambda g: g.end_time)

    created = 0
    for g in parsed:
        if is_processed(conn, g.game_url):
            continue

        if args.dry_run:
            print(f"[dry-run] Would process: {g.end_dt_utc.isoformat()} {g.game_url}")
            created += 1
            continue

        last_err: Optional[Exception] = None
        for attempt in range(1, args.retries + 1):
            try:
                out = process_game(conn, args, g)
                if out:
                    print(f"[ok] Wrote: {out}")
                    created += 1
                last_err = None
                break
            except Exception as e:
                last_err = e
                print(
                    f"[warn] attempt {attempt}/{args.retries} failed for {g.game_url}: {e}",
                    file=sys.stderr,
                )
                time.sleep(min(3 * attempt, 12))

        if last_err is not None:
            print(f"[error] Giving up on {g.game_url}: {last_err}", file=sys.stderr)

    return created


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _call_selected_llm_backend(*, args: argparse.Namespace, system_msg: str, user_msg: str) -> str:
    provider = get_provider()
    if provider == "gpt":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            logger.error("OPENAI_API_KEY is missing while provider=gpt.")
            raise RuntimeError("OPENAI_API_KEY is not set but --provider gpt was selected.")
        return call_openai_chat(
            api_key=api_key,
            model=args.gpt_model,
            system_msg=system_msg,
            user_msg=user_msg,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
        )

    if not str(getattr(args, "ollama_url", "") or "").strip():
        logger.error("OLLAMA_URL is missing while provider=ollama.")
        raise RuntimeError("OLLAMA_URL is not set but --provider ollama was selected.")

    _validate_ollama_endpoint(base_url=str(args.ollama_url), timeout=int(args.timeout))
    return call_ollama_generate(
        base_url=args.ollama_url,
        model=args.ollama_model,
        system_msg=system_msg,
        user_msg=user_msg,
        timeout=args.timeout,
    )


def _running_in_docker() -> bool:
    return Path("/.dockerenv").exists()


def _is_loopback_ollama_url(url: str) -> bool:
    lowered = (url or "").strip().lower()
    return lowered.startswith("http://127.0.0.1") or lowered.startswith("https://127.0.0.1") or lowered.startswith("http://localhost") or lowered.startswith("https://localhost")


def _resolve_ollama_url_for_runtime(raw_url: str) -> str:
    cleaned = (raw_url or "").strip()
    if not cleaned:
        return ""
    if _running_in_docker() and _is_loopback_ollama_url(cleaned):
        return "http://host.docker.internal:11434"
    return cleaned


def _validate_ollama_endpoint(*, base_url: str, timeout: int) -> None:
    url = base_url.rstrip("/") + "/api/tags"
    try:
        resp = requests.get(url, timeout=max(1, timeout))
    except Exception as exc:
        logger.error("OLLAMA_URL is unreachable: %s (%s)", base_url, exc)
        raise RuntimeError(f"OLLAMA_URL is unreachable: {base_url}") from exc
    if resp.status_code >= 400:
        logger.error("OLLAMA_URL health check failed: %s -> HTTP %s", base_url, resp.status_code)
        raise RuntimeError(f"OLLAMA_URL health check failed: {base_url} (HTTP {resp.status_code})")


def _apply_provider_runtime_fallback(args: argparse.Namespace) -> None:
    args.provider = str(getattr(args, "provider", "ollama") or "ollama").strip().lower()
    if args.provider != "ollama":
        return
    if str(getattr(args, "ollama_url", "") or "").strip():
        return
    logger.warning("PROVIDER=ollama but OLLAMA_URL is not configured; falling back to provider=gpt.")
    args.provider = "gpt"


def _sync_runtime_provider(args: argparse.Namespace) -> str:
    runtime_provider = str(get_provider() or "ollama").strip().lower()
    if runtime_provider not in {"gpt", "ollama"}:
        logger.warning("Invalid runtime provider '%s'; defaulting to ollama.", runtime_provider)
        runtime_provider = "ollama"
        set_provider(runtime_provider)
    previous_provider = str(getattr(args, "provider", "") or "").strip().lower()
    args.provider = runtime_provider
    if args.provider == "ollama":
        args.ollama_url = _resolve_ollama_url_for_runtime(str(args.ollama_url))
        _apply_provider_runtime_fallback(args)
        if args.provider != runtime_provider:
            set_provider(args.provider)
    if previous_provider != args.provider:
        logger.info("Active LLM Provider: %s", args.provider)
    return args.provider


def _telegram_command_loop(args: argparse.Namespace, stop_event: threading.Event) -> None:
    conn = init_db(Path(args.state_db))
    try:
        while not stop_event.is_set():
            try:
                poll_telegram_commands(conn, args)
            except Exception as e:
                logger.debug("Telegram command polling failed: %s", e, exc_info=True)
            stop_event.wait(2.5)
    finally:
        conn.close()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Chess.com game poller -> LLM coach notes -> Markdown files")
    output_root: Optional[Path]
    try:
        output_root = get_output_root()
    except KeyError:
        output_root = None

    p.add_argument(
        "--username",
        default=os.environ.get("CHESS_USERNAME", ""),
        help="Chess.com username (or env CHESS_USERNAME).",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=output_root,
        help="Output directory (defaults to CHESS_OUTPUT_DIR if set).",
    )
    p.add_argument(
        "--state-db",
        type=Path,
        default=os.environ.get("STATE_DB", "/data/state.sqlite"),
        help="Path to the SQLite state database (default from STATE_DB env or /data/state.sqlite)",
    )

    p.add_argument(
        "--provider",
        choices=["gpt", "ollama"],
        default=os.environ.get("PROVIDER", "ollama"),
        help="LLM provider: gpt (OpenAI) or ollama (local).",
    )
    p.add_argument("--gpt-model", default="gpt-4o-mini", help="OpenAI model (provider=gpt)")
    p.add_argument("--max-tokens", type=int, default=1400, help="Max tokens for OpenAI responses")

    p.add_argument(
        "--ollama-url",
        default=os.environ.get("OLLAMA_URL", ""),
        help="Ollama base URL",
    )
    p.add_argument(
        "--ollama-model",
        default=os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
        help="Ollama model name",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=_env_int("LLM_TIMEOUT", 120),
        help="LLM request timeout in seconds (env LLM_TIMEOUT, default 120).",
    )
    p.add_argument(
        "--enable-engine",
        dest="enable_engine",
        action="store_true",
        default=_env_bool("ENABLE_ENGINE", True),
        help="Enable deterministic Stockfish oracle analysis (env ENABLE_ENGINE, default true).",
    )
    p.add_argument(
        "--disable-engine",
        dest="enable_engine",
        action="store_false",
        help="Disable Stockfish engine (rejected at runtime in strict mode).",
    )
    p.add_argument(
        "--stockfish-path",
        default=os.environ.get("STOCKFISH_PATH", "/usr/bin/stockfish"),
        help="Path to Stockfish binary (env STOCKFISH_PATH).",
    )
    p.add_argument(
        "--engine-depth",
        type=int,
        default=_env_int("ENGINE_DEPTH", 15),
        help="Stockfish analysis depth (env ENGINE_DEPTH, default 15).",
    )

    p.add_argument("--poll-seconds", type=int, default=300, help="Polling interval (seconds)")
    p.add_argument("--once", action="store_true", help="Run one poll cycle then exit")
    p.add_argument(
        "--backfill",
        type=int,
        default=0,
        help="Backfill last N games with strict Stockfish engine payloads only (no LLM/polling/Telegram).",
    )
    p.add_argument(
        "--rebuild-payloads",
        "--backfill-reset",
        dest="rebuild_payloads",
        action="store_true",
        help="Force re-analysis and overwrite engine payloads during backfill (default keeps idempotent reuse).",
    )
    p.add_argument("--lookback-days", type=int, default=10, help="How far back to scan for unprocessed games")

    p.add_argument(
        "--rules-filter",
        default="chess",
        help="Only process games matching this rules value (e.g., chess, chess960). Set empty string to disable.",
    )

    p.add_argument("--timezone", default="America/Chicago", help="Timezone for filenames (IANA)")

    p.add_argument("--retries", type=int, default=3, help="Retries per game when LLM/network fails")

    p.add_argument("--update-index", action="store_true", help="Update index.md after new reviews")
    p.add_argument("--dry-run", action="store_true", help="Detect new games but do not call LLM or write files")
    p.add_argument(
        "--player-summary-every-n",
        type=int,
        default=_env_int("PLAYER_SUMMARY_EVERY_N", 20),
        help="Generate player_summary.md every N newly processed games (env PLAYER_SUMMARY_EVERY_N, default 20).",
    )
    p.add_argument(
        "--player-trait-window",
        type=int,
        default=_env_int("PLAYER_TRAIT_WINDOW", 20),
        help="Rolling window size used to recompute deterministic engine traits for player summary (env PLAYER_TRAIT_WINDOW, default 20).",
    )
    p.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Disable first-run Postgres bootstrap seeding.",
    )
    p.add_argument(
        "--bootstrap-games",
        type=int,
        default=None,
        help="Number of games to bootstrap on first run (overrides CHESS_BOOTSTRAP_GAMES).",
    )

    # Telegram notifications (optional)
    p.add_argument(
        "--telegram-bot-token",
        default=os.environ.get("TG_BOT_TOKEN", ""),
        help="Telegram bot token (or env TG_BOT_TOKEN).",
    )
    p.add_argument(
        "--telegram-chat-id",
        default=os.environ.get("TG_CHAT_ID", ""),
        help="Telegram chat_id (or env TG_CHAT_ID).",
    )
    p.add_argument(
        "--telegram-disable-notification",
        action="store_true",
        help="Send Telegram message silently (no notification sound).",
    )
    p.add_argument(
        "command",
        nargs="?",
        choices=list_command_names(),
        help="Run a command and exit (status, summary, stats, health, help).",
    )

    args = p.parse_args(argv)

    if not args.username:
        p.error("the following arguments are required: --username (or set CHESS_USERNAME)")
    if args.out is None:
        p.error("CHESS_OUTPUT_DIR is not set; set it or pass --out explicitly.")
    if args.bootstrap_games is not None and args.bootstrap_games <= 0:
        p.error("--bootstrap-games must be > 0.")
    if args.player_summary_every_n <= 0:
        p.error("--player-summary-every-n must be > 0.")
    if args.player_trait_window <= 0:
        p.error("--player-trait-window must be > 0.")
    if args.engine_depth <= 0:
        p.error("--engine-depth must be > 0.")
    if args.timeout <= 0:
        p.error("--timeout must be > 0.")
    if args.backfill < 0:
        p.error("--backfill must be >= 0.")

    # Normalize rules filter: allow disabling with empty string
    if args.rules_filter is not None:
        args.rules_filter = args.rules_filter.strip()
        if args.rules_filter == "":
            args.rules_filter = None

    return args


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    if not bool(getattr(args, "enable_engine", False)):
        raise RuntimeError("Engine cannot be disabled in strict mode.")
    args.out = Path(args.out)
    args.state_db = Path(args.state_db)

    if int(getattr(args, "backfill", 0) or 0) > 0:
        logger.info("Running backfill mode for last %d games.", int(args.backfill))
        conn = init_db(args.state_db)
        try:
            try:
                result = run_backfill(conn, args)
            except Exception as exc:
                logger.error("Backfill failed: %s", exc)
                return 1
            _print_backfill_summary(result)
            return 0
        finally:
            conn.close()
            close_ingest_db_check()

    if args.provider == "ollama":
        args.ollama_url = _resolve_ollama_url_for_runtime(str(args.ollama_url))
        _apply_provider_runtime_fallback(args)
    set_provider(args.provider)
    logger.info("Active LLM Provider: %s", get_provider())
    if get_provider() == "gpt":
        logger.info("Using OpenAI model: %s", args.gpt_model)
    else:
        logger.info("Using Ollama model: %s (URL: %s)", args.ollama_model, args.ollama_url)
    logger.info("LLM timeout (seconds): %d", args.timeout)
    logger.info("SQLite state DB path: %s", args.state_db)

    conn = init_db(args.state_db)
    if getattr(args, "command", None):
        result = run_command(str(args.command), conn, args)
        text = str(result.get("text", "") or "")
        if text:
            print(text)
        conn.close()
        return 0

    schema_status = ensure_postgres_core_schema()
    _report_postgres_schema_status(schema_status)

    if not args.no_bootstrap:
        if schema_status.get("ready"):
            bootstrap_result = ensure_bootstrap(
                username=args.username,
                bootstrap_games=args.bootstrap_games,
                fetch_recent_games_fn=fetch_recent_games,
                parse_game_fn=parse_game,
            )
            _report_bootstrap_status(args.username, bootstrap_result)
        else:
            logger.info("Bootstrap skipped: Postgres core schema is not ready.")

    telegram_stop_event = threading.Event()
    telegram_thread: Optional[threading.Thread] = threading.Thread(
        target=_telegram_command_loop,
        args=(args, telegram_stop_event),
        daemon=True,
        name="telegram-command-loop",
    )
    telegram_thread.start()
    logger.info("Telegram command loop started")
    logger.info("Polling loop started (interval=%ss)", args.poll_seconds)

    try:
        if args.once:
            _sync_runtime_provider(args)
            poll_once(conn, args)
            return 0

        while True:
            try:
                _sync_runtime_provider(args)
                created = poll_once(conn, args)
                logger.info("Poll cycle complete: created=%d", created)
                # if we just created output, poll quickly once more (sometimes games arrive slightly delayed)
                sleep_s = 20 if created > 0 else args.poll_seconds
            except KeyboardInterrupt:
                return 0
            except Exception as e:
                print(f"[error] poll cycle failed: {e}", file=sys.stderr)
                sleep_s = min(args.poll_seconds, 120)

            time.sleep(sleep_s)
    finally:
        telegram_stop_event.set()
        if telegram_thread is not None:
            telegram_thread.join(timeout=1.5)
        conn.close()
        close_ingest_db_check()


def _report_postgres_schema_status(status: Mapping[str, Any]) -> None:
    reason = str(status.get("reason", "unknown"))
    if bool(status.get("ready")):
        tables = status.get("tables_ready", [])
        logger.info("Postgres schema ready: %s", ", ".join(str(t) for t in tables) if tables else "players,games")
        return

    if reason == "no_database_url":
        logger.info("Postgres integration disabled: DATABASE_URL is not configured. Running SQLite-only.")
        return

    if reason == "db_unreachable":
        logger.warning("Postgres integration unavailable: DATABASE_URL is set but connection failed.")
        return

    logger.warning("Postgres schema not ready (reason=%s). Bootstrap will be skipped.", reason)


def _report_bootstrap_status(username: str, result: Mapping[str, Any]) -> None:
    ran = bool(result.get("ran"))
    reason = str(result.get("reason", "unknown"))
    inserted = int(result.get("inserted_games", 0) or 0)
    requested = int(result.get("requested_games", 0) or 0)

    if ran:
        logger.info("Bootstrap seeded %d/%d games for %s.", inserted, requested, username)
        return

    if reason in {"already_seeded", "no_recent_games", "no_new_games"}:
        logger.info("Bootstrap skipped for %s (%s).", username, reason)
        return

    if reason == "no_database_url":
        logger.info("Bootstrap disabled: DATABASE_URL is not configured.")
        return

    if reason == "db_unreachable":
        logger.warning("Bootstrap skipped: unable to connect to DATABASE_URL.")
        return

    logger.warning("Bootstrap skipped for %s (%s).", username, reason)


if __name__ == "__main__":
    raise SystemExit(main())
