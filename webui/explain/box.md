# Boxes

Isolated containers you add **inside a dind project** (sandbox-dind or
research) — for un-vetted code, a scoped agent task, or a browser / data
session. A box stays until you remove it: restarting the project brings its
boxes back, and a box's files, in the project's `boxes/<name>/` folder, stay
across restarts.

## What it is

A box is a container in the project's inner Docker daemon, on the `rs-inner`
bridge, behind the project's egress filter. It boots **credential-free** — even a
box with an agent has no login until you sign the agent in inside it (`claude`
then `/login`, or `pi` then `/login` with an API key). Boxes are added from the
**+** button (Add a box) on the project's tab strip (and the in-container
`rs-sandbox` command).

## Box types

Each **type** is a preset — the same two box images with different baked
instructions + bundled capability:

- **empty** — a clean box, no instructions, no capability, agent off. The
  blank slate.
- **websearcher** — Playwright + headless Chromium baked in, with instructions
  for web research; its browser is restricted unless **Trusted browser** is
  ticked.
- **data-wrangler** — data-shaping instructions; add a database MCP to give it
  something to wrangle.
- **byo** — clone a ref-pinned repo (with an optional setup command run at
  boot) and work in it.
- **paper-orchestra** — clones the PaperOrchestra repo at boot; the editor is
  on by default.
- **open-knowledge** — an OpenKnowledge knowledge base in the box's `notes/`
  folder, edited in its own tab beside the agent. **Share files** starts
  ticked, and **Gitea history** is offered.
- **zotero** — your Zotero library through the zotero MCP server, over the web
  API. The window asks for an API key and a library ID, which the box keeps.
- **dev** — a software-dev box from a GitHub URL and a branch: the repo is
  mirrored and forked on the shared Gitea, the fork is cloned at boot, and work
  is delivered as pull requests. The editor is on by default; MCP tools,
  Browser, rs-fetch and read-only folders are not offered.

Each type card's ⓘ opens that type's own page. Operators can register more
types in `box-registry.json`.

## Toggles

Independent of the type:

- **Agent** — None, or one of the project's agents. Picking any agent installs
  every agent the project carries, each still signed out; the pick decides
  which agent's settings the window shows. The type sets the default:
  **empty** starts with None, every other type with the project's first
  agent. Selecting any MCP rules out None. With Claude picked, a row beside it
  sets the model and effort.
- **Editor** — bundle the code-server editor. Off by default; the **dev** and
  **paper-orchestra** types turn it on.
- **Browser** — run the box on the Playwright + headless Chromium image so its
  agent can browse. The box type only sets the default (websearcher pre-checks
  it; its instructions assume the browser tools, so un-checking it there leaves
  them pointing at tools the box doesn't have). Heavier image — leave it off
  unless the box needs the web. Not available on dev boxes.
- **Trusted browser** — shown once Browser is on, off by default. A browser box
  is *restricted* unless this is ticked: nine risky actions a web page could
  talk the agent into (running page script or code, uploading or dropping
  files, dragging, answering dialogs, saving PDFs, installing browsers,
  resizing the window) are blocked for every agent, and the browser ignores
  Playwright settings in the box's environment. A trusted browser gives the
  agents every browser tool and honours those settings. Chosen when the box is
  added; to change it, remove the box and add it again.
- **rs-fetch** — wire the box to pull dev-lane agent commits from the shared
  Gitea into a git clone here (read-only; off by default; not on dev boxes;
  not with Gitea history).
- **Gitea history** — offered for the OpenKnowledge type when this host's Gitea
  is enabled: the box's notes are pushed to a private repo on the shared
  Gitea; the box can add to its main history but never delete or rewrite it.
  Not with rs-fetch.
- **MCP tools** — wire any of the project's allowed MCP servers into the box's
  agent through the inner proxy.
- **Read-only folders** — tick **Add folders** and type the project folders the
  box should read, comma-separated, as paths in the project (`data`,
  `results/run3`). Each appears in the box under `/mnt/<path>` and cannot be
  changed from the box — by its agent, by `sudo`, by anything. None by
  default. Not on dev boxes.
- **Share files** — off by default (the OpenKnowledge type starts with it
  ticked), and set when the box is created: to change it, remove the box and
  add it again. A box that shares gets a mailbox, `/workspace/shared/<box>/`
  — the one project folder it writes, which the project agent, the workers
  and the other sharing boxes read — and reads, under `/workspace/shared/`,
  the project agent's mailbox and the other sharing boxes' mailboxes,
  including boxes added after it. It never sees the workers' or the role
  services' output unless you give it `shared` as a read-only folder. A box
  that does not share has no mailbox and no `/workspace/shared` (only what you
  give it as read-only folders), and nothing it writes is put where the project agent
  or the workers look. On the project side a sharing box's mailbox is
  `shared/.boxes/<box>/`; it stays when the box is removed, and a sharing box
  added later under the same name writes into it again. When the project has
  a sharing OpenKnowledge box, every other sharing box and the project's own
  agent have a user-only command `/ok` that writes knowledge-base notes into
  their mailbox, and the OpenKnowledge box's `/ok-ingest` brings them in;
  neither runs unasked. The ⚙ panel's **Box folders** section names the boxes
  that share.

## Read-only folders

A folder is given by its path in the project, any depth (`data`,
`results/run3`); the box window lists the project's top-level folders under the
field as a reminder. Every level of the path must be a real folder — a link or
a file anywhere along it is refused. Giving `shared` gives the box every
mailbox and every role service's publication, read-only at `/mnt/shared` — the
way to show a box the workers' output. Inside `boxes/`, `workers/`, `shared/`,
`.role-mcps/` and `.orchestrator/` only the whole folder can be given: other
agents write there, and a folder they could swap for a link would hand the box
what the link points at. For the same reason a host folder mounted at
`mnt/<name>` is given whole, never a folder inside it. Change a box's folders
later in the project's ⚙ panel under **Box folders** (the same comma-separated
field): saving restarts the box fresh, so its agent logs in again and its setup
runs again; the box's own files are untouched.

Everything in a given folder is readable inside the box, **credentials
included**: `workers/` holds a copy of the project's Claude login in each
worker's folder, `boxes/` holds every box's own files (other boxes'
notes, clones and configuration), `.orchestrator/` holds the project's wiring, `.creds-stash/`
holds the login while the project restarts, and a sandbox-dind project's setup
clone keeps the GitHub token it was cloned with in its git settings.

A given folder that is gone when the box starts (deleted, renamed, a level
replaced by a link or a file) is skipped: the box starts without it and says
so in the progress list (or in the ⚙ panel when a save there restarts it), and
mounts it again at its next restart (a project restart, a port change, a
folders change) once it is back. Links
between folders: a relative link between two folders the box was given works
inside the box; an absolute link, or one into a folder it was not given, shows
as broken.

## Orchestration

RSORCHESTRATIONSVG

## How it works

Adding a box creates it in the project's inner Docker daemon with a pinned
IP on `rs-inner`. The chosen type's instructions land in the box agent's home
rules folder (`~/.claude/rules/rs-instructions.md`, rewritten at every start; the
box's own `/workspace/.claude/` is yours); any selected MCPs are wired to `mcp-proxy:8888` so the
box's agent reaches them by name. Egress is the project's router policy (a
sandbox-dind project defaults to locked). Remove a box from the `✕` on its tab.
By default its container and its files go; tick **preserve artifacts** in the
remove dialog to keep its files in the project's `boxes/<name>/` folder. A
sharing box's mailbox stays either way.
