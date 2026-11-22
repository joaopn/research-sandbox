---
title: The browser UI
parent: User guide
nav_order: 1
---
# The browser UI

`research webui start` serves a single-page app over HTTPS. With the **broker** running it's a full control plane; with it stopped it's a read-only terminal surface. It's the recommended way to drive everything short of authoring images.

- **Workflow store.** New Project opens a grid of clickable workflow cards (Research / Base / Store) over the page. Click one → a prefilled create form → live progress as the project comes up. Per-card **Explain** docs deep-link the learning material (or the repo's README, for store cards).
- **Sandboxes page.** Its Management tab lists the host's live projects in a sortable table and drives the **whole** lifecycle — create / start / stop / update / destroy / attach — each long op streaming a progress checklist. `destroy` requires typing the name *and* re-entering the management password (step-up). Its **Infrastructure** tab holds the pass-through ports and the shared services (Gitea, the reviewer); its **Software** tab the agent and editor downloads, the image fleet, the version pins, and the apps RS's own project and box types install (OpenKnowledge: check npm for a newer release and move every OpenKnowledge project and box to it at its next start).
- **Per-project config (the ⚙ gear).** Show/hide terminal tabs, toggle the editor, and — on DIND projects — manage **boxes** (add/remove disposable sub-boxes with a preset + agent/editor/MCP toggles).
- **Terminals + editor.** In-browser ssh/byobu terminals per service (xterm + host-key TOFU + Ctrl-F search) and the code-server editor in an iframe, plus a split-pane to pin one service beside another.
- **Auth.** A client-side **vault password** encrypts your project bookmarks *in the browser* (never sent to the server). A separate **management password** (`broker passwd`) logs the Sandboxes page into the broker; the broker token lives server-side behind an opaque cookie. Signing in to **Claude** is still per-project (interactive `/login` inside the supervisor).

> [!NOTE]
> This is the **single-operator** setup — one shared management password, proportionate for a localhost / private-tailnet box. The webui and broker run as **you** (same uid): that's the contract that lets the webui reach the broker's socket, and the webui holds **no docker socket** of its own. Multi-user accounts, recoverable soft-delete, and login rate-limiting are planned before exposing it beyond localhost.
