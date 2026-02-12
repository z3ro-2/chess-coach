# Chess Coach v0.2.0

Automated chess game reviews for human players (~800–1600 rating) using **Chess.com**, **LLMs (Ollama or GPT)**, and clean **Markdown output**. Get practical, coach-style feedback focused on mistakes, turning points, and improvement themes—not engine noise.

---

## Quick Start with Docker (Recommended)

1. **Prerequisites**  
   - Docker & Docker Compose  
   - Chess.com account  
   - Ollama running locally (or OpenAI API key)  
   
   Start Ollama if needed:  
   ```bash
   ollama serve
   ollama pull llama3.2:latest
   ```

2. **Clone and Configure**  
   ```bash
   git clone <repo-url>
   cd chess-coach
   cp .env.example .env
   ```  
   Edit `.env` to set at least:  
   ```env
   CHESS_USERNAME=your_chesscom_username
   ```

3. **Create Output Directory**  
   ```bash
   mkdir -p "${HOME}/chess"
   ```  
   This directory is bind-mounted into the container as `/data`. No special user or permission setup needed.

4. **Start the App**  
   ```bash
   docker compose up -d --build
   docker logs -f chess-coach
   ```  
   The container runs continuously, polling Chess.com every 5 minutes by default.

---

## What It Does

- Polls Chess.com public API for **new games only** (restart-safe, no duplicates)  
- Generates **one Markdown review per game** in `/data/md/`  
- Saves raw PGN archives in `/data/pgn/`  
- Tracks player traits and stats over time  
- Produces rolling stats (`player_stats.md`) and periodic summaries (`player_summary.md`)  
- Optionally sends reviews and summaries to Telegram  

All data and state are stored locally under your bind-mounted directory, ensuring easy access and persistence.

---

## Bootstrap (First Run Only)

If Postgres is enabled and empty, the app will:

- Fetch your most recent games directly from Chess.com (default 100 games, configurable via `CHESS_BOOTSTRAP_GAMES`)  
- Seed raw game data, player traits, and ratings  
- **Skip LLM game reviews during bootstrap**  
- Generate and optionally send an initial player summary to Telegram  

Bootstrap is idempotent and won’t rerun on restart.

---

## Ongoing Behavior

- For each new game played:  
  - Generate a Markdown review  
  - Update player stats  
  - Send Telegram message if enabled  

- Generate a full player summary every N games (`PLAYER_SUMMARY_EVERY_N`, default 20), tracked in SQLite to avoid duplicates on restart.

---

## Telegram Integration (Optional)

Set in `.env`:

```env
TG_BOT_TOKEN=...
TG_CHAT_ID=...
```

- Sends each game review as a document  
- Sends summaries when generated  
- Handles failures gracefully without interrupting processing  
- Supports commands without waiting for polling intervals  

---

## Commands (No Polling)

Run commands directly without polling:

```bash
python -m src.main status
python -m src.main stats
python -m src.main summary
python -m src.main health
python -m src.main help
```

Telegram commands (if enabled):

```
/status
/stats
/summary
/health
/help
```

Responses include text and attached Markdown files when applicable.

---

### Backfill Mode

Usage:

```bash
python3 chess_review.py --backfill 50
```

If `CHESS_USERNAME` is not set in your environment, provide it explicitly:

```bash
python3 chess_review.py --username your_chesscom_username --backfill 50
```

This mode:

- fetches up to `N` recent games for your configured username
- runs strict Stockfish analysis on each
- persists engine payloads
- recomputes trait scores
- prints a deterministic summary

Constraints:

- No Telegram notifications
- No LLM calls
- max limit = 200
- exit code `0` on success
- exit with error if engine fails
- `games fetched from chess.com` means valid games parsed and considered before final top-`N` selection

If `N > 200`, backfill aborts with:

```text
ValueError("Backfill limit exceeded: max 200 games at once.")
```

Expected stdout summary format:

```text
Backfill Summary:
- total games requested: <N>
- games fetched from chess.com: <M>
- games analyzed with Stockfish: <P>
- traits (post-backfill):
  tactical_awareness: <int>
  material_discipline: <int>
  conversion_ability: <int>
  defensive_resilience: <int>
  blunder_frequency: <int>
```

Example:

```bash
python3 chess_review.py --username your_chesscom_username --backfill 20
```

Example output:

```text
Backfill Summary:
- total games requested: 20
- games fetched from chess.com: 36
- games analyzed with Stockfish: 20
- traits (post-backfill):
  tactical_awareness: 86
  material_discipline: 81
  conversion_ability: 67
  defensive_resilience: 74
  blunder_frequency: 95
```

---

## Configuration Highlights

```env
CHESS_USERNAME=your_username
CHESS_OUTPUT_DIR=/data
STATE_DB=/data/state.sqlite
DATABASE_URL=postgresql://chess:chess@postgres:5432/chesscoach
OLLAMA_URL=http://host.docker.internal:11434
OLLAMA_MODEL=llama3.2:latest
ENABLE_ENGINE=true
STOCKFISH_PATH=/usr/bin/stockfish
ENGINE_DEPTH=15
CHESS_BOOTSTRAP_GAMES=100
PLAYER_SUMMARY_EVERY_N=20
```

- `STATE_DB` points to the local SQLite DB (default `/data/state.sqlite`), writable by container user  
- If `OLLAMA_URL` is unset or localhost, it auto-resolves to `http://host.docker.internal:11434` inside the container
- If `ENABLE_ENGINE=true`, Stockfish runs first and only structured engine labels are passed to the LLM

---

## Design Notes

- SQLite is authoritative for deduplication and cadence  
- Postgres is optional for long-term analysis  
- Ollama runs outside Docker to avoid GPU contention  
- Engine analysis is deterministic and optional via `ENABLE_ENGINE`  

---

## Output Directory Structure

```
chess/
├── md/                    # Markdown reviews per game
├── pgn/                   # Raw PGN archives
├── player_stats.md        # Rolling player metrics and traits
├── player_summary.md      # Periodic narrative summaries
├── index.md               # Optional index
└── state.sqlite           # Local SQLite state DB
```

---

## License

MIT
