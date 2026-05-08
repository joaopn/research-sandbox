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
}


def get(service_id: str) -> dict | None:
    return SERVICES.get(service_id)
