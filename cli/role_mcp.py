"""role_mcp — host-side helpers for per-project role-MCP lifecycle.

Stdlib only. Used by research.py's `project role-mcp {enable,disable,
list,status}` subgroup. Mirrors the shape of cli/mcp_registry.py +
research.py's per-project allowlist module.

Boundary: writes / reads ``<workspace>/.orchestrator/role-mcps.json``
(host-visible, supervisor-readable via bind-mount), and shells out to
``docker exec <supervisor> docker run|stop|rm ...`` for inner-dockerd
container management. There is no host-side container ever; the
role-MCP container lives in the supervisor's inner dockerd.

The "general MCP registry" and the "role-MCP registry" are deliberately
kept as two separate surfaces — external MCPs are *capabilities the
project has access to*, role-MCPs are *internal orchestration
containers*. Both happen to land in the worker's mcp-proxy routing
table, but their lifecycle and ownership are distinct.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Registry — names, images, pinned inner-bridge IPs
# ---------------------------------------------------------------------------

# Pinned IPs on rs-inner (192.168.99.0/24). See `.claude/CLAUDE.md`
# load-bearing-IPs rule. Range .4-.9 is reserved for role-MCPs; .10-.19
# reserved for PI role containers; .2 is mcp-proxy; .3 reserved for gitea.
#
# Order is the canonical allocation: echo-mcp is .4 (B.0 test target),
# websearcher .5 (B.1), librarian .6 (B.2), wrangler .7 (B.3). New roles
# claim .8 / .9 deliberately and update the table; .10+ is for PI roles.
ROLE_IPS: dict[str, str] = {
    "echo-mcp":     "192.168.99.4",
    "websearcher":  "192.168.99.5",
    "librarian":    "192.168.99.6",
    "wrangler":     "192.168.99.7",
}

# Per-role docker image tags built by research.py's _build_images. Each
# entry's image FROMs rs-role-mcp-base:latest and bakes the role-specific
# role.md + summarize.md (+ optionally skills/) under /opt/role-mcp/role/.
#
# Naming inconsistency: echo-mcp carries the `-mcp` suffix in its key
# because it was the protocol's no-op test target — the suffix made it
# read as "the echo MCP" rather than the name of an actual research role.
# Real roles (wrangler, librarian, websearcher) drop the suffix: the role
# IS the name. New roles follow the no-suffix pattern.
#
# _build_images derives the Dockerfile name from the key: role `wrangler`
# -> `agent/Dockerfile.wrangler`. role `echo-mcp` -> `agent/Dockerfile.echo-mcp`.
ROLE_IMAGES: dict[str, str] = {
    "echo-mcp":     "rs-echo-mcp:latest",
    "wrangler":     "rs-wrangler:latest",
}

# Fixed listen port inside every role-MCP container. Each container is
# in its own network namespace, so the constant doesn't collide with
# anything — no port-mapping arithmetic. Workers reach via the proxy.
ROLE_MCP_PORT = 8000

# Container-name idiom — same prefix-with-role-name pattern as workers
# use, mirrors how `mcp-proxy` is just a fixed name on the inner bridge.
def role_container_name(role: str) -> str:
    return f"rs-{role}"


# ---------------------------------------------------------------------------
# Per-project state file
# ---------------------------------------------------------------------------


def role_mcps_path(workspace: Path) -> Path:
    """Sibling to mcp-allow.json. Read by:
       - host (this module) — for enable/disable/list/status orchestration.
       - supervisor's mcp_render_config.py — to add role-MCP routes to the
         per-supervisor mcp-proxy alongside external MCPs.
       - role-MCP container entrypoint — to look up its own `upstream_mcps`
         and render the spawned-claude .mcp.json."""
    return workspace / ".orchestrator" / "role-mcps.json"


def load_role_mcps(workspace: Path) -> dict[str, dict]:
    p = role_mcps_path(workspace)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_role_mcps(workspace: Path, data: dict[str, dict]) -> None:
    """Atomic-rename write. Parent dir is bind-mounted into the supervisor,
    so the rename is visible to in-supervisor consumers immediately (the
    single-file-bind-mount rule does not apply here — we bind-mount the
    *parent dir*, not the file)."""
    p = role_mcps_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_role(role: str) -> None:
    if role not in ROLE_IPS or role not in ROLE_IMAGES:
        known = sorted(set(ROLE_IPS) & set(ROLE_IMAGES))
        raise ValueError(
            f"unknown role {role!r}. Known roles: {', '.join(known) or '(none)'}"
        )


def validate_upstreams(upstreams: list[str], allow_entries: list[dict]) -> None:
    """Hard-error if any requested upstream is missing from the project's
    mcp-allow.json. We deliberately validate at enable time, not at
    container-start time — so a misconfiguration surfaces to the operator
    on the host CLI, not as a silent missing-MCP at the spawned claude -p
    level."""
    by_name = {e.get("name") for e in allow_entries if isinstance(e, dict)}
    missing = [u for u in upstreams if u not in by_name]
    if missing:
        raise ValueError(
            "the following upstream MCP(s) are not allowed for this "
            f"project: {', '.join(missing)}. Run `research project mcp "
            f"allow <project> {' '.join(missing)}` first, or pick a "
            "different --upstream set."
        )


UPSTREAM_SOURCES = ("auto", "explicit")


def build_entry(role: str, upstreams: list[str], *,
                upstream_source: str = "auto",
                memory: str = "",
                max_concurrent_calls: int = 0) -> dict[str, Any]:
    """Render a per-project role-mcps.json entry.

    ``upstream_source`` discriminates how ``upstream_mcps`` was chosen:
      - ``"auto"``: derived from the registry's ``roles`` field. Re-derived
        by ``project mcp sync`` whenever the registry/allow set changes.
      - ``"explicit"``: operator-pinned via ``--upstream <csv>``. Survives
        sync untouched.

    ``memory`` is the per-role-MCP container memory cap (docker syntax,
    e.g. ``"2g"``). Always present in persisted entries so `_recreate_supervisor`
    re-applies the same cap without consulting Config defaults — the value
    is captured at enable time. Operator overrides via `--memory` at enable.

    ``max_concurrent_calls`` is the daemon-side cap on in-flight send_job
    calls. Beyond it, send_job returns an MCP tool error with structured
    `concurrency_limit` payload immediately. 0 disables the cap. Same
    "captured at enable time" semantics as ``memory``.

    Opaque to the role-MCP container (only ``upstream_mcps`` is read by
    the entrypoint); the upstream_source field is a registry-management
    discriminator."""
    if upstream_source not in UPSTREAM_SOURCES:
        raise ValueError(
            f"upstream_source must be one of {UPSTREAM_SOURCES}, "
            f"got {upstream_source!r}"
        )
    return {
        "ip": ROLE_IPS[role],
        "port": ROLE_MCP_PORT,
        "image": ROLE_IMAGES[role],
        "upstream_mcps": list(upstreams),
        "upstream_source": upstream_source,
        "memory": memory,
        "max_concurrent_calls": max_concurrent_calls,
    }
