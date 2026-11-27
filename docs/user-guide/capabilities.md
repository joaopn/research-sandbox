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

`mcp add` writes the registry; `mcp enable` flips the per-MCP flag (and auto-allows it into new projects); `project mcp allow` refuses a disabled MCP; `research start` auto-launches every enabled *shared* MCP. Every MCP is reached through the per-project **mcp-proxy** at a single internal address — workers can't reach an MCP they weren't granted. The server must speak streamable-HTTP and follow a small contract; see [Authoring an MCP server](mcp-authoring.md).

**The MCP tab (Services → MCP).** A second, newer list holds MCP servers by address (`http://` or `https://`) with optional secret request headers, behind one shared gateway container. The tab adds, edits, tests and removes them, and wires them: pick a project, pick its agent, one of its boxes or, in a research project, its analysis workers, then tick the servers it may use. The agent sees a wired server in its next session, in Claude Code and pi alike; nothing restarts. From the host: `research mcp server list | add | edit | remove | test`, `research mcp wire <project> [--box NAME | --workers] --set a,b` and `research mcp wiring`. A research project's own agent is not wired; its analysis workers and its boxes are. Each worker gets only the servers the supervisor names when it spawns it, from this tab or the registry above.

**Aldine, the shared LaTeX editor.** Turned on under Services → Aldine (or `research aldine on`), it appears on the MCP tab as the server `aldine`; tick it for an agent, a box or a research project's workers like any server. A project with something wired to it also gets an **Aldine** tab. There is one Aldine and one set of papers for the whole installation. See [Aldine](../architecture/Aldine.md).

**The search service.** Turned on under Services → Search (or `research search on`), it appears on the MCP tab as the server `search`. On the Search tab's **Settings**, sign it in once: run `claude setup-token` in any project's terminal tab, signed in to the Claude account the searches should use, and paste what it prints; every search uses that one sign-in. The same sub-tab sets the searcher's model and effort, how many searches run at once, an optional contact address for the scholarly databases, how many ended searches Activity lists, and whether the sidebar shows Search. Its two switches **Browser** and **Stealth browser** (both on by default) let a search go on, when the plain tier (scholarly APIs, a page fetch, a web search) is not enough, to a headless Chromium and then to camoufox, a browser that looks like a person's; switched off, a tier is never used. Each search gets its own browsers on its own network, removed with it, and the browser inside starts only at the first page; a file a browser downloads arrives as a file of the search. The cost: about 4.8 GB more images at the first Turn on, and the memory of each running search's browsers. Then tick `search` for an agent, a box or a research project's workers on the MCP tab. The agent can submit a question and go on working, check how the search goes, download the files it found (PDFs, reports) and collect a written summary with every source and what was tried. Each search runs in containers made for it alone, removed once it is collected. The Search tab's **Activity** lists the searches that are waiting or running, shows each one's work so far, and can stop one; its **Quick Search** lets you ask the searcher yourself, one search per message. See [Search service](../architecture/Search.md).

**Checking a paper's citations.** The agent of a `researcher`, `researcher-subagents` or `companion` project, or of a zotero box, can check a paper's citations once it is ticked for Aldine. Ask it with `/paper-check <paper>` in Claude Code, `/skill:paper-check <paper>` in pi, or in plain words. It leaves one review comment per problem on the paper, read in the Aldine tab, and lists every citation with its verdict in `/workspace/paper-check/<paper>/checks.md`. See [Checking a paper's citations](../architecture/Aldine.md#checking-a-papers-citations).

### Worker services — `worker`

Pipeline-side agents the supervisor's analysis workers call through the proxy (`research worker list`):

| Service | Purpose |
|---|---|
| **`websearcher`** | image-baked browser (Playwright + Chromium) for web research. **Default-on** in new projects. |
| **`wrangler`** | data-wrangling helper; inert until you allow DB MCPs for the project. |
| **`echo-mcp`** | a no-op fixture used for substrate testing. |

(Plus the **analysis** worker itself — spawned per question by the supervisor via `rs-worker`, not pre-registered.) A host-level *enabled* flag auto-applies a service to every new project at `create`; only `websearcher` is default-on. `--disable websearcher` overrules per project.

### Boxes

A project with its own inner Docker (`research`, `researcher`, `researcher-subagents`, `companion`, `sandbox-dind`, `dev`, and the `social-data-pipeline` store workflow) can hold **boxes**: isolated sub-containers, each with its own workspace and no credentials. Add one from the **+** button on the project's tab strip, or with the `rs-sandbox` CLI in the project's terminal; the ✕ on the box's terminal tab removes it. A single-container project (`sandbox` and the other store workflows) cannot hold boxes.

Each box comes from a **box type** over two images (base, or browser with Playwright + Chromium):

| Type | Image | Agent default | What it is |
|---|---|---|---|
| **`empty`** | base | off | a blank box, no instructions |
| **`byo`** | base | on | bring your own: clones a repo at a ref and runs a setup command at boot |
| **`dev`** | base | on, editor on | works a GitHub repo through its own fork in the shared Gitea, delivering through pull requests |
| **`zotero`** | base | on | a Zotero library through the zotero MCP server; asks for a Zotero API key and library ID; its agent can check a paper's citations in Aldine |
| **`open-knowledge`** | base | on | an OpenKnowledge knowledge base in its own tab; Share files on by default, notes' history optionally on Gitea |
| **`paper-orchestra`** | base | on, editor on | the PaperOrchestra repo, cloned at boot |
| **`websearcher`** | browser | on | web research with Playwright and headless Chromium |
| **`data-wrangler`** | base | on | schemas, queries and extracts through the project's database MCPs |

The box window (or `rs-sandbox create`) sets per-box options over the type's defaults: the agent (`--agent claude|pi|none`; an agent-bearing box receives every agent the project carries), `--editor`, `--mcps <subset of the project allowlist>`, and `--repo/--ref/--setup` for the `byo` type. A box boots signed out and reaches an LLM, pip and apt through the project's egress — the containment is router + no credentials + container isolation.

**Agents in a box.** Sign in inside the box: `claude` then `/login`, or `pi` then `/login` with an API key (OpenRouter or another provider). pi reads a repo's `.claude/rules/`, `.claude/CLAUDE.md` and nested `CLAUDE.md` files as Claude Code does, and carries subagents. It runs like a stock install: `pi update` updates it in place (restaging or re-creating the project or box returns it to the pinned version) and `pi install` / `pi remove` manage its packages. Details are in the [CLI reference](cli-reference.md).

### Publishing a dev repo's image

RS builds a pushed tag or branch of a mirrored dev repo into a Docker image from the Services page's **Repos** tab (each repo's Image column), and keeps one built image per repo. `rs-publish`, in any fetch-enabled project or box terminal, uploads it to a public registry under each git tag on its commit (or the full commit id when it has none) and moves `latest`. RS itself never holds the registry credential. When the repo has a built image, its card on the Services page's Fetch tab and its row on the Repos tab each have a 📋 rs-publish button that copies the command.

1. On GitHub, create a classic token with `write:packages`.
2. In the fetch terminal, write `/workspace/.rs-publish.env` and `chmod 600` it:

   ```
   RS_PUBLISH_REGISTRY=ghcr.io/<owner>
   RS_PUBLISH_USER=<owner>
   RS_PUBLISH_TOKEN=<the token>
   ```

3. Tag and push the collected commit, build it on the Repos tab, then:

   ```bash
   rs-publish <repo>           # shows what it would do; uploads nothing
   rs-publish <repo> --apply   # publishes
   ```

`--no-latest` leaves `latest` alone; `--latest <tag> --apply` moves `latest` to a tag already published. A version tag that already holds other content is never moved: the run stops before uploading anything.

Like `rs-sync`'s settings, the file sits in the project's `/workspace`, where an agent working in the same project can read it: keep the fetch terminal in a project without an agent if that matters.

GitHub keeps a newly published package **private**: `rs-publish` says so after the first publish. Make it public once in the package's settings on GitHub (this cannot be undone). If the package page is not linked to the repository, link it there too ("Connect repository").
