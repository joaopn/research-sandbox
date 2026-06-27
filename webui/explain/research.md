# Research workflow

The full research lab: a long-lived **supervisor** agent that plans with you,
delegates work to disposable **analysis workers**, consults service **role-MCPs**,
and hosts interactive **PI-role** sessions — all inside one isolated, egress-filtered
project.

## What it is

A `dind-sysbox` project whose top-level container (`rs-supervisor`) runs a Claude
Code session — *the supervisor*. You talk to the supervisor; it does not do the
analysis itself. Instead it writes a plan per question and spawns a worker
container to execute it, reviews the result, and reports back. It is the right
workflow when your work decomposes into parallel, fire-and-forget analyses with
you (the PI) supervising.

## When to use

- You want parallelism across many sub-questions without juggling many chat tabs.
- Outputs are research artifacts (notebooks, data, figures, written analysis),
  reviewed before they reach you.
- A weeks-to-months project that accumulates a logbook and a results inventory.

Reach for **sandbox** instead if you just want one confined shell; **sandbox-dind** if
you want to run isolated boxes yourself without a supervisor agent.

## How it works

You authenticate Claude once inside the supervisor. From then on the supervisor
plans, spawns workers in its own inner Docker daemon, blocks on their completion,
reviews their `outputs/`, and accepts or iterates. Workers are session-scoped and
disposable; their bind-mounted workspace (and the supervisor's logbook + results)
persist across sessions.

## Orchestration

RSORCHESTRATIONSVG

## Components

- **Supervisor** — the Claude session you drive; plans, spawns, reviews, reports.
  Never runs the analysis itself.
- **Analysis workers** — per-question headless containers spawned in the inner
  Docker daemon; produce the deliverables.
- **Role-MCPs** — service agents (websearcher, wrangler, …) workers call through
  the **mcp-proxy**; each spawns its own per-call Claude session.
- **PI-role tabs** — long-lived interactive containers you open directly in the
  webui for hands-on exploration, kept isolated from the worker pipeline.
- **rs-router** — per-project egress filter; open or locked.

## Lifecycle

`create` → authenticate in the supervisor → ask a question → the supervisor plans
and spawns workers → review and accept cycles → `/log` writes the logbook and
parks workers between sessions → `destroy` tears the whole project down.
