---
title: The four-section plan
parent: User guide
nav_order: 11
---
# The four-section plan

`rs-worker spawn` validates the plan structurally. Missing or malformed sections refuse the spawn:

```
## Question
What specifically is this worker answering this cycle?
(On respawn this is the new cycle's question — the worker also has summary.md
as prior-session memory.)

## Inputs
Explicit paths the worker needs (e.g. /workspace/mnt/<name>/...).
Any assumptions about format, schema, size.

## Deliverables
What the worker must produce in /workspace/outputs/<slug>/:
  - notebook name(s)
  - data files (CSV, parquet, etc.)
  - figures
Plus what must appear in research_log.md's `## Cycle <slug>` section.
Explicitly name the slug you chose.

## Verification
How the supervisor will know the deliverable is correct. Concrete: expected
row counts, numeric ranges, shape of the output, sanity checks the worker
itself must run before returning to WAITING.
```

Plans live at two paths during their lifetime:
- **Draft** (`plan/draft/<name>.md`) — supervisor wrote it, awaiting PI approval. PI-visible, not yet authoritative.
- **Canonical** (`plan/<name>.md`) — harness-owned. Written *only* by `rs-worker spawn` from the draft. Editing it directly is the easiest way to break things.

`accept` snapshots `plan/<name>.md` into `results/<name>/<NNN>_<slug>/plan.md` — accepted cycle bundles preserve the plan that produced them, so respawning a worker with a new plan never loses the history of what produced past results.
