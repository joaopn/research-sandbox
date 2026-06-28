# Bring-your-own box

You are an agent in a disposable **BYO box** — a confined container seeded with a
repo cloned at boot (ref-pinned) and an optional setup command already run. The
clone lives at `/workspace/<repo-name>`; start there.

Read the repo's own `README` / `CLAUDE.md` / setup docs — the box carries no
project-specific instructions of its own beyond this note. Whatever conventions
the cloned repo defines are the conventions to follow.

If `/workspace/.tools-inventory.md` exists, read it for any project MCP servers
wired into this box.

This box is disposable and credential-free — run `claude` then `/login` inside to
authenticate. There is no artifact-publishing contract; your outputs live in
`/workspace` (and persist on the project volume across box stop/start).
