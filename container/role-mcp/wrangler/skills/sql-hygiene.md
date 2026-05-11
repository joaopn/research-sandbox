# SQL hygiene for the wrangler

Project-agnostic discipline for writing queries against whatever
read-only DB MCP this project gives you. Read this on your first call
for a caller, or any time the task shape is unfamiliar.

## DQL only — never write to the database

You are read-only. Every query you send must be a `SELECT`, `EXPLAIN`,
schema introspection (`information_schema`, `\d`, `db.collection.indexInfo()`,
etc.), or an equivalent read-only call. If the task asks for `INSERT`,
`UPDATE`, `DELETE`, `CREATE`, `DROP`, `ALTER`, `TRUNCATE`, or any
"write the result back to the DB" operation, exit with
`outcome: needs_human` and explain in your per-call log why.

The exception some MCPs offer: a **query-scheduler** with an
auto-approve mode for long-running read-side `COPY`/aggregation jobs.
That's still read-side from the database's perspective; the scheduler
arbitrates resource budget, not write capability. Read its description
in `.tools-inventory.md` before using it.

## Explore before you extract

For non-trivial extractions, run two preliminary queries first:

1. **Schema introspection.** Confirm the columns and types you assume
   exist. Joins fail subtly when a column is `nullable` and your
   `INNER JOIN` silently drops rows; aggregations lie when a column is
   `text` not `numeric` and `SUM` silently casts. Look before you
   leap.
2. **`SELECT … LIMIT N`.** Pull a small sample to confirm the data
   shape matches your mental model. "Per-month counts" is a different
   query depending on whether timestamps are `epoch_seconds`,
   `epoch_millis`, or ISO strings.

Capture both in `## What worked` of your per-call log — they're cheap
to re-do and future calls benefit from seeing them.

## EXPLAIN for non-trivial queries

If the query touches more than one table, has a `GROUP BY`/`ORDER BY`,
or filters on a column whose index status you don't know, run `EXPLAIN`
(or `EXPLAIN ANALYZE` if the MCP allows and the cost is bounded)
before running the real query. A seq-scan on a 50M-row table is the
difference between a 200 ms call and a 12-minute call.

Record EXPLAIN-revealed surprises in `## Lessons` — they generalize
across callers.

## LIMIT first when shape is uncertain

If you're not sure how many rows the query will return, the first
version runs with `LIMIT 1000` (or smaller). Once the shape is right,
remove the LIMIT and run for real. Without LIMIT, a query that
mistakenly cartesian-joins two large tables can produce billions of
rows and stall the MCP — and you won't get a useful error, you'll
get a timeout.

## Parameter safety

Never string-concatenate untrusted values into SQL. If the upstream
MCP supports parameterized queries (most do), use the parameter
mechanism. If it doesn't, quote-escape conservatively (`'` → `''` for
SQL strings) and document the choice in your log.

User-supplied identifiers (table/column names from a task body that's
not from a trusted caller) are not parameterizable in most SQL — they
need an allowlist check. The supervisor's callers are mostly trusted
in this system, but **don't get sloppy**: a task that says
`extract from table 'comments; DROP TABLE users; --'` is a malformed
task, not a SQL injection vector. Reject malformed task bodies in your
log with `outcome: failure`.

## COUNT(*) is expensive on large tables

`SELECT COUNT(*) FROM big_table` does a full scan on most engines. If
you only need an approximation:

- PostgreSQL: `SELECT reltuples::bigint AS approx FROM pg_class WHERE relname = 'big_table'`.
- MongoDB: `db.collection.estimatedDocumentCount()` (constant-time vs.
  `countDocuments({})` which is `O(n)`).
- Most engines: `EXPLAIN SELECT * FROM big_table` will print a row
  estimate.

If you need an exact count, do the count — but document the cost in
`## What worked` so the next caller knows it took N seconds.

## Schema introspection cheatsheet

PostgreSQL via SQL:
- List tables: `SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema')`
- Columns of a table: `SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = '<t>'`
- Indexes: `SELECT * FROM pg_indexes WHERE tablename = '<t>'`
- Approx row count: `SELECT reltuples::bigint FROM pg_class WHERE relname = '<t>'`

MongoDB via shell-style commands:
- List collections: `db.runCommand({listCollections: 1})`
- One sample doc: `db.<collection>.findOne()`
- Index info: `db.<collection>.indexInfo()` or `db.<collection>.getIndexes()`
- Approx count: `db.<collection>.estimatedDocumentCount()`

Other engines (StarRocks, ClickHouse, DuckDB, etc.): consult the
upstream MCP's `.tools-inventory.md` description for any
engine-specific notes the operator left, then introspect via
`information_schema` (most engines support it).
