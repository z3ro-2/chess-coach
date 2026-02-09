

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
├── docker-compose.yml     # Runtime configuration
├── requirements.txt       # Python deps (requests)
├── .env                   # Secrets (Telegram, GPT)
└── chess_reviews/         # Output directory (bind-mounted)
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

Create a `.env` file in the repo root:

```env
# Telegram (optional)
TG_BOT_TOKEN=123456:ABC...
TG_CHAT_ID=123456789

# OpenAI (only if using provider=gpt)
# OPENAI_API_KEY=sk-...
```

Telegram variables are optional; if unset, notifications are skipped.

---

## Docker Compose (default setup)

The provided `docker-compose.yml`:

- Runs **only the chess-coach container**
- Connects to Ollama on the host via `network_mode: host`
- Stores output files in a bind-mounted directory

### Username

The `--username` flag refers to your **Chess.com username** (the one in your profile URL).

Example:
```
https://www.chess.com/member/<username>  →  username = chess.com username
```

---

## Output location

The compose file mounts a host directory (example):

```yaml
volumes:
  - /home/<USER>>/Documents/chess:/data
```

Inside the container, all output goes to `/data`.

On the host, you will find:

```
~/Documents/chess/md/*.md
~/Documents/chess/pgn/*.pgn
~/Documents/chess/state.sqlite
```

This directory can be a **Nextcloud-synced folder**.

---

## Running it

### First-time setup

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

---

## Configuration flags (common)

Inside `docker-compose.yml`, the container is launched with flags such as:

- `--username` – Chess.com username (required)
- `--provider` – `ollama` or `gpt`
- `--ollama-url` – usually `http://127.0.0.1:11434`
- `--ollama-model` – e.g. `llama3.2:latest`
- `--poll-seconds` – polling interval (default: 300)
- `--update-index` – maintain `index.md`

---

## Telegram notifications (optional)

If `TG_BOT_TOKEN` and `TG_CHAT_ID` are set:

- Each generated `.md` file is sent as a Telegram document
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

## License

MIT