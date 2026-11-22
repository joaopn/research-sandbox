---
title: "Capabilities: MCPs, workers, boxes"
parent: User guide
nav_order: 7
---
# Capabilities: MCPs, workers, boxes

Two host-level registries plus the disposable-box surface. The mental model: **`mcp` = tools, `worker` = pipeline agents, boxes = your interactive containers.**

### MCP servers — `mcp`

If a worker needs more than your local data — search arxiv, query a DB, hit a private tool — register an [MCP server](https://modelcontextprotocol.io/) and grant projects access:

```bash
python research.py mcp add arxiv --kind shared --image ghcr.io/blazickjp/arxiv-mcp-server:latest --port 8000
python research.py mcp enable arxiv
python research.py project mcp allow myproj arxiv
```

`mcp add` writes the registry; `mcp enable` flips the per-MCP flag (and auto-allows it into new projects); `project mcp allow` refuses a disabled MCP; `research start` auto-launches every enabled *shared* MCP. Every MCP is reached through the per-project **mcp-proxy** at a single internal address — workers can't reach an MCP they weren't granted. The server must speak streamable-HTTP and follow a small contract; see [docs/GUIDE.md](mcp-authoring.md).

**The MCP page (Sandboxes → MCP).** A second, newer list holds MCP servers by address (`http://` or `https://`) with optional secret request headers, behind one shared gateway container. The page adds, edits, tests and removes them, and wires them: pick a project, pick its agent or one of its boxes, tick the servers it may use. The agent sees a wired server in its next session, in Claude Code and pi alike; nothing restarts. From the host: `research mcp server list | add | edit | remove | test`, `research mcp wire <project> [--box NAME] --set a,b` and `research mcp wiring`. A research project's own agent is not wired (its workers still use the registry above); its boxes are.

### Worker services — `worker`

Pipeline-side agents the supervisor's analysis workers call through the proxy (`research worker list`):

| Service | Purpose |
|---|---|
| **`websearcher`** | image-baked browser (Playwright + Chromium) for web research. **Default-on** in new projects. |
| **`wrangler`** | data-wrangling helper; inert until you allow DB MCPs for the project. |
| **`echo-mcp`** | a no-op fixture used for substrate testing. |

(Plus the **analysis** worker itself — spawned per question by the supervisor via `rs-worker`, not pre-registered.) A host-level *enabled* flag auto-applies a service to every new project at `create`; only `websearcher` is default-on. `--disable websearcher` overrules per project.

### Boxes (DIND projects)

On any DIND project (`research` / `sandbox-dind`) you can spin **boxes** — isolated, disposable sub-containers — from the webui's ⚙ → Boxes, or the in-box `rs-sandbox` CLI. Each box is a **preset** over two images:

| Preset | What it is |
|---|---|
| **`empty`** | a blank box (base image) |
| **`websearcher`** | the base + a baked browser (Playwright + Chromium) |
| **`data-wrangler`** | the base, oriented for data work |
| **`byo`** | bring your own — clone a repo / run a setup snippet at boot |

pi reads a repo's `.claude/rules/`, `.claude/CLAUDE.md` and nested `CLAUDE.md` files like Claude Code does (an RS-owned `rs-rules` extension rides the pi dist), and it carries subagents (the `pi-subagents` package rides the pi dist too, pinned). It runs online like a stock install: `pi update` updates it in place (like `claude update`; restaging or recreating the project or box returns it to the pinned version), `pi install` / `pi remove` manage its packages, and it refreshes its model catalogs and fetches `rg` / `fd` on first need. Install telemetry stays off. Its settings and packages are yours: they survive a restage and a recreate of a dev / sandbox-dind project, like its login and sessions. The one exception is a one-time addition: settings written before pi-subagents shipped get its entry added once, at the first pi start after the next redeploy (replacing a hand-installed `npm:pi-subagents`); removing it afterwards sticks, until a box or docker project is re-created from the shipped settings. Per-box toggles: `--agent claude|pi` (or none — an agent-bearing box receives every agent the project carries), `--editor`, `--mcps <subset of the project allowlist>`, and `--repo/--ref/--setup` for the `byo` preset. Boxes boot un-authed and reach an LLM / pip / apt through the project's locked egress — the containment is router + no-creds + container isolation.

### Publishing a dev repo's image

RS builds a pushed tag or branch of a mirrored dev repo into a Docker image from the Development page's **Images** tab, and keeps one built image per repo. `rs-publish`, in any fetch-enabled project or box terminal, uploads it to a public registry under each git tag on its commit (or the full commit id when it has none) and moves `latest`. RS itself never holds the registry credential. When the repo has a built image, its card on the Development page's Fetch tab and its row on the Images tab each have a 📋 rs-publish button that copies the command.

1. On GitHub, create a classic token with `write:packages`.
2. In the fetch terminal, write `/workspace/.rs-publish.env` and `chmod 600` it:

   ```
   RS_PUBLISH_REGISTRY=ghcr.io/<owner>
   RS_PUBLISH_USER=<owner>
   RS_PUBLISH_TOKEN=<the token>
   ```

3. Tag and push the collected commit, build it on the Images tab, then:

   ```bash
   rs-publish <repo>           # shows what it would do; uploads nothing
   rs-publish <repo> --apply   # publishes
   ```

`--no-latest` leaves `latest` alone; `--latest <tag> --apply` moves `latest` to a tag already published. A version tag that already holds other content is never moved: the run stops before uploading anything.

Like `rs-sync`'s settings, the file sits in the project's `/workspace`, where an agent working in the same project can read it: keep the fetch terminal in a project without an agent if that matters.

GitHub keeps a newly published package **private**: `rs-publish` says so after the first publish. Make it public once in the package's settings on GitHub (this cannot be undone). If the package page is not linked to the repository, link it there too ("Connect repository").
