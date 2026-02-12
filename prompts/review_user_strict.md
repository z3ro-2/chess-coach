Return Markdown only.

Use this exact structure and headings with no additions:

---
engine: Stockfish
engine_depth: <game_summary.engine_depth>
date_utc: <game_summary.date_utc>
your_color: <game_summary.your_color>
opponent: <game_summary.opponent>
result: <game_summary.result>
time_control: <game_summary.time_control>
rated: <game_summary.rated>
url: <game_summary.url>
---

# Game Review

## Summary
- <bullet 1>
- <bullet 2>
- <bullet 3>

## Four Critical Positions

### 1. Move <move_number> – <player>
Label: <blunder|mistake|inaccuracy|good|brilliant>
Played: `<played_san>`
Engine: `<best_san>`
Explanation: <max 2 short sentences>

### 2. Move <move_number> – <player>
Label: <blunder|mistake|inaccuracy|good|brilliant>
Played: `<played_san>`
Engine: `<best_san>`
Explanation: <max 2 short sentences>

### 3. Move <move_number> – <player>
Label: <blunder|mistake|inaccuracy|good|brilliant>
Played: `<played_san>`
Engine: `<best_san>`
Explanation: <max 2 short sentences>

### 4. Move <move_number> – <player>
Label: <blunder|mistake|inaccuracy|good|brilliant>
Played: `<played_san>`
Engine: `<best_san>`
Explanation: <max 2 short sentences>

## Recurring Tactical Pattern
<one short paragraph>

## Training Plan
- <item 1>
- <item 2>
- <item 3>

Hard rules:
- Exactly 4 critical positions. No more, no fewer.
- Use label verbatim from payload.
- Use only played_san and best_san from payload.
- Do not suggest any move not present in payload.
- No headings other than those shown above.
- No extra sections, no variation, no creative formatting.

Payload JSON:
{payload}
