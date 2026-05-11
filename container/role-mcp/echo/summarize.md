# echo-mcp summarize prompt

You are summarizing echo-mcp call logs. Echo is a no-op test role and has
no real skills to distill — your job is bounded.

Read the **New per-call logs** section below. Count the entries (each one
has its own ``# <caller> / <call_id>`` header). Emit **one** append-only
entry in this exact shape:

```
## <ISO 8601 timestamp — use the current time>
Echo processed N calls. No skills distilled (no-op role).
- Latest call_id: <last call_id seen, the one with the largest timestamp prefix>
- Callers in this batch: <comma-separated unique caller names>
```

Where:
- `N` = total count of per-call entries in this batch.
- `<ISO 8601 timestamp>` = right now, format `YYYY-MM-DDTHH:MM:SSZ`.

Output ONLY this entry — no commentary, no explanation, no markdown
preamble. The daemon appends what you emit verbatim to `global.md`.
