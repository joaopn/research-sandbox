# Extraction workflow for the wrangler

The arc from "task arrives" to "extract published + log written." Read
this on your first call for a caller, or any time the task is more
than a one-shot exploration.

## The arc

```
inventory tools  →  explore schema  →  draft query  →  iterate small
                                              ↓
                                       full-size run
                                              ↓
                                  publish artifact + log
```

Each arrow is an opportunity for retry-memory to fire. Before each
step, check whether `/workspace/memories/<caller>/*.md` already
recorded a failure at this transition for a similar task.

## Retry-memory: how to use it without paralysis

Retry-memory is **other versions of you, talking to you across time
through files**. The discipline:

1. **Read prior `## What failed` sections first.** Don't draft your
   approach in a vacuum — at least skim the most recent 5-10 logs
   from this caller. Most recent first; the oldest may be stale.
2. **If a prior call failed at step X with cause Y, do not repeat
   step X without addressing Y.** Either change approach, or
   explicitly note "Y has been resolved by Z; retrying with mitigation
   W" in your `## Approach`.
3. **Don't be paralyzed by stale failures.** A failure logged a week
   ago may no longer apply (upstream schema changed, partition added,
   MCP version bumped). Use judgment; document your judgment.
4. **Write your own failures specifically.** "Query timed out" is
   useless to future-you. "Query `SELECT … WHERE timestamp >
   '2024-01-01'` timed out at 30 s on the read-mongo MCP; suspect
   missing index on `timestamp` since EXPLAIN showed a `COLLSCAN`" is
   useful. Be the colleague you wish you had.

## When to use a scheduler vs. inline

If the tools-inventory lists a query-scheduler MCP and your task is
one of:

- **Long-running aggregations** (> ~30 s expected runtime, where
  inline MCPs may time out).
- **Large COPY/export** to disk that the read MCP can't stream.
- **Anything labeled "extraction" rather than "exploration"** when
  the result is multi-million-row.

…submit through the scheduler. Otherwise run inline through the
read-only DB MCP.

If no scheduler is listed and the task seems to need one (large
aggregation, write-side anything), exit with `outcome: needs_human`
and explain in your log. The supervisor or operator will provision a
scheduler MCP.

## The publish surface

Inside the container, your publishable artifacts go under
`/workspace/published/`. From the host this is the role's public
artifact directory; future cross-role consumers (analysis workers,
paper-writer) read it RO. Stay disciplined:

- `/workspace/published/extracts/<topic>/<slug>.parquet` — the data.
- `/workspace/published/extracts/<topic>/<slug>.sql` — the verbatim
  final query (the one that produced the parquet, not the failed
  drafts; those live in your per-call log).
- `/workspace/published/extracts/<topic>/<slug>.metadata.json` —
  provenance, schema, shape. Template in role.md.

**Never** put debug dumps, intermediate query outputs, or your scratch
work under `/workspace/published/`. That dir is for what you'd hand a
colleague — published artifacts only. Failed query attempts and dead-end
SQL belong in your per-call log's `## What failed` section as **inline
text**, not as standalone files anywhere on disk.

## Topic slug conventions

If the caller's task starts with `Topic: <slug>`, use exactly that
slug for the `<topic>` directory. If not, pick a slug:

- Kebab-case, lowercase, ASCII, no spaces.
- Domain-then-shape: `subreddit-counts-2024`, `comments-pii-audit`,
  `events-per-user-q3`.
- ≤ ~40 characters; longer is OK if necessary but unwieldy.
- **Stable across the project** — if a similar task comes in next
  week from a different caller, use the same `<topic>` so the
  extracts cluster. The per-extract `<slug>` disambiguates.

## When two callers want the same slug

If you'd write to `extracts/<topic>/<slug>.parquet` and that file
already exists from a prior call, suffix with your call_id:
`<slug>-<call_id>.parquet`. The `metadata.json` carries the call_id
either way; consumers reading `extracts/<topic>/` should sort by
metadata's `ts` field, not by filename.

## What goes in `## What worked` vs `## Lessons`

- **`## What worked`**: the specific queries, paths, observations that
  produced the answer for this call. Caller-specific. Concrete.
- **`## Lessons`**: generalizable. "The cross-year join uses
  `event_id`, not `(user, timestamp)`." "The mongo MCP's
  `aggregate` tool reliably handles pipelines up to ~5 stages; beyond
  that, switch to the scheduler."

Summarize at `/log` boundary lifts `## Lessons` content (across many
calls, many callers) into `global.md`. So **the more general your
`## Lessons` entries, the more you help future-you across the project**.
Caller-specific tactics in `## What worked` are fine — they're for the
same caller's next call to read directly, not for global graduation.

## A note on `metadata.json` discipline

For every `.parquet`/`.csv`, the sibling `.metadata.json` must exist
**before you exit**. Consumers of `extracts/` may discover artifacts
via `find extracts/ -name '*.metadata.json'`; a parquet without
metadata is a dangling artifact that confuses everyone. If you produce
multiple parquets in one call, write multiple metadata files.

`row_count` and `schema` are load-bearing for downstream consumers
(analysis workers reading the extract often want to know whether they
should `pd.read_parquet` vs. stream-process based on size). Get them
right; estimate with a clear "approx" note if exact is too expensive
to compute.
