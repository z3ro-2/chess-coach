# Label Counts Investigation Note

## Scope
- Trace exact creation path for `game_summary.label_counts` in engine payloads.
- Verify unit, side coverage, and label semantics.
- Verify `key_positions` contents/scope vs full-ply analysis.

## Findings

### 1) Where `label_counts` is created
- Canonical assignment is in `engine/stockfish_oracle.py:71` and `engine/stockfish_oracle.py:144`.
  - `label_counts` starts at zero and is incremented once per analyzed mainline ply inside the PGN loop.
  - PGN loop is `for ply_index, move in enumerate(mainline_moves, start=1)` (`engine/stockfish_oracle.py:89`).
- Returned under `game_summary` at `engine/stockfish_oracle.py:188`.

### 2) Unit: ply vs move
- `label_counts` is per **ply** (half-move), not per full move.
- Evidence:
  - Increment happens once per ply iteration (`engine/stockfish_oracle.py:89`, `engine/stockfish_oracle.py:144`).
  - `total_plies = len(mainline_moves)` (`engine/stockfish_oracle.py:186`).
  - `total_moves = ceil(total_plies/2)` via `(len(mainline_moves)+1)//2` (`engine/stockfish_oracle.py:187`).
  - Schema validator enforces `sum(label_counts) == total_plies` (`engine/payload_schema.py:154`).

### 3) Side coverage: both sides vs player-only
- Raw oracle `label_counts` is **both sides combined**.
- Oracle also emits side split as `label_counts_by_side.{white,black}` (`engine/stockfish_oracle.py:78`, `engine/stockfish_oracle.py:189`).
- Per-player fields are added after oracle output using `enrich_summary_with_player_fields(...)`:
  - `player_total_plies`, `player_total_moves`, `player_label_counts` (`engine/payload_schema.py:93`, `engine/payload_schema.py:97`).
  - Called in ingestion paths before persistence/validation:
    - `analysis_pipeline.py:35`
    - `chess_review.py:617`
    - `backfill.py:73`

### 4) Label semantics: played move vs best move
- Label is from **played move outcome vs engine best line**, not “best move quality”.
- Computation:
  - Best move/eval before move: `best_move`, `best_eval` (`engine/stockfish_oracle.py:114`, `engine/stockfish_oracle.py:115`).
  - Played move eval after push: `eval_after` (`engine/stockfish_oracle.py:125`, `engine/stockfish_oracle.py:131`).
  - Classification uses `loss = max(0, best_eval - played_eval)` (`engine/stockfish_oracle.py:31`).
  - Thresholds map to `good/inaccuracy/mistake/blunder`; special “brilliant” branch only when played move is best and improves strongly (`engine/stockfish_oracle.py:33`-`engine/stockfish_oracle.py:41`).

### 5) Does oracle produce labels per ply and include player identifier?
- Yes.
- Per-ply row includes `player` and `move_number`, plus SAN fields and label:
  - row construction: `engine/stockfish_oracle.py:159`-`engine/stockfish_oracle.py:169`.
  - public key-position projection keeps `player`, `move_number`, `played_san`, `best_san`, `label` (`engine/stockfish_oracle.py:365`-`engine/stockfish_oracle.py:376`).

### 6) Are `key_positions` all moves or swing-only?
- `key_positions` are **not all plies**.
- Oracle tracks all plies in `all_positions` (`engine/stockfish_oracle.py:69`, `engine/stockfish_oracle.py:171`).
- `key_candidates` are filtered (non-good, forcing, or material change) (`engine/stockfish_oracle.py:172`-`engine/stockfish_oracle.py:173`).
- Selector returns exactly 4 by swing priority/fallback/duplication (`engine/stockfish_oracle.py:298`-`engine/stockfish_oracle.py:338`).

### 7) Canonical payload schema in `payload_json`
- `game_summary` fields validated as canonical by `validate_engine_payload(...)`:
  - `schema_version`, `total_plies`, `total_moves`, `label_counts`, `label_counts_by_side`, `your_color`, `player_total_plies`, `player_total_moves`, `player_label_counts`, plus result/context fields (`engine/payload_schema.py:101`-`engine/payload_schema.py:214`).
- `key_positions` expected as list (strict mode requires exactly 4) (`engine/payload_schema.py:159`-`engine/payload_schema.py:164`).
- LLM-safe projection preserves the key position move-vs-best fields (`llm/safe_payload.py:48`-`llm/safe_payload.py:61`).

## What is missing for player-only aggregation?
- For stored schema-v2 payloads generated through ingestion paths: nothing critical is missing.
  - `player_label_counts` and `label_counts_by_side` are present/validated.
- For raw oracle output alone (before enrichment): `your_color` and player-projected fields are not intrinsic.
  - Enrichment step is required to derive player-only counts (`engine/payload_schema.py:93`-`engine/payload_schema.py:98`).

## Proposed schema hardening change
- Keep `label_counts_by_side` as mandatory canonical source in `game_summary`.
- Treat `label_counts` as derived redundancy (validate but never use as primary in player-only metrics).
- Require `your_color`, `player_total_plies`, `player_label_counts` at persistence boundaries (already enforced in strict validation paths).
- Optional stronger future extension: add `move_annotations` (one row per analyzed ply with `ply_index`, `player`, `label`, `played_san`, `best_san`) for full re-aggregation/debug provenance.
