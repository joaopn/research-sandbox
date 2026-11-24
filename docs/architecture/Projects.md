---
title: Projects
parent: Architecture
nav_order: 1
---
# Projects

A **project** is one container on the RS host, with its own agent, workspace folder and network. The user creates projects from the browser and picks a **workflow**, which decides what the container holds. The research workflows, two built and two planned, are described in [Research](Research.md).

## What a project is

A project's files live on the host under the projects folder (`PROJECTS_DIR`, by default `container_volumes/` in the RS folder), at `<project>/workspace/`. That folder is the container's `/workspace`, so its contents survive stops, starts and updates. Host folders chosen at create ("Add folders", or later under ⚙ Project folders) appear read-only at `/workspace/mnt/<name>/`.

A project starts without credentials. The user signs its agent in once, inside the project, and the login is kept with the project. All of a project's outbound traffic passes through the router (see [Networking and egress](Networking-and-egress.md)); its terminal and editor are described in [Browser and broker](Browser-and-broker.md).

## Kinds of project

A workflow uses one of two container kinds. An **inner-Docker** project runs its own Docker daemon, so it can hold containers of its own: workers, role services, boxes, test containers. A **single-container** project is one plain container with no daemon inside.

| Workflow | Container | Agent (default) | Egress (default) | Gives the user |
|---|---|---|---|---|
| research | inner Docker | Claude only | operator default | A supervisor with analysis workers and role services; see [Research workflow](Research-workflow.md) |
| researcher | inner Docker | Claude or pi | locked | One research agent with a memory, read in the always-on Reader tab; see [Researcher workflow](Researcher-workflow.md) |
| sandbox-dind | inner Docker | Claude or pi | locked | An agent with its own Docker and boxes |
| sandbox | single | none unless chosen | locked | A bare confined container |
| dev | inner Docker | Claude or pi | locked | An agent on one repository's fork; see [Dev lane](Dev-lane.md) |
| open-knowledge | single | Claude | locked | An OpenKnowledge notes app in its own tab, optional history on Gitea |
| academic-research-skills | single | Claude | locked | A collection of research-writing skills |
| scientific-agent-skills | single | Claude | locked | A collection of scientific skills |
| social-data-pipeline | inner Docker | Claude | locked | A social-data processing pipeline |

The New Project window groups them as Research (research, researcher), Base (sandbox-dind, sandbox, dev) and Store (the rest). A store workflow clones a public repository at a fixed branch into the workspace and runs its setup once, at create; open-knowledge clones nothing and installs OpenKnowledge instead. Long-lived extra containers inside a project are [Boxes](Boxes.md).

## Agents

RS ships two agents, **Claude Code** and **pi**. Claude signs in with a Claude account; pi uses an API key from OpenRouter or another provider. The agent software is downloaded once on the host (Sandboxes → Software) and copied into each project at boot, never baked into an image. Editor extensions shipped with an agent or with the editor are installed at each start of a project or box when the shipped version is newer than the installed one; an equal or newer installed version is kept, so a start never downgrades.

A research project always runs Claude, because its supervision check is a Claude Code hook; the researcher workflow and the planned research workflows run either agent (see [Research](Research.md)). A dev or researcher project needs at least one agent; a bare sandbox has none unless one is chosen.

Each interactive agent shows RS's **status bar** under its prompt. Claude Code's bar shows the session's name, the model, how long the prompt cache stays warm, the session's cost and how full the context is; pi's bar also splits the cost into cache reads, writes and output, and shows a session's name on its folder line. It is on by default and switched per container in the Extensions boxes (New Project, Add box, the project's ⚙ window). Workers and role services never show it.

Each container's command search path holds only the RS commands a person types there: `rs-worker` and `rs-role-mcp` in a research project, `rs-sandbox` where there is inner Docker, `rs-wt`, `rs-land` and `rs-board` in a dev project or dev box, `rs-web-restart` where a web app runs, `rs-fetch` and `rs-sync` where fetch was chosen. A plain sandbox has none. RS's own helpers (the network guard, the web-app loop, the port forwarder, the stop hook) live in `/usr/local/lib/rs`.

RS gives each agent its instructions in one read-only file in the agent's home, `~/.claude/rules/rs-instructions.md`, which both agents load. RS rewrites it at every start, so edits to it are lost. The workspace's `.claude/` and `.pi/` folders are the user's; RS writes nothing there. Its skills (`/ok`, `/ok-ingest`), the research `/log` command and pi's MCP servers live in the agents' home folders.

A project or box that still holds a file an older RS wrote there (`/workspace/CLAUDE.md`, a `.claude/settings.json`, a skill, `/log`, pi's `.pi/mcp.json` link) finishes its operation and then fails with an error listing the files. The user removes them from the project's terminal or editor and starts again; RS never moves them. Once a project or box is clean, files the user adds later are theirs.

## Lifecycle

Stop, Start, Update and Destroy are under Sandboxes → Management. Stop and Start are also on each project's row in the sidebar (see [Browser and broker](Browser-and-broker.md)). An inner-Docker project cannot be restarted in place, so every start re-creates its container from the current RS image.

| Action | Inner-Docker project | Single-container project |
|---|---|---|
| Stop | Stopped; everything kept | Stopped; everything kept |
| Start | Re-created; workspace, logins, folders and limits kept; installs outside `/workspace` lost | Started again as it was; re-created, with logins and settings kept, when folders were changed in ⚙, its app's version changed, or it predates RS's current command layout |
| Update | Re-created with the latest instruction templates; running work interrupted | Editor on/off only; re-creates the container, keeping logins and settings |
| Destroy | Container, workspace, inner Docker data, network and router rules deleted | The same |

Destroy asks for the project name and the master password, and cannot be undone. Projects created by an earlier RS version are kept across upgrades: a change to a project's shape ships with a repair at its next start or update, or a documented one-time procedure.

## Design decisions

**One container per project.** Everything a project runs (workers, boxes, test containers) lives inside it, in its own Docker daemon where it has one, so a project is isolated as a whole and destroying it removes everything it ran. The cost is an inner-Docker runtime on the host and a re-create at every start, which drops installs outside `/workspace`.

**No host credentials enter a project.** A project starts empty and its agent is signed in once inside it; a research project's workers get a copy of that login at spawn. The cost is one sign-in per project.

**RS's files live outside the workspace, and an old file is an error.** The agent's home holds RS's instructions, skills, `/log` and pi's MCP servers; the workspace's `.claude/` and `.pi/` are the user's alone. A file left by an older RS is never moved or ignored: the operation fails and lists it. The cost is one manual removal per old project or box.

**Only typed commands are on the search path, and an old container is refused.** RS's helpers sit in a private folder, so tab completion shows only what a person uses in that container. A container or image from before this layout carries no layout label; an action that would call a helper there stops before changing anything, with "stop the project and start it again" or "Rebuild all". A start always finishes and reports the problem at the end. The cost is one stop and start per old project, and a one-time re-create of an old single-container project, which drops installs outside `/workspace`.

**RS writes the status bar once and owns it only by name.** RS sets an agent's status line when the project is created, when the switch changes, or at the first start of an older project; after that the agent's own settings are the state. It adds or removes only its own line, never one the user set. The cost is that a status line removed inside the agent stays removed until the switch is used again.

**An app RS installs has one version for the installation.** OpenKnowledge's version is a setting on the Software page (Apps), moved forward to npm's latest and never down. RS fills it into the project and box setups when it installs them; a project records the version it installed and is rebuilt once at its next start when the setting changed. The cost is a one-minute rebuild per OpenKnowledge project after each change, which drops installs outside `/workspace`.
