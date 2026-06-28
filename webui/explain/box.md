# Boxes

Disposable, isolated sub-containers you run **inside a dind project** (research
or sandbox-dind) — for un-vetted code, a scoped agent task, or a quick browser /
data session. Spin up as many as you like; throw them away when done.

## What it is

A box is a container in the project's inner Docker daemon, on the `rs-inner`
bridge, behind the project's egress filter. It boots **credential-free** — even a
box with an agent has no login until you run `claude` then `/login` inside it.
Boxes are managed from the **+ Add box** button on the project's tab strip (and
the in-supervisor `rs-sandbox` CLI).

## Box types

Each **type** is a preset — the same two box images with different baked
instructions + bundled capability:

- **empty** — a clean box, no instructions, no capability. The blank slate.
- **websearcher** — Playwright + headless Chromium baked in, with web-research
  instructions.
- **data-wrangler** — data-shaping instructions; add a database MCP to give it
  something to wrangle.
- **byo** — clone a ref-pinned repo (with an optional setup command) and work in
  it. Operators can register more types in `box-registry.json`.

## Toggles

Independent of the type:

- **Agent** — deploy `claude` into the box (defaults per type: empty off, the
  others on). Selecting any MCP turns it on.
- **Editor** — bundle the code-server editor (off by default).
- **MCP tools** — wire any of the project's allowed MCP servers into the box's
  agent through the inner proxy.

## Orchestration

RSORCHESTRATIONSVG

## How it works

**+ Add box** creates the box in the project's inner Docker daemon with a pinned
IP on `rs-inner`. The chosen type's instructions land in the box's
`/workspace/CLAUDE.md`; any selected MCPs are wired to `mcp-proxy:8888` so the
box's agent reaches them by name. Egress is the project's router policy (a
sandbox-dind project defaults to locked). Discard a box from the `✕` on its tab —
its container and workspace go with it.
