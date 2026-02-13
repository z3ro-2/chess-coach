Return raw JSON only.
Output MUST start with "{" and end with "}".
Do not include any leading/trailing whitespace or commentary.
Do not include any text before or after the JSON object.
Do not wrap output in markdown, code fences, prose, or comments.
If you cannot produce exactly valid JSON, output this error object only.
If you cannot comply, output this minimal valid object only:
{"game_overview":"Insufficient compliant output.","critical_mistakes":[],"strengths":[],"training_focus":[],"confidence":"LOW"}

You are generating a structured chess game review.
Use only distilled_insights and context in the payload.
Do not invent facts, moves, or analysis.
Do not add extra keys. Do not omit required keys.

Required JSON schema (exact keys, exact structure):
{
  "game_overview": string,
  "critical_mistakes": [
    {
      "move_number": integer,
      "description": string,
      "why_it_matters": string,
      "improvement_tip": string
    }
  ],
  "strengths": [string],
  "training_focus": [string],
  "confidence": "LOW" | "MEDIUM" | "HIGH"
}

Payload JSON:
{payload}
