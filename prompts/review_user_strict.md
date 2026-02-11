Generate a chess review in valid Markdown using ONLY the structured engine payload provided below.

You must treat the payload as authoritative truth. You are not allowed to use any chess knowledge beyond what is explicitly contained in the payload fields.

Payload JSON:
```json
{payload}
```

STRICT OUTPUT RULES:

1) The output MUST begin with YAML front matter using triple dashes (---), not a code block.
   Example format:

---
date_utc: <from game_summary>
your_color: <from game_summary>
opponent: <from game_summary>
result: <from game_summary>
time_control: <from game_summary>
rated: <from game_summary>
url: <from game_summary>
---

2) After YAML, include these sections in order:

# Game Review

## Summary
- 2–4 short bullets derived ONLY from label_counts and key_positions.

## Key Inflection Points
For EACH key position in the payload:
- State the move number.
- State the player.
- State the engine label EXACTLY as given.
- Show the played move using `played_san`.
- Show the engine best move using `best_san` (if null, say "No alternative provided").
- Explain the label ONLY using these payload fields:
  - tactical_flag
  - material_change
  - mate_threat
  - forcing

You MUST NOT:
- Invent any moves.
- Mention any move not equal to played_san or best_san.
- Refer to openings, strategy, pawn structure, or positional concepts unless directly implied by tactical_flag or material_change.
- Paraphrase or reinterpret the label.

If material_change is negative:
- Explicitly state that material was lost.

If mate_threat is true:
- Explicitly state that the move allowed a mating threat.

If tactical_flag == "hanging_piece":
- Explicitly state that an undefended piece was involved.

If tactical_flag == "tactical_miss":
- State that a tactical opportunity was missed.

## What to Watch For Next Time
- Derive patterns ONLY from repeated labels or tactical_flag values.

## Training Plan
- Suggest 3–5 concrete training actions derived ONLY from observed tactical_flag patterns (e.g., hanging pieces → blunder-check drills).

Do NOT:
- Add narrative storytelling.
- Add speculative explanations.
- Add new chess ideas not present in the payload.
- Add example lines or variations.

Use clear, direct coaching language. Keep explanations short and concrete.
