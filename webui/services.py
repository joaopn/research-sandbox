"""Service registry — kinds the webui knows how to render.

W1 ships only `xterm`. Adding a new kind is two steps: an entry here, and a
matching server-side dispatcher in server.py (`kind=ssh` => ws_handler;
`kind=http` arrives in W2). Per-project enablement layers on top of this
registry via supervisor docker labels (`research.service.<id>=...`).
"""

# Each entry's `command` is what the SSH-kind service runs after auth — the
# byobu invocation lands the user in /workspace, the supervisor's project root.
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
}


def get(service_id: str) -> dict | None:
    return SERVICES.get(service_id)
