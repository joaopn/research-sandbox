---
title: FAQ
parent: User guide
nav_order: 21
---
# FAQ

<details markdown="1">
<summary><strong>Why one supervisor session and not parallel Claude windows?</strong></summary>

Two reasons. (1) Organizational separation: each worker gets only the context relevant to its question. The supervisor holds the bigger picture. Trying to do this in parallel windows means each window grows the same omnibus context. (2) The supervision harness — plan-before-spawn, finalize-before-accept, structural file checks — only enforces against one canonical orchestrator. Multiple parallel windows would race the registry and the canonical plan files.

</details>

<details markdown="1">
<summary><strong>Can workers themselves spawn workers?</strong></summary>

No. Worker containers don't have docker-in-docker; the inner daemon belongs to the supervisor. By design — the planning gate is the supervisor's responsibility, and recursive spawning would defeat the bookkeeping.

</details>

<details markdown="1">
<summary><strong>Can the PI talk directly to a worker?</strong></summary>

Yes, as an escape hatch — `rs-worker attach <name>` drops into byobu inside the worker's container. The default is "ask the supervisor"; the harness assumes the supervisor mediates, so direct intervention may surprise the supervisor's next read of the registry.

</details>

<details markdown="1">
<summary><strong>What happens if I `/clear` mid-session?</strong></summary>

Generally safe — the supervisor reads its own logbook + the registry on the next message, so context rebuilds. But the *current* session's intent (e.g. "we agreed to spawn worker X but haven't yet") is in the conversation, not on disk. Type `/log` first if you've made decisions you don't want to lose.

</details>

<details markdown="1">
<summary><strong>Where does the supervisor's prompt come from?</strong></summary>

`container/supervisor/CLAUDE.md` is baked into the supervisor image and written, under a short notice, into the agent's home folder as `~/.claude/rules/rs-instructions.md` at every start. That copy is replaced at every start; your own instructions for one project go in its `/workspace/.claude/CLAUDE.md`, which RS never writes. Editing the source and rebuilding applies to every research project at its next start.

</details>

<details markdown="1">
<summary><strong>How do I add a new worker type?</strong></summary>

Stage 3+ work. Today, all workers use `rs-analysis-base:latest`. The hooks for new types exist (`rs-worker spawn --image`, `--data-mount`, the `research.worker_type` label) but no canonical alternative images ship yet.

</details>

<details markdown="1">
<summary><strong>Can I use this on macOS / Windows?</strong></summary>

Untested. The host CLI is stdlib Python so it'll run; the question is whether sysbox is available (Linux only) and whether DIND-under-`--privileged` works on your Docker Desktop. Linux is the supported path.

</details>

<details markdown="1">
<summary><strong>How do I share results with collaborators?</strong></summary>

`results/<name>/<NNN>_<slug>/` is plain files in `<PROJECTS_DIR>/<proj>/workspace/results/`. Copy them, sync them, commit them — whatever your group does. Stage 4 will add a per-project Git layer; until then, manual.

</details>

<details markdown="1">
<summary><strong>Can I run this without Sysbox?</strong></summary>

Yes — `--dind privileged` falls back to running the supervisor with `--privileged` and a named volume for the inner Docker daemon. Functional, but the supervisor's container boundary is weaker. Sysbox is strongly preferred where available.

</details>

<details markdown="1">
<summary><strong>How do I migrate existing projects after editing supervisor code?</strong></summary>

Destroy + recreate. The supervisor image is staged at `project create` time; existing projects keep their snapshot. There is no live-migration path by design — the workspace is the durable artifact, the container is disposable.

</details>
