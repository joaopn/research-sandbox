"""extension — host-side helpers for the per-project box surface.

After STAGE_BOX_EXT_UX the PI-role / BYO-isolated *enable* lineage is gone
(retired greenfield). What remains here is the shared substrate the box
surface (``kind="sandbox"`` boxes, owned by the in-supervisor ``rs-sandbox``
CLI) leans on: the generic box-image registry refs, the inner-container
name / workspace-subdir / label conventions, and the per-project
``extensions.json`` state file (boxes write their entries here).

Per-project enabled state lives in
``<workspace>/.orchestrator/extensions.json`` — one entry per box, keyed by
name, carrying ``kind="sandbox"`` + resolved runtime fields.

Stdlib only. Imported by research.py + rscore.py (box lifecycle) and by
``_recreate_supervisor``'s restart loop.
"""

from __future__ import annotations

import json
from pathlib import Path

# GENERIC registry-delivered box images (STAGE_FEATURE_STAGING lane-3). Generic,
# non-role images: the two blank-box images the in-supervisor rs-sandbox runs.
# Mapping: host image base -> (registry repo, versions.env pin key); the snapshot
# ref (rs-registry:5000/<repo>:<pin>) is pulled by a project's inner dockerd,
# frozen into project.json (box_image_pins) at create. ``EXT_REGISTRY`` is the
# inner-pull locator and MUST match the inner daemon's insecure-registries entry
# (agent/Dockerfile.substrate-base). Lockstep: the host build tags
# rs-<base>:<pin> (rscore._build_images).
EXT_REGISTRY = "rs-registry:5000"
GENERIC_REGISTRY_IMAGES: dict[str, tuple[str, str]] = {
    "rs-sandbox-box":         ("sandbox-box", "SANDBOX_BOX_VERSION"),
    "rs-sandbox-box-browser": ("sandbox-box-browser", "SANDBOX_BOX_BROWSER_VERSION"),
}

# --- Sandbox-project boxes (kind="sandbox") -------------------------------
# Box flavor (STAGE_SANDBOX_PROJECT.md): blank rs-sandbox-box containers the PI
# spins up from the in-supervisor `rs-sandbox` CLI for running un-vetted code in
# isolation. They draw from the .14-.25 inner-bridge pool and reuse the iso-
# container/tab conventions (see container_name/workspace_subdir), so they need
# no new firewall rule — the whole .10-.25 PI range is already ACCEPTed by
# inner-firewall.sh. Egress is NOT gated per box; it is controlled project-wide
# at the router (a sandbox-dind project defaults to `--egress locked` →
# 80/443/53/ICMP only, RFC1918 blocked — usable for an LLM / pip while contained).
#
# kind value for boxes (owned by the in-supervisor rs-sandbox CLI). The host
# references this in the _recreate_supervisor restart loop to delegate restarts
# to rs-sandbox.
SANDBOX_KIND = "sandbox"

VALID_KINDS = ("sandbox",)


def generic_registry_ref(host_base: str, pin: str) -> str:
    """The local-registry pull ref for a GENERIC image at an EXPLICIT pin —
    ``rs-registry:5000/<repo>:<pin>``. The recreate/restart path passes the FROZEN
    pin (from project.json / extensions.json) here, so a versions.env bump never
    silently re-pulls a different version on restart."""
    repo, _ = GENERIC_REGISTRY_IMAGES[host_base]
    return f"{EXT_REGISTRY}/{repo}:{pin}"


def generic_image_ref(host_base: str, pins: dict[str, str] | None = None) -> str:
    """The MINT-path ref for a GENERIC image: resolve the pin from versions.env
    (``pins`` = rscore.load_versions()) and format the registry ref. Mirrors
    ``image_ref``'s pin-or-raise. Used at create/enable (the snapshot site) and as
    the spawn fallback when a pre-migration entry carries no frozen ``image``."""
    repo, vkey = GENERIC_REGISTRY_IMAGES[host_base]
    pin = (pins or {}).get(vkey)
    if not pin:
        raise ValueError(
            f"missing version pin {vkey} for {host_base!r}; add it to versions.env")
    return f"{EXT_REGISTRY}/{repo}:{pin}"


def container_name(name: str, kind: str | None = None) -> str:
    """Inner-dockerd container name for a box: ``rs-pi-iso-<name>``. Boxes ride
    the iso- family so the webui tab synthesis + iso- label selection work. The
    ``kind`` arg is accepted (callers pass the entry's kind) but no longer
    branches — every surviving entry is a ``"sandbox"`` box."""
    return f"rs-pi-iso-{name}"


# ---------------------------------------------------------------------------
# Per-project state file
# ---------------------------------------------------------------------------


def extensions_path(workspace: Path) -> Path:
    """Sibling to role-mcps.json + mcp-allow.json. Read by the host (this
    module), ``_recreate_supervisor`` (restart loop), and the webui
    (per-project tab filter). The containers don't consult it."""
    return workspace / ".orchestrator" / "extensions.json"


def load(workspace: Path) -> dict[str, dict]:
    p = extensions_path(workspace)
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
    p = extensions_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)
