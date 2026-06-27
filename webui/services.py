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
# via byobu) follows. Wrangler (interactive DB-extraction tab) trails.
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
    # management — the default tab for sandbox-dind projects
    # (STAGE_SANDBOX_PROJECT.md), REPLACING the Supervisor + Editor tabs.
    # Authority-without-agency: this surface can create/discard every box and
    # read their artifacts, so it deliberately runs no agent — a plain login
    # shell, never `claude`. It prints the `rs-sandbox` cheatsheet on a fresh
    # byobu session (bare `rs-sandbox` → usage), then drops to bash. The
    # flavor gate is in `project_services_handler` (server.py): management
    # shows iff project.json type == "sandbox-dind", and supervisor/code-server are
    # omitted there. always_on so it surfaces without a port probe.
    "management": {
        "label": "Management",
        "kind": "ssh",
        "always_on": True,
        "renderer": "xterm.js",
        "default_port": 22,
        "command": (
            "byobu attach -t main 2>/dev/null || "
            "byobu new-session -s main -c /workspace -- "
            "bash -lc 'cat /workspace/.orchestrator/greeting 2>/dev/null; exec bash -l'"
        ),
    },
    # pi-wrangler — interactive DB-extraction tab. The tab does NOT auto-start
    # claude (STAGE_SPAWN_GREETING): new-session runs the greet helper, which
    # cats the per-role greeting (the MOTD survives because claude's TUI would
    # clear it) then drops into a login shell. The PI types `claude` to start the
    # session — claude then auto-discovers `/workspace/CLAUDE.md` (symlinked to
    # role.md by the pi entrypoint) and `/workspace/.mcp.json` (rendered from the
    # project's worker-facing wrangler upstream set).
    #
    # `byobu new-session -c /workspace` pins the session cwd so the PI's claude
    # auto-discovery finds both files. Without -c, byobu inherits the exec's cwd
    # which is the docker default — claude would look for CLAUDE.md / .mcp.json
    # in /home/worker and miss them.
    #
    # The greet helper's path arg is per-role (`/opt/pi-templates/wrangler/
    # greeting`); the helper takes it as $1 so there's no single-quote nesting
    # inside this tab's `docker exec … bash -lc '…'` wrapper.
    #
    # always_on=False — visibility is per-project, gated on whether the
    # project enables the baked extension `wrangler`. The filter is in
    # `project_services_handler` in server.py; it reads the per-project
    # `.orchestrator/extensions.json` directly off the `/projects:ro`
    # bind-mount that already serves the rail's status sub-line, and maps
    # this `pi-wrangler` tab id to the baked extension key `wrangler`. No
    # SSH, no cache: lifecycle changes (`research project extension enable/
    # disable`) reflect on the next page load.
    #
    # New baked `pi-<short>` tabs follow the same shape — always_on=False
    # is the correct default; the filter generalizes across every
    # `kind=ssh` non-always-on service whose id starts with `pi-` (it
    # strips the prefix and looks up the baked extension of that name).
    #
    # Label is "Wrangler" without the "PI" prefix — from the PI's
    # perspective everything here IS PI mode; the prefix would be noise.
    # The underlying container is still `rs-pi-wrangler` and the baked
    # extension key in extensions.json is `wrangler`; only the user-facing
    # tab id keeps the `pi-` prefix.
    "pi-wrangler": {
        "label": "Wrangler",
        "kind": "ssh",
        "always_on": False,
        "renderer": "xterm.js",
        "default_port": 22,
        "command": (
            "docker exec -it rs-pi-wrangler bash -lc "
            "'byobu attach -t pi 2>/dev/null || "
            "byobu new-session -s pi -c /workspace -- "
            "/opt/pi-templates/greet-and-shell.sh /opt/pi-templates/wrangler/greeting'"
        ),
    },
    # pi-websearcher — interactive browser-driven web-research tab. Same
    # shape as pi-wrangler: byobu attach-or-new, the greet helper as the
    # inner command (no auto-claude — STAGE_SPAWN_GREETING), `-c /workspace`
    # to land in the role's workspace so the PI's claude auto-discovers
    # CLAUDE.md (symlinked from role.md) and .mcp.json (rendered by
    # entrypoint.pi.sh from the project's role-mcps.json[websearcher] entry
    # plus the image-baked Playwright extras).
    #
    # The docker-exec wrapper MUST stay `bash -lc` (login shell), matching
    # pi-wrangler. claude installs to ~/.local/bin and is added to PATH only
    # via ~/.bashrc (see Dockerfile.analysis-base); a non-login,
    # non-interactive `bash -c` never sources it, so the tmux server it
    # starts — and the shell where the PI runs `claude` — would inherit a PATH
    # without ~/.local/bin and `claude` dies with `command not found` (RC 127).
    # The greet helper's own `exec bash -l` re-establishes the login PATH too,
    # so the guarantee is doubly held, but keep the outer -lc (the minimal,
    # comment-consistent form). See BUG_BUCKET B6.
    #
    # Label keeps the "PI Websearcher" prefix — pi-wrangler dropped to
    # "Wrangler" during STAGE_2.5 polish, but the P.1 plan re-adds the
    # prefix here. (No technical reason to enforce one convention over
    # the other; the test gates this label specifically.)
    "pi-websearcher": {
        "label": "PI Websearcher",
        "kind": "ssh",
        "always_on": False,
        "renderer": "xterm.js",
        "default_port": 22,
        "command": (
            "docker exec -it rs-pi-websearcher bash -lc "
            "'byobu attach -t pi 2>/dev/null || "
            "byobu new-session -s pi -c /workspace -- "
            "/opt/pi-templates/greet-and-shell.sh /opt/pi-templates/websearcher/greeting'"
        ),
    },
    # pi-echo — substrate test fixture — is deliberately omitted from
    # the webui registry. It still ships as an image + lifecycle entry
    # (`research project pi enable <p> pi-echo`), still functions for
    # substrate verification, but doesn't clutter the production tab
    # strip. For substrate debugging from the Supervisor tab, use
    # `rs-pi echo` (the supervisor-baked CLI).
}


# PI-isolated agents (STAGE_PI_ISOLATED) are per-project and arbitrarily
# named, so they can't be static SERVICES entries. Their tab id is
# `pi-iso-<name>` and the tab is synthesized on demand: project_services_
# handler adds one per BYO (kind="byo") entry in the project's extensions.json,
# and `resolve()` reconstructs the command server-side for the ssh handler.
import re as _re

PI_ISOLATED_ID_PREFIX = "pi-iso-"
# Same grammar as the host registry's NAME_RE. Validated before the name is
# interpolated into the docker-exec command string — a non-matching id is
# rejected (404) rather than executed, closing shell-injection via the URL.
_PI_ISOLATED_NAME_RE = _re.compile(r"^[a-z][a-z0-9-]*$")


def pi_isolated_service(name: str) -> dict | None:
    """Synthesize the tab/service spec for PI-isolated agent ``name``, or
    None if the name fails validation.

    Unlike the baked PI roles (pi-wrangler / pi-websearcher), the inner
    command is a **login shell**, NOT ``claude`` — pi-isolated is a plain
    per-project extension (cloned repo + folder mount, no MCPs). Starting
    claude, authenticating, pulling skills from the marketplace, etc. are
    all the PI's to do, in whatever order — auto-launching claude would
    pre-empt that. This mirrors pi-echo's `bash -l` tab. ``-c /workspace``
    lands the shell where the clone + external folder live.

    The shell is reached via the shared greet helper (STAGE_SPAWN_GREETING),
    which cats a greeting then `exec bash -l` (full PATH, claude included).
    The greeting path is the future BYO convention ``/workspace/.rs-greeting``
    (a cloned skill repo MAY ship one) — a clean no-op until then. Using the
    helper also keeps the path out of single-quotes, so there's no nesting in
    this tab's ``docker exec … bash -c '…'`` wrapper."""
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
