# Trait Integrity Note

## Why It Was Wrong
- Engine-derived counts were previously mixed across both sides and/or inconsistent units (moves vs plies), which inflated error rates and distorted trait scores.
- Legacy payloads (non-v2) could be included in rolling windows, silently contaminating deterministic trait calculations.
- Summary/backfill output paths could report window metrics without clearly reflecting true schema-valid coverage.

## Why It Is Correct Now
- Trait scoring is player-only and ply-based from schema v2 fields (`your_color`, per-side counts, side plies), with strict payload validation before use.
- Trait windows are schema-aware: v1/invalid payloads are excluded, never mixed with v2, and confidence is forced LOW with explicit `insufficient v2 payloads` reason when coverage is short.
- Backfill supports explicit forced rebuild (`--rebuild-payloads` / `--backfill-reset`) to overwrite stale payloads with v2 deterministic payloads.
- `TRAITS_DEBUG` now exposes per-payload player accounting and rates (player plies, player counts, error rates, window rates), making denominator/unit correctness directly auditable.
