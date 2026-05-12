<div align="center">

# Research Sandbox

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose_v2-2496ED.svg?logo=docker&logoColor=white)](https://www.docker.com/)
[![Claude Code](https://img.shields.io/badge/Claude_Code-Agent-D97757.svg?logo=anthropic&logoColor=white)](https://github.com/anthropics/claude-code)
[![Sysbox](https://img.shields.io/badge/Sysbox-DIND-1F77B4.svg)](https://github.com/nestybox/sysbox)

An agentic sandbox for research and data analysis and modeled as a research lab: you are a research lead and interact with a per-project supervisor. It spawns sandboxed workers and their tools (MCP), supervises the tasks, keeps logbooks and writes executive summaries for you. Workers are long-lived, but disposable. Built to work with flat-rate agentic AI subscriptions like Claude Code.


</div>

### TL;DR

```bash
# 1. One-time setup
python research.py start

# 2. Create a project pointing at your dataset
python research.py project create myproj --data /path/to/dataset

# 3. Sign in to Claude inside the supervisor (once per project)
python research.py project attach myproj
# in the terminal: `claude` → device-code OAuth → /exit → Ctrl-A D → exit
# OR: VSCode Remote-SSH and click the Claude Code extension

# 4. Talk to Claude. Ask research questions. It plans, runs workers, reports back.

# 5. Read results on the host
ls container_volumes/myproj/workspace/results/
ls container_volumes/myproj/workspace/logbook/pi/

# 6. Tear down when done
python research.py project destroy myproj
```

---

### Table of Contents

[◾ What it does](#-what-it-does)
[◾ Why use this](#-why-use-this)
[◾ Prerequisites](#-prerequisites)
[◾ Quick Start](#-quick-start)
[◾ What you get on disk](#-what-you-get-on-disk)
[◾ MCP servers](#-mcp-servers)
[◾ CLI Reference](#-cli-reference)
[◾ File Structure](#-file-structure)
[◾ Roadmap](#-roadmap)
[◾ Further Reading](#-further-reading)

---

## ◾ What it does

```mermaid
flowchart TD
  PI([You / PI])
  subgraph SC[Supervisor container]
    direction TB
    S[Supervisor<br/><i>plans, reviews, reports</i>]
    subgraph WC[Worker containers — one per question]
      direction TB
      W1[stats]
      W2[figures]
      W3[...]
    end
    S -->|spawn| WC
  end
  D[(Your data<br/><i>read-only</i>)]
  M{{MCP servers<br/><i>arxiv, DBs, tools</i>}}
  R[results/<br/>logbook/]

  PI <-->|chat| S
  WC -.read.-> D
  WC -.call.-> M
  S -->|writes| R
  PI -.read.-> R
```

You talk to **one** Claude Code session — the *supervisor*. The supervisor lives in a per-project container that holds the conversation, the plans, and the project's memory. When a research question arrives, it doesn't try to answer end-to-end itself. Instead it:

1. **Drafts a plan** describing what the worker should do, what inputs it needs, what deliverable it owes back, and how the supervisor will verify the result. You see the plan and approve it before anything spawns.
2. **Spawns a worker** — a fresh container running headless Claude Code with your data mounted read-only. The worker writes a notebook, computes things, produces figures, and reports back.
3. **Reviews the deliverable** against the plan. If it looks right, the supervisor finalizes the cycle and asks you to accept. If something's off, it iterates with the worker (or escalates to you for a decision).
4. **Logs the session** — at the end, it writes one chronological record of what happened and N executive summaries (one per topic) for you to read next time.

Workers are persistent and themed: one worker per coherent question, multiple cycles over the project's life. Between sessions they go cold (container disposed, files preserved); next session picks up from their `summary.md`. The conversation is replaceable — the filesystem is the memory.

## ◾ Why use this

Compared to opening multiple Claude Code windows by hand:

- **No "where were we?" at the start of each session.** The supervisor reads its own logbook; workers read their own summaries. Cold-resume is automatic.
- **No context-window bloat.** Each worker only knows about its own question. The supervisor stays focused on orchestration.
- **Parallel work without coordination overhead.** The supervisor spawns multiple workers for one decomposed question; you don't manage them.
- **A clean record afterwards.** `results/<worker>/<NNN>_<slug>/` contains accepted notebooks + data + a snapshot of the plan that produced them. Nothing rejected sneaks in. Nothing is "almost done".
- **The PI is the editor, not the runner.** The harness keeps the supervisor honest — you approve plans before spawn, you accept deliverables before they're promoted. Skim or drill in as the work warrants.

Compared to a fully autonomous agent: **you're in the loop on every plan and every accept.** Workers can't be spawned without your "go", deliverables can't be marked accepted without your "approve". The harness enforces this; the supervisor's prompt instructs it to wait.

## ◾ Prerequisites

- [Docker](https://www.docker.com/) Engine with Compose v2 (`docker compose`)
- [Python](https://www.python.org/) 3.9+ (host CLI is stdlib-only — conda, pyenv, system Python, anything works)
- An [Anthropic Claude](https://www.anthropic.com/) subscription (Pro/Team or API access — you'll authenticate inside the project, no host-side API keys to set up)

> [!TIP]
> Strongly recommended: [Sysbox](https://github.com/nestybox/sysbox#installation). It's the cleanest way to run a Docker daemon inside a container. Without it the CLI falls back to `--privileged` mode, which works but is less isolated. Linux only.

## ◾ Quick Start

#### 1. Bring up shared infrastructure

```bash
python research.py start
```

Builds the container images on first run; starts a small router container that handles per-project network egress. Idempotent — re-run anytime. Add `--rebuild` after editing image sources.

#### 2. Create a project

```bash
python research.py project create myproj --data /path/to/your/data
```

Creates a per-project workspace at `container_volumes/myproj/workspace/`, brings up the supervisor, and prints an SSH password. Each `--data` path is mounted read-only at `/workspace/shared/data/<basename>/` — workers can read it but never write. `--data` is comma-separated for multiple paths (e.g. `--data /home/me/raw,/srv/parsed` lands as `/workspace/shared/data/raw/` and `.../parsed/`).

Useful flags: `--memory 16g`, `--cpus 4`, `--egress locked` (HTTPS/DNS only), `--inner-firewall` (tighter network isolation between workers and the proxy). See `project create --help`.

#### 3. Sign in to Claude (once per project)

Two ways:

```bash
# (a) Quick: byobu device-code OAuth in the supervisor's terminal
python research.py project attach myproj
# inside byobu, type:
#   claude
# complete the device-code OAuth flow in your browser, then:
#   /exit            (close the claude prompt)
#   Ctrl-A D         (detach from byobu)
#   exit             (close the SSH session)
```

```bash
# (b) Recommended for everyday work: VSCode Remote-SSH
# Connect to research@localhost:<ssh-port> with the password from `project create`,
# open the workspace at /workspace, and click the Claude Code extension to sign in.
```

Credentials are stored inside the supervisor (not on the host) and are copied into each worker at spawn time. `project destroy` deletes them.

#### 4. Run a research thread

In your Claude Code session, just describe what you want in plain English. *"Look at the dataset in `/workspace/shared/data/<your-data-name>/`. Tell me whether the response-time distribution is heavy-tailed and what the typical user looks like."*

What you'll see, paraphrased:

> *"I'm going to spawn a worker `stats` that produces basic distributional statistics, and a worker `userprofile` that characterizes a typical user. Here are the plans — the first writes `outputs/distribution-shape/` with a histogram and skew/kurtosis numbers, the second writes `outputs/user-typology/` with k-means clusters and per-cluster medians. OK to spawn?"*

You say "go". A few minutes later:

> *"Both done. `stats` found the response-time is heavy-tailed (Pareto-like, alpha ~1.7) — see the figure in `staging/stats/`. `userprofile` clustered users into three groups; one is much more active than the others. Take a look at `staging/userprofile/` and let me know if you want me to accept these or iterate."*

You browse the staged outputs in your editor (they're plain files), say "looks good, accept both" — and the deliverables move to `results/`.

#### 5. End the session

Type `/log`. The supervisor writes a chronological session log + executive summaries per topic, and shuts down all workers cleanly. Next time you start a session, those notes are what you (and the supervisor) read first.

#### 6. Tear down

```bash
python research.py project destroy myproj
```

Removes the container, the workspace dir, the network, and the credentials snapshot.

---

## ◾ What you get on disk

Everything lives under `container_volumes/<proj>/workspace/`. The bits you'll actually open:

- **`results/<worker>/<NNN>_<slug>/`** — every accepted cycle. Numbered (001, 002, ...) so you can read in order. Each contains the notebook(s), data files, figures, and a snapshot of the plan that produced it. Nothing rejected sneaks in.
- **`logbook/pi/<date>-<slug>.md`** — executive summaries the supervisor writes for you. One per coherent topic per session. Has `**Source:**` links down to the supervisor's own log if you want to see how the work actually happened.
- **`logbook/supervisor/<date>-<HHMM>.md`** — the supervisor's chronological notes. Drill-down from the PI logs.
- **`workers/<worker>/work/`** — each worker's full sandbox: notebooks (clean and scratch), `research_log.md` (its own narrative), every cycle including rejected attempts. Open in your editor or browse with Jupyter.
- **`plan/<worker>.md`** — the canonical plan currently bound to each worker.
- **`staging/<worker>`** — present only when a cycle is awaiting your accept.

You can edit any of these files. The supervisor does *not* edit `summary.md` or worker outputs by hand — those belong to the worker. Plans go through an approve gate. Logs are append-only.

---

## ◾ MCP servers

If a worker needs to do more than crunch your local data — search arxiv, query a database, hit a private tool — you can register an [MCP server](https://modelcontextprotocol.io/) and grant projects access to it.

```bash
# Register a shared MCP (managed Docker container)
python research.py mcp add arxiv --kind shared \
    --image ghcr.io/blazickjp/arxiv-mcp-server:latest --port 8000

# Or register an external one already running on the host (any port)
python research.py mcp add notes --kind external --host host.docker.internal:9000

# Or one running on a remote machine
python research.py mcp add remote --kind external --host 10.0.5.42:8443

# Enable each MCP you intend to use (gate for `project mcp-allow`)
python research.py mcp enable arxiv
python research.py mcp enable notes

# Launch shared MCP containers (externals have nothing to launch)
python research.py mcp start arxiv

# Allow an enabled MCP for a specific project
python research.py project mcp-allow myproj arxiv

# Now the supervisor can spawn workers that have access to it
# (inside the supervisor):
#   rs-worker spawn libworker --plan ... --mcps arxiv
```

`mcp add` only writes to the registry. `mcp enable` flips a per-MCP flag;
`project mcp-allow` refuses to grant a disabled MCP, and `research start`
auto-launches every enabled *shared* MCP after the router comes up.
`mcp start [<name>]` and `mcp stop [<name>]` manage shared-container
lifecycle on demand — bare invocation operates on all enabled / all running.
External MCPs have no container lifecycle, so `mcp start`/`stop` skip them.

The MCP must speak streamable-HTTP and follow a small contract; see [docs/GUIDE.md#authoring-an-mcp-server](docs/GUIDE.md#-authoring-an-mcp-server) for a 10-line example. Workers can't reach MCPs they weren't granted — see [docs/SECURITY.md](docs/SECURITY.md) for the gating layers.

---

<details>
<summary><h2>◾ CLI Reference</h2></summary>

### Host CLI: `research.py`

```
python research.py <command> [options]

Infrastructure:
  start [--rebuild]                  Build images + start router
  stop                               Stop router (projects untouched)

Project lifecycle:
  project create <name> [opts]       Create a project supervisor
  project attach <name>              docker exec + byobu attach
  project list                       Show all projects
  project status <name>              Detailed state + worker registry summary
  project stop|start <name|--all>    Stop/start the supervisor without destroying
  project destroy <name>             Remove container + workspace + network + creds
  project ssh <name>                 Print SSH connection string

  project mcp-allow <proj> <mcp>     Grant a project access to a registered MCP
  project mcp-deny  <proj> <mcp>     Revoke access

MCP registry:
  mcp add <name> --kind {external,shared} [opts]   Register an MCP
  mcp list [--json]                  Show all registered MCPs
  mcp remove <name> [--force]        Unregister (refuses if any project allows it)
  mcp enable|disable <name>          Toggle the per-MCP enabled flag (required for mcp-allow)
  mcp start [<name>]                 Start a shared MCP container (or all enabled shared)
  mcp stop  [<name>]                 Stop a shared MCP container (or all running)
  mcp test  [<name>]                 Probe reachability (or all)

project create options:
  --data <paths>                     Comma-separated host paths, each mounted RO at /workspace/shared/data/<basename>
  --memory <limit>                   Docker memory limit (e.g. 8g)
  --cpus <limit>                     Docker CPU limit
  --egress {open,locked}             Network egress policy (default open)
  --inner-firewall                   Tighter inter-bridge network isolation
  --dind {auto,sysbox,privileged}    Container runtime (default: auto)
  --ssh-port <port>                  Explicit SSH host port

mcp add options:
  --transport {http,sse}             Default http
  --host HOST:PORT                   (external) where the supervisor reaches the MCP
  --header K=V (repeatable)          (external) HTTP header to inject
  --image <image>                    (shared) Docker image
  --port <port>                      (shared) port the MCP listens on inside
  --env K=V (repeatable)             (shared) env var passed to container
  --path <path>                      Upstream URL path (default /mcp)
```

### In-supervisor CLI: `rs-worker`

The supervisor's Claude Code uses this; you rarely call it directly. Useful for debugging.

```
rs-worker <command> [options]

Worker lifecycle:
  spawn <name> --plan <path> [--mcps a,b] [--data-mount /path] [--image IMG]
  list [--all]                List live (or all) workers
  history                     Dump every registry entry by created_at
  status <name>               Container + registry + log tail (JSON)
  wait <name>... [--all] [--timeout S]
                              Block until terminal (default 540s)
  message <name> "<text>"     Queue a follow-up task in the worker's inbox

Cycle gating:
  finalize <name> --slug <slug>     Stage outputs/<slug>/ for PI review
  unstage <name>                     Remove staging symlink (PI rejected)
  accept <name> --slug <slug>        Promote to results/<name>/<NNN>_<slug>/
                  [--waived REASON]  Waive shape gate with a reason

Persistence:
  shutdown <name>             Graceful stop+rm; registry → down
  destroy <name> --yes        Tombstone the name; archive plan; delete workdir

Inspection:
  attach <name>               Drop into byobu inside the worker
  tail <name> [-f]            Stream the worker's log
```

</details>

<details>
<summary><h2>◾ File Structure</h2></summary>

Repository:

```
research-sandbox/
├── research.py                   Host CLI (Python stdlib only)
├── docker-compose.yml            Router service
├── .env / .env.example           Host config (PROJECTS_DIR, defaults)
├── cli/                          In-supervisor CLIs (worker, registry, audit hook)
├── agent/                        Container Dockerfiles + entrypoints
├── container/                    Templates baked into the supervisor image
│   ├── supervisor/               Supervisor agent's role doc + scripts
│   └── analysis/                 Worker agent's role doc template
├── router/                       Egress-filter router (Alpine + iptables)
├── docs/
│   ├── GUIDE.md                  Workflow, MCP authoring, debugging, FAQ
│   └── SECURITY.md               Threat model + isolation layers
└── external/                     Vendored reference code (not a dependency)
```

Per-project workspace at `container_volumes/<proj>/workspace/` — the supervisor sees this as `/workspace`:

```
.claude/                         Supervisor's Claude config + creds
.workers/<name>.json             Worker registry (one file per name, ever)
plan/
├── draft/<name>.md              Supervisor's proposal — awaiting your approval
├── <name>.md                    Canonical plan (harness-owned)
└── archive/<name>.md            Snapshot at destroy
workers/<name>/work/             Worker's own /workspace inside its container
├── outputs/<slug>/              Per-cycle deliverables
├── research_log.md              Worker's narrative
├── summary.md                   Cross-session memory
└── log.jsonl                    Stream-json log
shared/data/<basename>/          One RO subdir per --data path (basename → subdir)
staging/<name>                   Symlink during your review
results/<name>/<NNN>_<slug>/     PI-visible accepted deliverables
logbook/
├── supervisor/<date>-<HHMM>.md  Per-session chronological log
└── pi/<date>-<slug>.md          Per-topic executive summary
```

</details>

---

## ◾ Roadmap

- **Stage 3** — additional worker types: `librarian` (web/arxiv/search MCPs preloaded), `data-wrangler` (DB MCPs), `paper-writer` (read-only except `paper/`). Stdio MCPs.
- **Stage 4** — Git history per worker via per-project Gitea, so you can diff cycles, browse via web UI, and cherry-pick changes.
- **Long-running DB queries** — host-side approval GUI; worker writes intent, you approve, results come back.

Out of scope: Telegram/Discord/phone integration, fine-grained checkpointing, web dashboard for worker status (VS Code + notebooks is the review surface).

## ◾ Further Reading

- **[docs/GUIDE.md](docs/GUIDE.md)** — How a research thread actually plays out, the supervisor↔worker protocol, slugs and finalize/accept, authoring an MCP server, debugging recipes, FAQ.
- **[docs/SECURITY.md](docs/SECURITY.md)** — Threat model, isolation layers, what's prevented and what isn't. Read this if you're running on shared infrastructure or with sensitive data.

<div align="center">
<sub>Licensed under the file <a href="LICENSE">LICENSE</a>.</sub>
</div>
