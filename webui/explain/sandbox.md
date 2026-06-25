# Sandbox workflow

A single confined container — ssh + an optional editor, **no inner Docker, no
agent**. The lean "just give me a box" workflow.

## What it is

A `docker` substrate project: one plain runc container (`rs-minimal`), not a
docker-in-docker host. No supervisor, no workers, no role-MCPs. You get a shell
(and, if you enable it, the code-server editor) behind the project's egress
filter. By default it boots **un-authed and editor-off** — genuinely minimal.

## When to use

- You want a disposable, network-confined scratch box — run a script, try a
  package, poke at data — without standing up the whole research lab.
- You don't need an agent or parallel workers.

Reach for **research** if you want a supervising agent + workers; **box-host** if
you want to spin *several* isolated boxes yourself.

## How it works

`create --workflow sandbox` launches one runc container with its default route
injected through `rs-router`, defaulting to **locked** egress (80/443/53 + ICMP,
RFC1918 blocked) — enough for pip/apt/an LLM API, but not arbitrary ports or the
host LAN. There is no inner Docker daemon and no agent baked in. Add the editor
with `--enable code-server`; add an agent's CLI with `--agent <name>` (deployed
from the host-cached dist at boot).

## Orchestration

RSORCHESTRATIONSVG

## Components

- **The box** — one `rs-minimal` runc container: ssh + byobu, optional editor,
  optional agent CLI(s). No inner Docker, so it cannot spawn containers.
- **rs-router** — the project's egress filter; the real confinement (locked by
  default). `--egress open` opts into full outbound.

## Lifecycle

`create --workflow sandbox` → open the Shell (or Editor) tab → work → `destroy`.
No supervisor session, no logbook, no worker review cycle — it's just the box.
