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
    # byobu. The service id was once `xterm` (named after the renderer)
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
            "byobu new-session -s main -c /workspace -- bash"
        ),
    },
    # pi-wrangler — interactive DB-extraction tab. Command runs `claude`
    # in byobu — claude auto-discovers `/workspace/CLAUDE.md` (symlinked
    # to role.md by the pi entrypoint) and `/workspace/.mcp.json`
    # (rendered from the project's worker-facing wrangler upstream set).
    #
    # `byobu new-session -c /workspace` pins the session cwd so claude's
    # auto-discovery finds both files. Without -c, byobu inherits the
    # exec's cwd which is the docker default — claude would look for
    # CLAUDE.md / .mcp.json in /home/worker and miss them.
    #
    # always_on=False — visibility is per-project, gated on whether the
    # project's pi-roles.json lists `pi-wrangler`. The filter is in
    # `project_services_handler` in server.py; it reads the per-project
    # `.orchestrator/pi-roles.json` directly off the `/projects:ro`
    # bind-mount that already serves the rail's status sub-line. No
    # SSH, no cache: lifecycle changes (`research project pi enable/
    # disable`) reflect on the next page load.
    #
    # New `pi-<role>` entries follow the same shape — always_on=False
    # is the correct default; the filter generalizes across every
    # `kind=ssh` non-always-on service whose id starts with `pi-`.
    #
    # Label is "Wrangler" without the "PI" prefix — from the PI's
    # perspective everything here IS PI mode; the prefix would be noise.
    # The underlying container is still `rs-pi-wrangler` and the role
    # key in pi-roles.json is still `pi-wrangler`; only the user-facing
    # label drops the prefix.
    "pi-wrangler": {
        "label": "Wrangler",
        "kind": "ssh",
        "always_on": False,
        "renderer": "xterm.js",
        "default_port": 22,
        "command": (
            "docker exec -it rs-pi-wrangler bash -lc "
            "'byobu attach -t pi 2>/dev/null || "
            "byobu new-session -s pi -c /workspace -- claude'"
        ),
    },
    # pi-echo — substrate test fixture — is deliberately omitted from
    # the webui registry. It still ships as an image + lifecycle entry
    # (`research project pi enable <p> pi-echo`), still functions for
    # substrate verification, but doesn't clutter the production tab
    # strip. For substrate debugging from the Supervisor tab, use
    # `rs-pi echo` (the supervisor-baked CLI).
}


def get(service_id: str) -> dict | None:
    return SERVICES.get(service_id)
