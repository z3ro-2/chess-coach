# Chess Coach

Automated chess game reviews using **Chess.com**, **LLMs (Ollama or GPT)**, and clean **Markdown output**.

This project runs continuously, watches your Chess.com account for new games, and generates **coach-style reviews** focused on mistakes, turning points, and improvement themes (not engine noise).

It is designed for **human players (~800–1600 rating)** who want practical feedback over time.

---

## What this does (at a glance)

- Polls the Chess.com public API (default: every 5 minutes)
- Detects **new games only** (restart-safe, no duplicates)
- Generates **one Markdown review per game**
- Tracks **player traits and statistics over time**
- Produces:
  - Per-game reviews
  - Rolling stats (`player_stats.md`)
  - Periodic summaries (`player_summary.md`)
- Optionally sends reviews and summaries to Telegram

Everything is restart-safe and idempotent.

---

## Quick Start (Docker, recommended)

### 1. Prerequisites

- Docker + Docker Compose
- A Chess.com account
- **Ollama running on the host** (or OpenAI API key)

Ollama must already be running on your machine:
```bash
ollama serve
ollama pull llama3.2:latest
```

---

### 2. Clone and configure

```bash
git clone <repo-url>
cd chess-coach
cp .env.example .env
```

Edit `.env` and set at minimum:
```env
CHESS_USERNAME=your_chesscom_username
```

---

### 3. Create output directory

```bash
mkdir -p "${HOME}/chess"
```

This will be mounted into the container as `/data`.

---

### 4. Start the system

```bash
docker compose up -d --build
docker logs -f chess-coach
```

That’s it. The container will keep running.

---

## What gets generated

All outputs land under the host directory `${HOME}/chess`:

```
chess/
├── md/                    # One Markdown review per game
├── pgn/                   # Raw PGN archives
├── player_stats.md        # Rolling player metrics + traits
├── player_summary.md      # Every-N games summary
├── index.md               # Optional index
└── state.sqlite           # Local state (dedupe + cadence)
```

---

## Bootstrap behavior (first run only)

On first startup **only**, if Postgres is enabled and empty:

- The app fetches your most recent games **directly from Chess.com**
- Number of games is controlled by:
  ```env
  CHESS_BOOTSTRAP_GAMES=100
  ```
- Bootstrap:
  - Seeds raw game records
  - Seeds traits and ratings
  - **Does NOT generate LLM game reviews**
- After bootstrap completes:
  - One **initial player summary** is generated
  - It is sent to Telegram (if configured)

Bootstrap is **idempotent** and will not re-run on restart.

---

## Ongoing behavior (normal operation)

### Per game
For every new game you play:
- One Markdown review is generated
- One Telegram message is sent (if enabled)
- Player stats are updated

### Summaries
- A full `player_summary.md` is generated every **N games**
- Controlled by:
  ```env
  PLAYER_SUMMARY_EVERY_N=20
  ```
- Summary cadence is stored in SQLite, so restarts do not retrigger old summaries

---

## Player traits & stats

- Traits (strengths, weaknesses, tendencies) are tracked incrementally
- `player_stats.md` is rebuilt after every processed game
- `player_summary.md` is a higher-level narrative summary
- Summaries overwrite previous ones (no snapshot spam)

Postgres is optional but recommended for long-term analysis.

---

## Telegram integration (optional)

Set in `.env`:
```env
TG_BOT_TOKEN=...
TG_CHAT_ID=...
```

Behavior:
- Each game review is sent as a document
- Each summary is sent when generated
- Failures never stop processing
- Command handling runs in its own fast loop and does not wait for the game poll interval

---

## Commands (no polling)

You can run commands without polling:

```bash
python -m src.main status
python -m src.main stats
python -m src.main summary
python -m src.main health
python -m src.main help
```

### Telegram commands
When Telegram is configured, you can send:

```
/status
/stats
/summary
/health
/help
```

Responses include text output and attached Markdown files when applicable.

---

## Configuration reference

Common `.env` options:

```env
CHESS_USERNAME=your_username
CHESS_OUTPUT_DIR=/data
STATE_DB=/data/state.sqlite
DATABASE_URL=postgresql://chess:chess@postgres:5432/chesscoach
OLLAMA_URL=http://host.docker.internal:11434
OLLAMA_MODEL=llama3.2:latest
CHESS_BOOTSTRAP_GAMES=100
PLAYER_SUMMARY_EVERY_N=20
```

Docker note: if `OLLAMA_URL` is unset or uses `127.0.0.1`/`localhost`, runtime auto-resolves to `http://host.docker.internal:11434` in containers.
`STATE_DB` sets the local SQLite state DB path. It must be writable by the container user; default is `/data/state.sqlite`.

Common CLI flags:
- `--username`
- `--provider ollama|gpt`
- `--poll-seconds`
- `--player-summary-every-n`
- `--bootstrap-games`

---

## Design notes

- SQLite is authoritative for dedupe and cadence
- Postgres is optional and best-effort
- Ollama runs outside Docker to avoid GPU contention
- No engine analysis by default (human coaching focus)

---

## License

MIT
