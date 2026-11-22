---
title: Anatomy of a research thread
parent: User guide
nav_order: 9
---
# Anatomy of a research thread

A typical thread looks like this:

1. **PI session start.** PI attaches to the supervisor (VSCode Remote-SSH or `project attach`). Claude Code starts. The supervisor reads its most-recent `logbook/supervisor/*.md` to reload context, lists `.workers/*.json` to see what exists.
2. **Question.** PI poses a research question in plain English.
3. **Reuse-or-fresh decision.** The supervisor inspects existing `down` workers' `summary.md` top line (their thematic identity). Same theme → respawn; new theme → new worker name.
4. **Plan.** Supervisor drafts `plan/draft/<name>.md` with `## Question / ## Inputs / ## Deliverables / ## Verification`. PI reviews; explicit "go" required.
5. **Spawn.** Supervisor runs `rs-worker spawn <name> --plan plan/draft/<name>.md [--mcps a,b]`. The harness validates the four sections, promotes the draft to canonical (`plan/<name>.md`), copies it as the worker's `task.md`, deletes the draft, starts a headless worker container.
6. **Block.** Supervisor calls `rs-worker wait <name>` and does *not* return to the PI until the worker terminates.
7. **Review.** Worker is in state `waiting` (idle) or `done` (clean exit). Supervisor reads `research_log.md`, opens `outputs/<slug>/`, compares against the plan's `## Verification`. Decides:
   - **Meets brief** → `rs-worker finalize <name> --slug <slug>`. Shows `staging/<name>/` to PI. On PI approval: `rs-worker accept`. The cycle is now in `results/<name>/<NNN>_<slug>/`.
   - **Minor gap** → `rs-worker message <name> "<correction>"` (queues follow-up in inbox; same container).
   - **Wrong shape** → fail loudly; iterate via message with a new slug.
8. **Loop until satisfied** for the current PI question, or escalate back if blocked on a decision only the PI can make.
9. **Session end.** PI types `/log`. Supervisor sends summarize-and-shutdown messages to every live worker, writes one `logbook/supervisor/<date>-<HHMM>.md` (chronological) and N `logbook/pi/<date>-<slug>.md` (executive, one per coherent topic). Then `rs-worker shutdown <name>` for each worker → registry state goes to `down`, container is gone, bind-mount preserved.

Next session, the PI reads the relevant `logbook/pi/*.md` to remember where things stood. The supervisor reads `logbook/supervisor/*.md` to remember its own decisions. Workers respawn from their `summary.md`.
