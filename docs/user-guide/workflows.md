---
title: "Workflows & the store"
parent: User guide
nav_order: 2
---
# Workflows & the store

`project create` (or clicking a store card) selects a **workflow**. The workflow derives the container substrate and what gets stood up; bare `create <name>` defaults to `research`.

| Workflow | Substrate | What you get |
|---|---|---|
| **`research`** (default) | dind-sysbox | The full lab: a supervisor agent + headless analysis workers + service role-MCPs. |
| **`sandbox-dind`** | dind-sysbox | An agent box **with inner Docker**: spin isolated, disposable **boxes** via the webui or the in-box `rs-sandbox` CLI. |
| **`sandbox`** | docker (runc) | A **single confined container** — ssh + byobu + editor, no inner docker. Add agents with `--agent claude` and/or `--agent pi`. |
| **store** workflows | docker / dind | Curated public GitHub repos, **cloned into a box at create with the editor + claude on** — a ready-to-explore environment. |

The shipped store workflows (`research workflow list`):

| Card | Clones | Box |
|---|---|---|
| `social-data-pipeline` | joaopn/social-data-pipeline | sandbox-dind (inner Docker, locked egress) |
| `academic-research-skills` | Imbad0202/academic-research-skills | sandbox (runc, claude on) |
| `scientific-agent-skills` | k-dense-ai/scientific-agent-skills | sandbox |

```bash
python research.py project create lab --workflow research --mount ~/data
python research.py project create pipeline --workflow social-data-pipeline
python research.py workflow list                 # the full catalog
```

The sandbox flavors default to **`--egress locked`** (80/443 and DNS to the configured resolvers only, no ICMP, RFC1918 blocked) — containment first. `research` defaults to open egress (it needs pip/apt). Override either with `--egress`.
