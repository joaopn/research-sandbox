---
description: Write a logbook entry summarizing this session
---

Write a logbook entry for the current session.

**Slug:** pick one yourself — a short kebab-case identifier (2-4 words) that captures the central question or finding of this session. Examples: `basic-stats`, `tagalog-anomaly`, `null-lang-investigation`. Do not ask the PI to provide it; you have the context. If you're uncertain between two options, pick the more specific one.

**Target path:** `/workspace/logbook/<YYYY-MM-DD>-<slug>.md` using today's UTC date. If the target already exists, append `-2`, `-3`, etc. until you find a free name.

**Template:** `/workspace/.claude/logbook_template.md`. Read it first and use its exact section structure.

**Source material:**
- This session's conversation with the PI (what was asked, what you reported back).
- `/workspace/plan/archive/*.md` — plans for workers accepted this session.
- `/workspace/workers/<name>/work/research_log.md` — per-worker process logs.
- `/workspace/workers/<name>/work/outputs/` — to verify the deliverable file list.
- `rs-worker list` — to enumerate which workers exist and their accepted state.

**Scope:** include only the workers this session interacted with — not every worker ever spawned in the project. If unsure which workers belong to this session, ask the PI once, then proceed.

**Two-layer structure.** The template splits at a horizontal rule into a PI layer (top) and a supervisor layer (bottom). The PI will mostly only read above the divider; the next supervisor session reads the whole thing. Write accordingly:

- **PI layer** (question → results → open threads → next pickup): tight, complete enough that a technical PI can drill into worker deliverables from the pointers alone. Reference specific output paths in *Results* so the PI doesn't need the supervisor layer to verify a finding.
- **Supervisor layer** (approach → workers): this is the memory the next supervisor session inherits. Expand freely — longer is better if it saves rediscovery. Spend real effort on *Approach* (why this decomposition, what you rejected and why) and the per-worker *Surprises* + *Iteration notes* fields, which are where the next session's time savings come from.

**Load-bearing sections** (spend real effort on these; the rest is reference):
- **Results** (PI layer) — 3-6 sentences of narrative. The "if you read nothing else, read this" paragraph. Written for future-PI reading this in two weeks after forgetting the context.
- **Next session pickup** (PI layer) — 1-3 sentences. If a fresh claude session starts here and the PI says "continue this thread", what's the obvious first move? This is what makes `/clear` safe.
- **Approach** (supervisor layer) — why this split, alternatives rejected, mid-flight iterations. Prevents the next supervisor from relitigating settled decisions.
- **Per-worker Surprises / Iteration notes** (supervisor layer) — calibrate the next supervisor on what not to take for granted, and on which workers had to be corrected and why.

**After writing:**
1. Read the file back to verify it's well-formed.
2. Reply with: the final path, a one-line summary of what was captured, and the list of workers included.

**Do not** `/clear` the session, destroy any containers, or modify any other files. `/log` is additive only.
