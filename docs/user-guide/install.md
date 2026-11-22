---
title: Install
parent: User guide
nav_order: 4
---
# Install

Three surfaces. The **host CLI** is the foundation; the **browser UI** and the **dists** layer on top.

**1. Shared infrastructure + images** (required, once):

```bash
python research.py start
```

Builds all container images, starts the small **`rs-router`** egress-filter container, and auto-caches the agent + editor dists if absent. Idempotent — re-run anytime; add `--rebuild` after editing image sources. The image tree is layered so a rebuild only cascades where it must (a no-DIND base → the sysbox-DIND base → flavor leaves). Pins live in `versions.env` (`research images versions`).

**2. Agent + editor dists** (auto on `start`):

The two agents — Claude Code and pi — and the code-server editor are **not baked into images**: they're built once into a host cache and `cp`-deployed into every container at boot. `start` pulls Claude Code and the editor if missing; pull pi before you give a project pi, and touch the rest only to upgrade (the same downloads are on Sandboxes → Software):

```bash
python research.py agent show | pull | refresh [--agent claude|pi]   # the agent dists (default claude)
python research.py editor show | pull | refresh                      # the code-server editor dist
```

A DIND project requires a pulled agent dist; `start` guarantees Claude Code's. Upgrades are deliberate: `refresh` bumps the `versions.env` pin, and a running project picks it up via **Update** in its ⚙ window's Agents section, or `project update-agent`, which deploys the cached agents into the project's own terminal and every running box live (no recreate; Claude Code's editor extension is reinstalled too), on every project type.

**3. Browser UI + broker** (optional, recommended). Off by default — with the broker stopped the CLI is unchanged and the webui is read-only. To bring it up:

```bash
python research.py broker passwd   # choose the master password
python research.py broker start
python research.py webui start
```

Open `https://localhost:7777`, accept the self-signed certificate, and set the same master password in the browser. See [The browser UI](browser-ui.md).
