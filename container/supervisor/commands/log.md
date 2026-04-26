---
description: Close this session — summarize workers, write the two-stream logbook, shut down
---

End this session cleanly: have every live worker write its own `summary.md`, write one supervisor session log + N PI topic logs, then shut all live workers down to registry state `down`.

## Step 1 — Preconditions

Every live worker must be in runtime state `waiting` (idle, ready for input). If any is `working`, **refuse to proceed**. Report:

> /log: cannot log while workers are running: `<name>`, `<name>`.
> Wait for completion (rs-worker wait) or message them to stop before logging.

Then wait for them to return to `waiting` and retry. No `--force` escape hatch.

Run `rs-worker list` (without `--all`) to get the set of live workers. For each, confirm `state == "waiting"`.

## Step 2 — Summary round

For each live worker, send the summarize-and-shutdown message (exact text below — the worker's CLAUDE.md references this structure). Use `rs-worker message <name> "<text>"`.

```
This session is ending. Write a ≤1000-token session summary to /workspace/summary.md,
overwriting any prior contents, in exactly this structure:

# <your thematic identity, one sentence — be specific; this is what the
#  supervisor reads next session when deciding whether to reopen you>

## Accepted cycles
- <ordinal>_<slug>: one-line result
...

## Caveats and rejected attempts worth remembering
- ...

## Open threads for future sessions
- ...

## Pointers
- Full detail: research_log.md
- Prior deliverables: outputs/<slug>/

Do no analysis work. Just the summary. After writing, touch /workspace/WAITING
and stop. The supervisor will shut you down shortly.
```

Then `rs-worker wait <all live workers> --all` to confirm each returns to `waiting` with its `summary.md` refreshed.

Spot-check: for each worker, verify `/workspace/workers/<name>/work/summary.md` exists and its first non-empty line looks like a thematic identity sentence. If any worker's summary is missing or empty, re-send the message and wait again before proceeding.

## Step 3 — Supervisor session log (one file)

Write exactly **one** supervisor session log at:

```
/workspace/logbook/supervisor/<YYYY-MM-DD>-<HHMM>.md
```

`<YYYY-MM-DD>`: today's UTC date. `<HHMM>`: the `/log` command's start time, UTC, no separator (`1530`, not `15:30`). On filename collision, append `-2`, `-3`, … until free.

Use `/workspace/.claude/logbook_supervisor_template.md` as the structure. Fill every section. Cover:

- **Approach** — why this session's decomposition; alternatives rejected; mid-flight iterations. Detailed, not terse — the next supervisor session reads this.
- **Per-worker blocks** — one per worker touched this session (spawned fresh OR respawned from `down` OR iterated via message). For each:
  - Thematic-identity top line from that worker's `summary.md`.
  - Registry state at `/log` (was-live-now-down).
  - Cycles accepted this session, with slugs + `results/<name>/<NNN>_<slug>/` pointers.
  - Iteration notes (what was corrected mid-session, why first attempts failed).
  - Surprises (what contradicted your going-in expectation).
  - Pointers: `workers/<name>/work/research_log.md`, `workers/<name>/work/summary.md`.
- **Open threads for the next session** — things deferred, follow-ups flagged.
- **Cross-references** — the PI topic logs this `/log` emits (see step 4); list them.

## Step 4 — PI topic log(s) (one per coherent topic)

Decide partition: how many coherent topics did this session cover?

- Related workers all feeding one PI question → **one** PI topic log covering them all.
- Multiple independent PI questions → **one per topic**.
- Mix → group by coherent topic, one per group.

For each group, write:

```
/workspace/logbook/pi/<YYYY-MM-DD>-<topic-slug>.md
```

`<topic-slug>`: kebab-case, 2–4 words, captures the topic. Collision handling: append `-2`, `-3`, …

Use `/workspace/.claude/logbook_pi_template.md`. Each file's header must include:

```
**Date:** <YYYY-MM-DD>
**Source:** [../supervisor/<YYYY-MM-DD>-<HHMM>.md](../supervisor/<YYYY-MM-DD>-<HHMM>.md)
**Workers:** <comma-separated names>
```

Load-bearing PI-layer sections to spend real effort on:

- **Results** — 3-6 sentences of narrative. The "if you read nothing else, read this" paragraph. Reference specific deliverable paths (`results/<name>/<NNN>_<slug>/`) so the PI can drill in.
- **Next session pickup** — 1-3 sentences. If a fresh supervisor resumes here and the PI says "continue", what is the obvious first move? This is what makes `/clear` safe.

Do **not** duplicate the supervisor session log's `Approach` / iteration notes in the PI log — those stay in the supervisor log.

## Step 5 — Shutdown round

For each live worker (in any order):

```bash
rs-worker shutdown <name>
```

Expected JSON: `{"name": "<name>", "state": "down", "last_down_at": "..."}`.

Under the hood: `docker stop` (SIGTERM → entrypoint trap → `DONE` → exit), then `docker rm`. Bind-mount preserved. Registry → `state: down`.

After all shutdowns, `rs-worker list` (without `--all`) should return an empty array.

## Step 6 — Report back to the PI

Reply with, in this order:

1. The supervisor session log path.
2. The list of PI topic log paths.
3. Which workers were shut down (names).
4. A one-line summary of what was captured.

**Do not** `/clear` the session yourself, destroy any containers, or modify any files outside of the logbook writes and the shutdown round. `/log` is append-only for logbook files, and state-transitioning for workers. Nothing else.

## Immutability

Once written, the supervisor session log and PI topic logs are **never edited**. Corrections become new entries in the next `/log`.
