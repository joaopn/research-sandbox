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
- **Browser** — run the box on the Playwright + headless Chromium image so its
  agent can browse. The box type only sets the default (websearcher pre-checks
  it; its instructions assume the browser tools, so un-checking it there leaves
  them pointing at tools the box doesn't have). Heavier image — leave it off
  unless the box needs the web. Not available on dev boxes.
- **rs-fetch** — wire the box to pull dev-lane agent commits from the shared
  Gitea into a git clone here (read-only; off by default; not on dev boxes).
- **MCP tools** — wire any of the project's allowed MCP servers into the box's
  agent through the inner proxy.
- **Read-only folders** — give the box any of the project's top-level folders.
  A ticked folder appears in the box under `/mnt/<folder>` and cannot be changed
  from the box — by its agent, by `sudo`, by anything. Nothing is ticked by
  default. Not on dev boxes.

## Read-only folders

The box window lists every top-level folder of the project (plain names first,
hidden ones after; links and plain files are not offered, and a folder whose
name a mount cannot carry is shown greyed). Change a box's folders later in the
project's ⚙ panel under **Box folders**: saving restarts the box fresh, so its
agent logs in again and its setup runs again; the box's own files are untouched.

Everything in a ticked folder is readable inside the box, **credentials
included**: `workers/` holds a copy of the project's Claude login in each
worker's folder, `pi-isolated/` holds every box's own files (other boxes'
notes, clones and configuration), `.orchestrator/` holds the project's wiring, `.creds-stash/`
holds the login while the project restarts, and a sandbox-dind project's setup
clone keeps the GitHub token it was cloned with in its git settings.

A ticked folder that is gone when the box starts (deleted, renamed, replaced by
a link or a file) is skipped: the box starts without it and says so in the
progress list or the ⚙ panel, and mounts it again at its next restart (a
project restart, a port change, a folders change) once it is back. Links
between folders: a relative link between two folders the box was given works
inside the box; an absolute link, or one into a folder it was not given, shows
as broken.

## Orchestration

RSORCHESTRATIONSVG

## How it works

**+ Add box** creates the box in the project's inner Docker daemon with a pinned
IP on `rs-inner`. The chosen type's instructions land in the box's
`/workspace/CLAUDE.md`; any selected MCPs are wired to `mcp-proxy:8888` so the
box's agent reaches them by name. Egress is the project's router policy (a
sandbox-dind project defaults to locked). Discard a box from the `✕` on its tab —
its container and workspace go with it.
