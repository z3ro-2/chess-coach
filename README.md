# Chess Coach

Automated chess game reviews using **Chess.com**, **LLMs (Ollama or GPT)**, and **Markdown output**.

This project continuously polls your Chess.com account, detects newly finished games, and generates **coach-style game reviews** (focused on key mistakes, turning points, and improvement themes — not move-by-move engine noise). Each review is saved as a Markdown file and can optionally be sent to Telegram.

The intended audience is human players (≈800–1600 rating), not engines.

---

## What this does

- Polls the Chess.com public API on an interval (default: every 5 minutes)
- Detects **new games only** (no duplicates, state tracked via SQLite)
- Generates a **Markdown coaching review** per game
- Saves:
  - `*.md` coaching notes
  - `*.pgn` raw game archive
  - `state.sqlite` (processed-game tracking)
  - `player_stats.md` (rolling player metrics + traits snapshot)
  - `player_summary.md` (every-N games summary)
- Optionally:
  - Sends the Markdown file to Telegram via bot
  - Maintains an `index.md`

---

## LLM support

This project supports **two interchangeable backends**:

- **Ollama (local, recommended)**
- **OpenAI GPT (cloud)**

Switching providers is done with **one flag**.

---

## ⚠️ Important requirement (READ THIS)

### Ollama must already be running on the host

This Docker Compose setup **does NOT start Ollama for you**.

You must already have an Ollama server running on the host machine, listening on:

```
http://127.0.0.1:11434
```

From inside the container, this is reached as:

```
http://host.docker.internal:11434
```

And the model you reference **must already be pulled**, for example:

```bash
ollama pull llama3.2:latest
```

This design avoids:
- Duplicate model downloads
- GPU contention
- Port conflicts

---

## Repository layout

```
chess-coach/
├── chess_review.py        # Main application
├── Dockerfile             # Container definition
├── docker-compose.yml     # Runtime configuration (generic)
├── requirements.txt       # Python dependencies
├── .env.example           # Example environment variables (safe to commit)
└── ~/chess/               # Host output directory (bind-mounted to /data)
    ├── md/                # Generated coaching reviews
    ├── pgn/               # Raw PGN archives
    ├── index.md           # Optional index
    └── state.sqlite       # Processed-game tracking
```

---

## Requirements

- Docker + Docker Compose
- A Chess.com account
- **Ollama running on the host** (for local inference)
  - OR an OpenAI API key (optional alternative)

---

## Environment variables

Create a `.env` file in the repo root (this file should **not** be committed):

```env
# REQUIRED
CHESS_USERNAME=your_chesscom_username

# OPTIONAL
CHESS_OUTPUT_DIR=/data
DATABASE_URL=postgresql://chess:chess@postgres:5432/chesscoach
OLLAMA_URL=http://host.docker.internal:11434
OLLAMA_MODEL=llama3.2:latest
PLAYER_SUMMARY_EVERY_N=20

# Telegram notifications (optional)
TG_BOT_TOKEN=
TG_CHAT_ID=

# OpenAI (only if using provider=gpt)
# OPENAI_API_KEY=
```

A `.env.example` file is provided for reference.

When running in Docker, `CHESS_OUTPUT_DIR` inside the container should be `/data`.
The host path should be mounted to `/data` so all outputs (md, pgn, state.sqlite, trait_books, index.md) land on the host volume.

---

## Docker Compose (default setup)

The provided `docker-compose.yml` is intentionally generic and uses environment variables for all user-specific values.

Before running, ensure you have:
- Created a `.env` file (see above)
- Set `CHESS_USERNAME`

The compose file:
- Runs `chess-coach` and `postgres`
- Connects to host Ollama using `OLLAMA_URL=http://host.docker.internal:11434`
- Persists output by mounting host `${HOME}/chess` to container `/data`

---

## Telegram notifications (optional)

If `TG_BOT_TOKEN` and `TG_CHAT_ID` are set:

- Each generated `.md` file is sent as a Telegram document
- `player_summary.md` is also sent every `PLAYER_SUMMARY_EVERY_N` new games
- Failures do **not** stop processing

Useful for:
- Reviewing games on your phone
- Keeping a daily chess journal

---

## Notes / design decisions

- No engine analysis is used by default (human-focused coaching)
- SQLite is used for durability and simplicity
- Docker container runs continuously; no cron
- Ollama is intentionally external to avoid GPU duplication

---

## Future improvements (ideas)

- Optional Stockfish eval swing detection
- Opening classification
- Re-review old games with new models
- Training-plan summaries per week/month

---


## Quickstart

### First-time setup

Copy and configure environment:

```bash
cp .env.example .env
```

Set at least:
- `CHESS_USERNAME`
- `OLLAMA_MODEL`
- `CHESS_OUTPUT_DIR=/data`

Create the host output directory:

```bash
mkdir -p "${HOME}/chess"
```

Ensure Ollama is running on the host:

```bash
ollama serve
ollama pull llama3.2:latest
```

Then build and start:

```bash
docker compose up -d --build
docker logs -f chess-coach
```

On first startup, the app automatically:
- initializes core Postgres tables (`players`, `games`) if `DATABASE_URL` is reachable
- runs bootstrap seeding when the player has no existing games in Postgres

Generated files will appear on the host under `${HOME}/chess`:
- `md/`
- `pgn/`
- `state.sqlite*`
- `index.md`
- `player_stats.md`
- `player_summary.md`
- `trait_books/` (when snapshots are generated)

---

### Run from GHCR

Pull the published image:

```bash
docker pull ghcr.io/<owner>/<repo>:latest
```

One-time trait backfill seed (100 games) with snapshot check at the end:

```bash
docker run --rm \
  -e DATABASE_URL="<postgres_connection_url>" \
  -e CHESS_OUTPUT_DIR="/data" \
  -v "$(pwd)/output:/data" \
  ghcr.io/<owner>/<repo>:latest \
  -m src.cli.seed_traits --player <user> --games 100 --no-skip-snapshots
```

Normal poller mode:

```bash
docker run --rm \
  -e CHESS_USERNAME="<chesscom_username>" \
  -e CHESS_OUTPUT_DIR="/data" \
  -e OLLAMA_URL="http://host.docker.internal:11434" \
  -e OLLAMA_MODEL="llama3.2:latest" \
  -v "$(pwd)/output:/data" \
  ghcr.io/<owner>/<repo>:latest \
  -m src.main
```

---

## Configuration flags (common)

Default container entrypoint is `python -m src.main`.  
Common CLI flags (if you override command) include:

- `--username` – Chess.com username (required)
- `--provider` – `ollama` or `gpt`
- `--ollama-url` – usually `http://host.docker.internal:11434` in Docker
- `--ollama-model` – e.g. `llama3.2:latest`
- `--poll-seconds` – polling interval (default: 300)
- `--update-index` – maintain `index.md`

---

## Output location

In Docker, keep `CHESS_OUTPUT_DIR=/data` and mount a host directory to `/data`.
With the provided compose file, host output is `${HOME}/chess`.

---

## Username

The `--username` flag refers to your **Chess.com username** (the one in your profile URL).

Example:

```
https://www.chess.com/member/<username>  →  username = chess.com username
```

No personal usernames are hardcoded anywhere in this project or configuration.


## License

MIT

---
