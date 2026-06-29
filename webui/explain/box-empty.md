# Empty box

A clean, isolated box — **no baked instructions, no bundled capability**. The
blank slate: you decide what goes in it.

## What it is

A disposable container on the project's `rs-inner` bridge, behind the project's
egress filter, that boots **credential-free**. Nothing is pre-wired — there's no
role prompt in `/workspace/CLAUDE.md`, no browser, no data tooling.

## Good for

- A scratch container for un-vetted code or a quick experiment.
- A from-scratch agent task where you want to supply your own framing.
- A throwaway shell with the project's egress policy and nothing else.

## Build it out with the toggles

- **Agent** — off by default. Turn it on to deploy `claude`, then run `claude`
  and `/login` inside to authenticate.
- **Editor** — bundle the code-server editor if you want a GUI.
- **MCP tools** — wire any of the project's allowed MCP servers in (this turns
  the agent on, since nothing else reaches an MCP).

Your outputs live in `/workspace` and persist across box stop/start; the box is
discarded — container and workspace — from the `✕` on its tab.
