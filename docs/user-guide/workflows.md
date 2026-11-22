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
| **`sandbox-dind`** | inner Docker | Claude or pi | locked | An agent with its own Docker and boxes (`rs-sandbox` in its terminal) |
| **`sandbox`** | single | none unless chosen | locked | A bare confined container: ssh, byobu, editor. Add agents with `--agent claude` and/or `--agent pi` |
| **`dev`** | inner Docker | Claude or pi | locked | An agent on one repository's fork in the shared Gitea, delivering through pull requests |
| **store** workflows | either | Claude | locked | Curated public projects, set up at create (table below) |

The New Project window groups the cards as **Research** (research, researcher), **Base** (sandbox-dind, sandbox, dev) and **Store** (the rest).

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
python research.py project create pipeline --workflow social-data-pipeline
python research.py workflow list                 # the full catalog
```

Every shipped workflow except `research` defaults to **`--egress locked`** (80/443, and DNS to the configured resolvers only, no ICMP) — containment first. `research` takes the operator's configured default, open unless changed (it needs pip/apt). In both modes the router drops private (LAN) addresses; see [Security model](security-model.md) for what closes the paths around it. Override the mode with `--egress`, or in the create form.
