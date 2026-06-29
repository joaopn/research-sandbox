# PaperOrchestra box

A box that **clones the PaperOrchestra repo at boot**, with Claude and the editor
on by default. No fields to fill in — the repo is baked into the box type.

## What it is

A disposable container seeded at boot with
[PaperOrchestra](https://github.com/Ar9av/PaperOrchestra) cloned to
`/workspace/PaperOrchestra`. The agent starts there and follows **the repo's own**
conventions (`README` / setup docs) — the box carries no project-specific framing
of its own beyond a short pointer note.

## Good for

- Jumping straight into PaperOrchestra without typing the clone URL each time.
- Exploring or running the project from the bundled editor (code-server is on by
  default — uncheck it under Settings if you don't want it).
- A clean, disposable workspace per session — outputs live in `/workspace` and
  persist across box stop/start.

## Defaults

- **Agent** — Claude (auth-free: run `claude` then `/login` inside the box).
- **Editor** — on (code-server), pre-checked under Settings.

Add **MCP tools** if the work needs project MCP servers. Set up the project as its
README describes (install dependencies, etc.) once you're in.
