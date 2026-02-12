# Engine Payload Investigation Report

## Objective
Determine why deterministic engine-derived metrics produced inflated error rates and inconsistent trait scores.

## Root Cause Findings

1. `label_counts` source and unit (pre-fix)
- Source: `engine/stockfish_oracle.py` classified every analyzed ply and incremented a single `label_counts` map.
- Unit: plies (half-moves), not player-only moves.
- Scope: combined White + Black.

2. Denominator mismatch in trait scoring (pre-fix)
- `src/engine_traits.py` consumed `game_summary.label_counts` as numerator but used `total_moves` as denominator.
- `total_moves` is full move count (`ceil(total_plies / 2)`), while `label_counts` represented both sides' plies.
- Net effect: numerator and denominator represented different units, causing inflated effective error rates and unstable trait calibration.

3. Mixed scoping in one trait pipeline (pre-fix)
- Label-based rates were effectively both-sides combined.
- Key-position tactical/material signals were filtered by `your_color`.
- This mixed scope created mathematically inconsistent composites.

## Corrections Implemented

1. Schema v2 and player/side separation
- Added `engine/payload_schema.py` with `ENGINE_PAYLOAD_SCHEMA_VERSION = 2`.
- `engine/stockfish_oracle.py` now emits:
  - `label_counts_by_side.{white,black}`
  - `label_counts` (combined, derived)
  - `total_plies`, `total_moves`
  - `schema_version`
- Added deterministic player projections:
  - `player_total_plies`
  - `player_total_moves`
  - `player_label_counts`

2. Explicit invariants
- Validation in `validate_engine_payload(...)` enforces:
  - schema version support
  - `total_moves == ceil(total_plies/2)`
  - sum of combined label counts equals `total_plies`
  - side count sums equal expected white/black ply counts
  - player counts match side counts for `your_color`
  - player count sum equals `player_total_plies`
  - optional strict check for exactly 4 key positions

3. Strict-mode failure behavior
- `analysis_pipeline.py` now validates payload invariants before LLM formatting.
- Invariant violation raises `RuntimeError` and aborts per-game review generation.
- `chess_review.py` existing strict failure path sends Telegram error notification and does not emit review markdown.

4. Trait scoring refactor to player-only math
- `src/engine_traits.py` now validates payloads and computes rates only from:
  - numerator: `player_label_counts`
  - denominator: `player_total_plies`
- Key-position-derived rates now use key-position-derived denominators (position counts), not move counts.
- If payload invariants fail during trait scoring: log error + neutral score fallback.

5. Migration/backfill handling
- Existing stored payloads are version-gated.
- In backfill paths (`chess_review.py`, `backfill.py`):
  - reusable payloads must pass schema v2 validation
  - stale/old payloads are re-derived and replaced
- Trait-window loader ignores stale/invalid rows and logs the reason.

## Debug/Proof Tooling
- Added `src/cli/debug_engine_payload.py`:
  - validates payload invariants
  - optionally compares payload counts against PGN-derived ply totals
  - optionally runs Stockfish oracle and validates generated payload directly

## Validation Coverage
- Added/updated offline tests for:
  - schema invariants and strict validation failure
  - player-only scoping behavior
  - monotonicity with worsening errors
  - guardrails and saturation
  - migration behavior for stale payload rows
  - integration-style oracle payload consistency fixture

