# v0.3.0 — Engine Stability & Pipeline Hardening

Theme: Stabilize core analysis pipeline and eliminate poller/notification regressions.

⸻

Added
	•	Stockfish integration
	•	Deterministic engine analysis layer (configurable depth).
	•	Clear separation between engine evaluation and LLM narrative layer.
	•	Ollama provider support
	•	Environment-based configuration (OLLAMA_URL, OLLAMA_MODEL, timeout).
	•	Structured LLM audit logging.
	•	Format violation detection with retry logic.
	•	Host-editable prompts
	•	Prompts loaded from /app/prompts.
	•	Automatic seed on first container run.
	•	Runtime SHA256 + mtime logging for traceability.
	•	No container rebuild required for prompt edits.
	•	Telegram success flow
	•	Structured success message body.
	•	Markdown file attachment.
	•	Preview-safe URL formatting.
	•	Deterministic payload builder.
	•	Single-shot mode
	•	--once runs exactly one eligible pending game.
	•	Does not start scheduler or Telegram command loop.
	•	Deterministic exit codes.

⸻

Improved
	•	Poller eligibility logic (fully unified)
	•	Single eligibility predicate:
	•	success_notified = false
	•	engine_failed = false
	•	attempt_count < POLL_MAX_ATTEMPTS
	•	cooldown elapsed
	•	PGN present
	•	Oldest-first processing (played_at, created_at, id).
	•	Configurable:
	•	POLL_BATCH_SIZE
	•	POLL_MAX_ATTEMPTS
	•	POLL_COOLDOWN_SECONDS
	•	Queue execution diagnostics
	•	Optional poll execution audit (ENABLE_POLL_EXEC_AUDIT=1).
	•	Structured logging of:
	•	candidates
	•	skip reasons
	•	attempt results
	•	retry/backoff behavior
	•	Retry behavior
	•	Proper resend handling for Telegram failures.
	•	Success flags only written after confirmed notification.
	•	Engine failures properly terminal-flagged.
	•	False “already processed” fix
	•	Removed ingest-DB existence as a processing gate.
	•	Processing now gated strictly by terminal flags:
	•	success_notified
	•	engine_failed
	•	PGN recovery logic
	•	Missing PGN retry path with terminal detection.
	•	Backlog-safe processing behavior.

⸻

Fixed
	•	Poller stuck processing 0 eligible despite pending rows.
	•	Games incorrectly skipped due to ingest DB presence.
	•	Duplicate Telegram success messages.
	•	Telegram parse-entity failures (format handling tightened).
	•	Backlog cycles that never executed attempts.
	•	Inconsistent success flag writes.
	•	Improper state when TG document send failed.

⸻

Internal
	•	Deterministic test coverage for:
	•	Eligibility rules
	•	Batch limits
	•	Cooldown logic
	•	Terminal flag handling
	•	--once execution path
	•	Success notification flow
	•	Poll-cycle metrics logging expanded.
	•	Success state finalization normalized.

⸻

Operational Stability

v0.3.0 maintains:
	•	Reliable queue processing
	•	Reliable Telegram delivery
	•	Deterministic retry behavior
	•	Predictable batch handling
	•	Stable Docker startup behavior

Core engine → LLM → Markdown → Telegram pipeline is operational and stable.

_______________________

# v0.2.0 — Stable Docker Runtime & Player Summaries Latest
This release marks the first stable, end-to-end usable version of Chess Coach.
The system is now suitable for long-running, unattended use with Docker and requires no manual intervention after startup.

⸻

🚀 Highlights

Zero-friction Docker setup
• Simplified container model: no custom users, no UID/GID mapping
• Bind-mounted volumes now behave exactly as expected
• Works with standard docker compose up -d on first run

Reliable first-run bootstrap
• Automatically seeds recent games from Chess.com
• Bootstrap game count configurable via CHESS_BOOTSTRAP_GAMES (default: 100)
• Seeds traits, ratings, and stats without running LLM reviews
• One initial player summary is generated and sent after bootstrap

Per-game LLM reviews
• One Markdown review generated per new game
• Restart-safe, deduplicated, idempotent
• Raw PGNs are archived once and never overwritten

Player stats & summaries
• player_stats.md rebuilt after every processed game
• player_summary.md generated every N games (default: 20)
• Summary cadence persisted in SQLite so restarts do not retrigger
• Summaries overwrite previous ones (no snapshot clutter)

Telegram integration
• Instant command handling (no longer tied to poll interval)
• Sends:
• Per-game reviews
• Player summaries
• Supported commands:
• /status
• /stats
• /summary
• /health
• /help

Improved observability
• Clear startup logs:
• SQLite path
• Postgres readiness
• Bootstrap result
• Polling + Telegram loops
• Health checks clearly report:
• SQLite
• Postgres
• Telegram
• LLM endpoint state

Ollama compatibility
• Clean separation: Ollama runs on the host, not in Docker
• Automatic runtime resolution to host IP when running in containers
• Explicit health reporting for LLM reachability

⸻

🧱 Architecture Notes
• SQLite is authoritative for:
• Game deduplication
• Summary cadence
• Runtime state
• Postgres is optional and best-effort:
• Long-term game records
• Ratings history
• LLM reviews are intentionally human-focused (no engine spam)

⸻

🛠 Breaking Changes
• None.
Existing users may delete old containers and volumes and restart cleanly.

⸻

📦 Upgrade Notes
1. Pull latest code
2. Ensure your output directory exists and is writable
3. Run: docker compose up -d --build
4.	Watch logs once to confirm bootstrap completes

--------------------

# v0.1.0 Initial Release - Chess Coach - AI Game Review
Chess Coach v0.1.0 introduces an automated, self-hosted system for generating human-style chess game reviews using local or cloud LLMs.

This release focuses on correctness, durability, and ease of self-hosting rather than feature breadth.

⸻

✨ Key Features
• Automated Chess.com polling
• Detects newly finished games only
• Safe restart behavior with SQLite-backed state tracking
• Human-focused coaching reviews
• Highlights key mistakes, turning points, and improvement themes
• Avoids move-by-move engine noise and raw evaluation spam
• Output optimized for ~800–1600 rating players
• Local LLM support via Ollama (recommended)
• No cloud dependency required
• Supports any Ollama model (default: llama3.2)
• Designed for long-form reasoning and coaching tone
• Optional OpenAI GPT backend
• Swappable provider via a single flag
• No architectural changes required
• Markdown + PGN output
• One review per game
• Clean, readable Markdown files
• Raw PGN archived alongside reviews
• Optional index.md for browsing
• Dockerized, daemon-style operation
• Continuous polling (default: every 5 minutes)
• Restart-safe and idempotent
• Designed for long-running use
• Telegram notifications (optional)
• Sends generated Markdown files directly to a chat
• Failures do not interrupt processing
• Nextcloud-friendly storage
• Output directory is user-defined
• Works seamlessly with synced folders

⸻

🧠 Design Philosophy
• Human coaching > engine analysis
• Local-first, privacy-respecting architecture
• Minimal moving parts, maximal clarity
• Transparent state and reproducible outputs

This is not an engine replacement — it is a chess journal + coach.

⸻

⚠️ Important Notes
• Ollama must already be running on the host (http://<host.IP.from.docker>>:11434)
• First-time runs may process recent historical games
• First inference after idle may be slow (local model warm-up)

⸻

📦 What’s Included
• chess_review.py – core daemon
• Dockerfile + generic docker-compose.yml
• .env.example for safe configuration
• Markdown + PGN output pipeline
• SQLite state tracking

⸻

🚧 Known Limitations
• No Stockfish integration (by design)
• No opening classification yet
• No aggregate analytics (weekly/monthly summaries)

These are planned for future releases.

⸻

🔜 What’s Next
• Optional Stockfish eval swing detection
• Weekly or monthly improvement summaries
• Opening pattern identification
• Prompt and output customization
• Manual backfill and re-review modes

⸻

Thank you for using Chess Coach.
Feedback, ideas, and pull requests are welcome.