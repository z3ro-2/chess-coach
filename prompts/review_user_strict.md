Return raw JSON only.
Do not output prose outside JSON.
Do not wrap output in code fences.
Do not wrap output in markdown, code fences, prose, or comments.
Do not include any text outside JSON.
If you cannot produce exactly valid JSON, output this error object only: {"error":"FORMAT_VIOLATION"}.

You are generating a structured chess game review.

You MUST strictly follow the required JSON schema.
You MUST NOT add extra keys.
You MUST NOT omit required keys.
You MUST NOT include null values.
You MUST NOT include markdown, commentary, or explanation.
Output must begin with '{' and end with '}'.

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

Schema reminder (must match exactly):
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

Field Rules:

- game_overview:
  2–4 concise sentences summarizing how the game unfolded.
  Base strictly on engine facts and distilled_insights.

- critical_mistakes:
  Use ONLY moves present in distilled_insights.top_3_worst_moves.
  If no qualifying mistakes exist, return an empty list [].
  Do not invent moves.
  Do not infer unlisted mistakes.

- strengths:
  1–4 short bullet-style strings describing what the player did well.
  Must be grounded in distilled_insights.

- training_focus:
  1–4 short actionable training recommendations.
  Must directly correspond to the mistakes or error patterns shown in distilled_insights.

- confidence:
  Choose one of: LOW, MEDIUM, HIGH.
  Base it strictly on the quality and completeness of distilled_insights.

Authoritative Data Source:

Use distilled_insights as the authoritative source.
Do not invent facts.
Do not reference data not present in the payload.

Payload JSON:
{payload}
