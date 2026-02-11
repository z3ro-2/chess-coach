Generate a chess review in Markdown using EXACTLY the engine payload.

Payload JSON:
```json
{payload}
```

For each key position:
- Mention the played move (`played_san`)
- Mention the engine’s best alternative (`best_san`)
- Repeat the engine label (blunder/mistake/etc.)
- Explain why the label applies based only on the payload flags (tactical_flag, material_change, mate_threat, forcing)

Do NOT suggest any move not present in the payload.

Do not:
- Suggest any moves not present in the payload
- Infer additional tactics or lines
- Rewrite move quality beyond provided labels

Use clear, human coaching language.
