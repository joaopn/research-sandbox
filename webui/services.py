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
SERVICES = {
    "xterm": {
        "label": "xterm",
        "kind": "ssh",
        "always_on": True,
        "renderer": "xterm.js",
        "default_port": 22,
        "command": (
            "byobu attach -t main 2>/dev/null || "
            "byobu new-session -s main -c /workspace -- bash"
        ),
    },
    "code-server": {
        "label": "Editor",
        "kind": "http",
        "always_on": False,
        "renderer": "iframe",
        "default_port": 8443,
        "upstream_path": "/",
    },
    # pi-echo — STAGE_BACKEND_PI P.0 substrate test fixture. always_on=True
    # because per-project visibility filtering for kind=ssh services would
    # require a probe path the webui doesn't have (no docker socket; the
    # only port to probe is the supervisor's SSH 22, which is universal).
    # Tab shows on every project; clicking on a project that hasn't run
    # `research project pi enable <p> pi-echo` errors at the `docker exec`
    # step with a clear message. A real per-project filter is OQ-5 work
    # (would need a supervisor-side endpoint the webui can query).
    #
    # Command opens `bash -l` not `claude` — pi-echo is the substrate
    # fixture, not a production role. Real PI roles (P.1+) register their
    # own entries with `byobu new-session -s pi -- claude`.
    "pi-echo": {
        "label": "PI Echo",
        "kind": "ssh",
        "always_on": True,
        "renderer": "xterm.js",
        "default_port": 22,
        "command": (
            "docker exec -it rs-pi-echo bash -lc "
            "'byobu attach -t pi 2>/dev/null || "
            "byobu new-session -s pi -- bash -l'"
        ),
    },
    # pi-wrangler — interactive DB-extraction tab (P.3). Command runs
    # `claude` in byobu (NOT `bash -l` like pi-echo) — pi-wrangler is a
    # real role and the PI expects framing on tab open. claude
    # auto-discovers `/workspace/CLAUDE.md` (symlinked to role.md by the
    # pi entrypoint) and `/workspace/.mcp.json` (rendered from the
    # project's worker-facing wrangler upstream set).
    #
    # `byobu new-session -c /workspace` pins the session cwd so claude's
    # auto-discovery finds both files. Without -c, byobu inherits the
    # exec's cwd which is the docker default — claude would look for
    # CLAUDE.md / .mcp.json in /home/worker and miss them.
    #
    # always_on=True: same OQ-5 deferral as pi-echo. Tab shows on every
    # project; clicking on a project that hasn't enabled pi-wrangler
    # fails at the docker exec step with a clear `No such container:
    # rs-pi-wrangler` message.
    "pi-wrangler": {
        "label": "PI Wrangler",
        "kind": "ssh",
        "always_on": True,
        "renderer": "xterm.js",
        "default_port": 22,
        "command": (
            "docker exec -it rs-pi-wrangler bash -lc "
            "'byobu attach -t pi 2>/dev/null || "
            "byobu new-session -s pi -c /workspace -- claude'"
        ),
    },
}


def get(service_id: str) -> dict | None:
    return SERVICES.get(service_id)
