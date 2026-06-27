# Sandbox-dind workflow

An **agent-less** docker-in-docker management host: a shell from which *you* spin
up isolated boxes via `rs-sandbox`, with no supervising Claude session.

## What it is

A `dind-sysbox` project using the `rs-sandbox-dind` overlay — the same isolated
DIND substrate as the research lab, but with **no agent layer**: no supervisor
Claude, no `rs-worker`. Its top-level container gives you a management shell whose
job is to launch and tear down isolated boxes inside its inner Docker daemon.

## When to use

- You want to run several independent, isolated boxes and drive them yourself,
  rather than have a supervisor agent plan and spawn workers for you.
- You want DIND isolation + per-box containment without an agent in the loop.

Reach for **research** if you want the supervising agent + worker pipeline;
**sandbox** if a single confined box is all you need.

## How it works

The management container (`rs-sandbox-dind`) boots the inner Docker daemon and the
`rs-inner` bridge itself (it has no supervisor MCP-reload path), then you use the
in-container `rs-sandbox` CLI to create blank isolated boxes (`rs-pi-iso-<name>`).
Boxes draw from the PI IP range, sit behind the project's (locked-by-default)
router egress, and boot un-authed — containment is router + no-creds + container
isolation. Agents and the editor are delivered per-box from the host-cached dists.

## Orchestration

RSORCHESTRATIONSVG

## Components

- **Management host** — `rs-sandbox-dind`, agent-less; runs the inner Docker daemon
  and `rs-inner` bridge; you drive it via its shell + `rs-sandbox`.
- **Isolated boxes** — `rs-pi-iso-<name>` containers you create/destroy on demand;
  isolated from each other and the host.
- **rs-router** — the project's egress filter (locked by default) for every box.

## Lifecycle

`create --workflow sandbox-dind` → open the Management shell → `rs-sandbox` create
boxes as needed → work in them → tear boxes down → `destroy` the project.
