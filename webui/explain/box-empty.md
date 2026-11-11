# Empty box

A clean, isolated box — **no baked instructions, no bundled capability**. The
blank slate: you decide what goes in it.

## What it is

A container on the project's `rs-inner` bridge, behind the project's
egress filter, that boots **credential-free**. Nothing is pre-wired — there's no
role prompt (only RS's short notice in the agent's home rules), no browser, no data
tooling.

## Good for

- A scratch container for un-vetted code or a quick experiment.
- A from-scratch agent task where you want to supply your own framing.
- A plain shell with the project's egress policy and nothing else.

## Build it out with the toggles

- **Agent** — None by default. Pick one of the project's agents to install
  them; sign it in inside (`claude` then `/login`, or `pi` then `/login`).
- **Editor** — bundle the code-server editor if you want a GUI.
- **MCP tools** — wire any of the project's allowed MCP servers in (this turns
  the agent on, since nothing else reaches an MCP).

Your outputs live in `/workspace` (the project's `boxes/<name>/`) and stay
across box and project restarts. Remove the box from the `✕` on its tab: its
container and files go, unless you tick **preserve artifacts**.
