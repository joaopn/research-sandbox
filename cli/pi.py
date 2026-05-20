"""pi — host-side helpers for per-project PI-role container lifecycle.

Stdlib only. Used by research.py's `project pi {enable,disable,list,
status,sync-creds}` subgroup. Mirrors the shape of cli/role_mcp.py but
for the PI-facing surface (interactive role containers accessed via
webui tabs / xterm+byobu).

Boundary: writes/reads ``<workspace>/.orchestrator/pi-roles.json``
(host-visible, supervisor-readable via bind-mount), and shells out to
``docker exec <supervisor> docker run|stop|rm ...`` for inner-dockerd
container management. There is no host-side PI container ever; PI
containers live in the supervisor's inner dockerd alongside workers,
mcp-proxy, and role-MCPs.

Two registries side by side:
- ``cli/role_mcp.py``       — worker-facing role-MCP daemons (echo-mcp,
                              wrangler, …). Per-project ``role-mcps.json``.
- ``cli/pi.py``  (this)     — PI-facing interactive role containers
                              (pi-echo, pi-wrangler, …). Per-project
                              ``pi-roles.json``.

The two are deliberately distinct surfaces with separate lifecycles. A
name collision between them (e.g. ``wrangler`` and ``pi-wrangler``) is
prevented by the ``pi-`` prefix on PI image keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Registry — names, images, pinned inner-bridge IPs
# ---------------------------------------------------------------------------

# Pinned IPs on rs-inner (192.168.99.0/24). See `.claude/CLAUDE.md`
# load-bearing-IPs rule. Range .10-.25 is reserved for PI containers
# (a /28 in spirit; iptables ACCEPT uses -m iprange for explicit
# semantics, see inner-firewall.sh).
#
# Order is the canonical allocation: pi-wrangler is .10 (ships with P.3,
# mirrors B.3), pi-librarian .11 (P.2), pi-websearcher .12 (P.1),
# pi-echo .13 (P.0 substrate test fixture). New PI roles claim .14+ and
# update this table + the IP-allocation rule in .claude/CLAUDE.md in
# lockstep.
PI_IPS: dict[str, str] = {
    "pi-wrangler":    "192.168.99.10",
    "pi-librarian":   "192.168.99.11",
    "pi-websearcher": "192.168.99.12",
    "pi-echo":        "192.168.99.13",
}

# Per-role docker image tags built by research.py's _build_images. Each
# entry's image FROMs rs-pi-base:latest and bakes the role-specific
# role.md under /opt/pi-templates/<short>/. The short name is derived
# from the key by stripping the `pi-` prefix — see role_short().
#
# _build_images derives the Dockerfile name from the key: role
# `pi-echo` -> `agent/Dockerfile.pi-echo`.
PI_IMAGES: dict[str, str] = {
    "pi-echo":         "rs-pi-echo:latest",
    "pi-wrangler":     "rs-pi-wrangler:latest",
    "pi-websearcher":  "rs-pi-websearcher:latest",
}

# Container-name idiom — same prefix-with-role-name pattern as role-MCPs.
# Inner-dockerd container names: `pi-echo` key → `rs-pi-echo` container.
def role_container_name(role: str) -> str:
    return f"rs-{role}"


def role_short(role: str) -> str:
    """Strip the ``pi-`` prefix for use as the entrypoint's RS_PI_ROLE
    env var value and the template-directory name. ``pi-echo`` → ``echo``."""
    return role[len("pi-"):] if role.startswith("pi-") else role


# ---------------------------------------------------------------------------
# Per-project state file
# ---------------------------------------------------------------------------


def pi_roles_path(workspace: Path) -> Path:
    """Sibling to role-mcps.json + mcp-allow.json. Read by:
       - host (this module) — for enable/disable/list/status orchestration.
       - `_recreate_supervisor` in research.py — to restart enabled PI
         containers after the supervisor swap.
       The PI containers themselves do not consult this file; their
       runtime config is fully expressed via env + bind-mount sources."""
    return workspace / ".orchestrator" / "pi-roles.json"


def load_pi_roles(workspace: Path) -> dict[str, dict]:
    p = pi_roles_path(workspace)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_pi_roles(workspace: Path, data: dict[str, dict]) -> None:
    """Atomic-rename write. Parent dir is bind-mounted into the supervisor,
    so the rename is visible to in-supervisor consumers immediately (the
    single-file-bind-mount rule does not apply — we bind-mount the parent
    dir, not the file)."""
    p = pi_roles_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_role(role: str) -> None:
    if role not in PI_IPS or role not in PI_IMAGES:
        known = sorted(set(PI_IPS) & set(PI_IMAGES))
        raise ValueError(
            f"unknown pi role {role!r}. Known pi roles: "
            f"{', '.join(known) or '(none)'}"
        )


def build_entry(role: str) -> dict[str, Any]:
    return {
        "ip": PI_IPS[role],
        "image": PI_IMAGES[role],
    }
