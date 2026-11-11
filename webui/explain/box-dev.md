# Dev box

A software-dev box — **made from a GitHub URL and a branch**. The repo is
mirrored and forked on the shared Gitea, your fork is cloned at boot, and the
agent delivers its work as pull requests. The agent and the editor are on by
default.

## What it is

A container working exactly one repo: its fork on the shared Gitea, cloned into
the box when it boots. The agent branches off the branch you name, keeps in
step with it, and opens pull requests against it, which you review on
Gitea. Adding the box mirrors the repo, forks it and wires the box to
the fork in one step.

Needs this host's Gitea: enable it under Sandboxes → Infrastructure first, or
adding the box stops with that message.

## Good for

- Software work on an existing GitHub repo, kept apart from the project's own
  workspace.
- Reviewing an agent's changes as pull requests before you take them.

## The dev fields (below Settings)

- **GitHub repo (https)** — the repo's URL, `https://github.com/owner/repo`.
- **Branch** — the branch the agent works from: it syncs this branch, branches
  off it and targets it with pull requests. It must already exist on the repo.
- **GitHub PAT (optional)** — for private repos only (a read-only fine-grained
  token is best). Gitea keeps it for the repo's mirror; RS stores none.

## Settings on a dev box

- **Agent** — the project's first agent by default. With an agent picked
  (outside a research-workflow project), a **Working mode** group sets how the
  agent works.
- **Editor** — on by default (code-server).
- **Share files** — available, as on any box.
- **MCP tools**, **Browser**, **rs-fetch** and **Read-only folders** are not
  offered: a dev box works only its fork.

Boots credential-free: sign the agent in inside (`claude` then `/login`, or
`pi` then `/login`). Egress follows the project's router policy.
