---
title: Quick Start
parent: User guide
nav_order: 5
---
# Quick Start

The research flow, end to end. The browser does all of it without a shell; the CLI path follows.

**In the browser** (after [Install](install.md), with the broker and webui up):

1. **Create.** New Project → the `research` card (in the Research group) → optionally **Add folders** → **Create**, and watch the progress checklist.
2. **Sign in.** Open the project's terminal tab (**Supervisor (CLI)**) and run `claude`, then `/login`. (A research project runs Claude; most other workflows can carry pi, where you run `pi`, then `/login` with an API key from OpenRouter or another provider.)

**On the CLI:**

```bash
python research.py start                                   # 1. shared infra
python research.py project create myproj --mount /path/to/data  # 2. create
python research.py project attach myproj                    # 3. sign in to Claude
#   inside byobu:  claude  → /login → /exit → Ctrl-A D → exit
#   (or VSCode Remote-SSH to research@localhost:<ssh-port> and use the Claude Code extension)
```

Each `--mount` folder is mounted **read-only** at `/workspace/mnt/<basename>/` (comma-separated, or repeat the flag). From the browser, the create form's **Add folders** takes paths relative to RS's `data/` folder instead: put datasets there (or mount another disk into it on the host). A project's folders can be changed later in the **Sharing** section of its ⚙ window; the change applies the next time the project starts. Credentials live **inside** the supervisor (never on the host), are copied into each worker at spawn, and are deleted on `destroy`.

**Run a thread.** Describe what you want in plain English — *"Is the response-time distribution in `/workspace/mnt/<name>/` heavy-tailed, and what does a typical user look like?"* The supervisor proposes worker plans, you say "go", it spawns workers, reviews their staged outputs, and asks you to accept.

**End the session.** Type `/log`. The supervisor writes a session log + per-topic executive summaries and shuts workers down cleanly.

**Tear down.** Host → Projects → **Destroy** (type the name and the master password), or `python research.py project destroy myproj` — removes the container, workspace dir, network, and credentials snapshot.
