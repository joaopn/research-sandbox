# Data-wrangler box

A data-wrangling box — **extraction-and-shaping instructions baked in**. The
agent is on by default; add the project's database/query MCP servers to give it
something to wrangle.

## What it is

A disposable container for exploring, querying, and shaping data. The baked
instructions tell the agent to introspect schemas, draft and refine queries,
sample data, and produce clean extracts. It does **not** know your data sources
ahead of time — it reads `/workspace/.tools-inventory.md` (populated from the
MCPs you wire in) to discover them.

## Good for

- Schema introspection, exploratory `SELECT … LIMIT`, `EXPLAIN`-driven tuning.
- Producing reproducible extracts (the query, its cost, and a sample alongside).
- Any "shape this dataset" task against a project database.

## Notes

- **Add a database/query MCP** when you create the box (the MCP toggle) — without
  one the box has nothing to wrangle.
- Extracts are saved under `/workspace/` (e.g. `extracts/<topic>/<slug>.parquet`
  with its `.sql` + `metadata.json`).
- Boots credential-free: run `claude` then `/login` inside. `pip install` and
  network access work, subject to the project's egress policy.
