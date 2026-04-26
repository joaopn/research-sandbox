# Supervisor session — <YYYY-MM-DD HH:MM UTC>

**Date:** <YYYY-MM-DD>
**Start:** <HH:MM UTC>
**Workers this session:** <comma-separated, e.g. stats, lang_coverage, outliers>

<!--
Chronological, detailed log of THIS session. Written at /log time, immutable
after that. This is what the next supervisor session (possibly a fresh
claude process after /clear) reads on cold resume to understand what was
decided, what was tried, and where to pick up.

Audience: the next supervisor agent. Not the PI. Expand freely — longer is
fine here if it saves future rediscovery. The PI's reading lives in the
companion PI topic log(s) referenced at the bottom.
-->

## Approach

<How you decomposed the PI's question(s) into worker tasks this session.
Why this particular split? Alternatives considered and rejected, with the
reason — this prevents the next supervisor from relitigating settled
decisions. If you iterated mid-flight (respawned a worker, amended a plan,
dropped a planned worker, changed the slug naming strategy), note the
trigger and what you learned.>

## Per-worker notes

<!-- Repeat this block per worker touched this session. Includes fresh
spawns, respawns of prior-session `down` workers, and workers iterated via
message. -->

### <worker-name>

- **Thematic identity (from summary.md):** <top line of /workspace/workers/<name>/work/summary.md>
- **Registry state at /log:** down (previously: live — or: fresh this session / respawned from down)
- **Cycles accepted this session:**
  - `<NNN>_<slug>`: one-line result
  - …
- **Iteration notes:** <what was corrected or re-scoped mid-session; which cycles were rejected at staging and why; omit if none>
- **Surprises:** <what contradicted the going-in expectation; what the next supervisor should not take for granted>
- **Caveats / assumptions:** <gaps in the brief the worker papered over, data ambiguities, etc.>
- **Deliverables:** `results/<name>/<NNN>_<slug>/` (accepted cycles this session); `workers/<name>/work/outputs/<slug>/` (all attempts including rejected)
- **Pointers:**
  - Process log: `workers/<name>/work/research_log.md`
  - Memory: `workers/<name>/work/summary.md`

## Open threads for the next session

- <Follow-ups the PI flagged but deferred>
- <Anomalies the supervisor noticed but didn't chase this session>
- <Decisions postponed>

## Cross-references

PI topic logs emitted by this `/log`:

- `../pi/<YYYY-MM-DD>-<topic-slug>.md`
- …
