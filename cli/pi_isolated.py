"""pi_isolated — host-side helpers for per-project PI-isolated containers.

Stdlib only. Companion to cli/pi.py (baked PI roles) and cli/role_mcp.py
(worker-facing role-MCPs). Where cli/pi.py drives the fixed, image-baked PI
roles (pi-echo, pi-wrangler, …), this module drives *PI-isolated* agents:
generic containers (rs-pi-isolated:latest) that clone an arbitrary skill
repo at enable time, parameterized by a host-side type definition in
``cli/pi_isolated_registry.py``.

Three things this module owns:
  - per-project snapshot ``<workspace>/.orchestrator/pi-isolated.json`` —
    the worker-/supervisor-facing source of truth (read by
    ``_recreate_supervisor`` to restart enabled agents, by the webui to
    filter tabs). Distinct from pi-roles.json (baked roles) and
    role-mcps.json (worker-facing roles).
  - container naming: ``rs-pi-iso-<name>``. The ``rs-pi-`` prefix is
    deliberate — pi-creds-watch.sh selects its fan-out targets by
    ``rs-pi-*``, so cred propagation covers isolated containers with no
    watcher change.
  - inner-bridge IP allocation from the PI range reserved for these agents
    (``.14-.25``; the firewall already ACCEPTs the whole PI range, so no
    inner-firewall.sh change is needed — see ``.claude/CLAUDE.md`` IP table).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Inner-bridge IP pool for PI-isolated agents. The built-in PI roles claim
# 192.168.99.10-.13 (cli/pi.py PI_IPS); .14-.25 is the reserved range for
# additional PI containers per .claude/CLAUDE.md. PI-isolated agents draw
# sequentially from here, per-project (each supervisor's inner dockerd is
# its own namespace, so the pool resets per project). RS_INNER_PI_RANGE in
# inner-firewall.sh already covers .10-.25 — no firewall edit on a new agent.
IP_POOL_LO = 14
IP_POOL_HI = 25  # inclusive
IP_PREFIX = "192.168.99."


def container_name(name: str) -> str:
    """``rs-pi-iso-<name>``. Keeps the ``rs-pi-`` prefix so pi-creds-watch.sh
    fans creds in unchanged, and the ``-iso-`` infix avoids colliding with a
    baked PI role's ``rs-pi-<role>`` name."""
    return f"rs-pi-iso-{name}"


# ---------------------------------------------------------------------------
# Per-project snapshot file
# ---------------------------------------------------------------------------


def snapshot_path(workspace: Path) -> Path:
    """Sibling to pi-roles.json / role-mcps.json / mcp-allow.json. Read by
    the host (this module), ``_recreate_supervisor`` (to restart enabled
    agents after a supervisor swap), and the webui (per-project tab filter).
    The containers themselves don't consult it — their runtime config is
    fully expressed via env + bind-mount sources."""
    return workspace / ".orchestrator" / "pi-isolated.json"


def load(workspace: Path) -> dict[str, dict]:
    p = snapshot_path(workspace)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save(workspace: Path, data: dict[str, dict]) -> None:
    """Atomic-rename write. Parent dir is bind-mounted into the supervisor,
    so the rename is visible to in-supervisor consumers immediately (the
    single-file-bind-mount rule does not apply — parent-dir mount, not file)."""
    p = snapshot_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# IP allocation + snapshot entry construction
# ---------------------------------------------------------------------------


def allocate_ip(entries: dict[str, dict], name: str) -> str:
    """Return the IP for ``name``: its existing one if already enabled
    (idempotent re-enable keeps the pin), else the lowest free address in
    the pool not used by another entry. Raises if the pool is exhausted."""
    existing = entries.get(name, {}).get("ip")
    if isinstance(existing, str) and existing:
        return existing
    taken = {e.get("ip") for e in entries.values() if isinstance(e, dict)}
    for octet in range(IP_POOL_LO, IP_POOL_HI + 1):
        ip = f"{IP_PREFIX}{octet}"
        if ip not in taken:
            return ip
    raise ValueError(
        f"PI-isolated IP pool exhausted ({IP_PREFIX}{IP_POOL_LO}-"
        f"{IP_PREFIX}{IP_POOL_HI}); disable an unused isolated agent first"
    )


def build_entry(name: str, type_entry: dict[str, Any], ip: str) -> dict[str, Any]:
    """Snapshot the resolved host-registry type config into the per-project
    entry, plus the assigned IP + container name. Snapshotting (rather than
    re-reading the host registry at restart time) keeps the project's wiring
    stable even if the host registry is later edited — same posture as
    mcp-allow.json snapshotting from mcp-registry.json."""
    import pi_isolated_registry as reg
    return {
        "ip": ip,
        "container": container_name(name),
        "repo": type_entry.get("repo"),
        "ref": type_entry.get("ref"),
        "setup": type_entry.get("setup"),
        "root": type_entry["root"],
        "mount": reg.mount_for(type_entry),
    }
