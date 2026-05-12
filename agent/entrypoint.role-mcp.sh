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
#   4. Render /workspace/.tools-inventory.md from the same intersection
#      (human-readable inventory the role-worker reads at call start to
#      know what tools it has + each one's description).
#   5. Exec daemon.py.

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
# Restore ~/.claude.json (the supervisor's sibling-of-.claude/ dotfile,
# carrying `oauthAccount` + onboarding state). Without it claude treats
# the spawned process as logged-out and `claude -p` calls fail auth.
# The supervisor stages it under the sentinel name `home_claude.json`
# inside .creds/ to avoid colliding with the .claude/-dir convention.
if [[ -f /workspace/.creds/home_claude.json ]]; then
    cp /workspace/.creds/home_claude.json ~/.claude.json
    chmod 600 ~/.claude.json
fi

# --- Render spawn-mcp.json + tools-inventory.md ----------------------------
# Two siblings rendered from the same intersection:
#   /etc/role-mcp/spawn-mcp.json    machine-facing .mcp.json for `claude -p`
#   /workspace/.tools-inventory.md  human-facing inventory the role-worker
#                                   reads at call start to learn what tools
#                                   this project gives it + each one's
#                                   `description` from mcp-allow.json
# The upstream list lives in /etc/orchestrator/role-mcps.json under this
# role's entry; URL/headers/description per upstream live in
# /etc/orchestrator/mcp-allow.json. The two outputs are derived from the
# same data so they cannot drift.
#
# Empty intersection => spawn-mcp.json is "{}" (spawn.sh's empty-config
# skip), and the inventory writes a configuration-problem note for the
# role-worker to flag in its per-call log.
python3 - <<'PYEOF'
import json
import os
from pathlib import Path

role = os.environ["RS_ROLE_NAME"]
orch = Path("/etc/orchestrator")
spawn_cfg_path = Path("/etc/role-mcp/spawn-mcp.json")
inventory_path = Path("/workspace/.tools-inventory.md")
spawn_cfg_path.parent.mkdir(parents=True, exist_ok=True)
inventory_path.parent.mkdir(parents=True, exist_ok=True)

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
inventory_rows: list[tuple[str, str, str]] = []  # (name, path, description)
for name in upstream_names:
    e = allow_by_name.get(name)
    if not e:
        # Validation should prevent this at enable time; tolerate at boot
        # by skipping the entry so the daemon still comes up. Surface the
        # missing-allow in the inventory so the role-worker can flag it.
        inventory_rows.append(
            (name, "(?)", "(NOT in mcp-allow.json — operator must allow it)")
        )
        continue
    path = e.get("path") or "/mcp"
    url = f"http://mcp-proxy:8888/{name}{path}"
    server: dict = {"type": "http", "url": url}
    headers = e.get("headers")
    if isinstance(headers, dict) and headers:
        server["headers"] = headers
    cfg["mcpServers"][name] = server
    desc = e.get("description") or "(no description — ask operator to set one)"
    inventory_rows.append((name, path, desc))

# Empty mcpServers => write "{}" so spawn.sh's empty-config skip kicks in.
if not cfg["mcpServers"]:
    spawn_cfg_path.write_text("{}\n")
else:
    spawn_cfg_path.write_text(
        json.dumps(cfg, indent=2, sort_keys=True) + "\n")

# Render the human-facing inventory.
lines: list[str] = [
    f"# Tools available to {role} in this project",
    "",
    "Rendered at container start by entrypoint.role-mcp.sh from the project's",
    "mcp-allow.json. Reach each tool via the supervisor's mcp-proxy at",
    "`http://mcp-proxy:8888/<name>/...` — claude -p's MCP config wires this",
    "automatically; you call them by their tool name like any other MCP.",
    "",
]
if not inventory_rows:
    lines += [
        "**No upstream MCPs allowlisted for this role in this project.**",
        "",
        "Surface this as a configuration problem in your per-call log",
        "(`outcome: needs_human`, naming the operator action required",
        "— `research project role-mcp enable <project> <role> --upstream <mcp,...>`).",
        "",
    ]
else:
    lines += [
        "| Name | Path | Description |",
        "|---|---|---|",
    ]
    for name, path, desc in inventory_rows:
        # Escape pipes in the description so the markdown table stays valid.
        safe_desc = desc.replace("|", "\\|").replace("\n", " ").strip()
        lines.append(f"| `{name}` | `{path}` | {safe_desc} |")
    lines.append("")

inventory_path.write_text("\n".join(lines))
PYEOF

echo "role-mcp[${RS_ROLE_NAME}]: spawn-mcp.json + tools-inventory.md written"

# --- Exec daemon ------------------------------------------------------------
export RS_ROLE_WORKSPACE="${RS_ROLE_WORKSPACE:-/workspace}"
export RS_ROLE_MCP_PORT="${RS_ROLE_MCP_PORT:-8000}"
export RS_ROLE_TEMPLATE_DIR="${RS_ROLE_TEMPLATE_DIR:-/opt/role-mcp/role}"
export RS_ROLE_SPAWN_MCP_CONFIG="${RS_ROLE_SPAWN_MCP_CONFIG:-/etc/role-mcp/spawn-mcp.json}"
export RS_ROLE_SPAWN_SH="${RS_ROLE_SPAWN_SH:-/opt/role-mcp/spawn.sh}"
export PATH="$HOME/.local/bin:/opt/conda/bin:$PATH"

cd /workspace
exec /opt/conda/bin/python3 /opt/role-mcp/daemon.py
