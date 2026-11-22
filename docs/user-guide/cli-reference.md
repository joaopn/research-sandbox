---
title: CLI reference
parent: User guide
nav_order: 22
---
# CLI reference

The complete `research.py` surface. Every subcommand also has `--help`; this is the map, not a substitute for it.

### Infrastructure

```
start [--rebuild]                  Build images (if missing) + start the shared fleet (router, registry, gitea,
                                   enabled MCPs, and the webui if it was stopped) + cache agent/editor dists
stop                               Stop the whole shared fleet: webui, shared MCPs, gitea, registry, router.
                                   Projects, images and volumes untouched. Non-zero exit if anything survives
images versions                    Print current image version pins (from versions.env)
```

### Project lifecycle

```
project create <name> [opts]       Create a project (options below)
project attach <name>              docker exec + byobu attach (sign in to Claude here)
project ssh <name>                 Print SSH connection info (host port + password)
project list                       Show all projects
project status <name>              Detailed state + worker summary
project stop  <name|--all>         Stop the supervisor without destroying
project start <name|--all>         Start a stopped supervisor
project update <name> [--rebuild] [--enable IDS] [--disable IDS]
project update-agent <name>        Deploy the cached agents into a running project + its running boxes (live)
project destroy <name|--all>       Remove container + workspace + network + creds (multiple names ok)
```

**`project create` options:**

```
--workflow <name>          Workflow selector (default: research). See `workflow list`.
--mount <paths>            Comma-separated host folders (repeatable), each RO at /workspace/mnt/<basename>/
--egress {open,locked}     Egress policy (default: DEFAULT_EGRESS from .env for research, usually open; locked for the other shipped workflows)
--enable / --disable <ids> Worker services to (de)activate at create
--mcp <names>              MCPs to auto-allow: 'all-enabled' (default), 'none', or a CSV of registry names
--memory / --cpus <limit>  Docker resource limits (e.g. 16g / 4)
--inner-firewall           Defense-in-depth ACL on the supervisor's inner bridge
--dind {auto,sysbox,privileged}   Container runtime (default: auto)
--ssh-port <port>          Explicit SSH host port
--agent / --agents <a[,b]> agent dist(s) to deploy at boot (docker boxes: the full set; a sandbox-dind or dev supervisor: exactly the set, with claude still offered to its boxes)
--repo URL --ref SHA       clone repo@ref into /workspace/<name> at create (docker boxes and sandbox-dind supervisors)
--setup-script <snippet>   Shell snippet run once after checkout (RS_GITHUB_PAT for a private clone)
--git-user-name / --git-user-email   git commit identity configured in the box at create
--role-mcp-upstream ROLE=CSV   Pin an explicit upstream list for a --enable'd role-mcp (repeatable)
--harness-mode <mode>      Working mode a dev project's agent starts in (none, direct, doer-checker, checker-doer, planner-doer-checker)
--test-policy <policy>     Test policy a dev project starts on: light (default) or full
```

#### Authenticating the clone

Two mutually exclusive options; supplying both is refused.

| | PAT | SSH key |
|---|---|---|
| Supplied as | `RS_GITHUB_PAT=<token>` | `RS_GITHUB_SSH_KEY_FILE=<path to private key>` |
| Clone transport | https | ssh, via `ssh.github.com:443` |
| Stored in the box | the clone's remote URL, i.e. `.git/config` under `/workspace` — **which is a host bind-mount** | `~/.ssh/github` (0600), in the container layer only |
| Later pushes | work (token is in the remote) | work, if the key has write access |
| Scope | can be fine-grained per-repo | account-wide unless you use a per-repo deploy key |

Neither is ever a CLI flag, and the SSH key is a **file path**, never an env value —
key material must not reach `ps` or `/proc/<pid>/environ`. The key is delivered into
the container over `docker exec` stdin, so it never appears in `docker inspect`.

The SSH option requires a `github.com` repo (it writes a GitHub-specific
`~/.ssh/config`) and uses port 443 because the locked-egress router drops port 22.
GitHub's host keys are pinned, so there is no trust-on-first-use step.

Pair either with `--git-user-name` / `--git-user-email` — without a commit identity
git refuses to commit. Both the key and the identity survive `project stop`/`start`.

> **Prefer a per-repo deploy key.** The agent running in the box can read
> `~/.ssh/github`, and RS cannot scope the key for you. See `docs/SECURITY.md`.

### Dists (host-cached, cp-deployed at boot — not baked)

```
agent  show | pull | refresh [--agent claude|pi]   Manage the agent dists (Claude Code, pi)
editor show | pull | refresh                      Manage the code-server (editor) dist
```

pi reads `.claude/rules/` (path-scoped rules), `.claude/CLAUDE.md`, `CLAUDE.local.md` and a subdirectory's `CLAUDE.md` like Claude Code does, through the RS-owned `rs-rules` extension the pi dist carries (`/rules` inside pi lists what it found); it also refuses an edit of a file that was not read first, as Claude Code does, and refuses a single-child subagent launch the agent makes without a brief (an empty or missing `task`) before the child starts: Claude Code's Task tool requires its prompt, and a child started on an empty task may invent one. Every subagent pi itself runs through pi-subagents (in the foreground or the background) loads the same extension, so a child sees the project's `.claude/CLAUDE.md` and rules as the parent does, except RS's own `doer` and `checker`, which get their part of RS's instructions and the workspace's `.claude/CLAUDE.md` and `CLAUDE.md` in place of the session's other rules and instruction files (path-scoped rules still load as they read files); agents on pi-subagents' external-CLI runners (such as `claude-code`) are other programs and load nothing of pi's, and a launch whose capability ceiling denies extensions is refused. The same extension gives pi Claude Code's shell directory: a `cd` in one shell command carries over to the next as long as it stays inside the folder pi was started in (a command that leaves it is answered with "Shell cwd was reset to …" and the next one starts there again), and relative paths given to pi's file tools resolve against that same directory.

pi also carries subagents: the pi dist ships the `pi-subagents` extension package at the `PI_SUBAGENTS_VERSION` pin (the pi row under Sandboxes → Software shows it as `subagents`), named in pi's settings. A project whose pi settings predate it gets the entry added once, the first time pi starts after the project's pi download is redeployed (a dev or sandbox-dind project: its next stop and start, Update or agent Add; a docker project: its next Update or editor toggle; a box: an Update or Add while it runs, or its re-creation); a hand-installed `npm:pi-subagents` entry is replaced by the shipped copy. Removing the entry afterwards sticks on a dev or sandbox-dind project (across a stop and start too); a box or a docker project that is re-created starts from the shipped settings again, entry included.

### Browser UI + broker (optional)

```
broker passwd                      Set/replace the master password (the same one the browser uses)
broker start | stop | status       The host-side lifecycle-verb daemon (owns docker access)
webui  start [--bind IP] [--port N] [--rebuild]   Start the HTTPS browser UI
webui  stop | status | import | cert-tailscale
```

`cert-tailscale` stages a Tailscale certificate for the browser UI and also turns on its automatic renewal: from then on `research start` and `research webui start` ask Tailscale for a newer certificate and install it. An install whose Tailscale certificate was staged before this release runs `cert-tailscale` once to turn renewal on.

### Workflows

```
workflow list                      List available workflows (built-in + store + BYO)
```

### `mcp` registry — external tools (reached through the per-project proxy)

```
mcp add <name> --kind {external,shared} [--image IMG --port N | --host HOST]
mcp list | remove | enable | disable | start | stop | test ...
mcp set-workers <name> <csv>       Which worker services auto-wire this MCP as an upstream
project mcp allow | deny | list | sync <proj> [<mcp>]
```

### `worker` registry — pipeline agents (analysis worker + service role-MCPs)

```
worker list [--json]               Catalog (DEFAULT col = auto-enabled in new projects)
worker enable | disable <name>     Flag a service for auto-enable in new projects
project worker enable <proj> <name> [--upstream csv | --auto]
project worker disable | stop | start | list | status <proj> [<name>]
```

### In-supervisor CLIs

The supervisor's Claude uses these; you rarely call them directly.

- **`rs-worker`** — analysis-worker lifecycle: `spawn --plan`, `list`, `status`, `wait`, `message`, `finalize`/`accept`/`unstage` (the cycle gates), `shutdown`/`destroy`, `attach`/`tail`.
- **`rs-sandbox`** — (DIND projects) box lifecycle: `create [name] --preset <type> [--agent claude|pi|none] [--editor] [--mcps csv] [--repo URL --ref SHA --setup CMD]`, `list [--json]`, `stop`, `start`, `restart`, `discard [--keep-workspace]`. `<type>` is any box type in the catalog: the eight built-in ones (see [Capabilities](capabilities.md#boxes)) or one the operator registered; default `empty`.
