"""extension — host-side helpers for the per-project box surface.

After STAGE_BOX_EXT_UX the PI-role / BYO-isolated *enable* lineage is gone
(retired greenfield). What remains here is the shared substrate the box
surface (``kind="sandbox"`` boxes, owned by the in-supervisor ``rs-sandbox``
CLI) leans on: the box-image registry locator, the inner-container
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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import orch_files  # noqa: E402  (no-follow access to <workspace>/.orchestrator)

# The registry-delivered box images: the two blank-box images the in-supervisor
# rs-sandbox runs (rscore._BOX_IMAGES names them and their registry repos). A
# project's inner dockerd pulls rs-registry:5000/<repo>@<digest>; the project
# records the host image id it holds (project.json `box_images`,
# rscore._bring_box_image), which is what moves it to a new build.
# ``EXT_REGISTRY`` is the inner-pull locator and MUST match the inner daemon's
# insecure-registries entry (agent/Dockerfile.substrate-base).
EXT_REGISTRY = "rs-registry:5000"

# --- Sandbox-project boxes (kind="sandbox") -------------------------------
# Box flavor (STAGE_SANDBOX_PROJECT.md): blank rs-sandbox-box containers the PI
# spins up from the in-supervisor `rs-sandbox` CLI for running un-vetted code in
# isolation. They draw from the .14-.25 inner-bridge pool and reuse the iso-
# container/tab conventions (see container_name/workspace_subdir), so they need
# no new firewall rule — the whole .10-.25 PI range is already ACCEPTed by
# inner-firewall.sh. Egress is NOT gated per box; it is controlled project-wide
# at the router (a sandbox-dind project defaults to `--egress locked` →
# 80/443 + DNS to the configured resolvers only, no ICMP, RFC1918 blocked — usable for an LLM / pip while contained).
#
# kind value for boxes (owned by the in-supervisor rs-sandbox CLI). The host
# references this in the _recreate_supervisor restart loop to delegate restarts
# to rs-sandbox.
SANDBOX_KIND = "sandbox"

VALID_KINDS = ("sandbox",)


def container_name(name: str, kind: str | None = None) -> str:
    """Inner-dockerd container name for a box: ``rs-box-<name>`` — the same
    name the box tool derives (rs_sandbox.box_container) and the webui tab
    synthesis execs into. The
    ``kind`` arg is accepted (callers pass the entry's kind) but no longer
    branches — every surviving entry is a ``"sandbox"`` box."""
    return f"rs-box-{name}"


# ---------------------------------------------------------------------------
# Per-project state file
# ---------------------------------------------------------------------------


def extensions_path(workspace: Path) -> Path:
    """Sibling to role-mcps.json + mcp-allow.json. Read by the host (this
    module), ``_recreate_supervisor`` (restart loop), and the webui
    (per-project tab filter). The containers don't consult it."""
    return workspace / ".orchestrator" / "extensions.json"


def load(workspace: Path) -> dict[str, dict]:
    """The project's box records; {} when the file is absent, unparseable, or
    not usable (a link at the file or at the settings folder, or not a
    regular file — the folder is container-writable, so nothing is read
    through a link)."""
    try:
        data = json.loads(orch_files.read_text(workspace, "extensions.json"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(workspace: Path, data: dict[str, dict]) -> None:
    """Atomic-rename write. Parent dir is bind-mounted into the supervisor,
    so the rename is visible to in-supervisor consumers immediately
    (parent-dir mount, not file — the single-file-bind-mount rule doesn't
    apply). The settings folder is opened without following a link and the
    temp file is freshly named (orch_files); a settings folder that is a link
    or not a folder refuses the change the way a die() does (an error line,
    exit 1)."""
    try:
        orch_files.write_text(workspace, "extensions.json",
                              json.dumps(data, indent=2, sort_keys=True) + "\n",
                              create=True)
    except orch_files.Unusable as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
