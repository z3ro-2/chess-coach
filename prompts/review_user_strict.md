Generate a chess review in valid Markdown using ONLY the structured engine payload provided below.

You must treat the payload as authoritative truth. Do not use any chess knowledge beyond what is explicitly contained in the payload.

INPUT DATA (DO NOT REPEAT IN OUTPUT):
--------------------------------------
{payload}
--------------------------------------
Do NOT reproduce the input data in your output.

OUTPUT FORMAT (FOLLOW EXACTLY):

Start with YAML front matter (no code block):

---
date_utc: <from game_summary>
your_color: <from game_summary>
opponent: <from game_summary>
result: <from game_summary>
time_control: <from game_summary>
rated: <from game_summary>
url: <from game_summary>
---

# Game Review

## Summary
Write 2–4 short bullet points based ONLY on:
- label_counts
- frequency of blunders, mistakes, inaccuracies
- repeated tactical_flag patterns

Each bullet must be one short sentence.

## Key Inflection Points

For EACH key position in the payload, use EXACTLY this structure:

### Move <move_number> – <player>
Label: <label>

You played: `<played_san>`
Engine preferred: `<best_san>`

(If best_san is null, write: Engine preferred: No alternative provided)

Explanation:
- If material_change is not zero, state the material change.
- If material_change is negative, explicitly say material was lost.
- If mate_threat is true, explicitly say the move allowed a mating threat.
- If tactical_flag == "hanging_piece", say an undefended piece was involved.
- If tactical_flag == "tactical_miss", say a tactical opportunity was missed.
- If forcing is true, say the position became forcing.

Keep explanation to 2–4 short sentences. No extra commentary.

## What to Watch For Next Time
Write 2–3 bullets derived ONLY from repeated:
- labels
- tactical_flag values

## Training Plan
Suggest 3–5 concrete training actions derived ONLY from:
- hanging_piece → blunder-check habit
- tactical_miss → tactical puzzle training
- mate_threat → king safety drills
- frequent inaccuracies → slower move selection

STRICT RULES:
- Do NOT invent moves.
- Do NOT mention any move not equal to played_san or best_san.
- Do NOT suggest variations.
- Do NOT reference openings, strategy, pawn structure, or positional concepts.
- Do NOT reinterpret labels.
- Do NOT add narrative or storytelling.
- Do NOT add extra sections.

Use short sentences.
Direct language.
No fluff.