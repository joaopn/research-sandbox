---
title: "Workflows & the store"
parent: User guide
nav_order: 2
---
# Workflows & the store

Creating a project (a card in the New Project window, or `project create`) selects a **workflow**. The workflow decides what the project's container holds; bare `create <name>` defaults to `research`.

A workflow runs in one of two container kinds. An **inner-Docker** project has its own Docker daemon, so it can hold containers of its own (workers, [boxes](capabilities.md#boxes), test containers). A **single** project is one plain container with no daemon inside.

| Workflow | Container | Agent (default) | Egress (default) | What you get |
|---|---|---|---|---|
| **`research`** (default) | inner Docker | Claude only | operator default | The full lab: a supervisor agent, headless analysis workers and role services |
| **`researcher`** | inner Docker | Claude or pi | locked | One research agent that does its own analysis and keeps a memory; the Reader tab is always on, because the memory is read there |
| **`researcher-subagents`** | inner Docker | Claude or pi | locked | A researcher that hands analyses to helpers running in parallel; a finding is re-run and read by an independent referee before you accept it ([how it works](../architecture/Researcher-with-subagents.md)) |
| **`sandbox-dind`** | inner Docker | Claude or pi | locked | An agent with its own Docker and boxes (`rs-sandbox` in its terminal) |
| **`sandbox`** | single | none unless chosen | locked | A bare confined container: ssh, byobu, editor. Add agents with `--agent claude` and/or `--agent pi` |
| **`dev`** | inner Docker | Claude or pi | locked | An agent on one repository's fork in the shared Gitea, delivering through pull requests |
| **store** workflows | either | Claude | locked | Curated public projects, set up at create (table below) |

A `researcher-subagents` project's helpers (its analysts, and its surveyor under Claude) run the model and effort set in its ⚙ Agents box, starting from the defaults under **Sandboxes → Infrastructure → Analyst** and **→ Surveyor**.

The New Project window groups the cards as **Research** (research, researcher, researcher-subagents), **Base** (sandbox-dind, sandbox, dev) and **Store** (the rest).

A store workflow clones a public GitHub repository at a fixed branch into the workspace and runs its setup once, at create; `open-knowledge` clones nothing and installs an app instead. The shipped store workflows (`research workflow list`):

| Card | What it sets up | Container |
|---|---|---|
| `social-data-pipeline` | clones joaopn/social-data-pipeline | inner Docker |
| `academic-research-skills` | clones Imbad0202/academic-research-skills | single |
| `scientific-agent-skills` | clones k-dense-ai/scientific-agent-skills | single |
| `open-knowledge` | installs OpenKnowledge, a markdown knowledge base kept in `notes/` and edited in its own tab; its notes' history can be kept on Gitea | single |

```bash
python research.py project create lab --workflow research --mount ~/data
python research.py project create memo --workflow researcher --agent pi
python research.py project create study --workflow researcher-subagents --agent claude
python research.py project create pipeline --workflow social-data-pipeline
python research.py workflow list                 # the full catalog
```

Every shipped workflow except `research` defaults to **`--egress locked`** (80/443, and DNS to the configured resolvers only, no ICMP) — containment first. `research` takes the operator's configured default, open unless changed (it needs pip/apt). In both modes the router drops private (LAN) addresses; see [Security model](security-model.md) for what closes the paths around it. Override the mode with `--egress`, or in the create form.

## Working with a researcher

In a `researcher` or `researcher-subagents` project you talk with one research agent in its terminal, and read what it has for you in the **Reader** tab. The workspace looks like this:

| Path | What it is |
|---|---|
| `README.md` | The front page, for you: what waits for your decision, what is running, where things stand, what is known |
| `staging/<id>.md` | One page per claim waiting for you, with its statement, scope, hedge, evidence, checks, and the words to accept or strike it |
| `results/index.md` | Everything you accepted, by topic, each with how it stands now (current, superseded, withdrawn, contested) |
| `results/<id>/` | One page per accepted entry, and `evidence/`: a read-only copy of its evidence files, kept when you accepted it |
| `analyses/<name>/` | The files of each analysis |
| `memory/` | The agent's own memory: its journal, its record, its own front page, its skills |

- **What you read.** Start at `README.md`. The pages are written by the agent's memory command from its memory and rewritten at every change, so do not edit them: an edit is lost at the next change. If you keep a file of your own at one of their names (your own `README.md`, say), it is never overwritten; the agent tells you, and the page appears once you move or rename your file.
- **What you decide.** The agent proposes results as claims, each with a short title and an id: findings (`F-3`), facts about the data (`D-1`, such as an inventory line, a gap or a count), open questions and retractions. Each one waits on the front page with the words to decide it. Accept them in the chat ("accept F-3", "accept D-1 D-2"), or reject one and say why ("strike D-1, it double counts"). Only what you accept enters the record. In a `researcher-subagents` project a finding is first re-run and read by an independent referee, and its page shows what both found; facts need neither.
- **The helpers' work** (`researcher-subagents`). Each analyst writes its results and its full write-up, `writeup.md`, into its own folder under `analyses/`, and the agent journals the dead ends and surprises it reports.
- **Long runs.** A run left going for hours is registered by the agent and listed at every session start as running, ended (with its log's last line) or gone.

How the memory works: [Researcher workflow](../architecture/Researcher-workflow.md). The helpers and the checks: [Researcher with subagents](../architecture/Researcher-with-subagents.md).
