"""Service registry — kinds the webui knows how to render.

Two kinds today: `ssh` (WS-wrapped, browser xterm.js terminal) and `http`
(reverse-proxied with a cookie-issued session). Adding a same-kind service
is a registry entry plus a per-supervisor entrypoint conditional. New
kinds need a server-side dispatcher in server.py and a renderer branch
in static/app.js. Per-project enablement layers on top of this registry
via supervisor docker labels (`research.service.<id>=...`), read by
project_services_handler.
"""

# Each entry's `command` is what the SSH-kind service runs after auth — the
# byobu invocation lands the user in /workspace, the supervisor's project root.
# `default_port` is the in-container port the webui reaches via container DNS
# (rs-project-<proj>:<port>); for code-server this is the lazy-start stub's
# listen port, NOT the underlying code-server's. The stub takes the request
# and either spawns / reuses the code-server child behind 127.0.0.1.
# Tab order in the SPA = SERVICES dict insertion order. Editor (code-
# server) is the primary work surface (notebooks, file editing, code-
# reading); it leads. Supervisor (the in-container Claude Code session
# via byobu) follows. Boxes are synthesized per-project (pi-iso-<name>),
# not static entries here.
SERVICES = {
    "code-server": {
        "label": "Editor",
        "kind": "http",
        "always_on": False,
        "renderer": "iframe",
        "default_port": 8443,
        "upstream_path": "/",
    },
    # The supervisor's interactive Claude session, attached via SSH +
    # byobu. The new-session lands in bash (NON-login, as before) after
    # cat-ing the workflow greeting if one was staged (STAGE_SPAWN_GREETING):
    # the research workflow ships no greeting yet, so this is a clean no-op
    # today — the mechanism is in place for a later manifest-only add.
    # The service id was once `xterm` (named after the renderer)
    # and is now `supervisor` (named after what it actually connects
    # to). Existing projects' `research.service.xterm` labels are
    # orphaned by the rename — harmless (label-reading iterates
    # KNOWN_SERVICES, so missing labels default to enabled) and cleared
    # naturally on the next `research project update`.
    "supervisor": {
        "label": "Supervisor",
        "kind": "ssh",
        "always_on": True,
        "renderer": "xterm.js",
        "default_port": 22,
        "command": (
            "byobu attach -t main 2>/dev/null || "
            "byobu new-session -s main -c /workspace -- "
            "bash -c 'cat /workspace/.orchestrator/greeting 2>/dev/null; exec bash'"
        ),
    },
    # (The agent-less `management` tab was retired in STAGE_SANDBOX_DIND_AGENT —
    # sandbox-dind now runs an agent and shows the Supervisor tab like research.)
    # Boxes (kind="sandbox") are NOT static entries — they're per-project and
    # arbitrarily named, synthesized on demand as `pi-iso-<name>` tabs by
    # project_services_handler (see pi_isolated_service below).
}


# Boxes (kind="sandbox") are per-project and arbitrarily named, so they can't be
# static SERVICES entries. Their tab id is `pi-iso-<name>` and the tab is
# synthesized on demand: project_services_handler adds one per box entry in the
# project's extensions.json, and `resolve()` reconstructs the command
# server-side for the ssh handler.
import re as _re

PI_ISOLATED_ID_PREFIX = "pi-iso-"
# Same grammar as the box NAME_RE. Validated before the name is interpolated
# into the docker-exec command string — a non-matching id is rejected (404)
# rather than executed, closing shell-injection via the URL.
_PI_ISOLATED_NAME_RE = _re.compile(r"^[a-z][a-z0-9-]*$")


def pi_isolated_service(name: str) -> dict | None:
    """Synthesize the tab/service spec for box ``name``, or None if the name
    fails validation.

    The inner command is a **login shell**, NOT ``claude`` — a box boots
    un-authed and starting claude, authenticating, etc. are all the PI's to do,
    in whatever order; auto-launching claude would pre-empt that. ``-c /workspace``
    lands the shell where the box's work lives.

    The shell is reached via the shared greet helper (STAGE_SPAWN_GREETING),
    which cats a greeting then `exec bash -l` (full PATH, claude included). The
    greeting path is the box convention ``/workspace/.rs-greeting`` (rs-sandbox
    writes the box's CLAUDE.md / greeting at create). Using the helper also keeps
    the path out of single-quotes, so there's no nesting in this tab's
    ``docker exec … bash -c '…'`` wrapper."""
    if not _PI_ISOLATED_NAME_RE.match(name):
        return None
    return {
        "label": name,
        "kind": "ssh",
        "always_on": False,
        "renderer": "xterm.js",
        "default_port": 22,
        "command": (
            f"docker exec -it rs-pi-iso-{name} bash -c "
            "'byobu attach -t pi 2>/dev/null || "
            "byobu new-session -s pi -c /workspace -- "
            "/opt/pi-templates/greet-and-shell.sh /workspace/.rs-greeting'"
        ),
    }


def resolve(service_id: str) -> dict | None:
    """Static registry lookup, falling back to a synthesized PI-isolated
    spec for `pi-iso-<name>` ids. Used by the ssh handler so it can run the
    command for a per-project agent that isn't in the static registry."""
    svc = SERVICES.get(service_id)
    if svc is not None:
        return svc
    if service_id.startswith(PI_ISOLATED_ID_PREFIX):
        return pi_isolated_service(service_id[len(PI_ISOLATED_ID_PREFIX):])
    return None


def get(service_id: str) -> dict | None:
    return SERVICES.get(service_id)
