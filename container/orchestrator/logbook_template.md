# <Title — a natural sentence reflecting the PI's question>

**Date:** <YYYY-MM-DD>
**Slug:** <slug>
**Workers:** <comma-separated names, e.g. stats1, orphan1>

## PI's question

<One paragraph. What did the PI actually ask this session? Use their words or a close paraphrase. Include enough context that a future reader who missed the conversation can understand what motivated the work.>

## Approach

<2-4 sentences. How did you decompose the question into worker tasks? Why this particular split? Any alternatives you considered and rejected, if relevant.>

## Workers

### <worker-name>

- **Plan:** `plan/archive/<name>.md`
- **Status:** accepted | waived (reason: …) | failed-then-respawned | destroyed-unaccepted
- **Deliverables:** `workers/<name>/work/outputs/` — `<file1>`, `<file2>`, …
- **Key findings:**
  - <3-5 bullets. Concrete numbers where possible.>
- **Caveats / assumptions:**
  - <Anything the worker assumed or papered over. Missing data, ambiguous filters, etc.>

<!-- Repeat the above block per worker touched this session. -->

## Synthesis for the PI

<3-6 sentences of narrative. The "if you read nothing else, read this" paragraph. This is what future-PI will read in two weeks after forgetting the context, and what a fresh claude session reads to re-enter the project cold. Optimize for clarity over completeness — specific details live in the per-worker sections above.>

## Open threads

- <Follow-ups the PI flagged but deferred>
- <Anomalies the supervisor noticed but didn't investigate this session>
- <Decisions postponed for a later session>

## Next session pickup

<1-3 sentences. If a fresh claude session starts from this logbook entry and the PI says "let's continue", what is the obvious first move? A specific next worker to spawn? A question to ask the PI? An open thread to pick up? This is what makes `/clear` safe.>
