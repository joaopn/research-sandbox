# <Title — a natural sentence reflecting the PI's question>

**Date:** <YYYY-MM-DD>
**Slug:** <slug>
**Workers:** <comma-separated names, e.g. stats1, orphan1>

<!-- ────────────────────────────────────────────────────────────────
     PI layer. Read top-down; stop at the divider unless you want
     the supervisor-internal details.
     ──────────────────────────────────────────────────────────────── -->

## PI's question

<One paragraph. What did the PI actually ask this session? Use their words or a close paraphrase. Include enough context that a future reader who missed the conversation can understand what motivated the work.>

## Results

<3-6 sentences of narrative. The "if you read nothing else, read this" paragraph. This is what future-PI will read in two weeks after forgetting the context, and what a fresh claude session reads to re-enter the project cold. Concrete numbers where they matter. Reference specific deliverable files by path (`workers/<name>/work/outputs/…`) so the PI can verify without reading on.>

## Open threads

- <Follow-ups the PI flagged but deferred>
- <Anomalies the supervisor noticed but didn't investigate this session>
- <Decisions postponed for a later session>

## Next session pickup

<1-3 sentences. If a fresh claude session starts from this logbook entry and the PI says "let's continue", what is the obvious first move? A specific next worker to spawn? A question to ask the PI? An open thread to pick up? This is what makes `/clear` safe.>

---

*PI layer ends here. Below: supervisor-internal notes for the next session. Expand freely — longer is fine here if it saves future rediscovery.*

## Approach

<How you decomposed the question into worker tasks and why this particular split. Alternatives you considered and rejected, with the reason — this is what keeps the next supervisor from relitigating settled decisions. If you iterated (respawned a worker, amended a plan mid-flight, dropped a planned worker), note the trigger and what you learned.>

## Workers

### <worker-name>

- **Plan:** `plan/archive/<name>.md`
- **Status:** accepted | waived (reason: …) | failed-then-respawned | destroyed-unaccepted
- **Deliverables:** `workers/<name>/work/outputs/` — `<file1>`, `<file2>`, …
- **Key findings:**
  - <3-5 bullets. Concrete numbers where possible.>
- **Caveats / assumptions:**
  - <Anything the worker assumed or papered over. Missing data, ambiguous filters, etc.>
- **Surprises:**
  - <Anything that contradicted the supervisor's expectation going in. The next supervisor should know what not to take for granted.>
- **Iteration notes** (omit if none):
  - <What was corrected mid-run, or why the first attempt failed. Skip if the worker succeeded first try.>

<!-- Repeat the above block per worker touched this session. -->
