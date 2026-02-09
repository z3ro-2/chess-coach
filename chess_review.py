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
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple



import requests

CHESSCOM_GAMES_URL = "https://api.chess.com/pub/player/{username}/games/{year}/{month:02d}"
DEFAULT_TIMEOUT = 120  # seconds (local Ollama cold starts can be slow)
USER_AGENT = "chess-review-daemon/0.1 (+https://chess.com/pubapi)"

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
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def is_processed(conn: sqlite3.Connection, game_url: str) -> bool:
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


def build_prompt(game: GameInfo) -> Tuple[str, str]:
    """Return (system_message, user_message)."""
    date_iso = game.end_dt_utc.strftime("%Y-%m-%d")

    white_rating = game.white_rating if game.white_rating is not None else "?"
    black_rating = game.black_rating if game.black_rating is not None else "?"

    system = (
        "You are a chess coach. Produce a concise but insightful game review aimed at ~1000-rated players. "
        "Do NOT go move-by-move. Focus only on the critical turning points and plans. "
        "Be concrete: reference move numbers and describe the position/idea in words. "
        "Assume no engine; prefer human-coachable heuristics and typical tactical motifs. "
        "Avoid filler, be direct."
    )

    user = f"""Analyze this chess game and generate a Markdown review file.

Context:
- Player is \"{game.your_color}\" (the user).
- Opponent: {game.opponent}
- Date (UTC): {date_iso}
- Ratings (approx): {game.white_username}={white_rating}, {game.black_username}={black_rating}
- Time control: {game.time_control}
- Rated: {game.rated}
- Rules: {game.rules}
- Result: {game.result}
- Game URL: {game.game_url}

Output requirements:
1) Output VALID Markdown.
2) Start with YAML front matter (---) including: date_utc, your_color, opponent, result, time_control, rated, url.
3) Include these sections (with headings):
   - Summary (2-4 bullets: what decided the game)
   - Key inflection points (3-7 items). For each: move number, what happened, better alternative, and a "rule of thumb".
   - What to watch out for next time (patterns, not moves)
   - Training plan (3-5 drills/tasks you can do this week)
   - Next-game checklist (short, practical)
4) Keep tone direct. No fluff.
5) If the PGN includes annotations like $1/$2/etc, interpret them as hints but do not blindly trust them.

Here is the PGN:
```pgn
{game.pgn}
```
"""

    return system, user


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


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
    write_text(pgn_path, game.pgn)

    system_msg, user_msg = build_prompt(game)

    used_model = args.ollama_model if args.provider == "ollama" else args.gpt_model

    if args.provider == "gpt":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set but --provider gpt was selected.")
        review_md = call_openai_chat(
            api_key=api_key,
            model=args.gpt_model,
            system_msg=system_msg,
            user_msg=user_msg,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
        )
    else:
        review_md = call_ollama_generate(
            base_url=args.ollama_url,
            model=args.ollama_model,
            system_msg=system_msg,
            user_msg=user_msg,
            timeout=args.timeout,
        )

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

    h = compute_content_hash(game, args.provider, used_model)
    mark_processed(
        conn=conn,
        game_url=game.game_url,
        end_time=game.end_time,
        md_path=md_path,
        pgn_path=pgn_path,
        provider=args.provider,
        model=used_model,
        content_hash=h,
    )

    if args.update_index:
        update_index(args.out)

    return md_path


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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Chess.com game poller -> LLM coach notes -> Markdown files")

    p.add_argument("--username", required=True, help="Chess.com username")
    p.add_argument("--out", type=Path, default=Path("./chess_reviews"), help="Output directory")
    p.add_argument(
        "--state-db",
        type=Path,
        default=Path("./chess_reviews/state.sqlite"),
        help="SQLite state DB path",
    )

    p.add_argument("--provider", choices=["gpt", "ollama"], default="ollama", help="LLM provider")
    p.add_argument("--gpt-model", default="gpt-4o-mini", help="OpenAI model (provider=gpt)")
    p.add_argument("--max-tokens", type=int, default=1400, help="Max tokens for OpenAI responses")

    p.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL")
    p.add_argument("--ollama-model", default="llama3.1:8b", help="Ollama model name")

    p.add_argument("--poll-seconds", type=int, default=300, help="Polling interval (seconds)")
    p.add_argument("--once", action="store_true", help="Run one poll cycle then exit")
    p.add_argument("--lookback-days", type=int, default=10, help="How far back to scan for unprocessed games")

    p.add_argument(
        "--rules-filter",
        default="chess",
        help="Only process games matching this rules value (e.g., chess, chess960). Set empty string to disable.",
    )

    p.add_argument("--timezone", default="America/Chicago", help="Timezone for filenames (IANA)")

    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds")
    p.add_argument("--retries", type=int, default=3, help="Retries per game when LLM/network fails")

    p.add_argument("--update-index", action="store_true", help="Update index.md after new reviews")
    p.add_argument("--dry-run", action="store_true", help="Detect new games but do not call LLM or write files")

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

    args = p.parse_args()

    # Normalize rules filter: allow disabling with empty string
    if args.rules_filter is not None:
        args.rules_filter = args.rules_filter.strip()
        if args.rules_filter == "":
            args.rules_filter = None

    return args


def main() -> int:
    args = parse_args()
    # Safety: ensure Ollama host resolution is always localhost when running with host networking
    if "ollama" in args.ollama_url:
        args.ollama_url = args.ollama_url.replace("ollama", "127.0.0.1")
    args.out = args.out.expanduser().resolve()
    args.state_db = args.state_db.expanduser().resolve()

    conn = init_db(args.state_db)

    if args.once:
        poll_once(conn, args)
        return 0

    while True:
        try:
            created = poll_once(conn, args)
            # if we just created output, poll quickly once more (sometimes games arrive slightly delayed)
            sleep_s = 20 if created > 0 else args.poll_seconds
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"[error] poll cycle failed: {e}", file=sys.stderr)
            sleep_s = min(args.poll_seconds, 120)

        time.sleep(sleep_s)


if __name__ == "__main__":
    raise SystemExit(main())