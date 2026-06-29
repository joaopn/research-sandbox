# Bring-your-own box

A BYO box — **clone a ref-pinned repo** (with an optional setup command run at
boot), then drop an agent into it. Supply the repo / ref / setup when you add the
box.

## What it is

A disposable container seeded at boot with a repo cloned at a pinned ref and your
optional setup command already run. The clone lives at `/workspace/<repo-name>`;
the agent starts there and follows **the repo's own** conventions
(`README` / `CLAUDE.md` / setup docs) — the box carries no project-specific
framing of its own.

## Good for

- Working inside an existing codebase without polluting the project workspace.
- Reproducible runs — the ref pins exactly what gets cloned.
- Bootstrapping a session that needs `pip install`/build steps done up front
  (put them in the setup command).

## The clone fields (below Settings)

- **Repo (https)** — the clone URL.
- **Ref** — branch / tag / commit to pin (required once a repo is set).
- **Setup** — a command run inside the box after the clone.

Add **MCP tools** if the repo's work needs project MCP servers. Boots
credential-free: run `claude` then `/login` inside. Outputs live in `/workspace`
and persist across box stop/start.
