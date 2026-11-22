---
title: Supervisor and workers
parent: User guide
nav_order: 10
---
# Supervisor and workers

The split is deliberate:

| | Supervisor | Worker |
|---|---|---|
| Lifetime | Per-project, long-lived (weeks/months) | Per-question; persistent across cycles, gone between sessions |
| Image | `rs-supervisor:latest` (miniconda + docker-ce + byobu + sshd) | `rs-analysis-base:latest` (miniconda + DS stack, no git, no sshd) |
| Auth | Per-project OAuth, once | Inherits the supervisor's creds at spawn time |
| Talks to | PI; rs-worker CLI; mcp-proxy (via spawned workers) | Its own task.md; MCPs via mcp-proxy (if granted) |
| Touches data | Reviews artifacts, never computes | Runs notebooks, queries, ML, etc. |
| Logbook | Writes `logbook/supervisor/*.md`, `logbook/pi/*.md` | Writes `summary.md`, `research_log.md` |

The supervisor *does not* run pandas. Writing briefs, reading deliverables, judging — yes; computing statistics — no. This keeps the supervisor's context window for orchestration, not for raw data.

Workers are *thematic*: one worker per coherent question, multiple cycles per worker. A statistics worker accepts cycles `001_basic-stats`, `002_per-language`, `003_outliers` over the project's life. Cycle ordinals (3-digit, accept-only) are stored in the registry; the rejected attempts stay on disk under `outputs/<slug>/` as provenance but don't get an ordinal.

Worker names are **project-permanent once used**. Destroying a worker tombstones the name. Pick `stats_v2` if you need to start over.
