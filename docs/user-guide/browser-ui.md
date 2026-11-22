---
title: The browser UI
parent: User guide
nav_order: 1
---
# The browser UI

`research webui start` serves a single-page app over HTTPS, by default at `https://localhost:7777`. With the **broker** running it's a full control plane; with it stopped it's a read-only terminal surface. It's the recommended way to drive everything short of authoring images.

The left rail lists your projects, with four entries at its bottom:

| Page | Opens as | What it does |
|---|---|---|
| Projects rail | Always shown | Lists projects and opens their tabs; host CPU and memory at the bottom |
| New Project | Floating window | Workflow cards to create a project; import an existing one |
| Development | Tabs: Gitea, Fetch, Repos, Images, Reviews, Backup | The dev lane: the shared Gitea, fetch commands, repositories, image builds, reviews, Gitea backups |
| Sandboxes | Tabs: Management, Infrastructure, Software, MCP | The project table; shared services and host settings; agent, editor and image builds; MCP servers |
| Settings | Floating window | Interface settings kept in the browser: themes, layout, zoom, terminal font |

- **New Project.** A grid of workflow cards grouped as Research, Base and Store (see [Workflows](workflows.md)). Click one → a prefilled create form → **Create** → live progress as the project comes up. Each card's **Explain** opens its learning material (or the repo's README, for store cards). **Import project** adds a box you run elsewhere, by its SSH address; a project on this host is added with Attach under Sandboxes → Management.
- **Sandboxes.** The **Management** tab lists the host's projects in a sortable table and drives the whole lifecycle — start / stop / update / destroy / attach — each long operation streaming a progress checklist. Destroy asks you to type the project name *and* the master password again. **Infrastructure** holds the pass-through ports and the shared services (Gitea, the reviewer); **Software** the agent and editor downloads, the image fleet, the version pins, and the apps RS's own project and box types install (OpenKnowledge: check npm for a newer release and move every OpenKnowledge project and box to it at its next start); **MCP** the MCP servers behind the shared gateway and which agents may use them (see [Capabilities](capabilities.md)).
- **The projects rail.** Running projects sit in the order and groups you drag them into; each row has Stop and ⚙. Stopped ones sit in a "Stopped (n)" fold below, each with Start and ⚙. Stop and Start ask inside the row first; the row moving is the feedback.
- **Per-project settings (the ⚙ window).** The gear on a project's row opens a floating window with these sections:

  | Section | What you set |
  |---|---|
  | Tabs | Which of the project's tabs show |
  | Extensions | The editor and the Reader on or off; the agents' status bar |
  | Agents | Which agents the project carries, their models, Update (deploys the downloaded agent versions live); a box's agent model |
  | Sharing | The project's read-only folders (applied at its next start) and each box's folders (saving restarts that box fresh, which signs its agent out) |
  | Ports | Ports a project or box serves, as tabs |
  | Alerts | Arm a notification when one of the project's agent sessions hands back |
  | Sidebar | Drop this browser's bookmark for the project |

- **Boxes.** On a project with inner Docker, the **+** button on its tab strip adds a box (a type plus agent / editor / MCP options); the ✕ on the box's terminal tab removes it. See [Capabilities](capabilities.md#boxes).
- **Terminals + editor.** In-browser ssh/byobu terminals per service (xterm + host-key TOFU + Ctrl-F search) and the code-server editor in an iframe; drag tabs to arrange up to three columns side by side. Each editor and app gets its own origin port, so each keeps its own browser storage.
- **Activity and alerts.** Each project row shows small coloured boxes counting the interactive Claude Code and pi sessions in the project and its boxes: green working, amber waiting for you, blue subagents running. Hovering a box says where each number comes from. A project armed under ⚙ → Alerts shows a bell; when one of its sessions hands back, the page shows a corner notice, marks the tab title while the tab is in the background, and sends a browser notification where the browser allows it.
- **Sign-in.** One **master password** covers the browser. You choose it when the browser's vault is first created, and it must match the password set on the host with `broker passwd`. The browser derives two values from it: one encrypts your saved project bookmarks and interface settings *in the browser* and never leaves it; the other signs in to the broker. Neither the webui nor the broker receives the password. Unlocking signs in for eight hours; locking signs out. Signing in to an **agent** is still per-project, inside it (`claude` then `/login`, or `pi` then `/login` with an API key).

> [!NOTE]
> This is the **single-operator** setup — one master password, proportionate for a localhost / private-tailnet box. The webui and broker run as **you** (same uid): that's the contract that lets the webui reach the broker's socket, and the webui holds **no docker socket** of its own. Ten failed sign-ins within a minute block sign-in for a minute, but there are no per-user accounts and a destroy cannot be undone: keep it off networks you do not trust.
