# Chess Coach v0.3.0

Deterministic engine-backed chess reviews with automated Telegram delivery, trait tracking, and a hardened polling pipeline.

Built for players ~800–1600 who want practical feedback after every game — not 40 lines of engine noise.

---

# What v0.3.0 Delivers

v0.3.0 focuses on **stability, determinism, and queue correctness**.

### Core Guarantees

- No duplicate Telegram sends  
- No false “already processed” skips  
- Deterministic retry + cooldown logic  
- Oldest-first queue processing  
- Markdown file + Telegram message always aligned  
- Single-shot processing mode (`--once`)  
- Host-editable prompts with runtime verification  

This release restores full pipeline integrity:

Chess.com → Engine → LLM → Markdown → Telegram → Database state

---

# Architecture Overview

### Engine Layer (Deterministic)

- Stockfish (configurable depth)
- Generates structured evaluation signals
- No narrative generation here
- Fully deterministic

### LLM Layer (Narrative Only)

- Ollama or GPT-compatible provider
- Receives structured engine-derived signals
- Produces coaching narrative
- Format validation + retry logic

### Persistence

- SQLite → authoritative local state (dedupe + cadence)
- Postgres → long-term game tracking, queue state, flags
- Host-mounted `/data` → Markdown + PGN + stats

---

# Quick Start (Docker Recommended)

## 1. Prerequisites

- Docker + Docker Compose
- Chess.com account
- Ollama running (or OpenAI-compatible endpoint)

Example:

```bash
ollama serve
ollama pull qwen2.5:14b-instruct
```

---

## 2. Clone + Configure

```bash
git clone <repo-url>
cd chess-coach
cp .env.example .env
```

Minimum required:

```env
CHESS_USERNAME=your_username
```

Optional Telegram:

```env
TG_BOT_TOKEN=...
TG_CHAT_ID=...
```

---

## 3. Create Output Directory

```bash
mkdir -p "${HOME}/chess"
```

Bind-mounted into container as `/data`.

---

## 4. Start

```bash
docker compose up -d --build
docker compose logs -f chess-coach
```

Poll interval defaults to 300 seconds.

---

# Poller & Queue Behavior (v0.3.0)

Games are processed only if:

- `success_notified = FALSE`
- `engine_failed = FALSE`
- `attempt_count < POLL_MAX_ATTEMPTS`
- cooldown elapsed
- PGN present

Processing order:

1. Oldest `played_at`
2. Oldest `created_at`
3. Lowest `id`

Batch size controlled by:

```env
POLL_BATCH_SIZE=5
POLL_MAX_ATTEMPTS=5
POLL_COOLDOWN_SECONDS=600
```

---

# Single-Shot Mode

Process exactly one eligible game and exit:

```bash
docker compose run --rm chess-coach python chess_review.py --once
```

Properties:

- Forces batch size = 1
- Does not start Telegram command loop
- Exits immediately
- Deterministic return codes

---

# Telegram Delivery Model

On success:

1. Sends formatted summary message (URL last line for preview)
2. Attaches Markdown file
3. Only then marks:
   - `success_notified = TRUE`
   - `review_notified = TRUE`

If Telegram fails:
- Success flags are NOT written
- Send-only retry will occur next cycle
- Analysis is not rerun

---

# Host-Editable Prompts

Prompts live in:

```
/app/prompts
```

On first run:
- Seeded automatically if empty

At runtime:
- SHA256 and mtime logged for each prompt
- No rebuild required to edit prompts

Modify:

- `review_system.md`
- `review_user_strict.md`

Restart container to reload.

---

# Bootstrap Behavior

On first run with empty Postgres:

- Fetches last `CHESS_BOOTSTRAP_GAMES` (default 100)
- Seeds DB
- Skips LLM narrative during bootstrap
- Generates initial stats/summary

Bootstrap is idempotent.

---

# Backfill Mode (Engine-Only)

```bash
python3 chess_review.py --username your_username --backfill 50
```

Constraints:

- Max 200 games
- No Telegram
- No LLM
- Deterministic trait recompute
- Exit 0 on success

---

# Commands (No Polling)

```bash
python -m src.main status
python -m src.main stats
python -m src.main summary
python -m src.main health
python -m src.main help
```

Telegram (if enabled):

```
/status
/stats
/summary
/health
/help
```

---

# Output Structure

```
chess/
├── md/
├── pgn/
├── player_stats.md
├── player_summary.md
├── state.sqlite
```

---

# Configuration Highlights

```env
CHESS_USERNAME=
DATABASE_URL=postgresql://chess:chess@postgres:5432/chesscoach
STATE_DB=/data/state.sqlite

ENABLE_ENGINE=true
ENGINE_DEPTH=15
STOCKFISH_PATH=/usr/bin/stockfish

OLLAMA_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen2.5:14b-instruct

POLL_BATCH_SIZE=5
POLL_MAX_ATTEMPTS=5
POLL_COOLDOWN_SECONDS=600
```

---

# Debugging & Audit

Enable poll execution audit:

```env
ENABLE_POLL_EXEC_AUDIT=1
```

Logs include:

- candidate list
- skip reasons
- attempt results
- retry outcomes

---

# Stability Philosophy

v0.3.0 prioritizes:

- Deterministic state transitions
- Explicit terminal flags
- No hidden ingest-based skips
- Strict success-write semantics
- Controlled retry behavior

Coaching quality improvements will build on this stable core in v0.4.0.

---

# License

MIT
