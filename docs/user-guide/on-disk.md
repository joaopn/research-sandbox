---
title: What you get on disk
parent: User guide
nav_order: 6
---
# What you get on disk

Everything lives under `container_volumes/<proj>/workspace/`. The bits you'll actually open:

- **`results/<worker>/<NNN>_<slug>/`** — every accepted cycle, numbered. Notebook(s), data, figures, + a snapshot of the plan that produced it.
- **`logbook/pi/<date>-<slug>.md`** — executive summaries for you, one per topic per session, with `**Source:**` links down to the supervisor's log.
- **`logbook/supervisor/<date>-<HHMM>.md`** — the supervisor's chronological notes (drill-down).
- **`workers/<worker>/work/`** — each worker's full sandbox: notebooks (clean + scratch), `research_log.md`, every cycle.
- **`plan/<worker>.md`** — the canonical plan bound to each worker; **`staging/<worker>`** — present only when a cycle awaits your accept.

Logs are append-only; the supervisor never edits worker outputs by hand; plans go through an approve gate.
