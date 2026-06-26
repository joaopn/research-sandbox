"""sandbox — unified host-side helpers for per-project PI-driven containers.

Merges the former ``cli/pi.py`` (baked PI roles) and ``cli/pi_isolated.py``
(BYO-repo isolated agents) into one "sandbox" surface. A *sandbox* is any
PI-driven, webui-tab-able container in the supervisor's inner dockerd —
the management counterpart to ``cli/role_mcp.py`` (worker-side services).
Two kinds:

  - ``baked``: a per-role image (``rs-pi-<name>``) baking ``role.md``, with
    optional MCP mirroring of the worker service of the same name. Fixed
    catalog (echo / wrangler / websearcher), pinned inner-bridge IPs.
  - ``byo``:   the generic ``rs-pi-isolated`` image cloning an operator-
    registered skill repo. Catalog is the host BYO type registry
    (``cli/pi_isolated_registry.py`` → ``~/.research-sandbox/
    sandbox-registry.json``), IPs drawn from a dynamic pool.

Per-project enabled state lives in
``<workspace>/.orchestrator/sandbox.json`` — one entry per enabled sandbox,
keyed by name, carrying ``kind`` + resolved runtime fields. It replaces the
old ``pi-roles.json`` + ``pi-isolated.json`` (greenfield: no migration).

Stdlib only. Imported by research.py for the ``sandbox`` / ``project
sandbox`` command groups and by ``_recreate_supervisor``'s restart loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# The BYO host type registry is reused verbatim as a sub-component (it owns
# the reusable repo+root type definitions, validation, and locking). Renamed
# only at the file level (sandbox-registry.json); see pi_isolated_registry.
import pi_isolated_registry as byo_registry

# ---------------------------------------------------------------------------
# Baked catalog (was cli/pi.py) — pinned inner-bridge IPs + per-role images
# ---------------------------------------------------------------------------
# Range .10-.25 on rs-inner is reserved for PI/sandbox containers (see the
# load-bearing-IPs rule in .claude/CLAUDE.md). Baked roles claim .10-.13;
# the BYO pool is .14-.25. Names here are the *short* role names (no ``pi-``
# prefix) — they double as RS_PI_ROLE and the /opt/pi-templates/<name>/ dir.
BAKED_IPS: dict[str, str] = {
    "wrangler":    "192.168.99.10",
    "librarian":   "192.168.99.11",
    "websearcher": "192.168.99.12",
    "echo":        "192.168.99.13",
}

# LEGACY save/load baked roles: per-role images built by _build_images (FROM
# rs-pi-base, baking /opt/pi-templates/<name>/role.md), staged into a project's
# inner dockerd via `docker save | docker load`. librarian is IP-reserved but not
# yet buildable (no image). websearcher MIGRATED to the extension lane (below) —
# it is no longer here, so its Dockerfile.pi-websearcher is no longer built by the
# loop (the old leaf lingers unused until a later cleanup).
BAKED_IMAGES: dict[str, str] = {
    "echo":         "rs-pi-echo:latest",
    "wrangler":     "rs-pi-wrangler:latest",
}

# MIGRATED extension-lane roles (STAGE_FEATURE_STAGING C1). Same `kind="baked"`
# entry shape + pinned IP as a legacy baked role — the ONLY difference is image
# DELIVERY: instead of a per-role rs-pi-<name> leaf staged by save/load, an
# extension builds a clean rs-ext-<name> image (FROM rs-ext-base, NO
# rs-analysis-base lineage), is pushed to the local registry, and is PULLED by a
# project's inner dockerd. Mapping: name -> (registry repo, versions.env pin key).
# ``EXT_REGISTRY`` is the inner-pull locator and MUST match the inner daemon's
# insecure-registries entry (agent/Dockerfile.substrate-base). The snapshot ref
# (rs-registry:5000/<repo>:<pin>) is stamped into sandbox.json at enable
# (image_ref) and read verbatim at spawn/recreate.
EXT_REGISTRY = "rs-registry:5000"
EXT_REGISTRY_REFS: dict[str, tuple[str, str]] = {
    "websearcher": ("ext-websearcher", "EXT_WEBSEARCHER_VERSION"),
}

# BYO inner-bridge IP pool (was cli/pi_isolated.py). Disjoint from the baked
# .10-.13. The whole .10-.25 range is ACCEPTed by inner-firewall.sh, so a new
# BYO agent needs no firewall edit.
BYO_IP_LO = 14
BYO_IP_HI = 25  # inclusive
IP_PREFIX = "192.168.99."

# --- Sandbox-project boxes (kind="sandbox") -------------------------------
# Agent-less "sandbox project" flavor (STAGE_SANDBOX_PROJECT.md): blank
# rs-sandbox-box containers the PI spins up from the in-supervisor `rs-sandbox`
# CLI for running un-vetted code in isolation. They draw from the SAME .14-.25
# pool as BYO and reuse the iso- container/tab conventions (see
# container_name/workspace_subdir), so they need no new firewall rule — the
# whole PI range is already ACCEPTed. Egress is NOT gated per box; it is
# controlled project-wide at the router (a sandbox project defaults to
# `--egress locked` → 80/443/53/ICMP only, RFC1918 blocked — usable for an LLM
# / pip while staying contained).
#
# kind value for sandbox-flavor boxes (owned by the in-supervisor rs-sandbox
# CLI, not the host baked/byo enable path). The host references this in the
# _recreate_supervisor restart loop to delegate restarts to rs-sandbox.
SANDBOX_KIND = "sandbox"

VALID_KINDS = ("baked", "byo", "sandbox")


def baked_names() -> list[str]:
    """Enableable baked-or-ext roles = roles with a pinned IP AND a buildable
    image source — either a LEGACY save/load image (BAKED_IMAGES) or a MIGRATED
    registry-pulled extension (EXT_REGISTRY_REFS). Both present as `kind="baked"`;
    the only divergence is image delivery (see image_ref / is_ext)."""
    sources = set(BAKED_IMAGES) | set(EXT_REGISTRY_REFS)
    return sorted(set(BAKED_IPS) & sources)


def is_baked(name: str) -> bool:
    return name in set(baked_names())


def is_ext(name: str) -> bool:
    """A MIGRATED extension-lane role (registry-pulled) vs a legacy save/load
    baked role. The fork is image delivery, not the entry's ``kind`` (both are
    ``"baked"``)."""
    return name in EXT_REGISTRY_REFS


def image_ref(name: str, pins: dict[str, str] | None = None) -> str:
    """Canonical image reference for a baked-or-ext role.

    Legacy baked -> the host image tag (staged via save/load).
    Ext          -> the local-registry PULL ref with the snapshot pin
                    (``rs-registry:5000/<repo>:<pin>``), pulled by the inner
                    dockerd.

    ``pins`` is the versions.env dict (``rscore.load_versions()``), required for
    ext so the pin can be snapshotted into the sandbox.json entry at enable.
    Raises ``ValueError`` if an ext role's pin is absent."""
    if name in EXT_REGISTRY_REFS:
        repo, key = EXT_REGISTRY_REFS[name]
        pin = (pins or {}).get(key)
        if not pin:
            raise ValueError(
                f"missing version pin {key} for extension {name!r}; add it to "
                f"versions.env"
            )
        return f"{EXT_REGISTRY}/{repo}:{pin}"
    return BAKED_IMAGES[name]


def container_name(name: str, kind: str) -> str:
    """``rs-pi-<name>`` for baked (preserves the firewall/IP/webui-tab
    conventions + the ``rs-pi-`` family prefix the ``rs-pi sync-creds`` label
    selection relies on); ``rs-pi-iso-<name>`` for BYO *and* sandbox-flavor
    boxes (kind="sandbox") — both ride the iso- family so the webui tab
    synthesis + iso- label selection work without a new branch."""
    return f"rs-pi-{name}" if kind == "baked" else f"rs-pi-iso-{name}"


def workspace_subdir(name: str, kind: str) -> str:
    """Host-side source path (under the project volume's /workspace) for the
    container's RW /workspace mount. Kept distinct per kind so the two
    entrypoints' boot expectations are unchanged."""
    return f"pi/{name}" if kind == "baked" else f"pi-isolated/{name}"


def pi_role_label(name: str, kind: str) -> str:
    """Value of the ``research.pi_role`` container label — the selector the
    manual ``rs-pi sync-creds`` bridge + the inner firewall key on. Baked:
    the short name; BYO: ``iso-<name>`` (preserves prior behavior)."""
    return name if kind == "baked" else f"iso-{name}"


def mirror_of(name: str) -> str | None:
    """For a baked sandbox, the worker role-MCP whose upstream MCP set it
    mirrors (same short name) — or None. ``echo`` has no worker twin
    (the worker service key is ``echo-mcp``, not ``echo``), so it bypasses
    the mirror gate, exactly as pi-echo did."""
    import role_mcp
    return name if name in role_mcp.ROLE_IMAGES else None


# ---------------------------------------------------------------------------
# Per-project state file (replaces pi-roles.json + pi-isolated.json)
# ---------------------------------------------------------------------------


def sandbox_path(workspace: Path) -> Path:
    """Sibling to role-mcps.json + mcp-allow.json. Read by the host (this
    module), ``_recreate_supervisor`` (restart loop), and the webui
    (per-project tab filter). The containers don't consult it."""
    return workspace / ".orchestrator" / "sandbox.json"


def load(workspace: Path) -> dict[str, dict]:
    p = sandbox_path(workspace)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save(workspace: Path, data: dict[str, dict]) -> None:
    """Atomic-rename write. Parent dir is bind-mounted into the supervisor,
    so the rename is visible to in-supervisor consumers immediately
    (parent-dir mount, not file — the single-file-bind-mount rule doesn't
    apply)."""
    p = sandbox_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# IP allocation + entry construction
# ---------------------------------------------------------------------------


def allocate_byo_ip(entries: dict[str, dict], name: str) -> str:
    """Lowest free address in the BYO pool not used by another entry (any
    kind), or the existing pin on idempotent re-enable. Raises if exhausted."""
    existing = entries.get(name, {}).get("ip")
    if isinstance(existing, str) and existing:
        return existing
    taken = {e.get("ip") for e in entries.values() if isinstance(e, dict)}
    for octet in range(BYO_IP_LO, BYO_IP_HI + 1):
        ip = f"{IP_PREFIX}{octet}"
        if ip not in taken:
            return ip
    raise ValueError(
        f"sandbox BYO IP pool exhausted ({IP_PREFIX}{BYO_IP_LO}-"
        f"{IP_PREFIX}{BYO_IP_HI}); disable an unused BYO sandbox first"
    )


def validate_baked(name: str) -> None:
    if not is_baked(name):
        raise ValueError(
            f"unknown baked sandbox {name!r}. Known baked sandboxes: "
            f"{', '.join(baked_names()) or '(none)'}"
        )


def build_baked_entry(name: str, pins: dict[str, str] | None = None) -> dict[str, Any]:
    """Build the sandbox.json entry for a baked-or-ext role. ``image`` is
    SNAPSHOTTED here via image_ref — for ext roles that freezes the current
    versions.env pin into the entry, so spawn/recreate reuse it verbatim and a
    later pin bump only affects a fresh enable. ``pins`` (load_versions()) is
    required for ext roles."""
    validate_baked(name)
    return {
        "kind": "baked",
        "ip": BAKED_IPS[name],
        "image": image_ref(name, pins),
        "container": container_name(name, "baked"),
        "mirror_of": mirror_of(name),
    }


def build_byo_entry(name: str, type_entry: dict[str, Any], ip: str) -> dict[str, Any]:
    """Snapshot the resolved host-registry type config into the per-project
    entry (same snapshot-don't-re-read posture as mcp-allow.json)."""
    return {
        "kind": "byo",
        "ip": ip,
        "container": container_name(name, "byo"),
        "repo": type_entry.get("repo"),
        "ref": type_entry.get("ref"),
        "setup": type_entry.get("setup"),
        "root": type_entry["root"],
        "mount": byo_registry.mount_for(type_entry),
    }


# ---------------------------------------------------------------------------
# Catalog (visibility) — every sandbox TYPE available to enable
# ---------------------------------------------------------------------------


def catalog(pins: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """All available sandbox *types*: baked + ext (constants) + BYO (host
    registry). Drives `research sandbox list`. Tolerates a malformed BYO
    registry — baked types still list. ``pins`` (load_versions()) lets ext rows
    show the real registry ref; without it they fall back to an unpinned display
    ref so the listing never crashes on a missing pin."""
    out: list[dict[str, Any]] = []
    for n in baked_names():
        try:
            img = image_ref(n, pins)
        except ValueError:
            img = f"{EXT_REGISTRY}/{EXT_REGISTRY_REFS[n][0]}:<unpinned>"
        out.append({
            "name": n,
            "kind": "baked",
            "image": img,
            "mirror_of": mirror_of(n),
            "repo": None,
            "root": None,
        })
    try:
        reg = byo_registry.load(expand=False)
    except byo_registry.RegistryError:
        reg = {"types": {}}
    for n, e in sorted(reg.get("types", {}).items()):
        out.append({
            "name": n,
            "kind": "byo",
            "image": "rs-pi-isolated:latest",
            "mirror_of": None,
            "repo": e.get("repo"),
            "root": e.get("root"),
        })
    return out


def known_type_names() -> set[str]:
    """All names that resolve to a sandbox type (baked + BYO). Used by the
    --enable token splitter and disjoint-name checks."""
    names = set(baked_names())
    try:
        names |= set(byo_registry.load(expand=False).get("types", {}))
    except byo_registry.RegistryError:
        pass
    return names
