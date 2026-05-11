#!/usr/bin/env bash
# entrypoint.role-mcp.sh — role-MCP container boot.
#
# Expected mounts (provided by the supervisor's `role-mcp enable` call):
#   /workspace                    — per-role RW state (jobs/, memories/, global.md, .calls/)
#   /etc/orchestrator             — RO snapshot of the supervisor's .orchestrator/
#                                   (we read mcp-allow.json + role-mcps.json from here;
#                                    parent-dir bind-mount so atomic-rename writes by
#                                    the host stay visible)
#
# Expected environment:
#   RS_ROLE_NAME   — e.g. echo-mcp, wrangler, librarian, websearcher
#   RS_ROLE_PORT   — listen port (default 8000); same constant in every
#                    role-MCP container's network namespace
#
# Startup order:
#   1. Restore home skel (volume hides image contents).
#   2. Stage creds from the supervisor's stash mount.
#   3. Render /etc/role-mcp/spawn-mcp.json from role-mcps.json ∩ mcp-allow.json
#      (the .mcp.json passed to every spawned `claude -p`).
#   4. Exec daemon.py.

set -euo pipefail

: "${RS_ROLE_NAME:?RS_ROLE_NAME must be set}"

# --- Restore home skel on first boot --------------------------------------
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/worker-skel/. ~/
fi

# --- Stage creds (per-call spawned claude -p needs OAuth) -----------------
# The supervisor copies its current ~/.claude/.credentials.json into the
# per-role workspace at /workspace/.creds/.credentials.json on each
# `role-mcp enable`. Stage that into the runtime user's home so claude -p
# picks it up. Failure is fatal — the daemon is useless without creds.
if [[ -f /workspace/.creds/.credentials.json ]]; then
    mkdir -p ~/.claude
    cp /workspace/.creds/.credentials.json ~/.claude/.credentials.json
    chmod 600 ~/.claude/.credentials.json
else
    echo "role-mcp[${RS_ROLE_NAME}]: missing /workspace/.creds/.credentials.json" >&2
    echo "role-mcp[${RS_ROLE_NAME}]: refusing to start — run \`research project role-mcp enable\` to stage creds" >&2
    exit 2
fi
# Optional: settings (theme, verbosity). Tolerant of absence.
if [[ -f /workspace/.creds/settings.json ]]; then
    cp /workspace/.creds/settings.json ~/.claude/settings.json
fi

# --- Render spawn-mcp.json -------------------------------------------------
# The spawned claude -p for each call needs an .mcp.json that exposes the
# role's allowed upstream MCPs through the supervisor's mcp-proxy. The
# upstream list lives in /etc/orchestrator/role-mcps.json under this role's
# entry; the URL/headers per upstream live in /etc/orchestrator/mcp-allow.json.
# We render the intersection.
#
# Empty result ({}) => spawned claude has no MCP wiring (echo's case).
python3 - <<PYEOF
import json
import os
from pathlib import Path

role = os.environ["RS_ROLE_NAME"]
orch = Path("/etc/orchestrator")
out_path = Path("/etc/role-mcp/spawn-mcp.json")
out_path.parent.mkdir(parents=True, exist_ok=True)

role_mcps_path = orch / "role-mcps.json"
allow_path = orch / "mcp-allow.json"

upstream_names: list[str] = []
if role_mcps_path.is_file():
    try:
        data = json.loads(role_mcps_path.read_text())
        entry = data.get(role) if isinstance(data, dict) else None
        if isinstance(entry, dict):
            up = entry.get("upstream_mcps") or []
            if isinstance(up, list):
                upstream_names = [n for n in up if isinstance(n, str)]
    except json.JSONDecodeError:
        pass

allow_by_name: dict[str, dict] = {}
if allow_path.is_file():
    try:
        rows = json.loads(allow_path.read_text())
        if isinstance(rows, list):
            for e in rows:
                if isinstance(e, dict) and isinstance(e.get("name"), str):
                    allow_by_name[e["name"]] = e
    except json.JSONDecodeError:
        pass

# Every spawned claude -p reaches its upstreams through mcp-proxy:8888,
# the same path workers use. The /<name>/ prefix is the proxy's routing
# key; the proxy strips it and forwards.
cfg: dict = {"mcpServers": {}}
for name in upstream_names:
    e = allow_by_name.get(name)
    if not e:
        # Validation should prevent this at enable time; tolerate at boot
        # by skipping the entry so the daemon still comes up.
        continue
    path = e.get("path") or "/mcp"
    url = f"http://mcp-proxy:8888/{name}{path}"
    server: dict = {"type": "http", "url": url}
    headers = e.get("headers")
    if isinstance(headers, dict) and headers:
        server["headers"] = headers
    cfg["mcpServers"][name] = server

# Empty mcpServers => write "{}" so spawn.sh's empty-config skip kicks in.
if not cfg["mcpServers"]:
    out_path.write_text("{}\n")
else:
    out_path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
PYEOF

echo "role-mcp[${RS_ROLE_NAME}]: spawn-mcp.json written"

# --- Exec daemon ------------------------------------------------------------
export RS_ROLE_WORKSPACE="${RS_ROLE_WORKSPACE:-/workspace}"
export RS_ROLE_MCP_PORT="${RS_ROLE_MCP_PORT:-8000}"
export RS_ROLE_TEMPLATE_DIR="${RS_ROLE_TEMPLATE_DIR:-/opt/role-mcp/role}"
export RS_ROLE_SPAWN_MCP_CONFIG="${RS_ROLE_SPAWN_MCP_CONFIG:-/etc/role-mcp/spawn-mcp.json}"
export RS_ROLE_SPAWN_SH="${RS_ROLE_SPAWN_SH:-/opt/role-mcp/spawn.sh}"
export PATH="$HOME/.local/bin:/opt/conda/bin:$PATH"

cd /workspace
exec /opt/conda/bin/python3 /opt/role-mcp/daemon.py
