#!/usr/bin/env bash
# entrypoint.pi.sh — PI-role container boot.
#
# Expected mounts (provided by the supervisor's `pi enable` call):
#   /workspace          — per-role RW state (role.md, .claude/, sessions/, …)
#   /creds              — RO snapshot of supervisor creds (.credentials.json,
#                         optional settings.json). Parent-dir bind-mount so
#                         atomic-rename writes by the supervisor stay visible.
#
# Expected environment:
#   RS_PI_ROLE          — e.g. echo, wrangler, librarian, websearcher
#
# Startup order:
#   1. Restore home skel (volume hides image contents).
#   2. Stage creds from /creds → ~/.claude/.
#   3. Stage role.md from /opt/pi-templates/<role>/role.md if absent.
#   4. tail -f /dev/null — keep the container up; byobu sessions are
#      created on first webui tab connect.

set -euo pipefail

: "${RS_PI_ROLE:?RS_PI_ROLE must be set}"

# --- Restore home skel on first boot ---------------------------------------
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/worker-skel/. ~/
fi

# --- Stage creds (in-container claude needs OAuth) -------------------------
# The supervisor stages its current ~/.claude/.credentials.json into the
# per-role creds bind-mount source at /workspace/.pi/<role>/.creds/ on
# enable, and the inotify watcher on the supervisor re-stages on re-auth.
# We copy it into the runtime user's home so the inner claude picks it up.
# Failure is fatal — claude won't work without creds, and we'd rather fail
# loud at boot than silent in a session.
if [[ -f /creds/.credentials.json ]]; then
    mkdir -p ~/.claude
    cp /creds/.credentials.json ~/.claude/.credentials.json
    chmod 600 ~/.claude/.credentials.json
else
    echo "pi[${RS_PI_ROLE}]: missing /creds/.credentials.json" >&2
    echo "pi[${RS_PI_ROLE}]: refusing to start — supervisor staging step was skipped or supervisor is unauthenticated" >&2
    exit 2
fi
# Optional: settings (theme, verbosity). Tolerant of absence.
if [[ -f /creds/settings.json ]]; then
    cp /creds/settings.json ~/.claude/settings.json
fi

# --- Stage role.md from template if absent ---------------------------------
# Workspace-template-refresh idiom: copy on first boot only; PI's edits to
# /workspace/role.md persist across container restarts. Operator who wants
# to pick up a new template removes /workspace/role.md and re-enables.
if [[ -d "/opt/pi-templates/${RS_PI_ROLE}" ]] && [[ ! -f /workspace/role.md ]]; then
    if [[ -f "/opt/pi-templates/${RS_PI_ROLE}/role.md" ]]; then
        cp "/opt/pi-templates/${RS_PI_ROLE}/role.md" /workspace/role.md
    fi
fi

# Symlink CLAUDE.md → role.md so an interactive `claude` session in this
# container auto-discovers the role prompt. role.md is the canonical
# filename (parity with worker-facing role-MCPs, which pass it via
# --system-prompt to claude -p); CLAUDE.md is the auto-discovery alias
# for PI mode where the PI types `claude` and expects framing.
if [[ -f /workspace/role.md ]] && [[ ! -e /workspace/CLAUDE.md ]]; then
    ln -s role.md /workspace/CLAUDE.md
fi

# --- Render .mcp.json + .tools-inventory.md if this role has upstreams -----
# Mirror of entrypoint.role-mcp.sh's render block: look up RS_PI_ROLE in
# /etc/orchestrator/role-mcps.json (the worker-facing role-MCP registry)
# and render the upstream set from there. Same source of truth as the
# worker-facing role-MCP, so PI mode never drifts from worker mode on
# what MCPs are available.
#
# Three outcomes:
#   1. RS_PI_ROLE matches a role-mcps.json key with non-empty upstream_mcps
#      → render /workspace/.mcp.json (auto-discovered by interactive
#        `claude`) + /workspace/.tools-inventory.md (markdown the role
#        prompt instructs claude to read at session start).
#   2. RS_PI_ROLE matches a key but upstream_mcps is empty (operator
#      forgot --upstream) → render an explicit "no upstreams allowlisted"
#      note in .tools-inventory.md, no .mcp.json (claude won't see any
#      MCPs).
#   3. RS_PI_ROLE does NOT match any key (e.g. pi-echo's short name
#      "echo" — no worker-facing "echo" role-MCP) → skip rendering
#      entirely. Substrate-only PI roles (pi-echo) don't get MCP wiring.
if [[ -f /etc/orchestrator/role-mcps.json ]]; then
    python3 - <<'PYEOF'
import json
import os
from pathlib import Path

role = os.environ["RS_PI_ROLE"]
orch = Path("/etc/orchestrator")
mcp_cfg_path = Path("/workspace/.mcp.json")
inventory_path = Path("/workspace/.tools-inventory.md")

role_mcps_path = orch / "role-mcps.json"
allow_path = orch / "mcp-allow.json"

# Look up the upstream set keyed by the PI role's *short* name. The
# convention (codified in cli/pi.py + the STAGE_BACKEND_PI plan) is that
# pi-<short> mirrors the <short> worker-facing role-MCP. Missing key =
# pi-echo-style substrate role with no MCP wiring; exit clean.
upstream_names: list[str] = []
role_mcps_has_entry = False
try:
    data = json.loads(role_mcps_path.read_text())
    entry = data.get(role) if isinstance(data, dict) else None
    if isinstance(entry, dict):
        role_mcps_has_entry = True
        up = entry.get("upstream_mcps") or []
        if isinstance(up, list):
            upstream_names = [n for n in up if isinstance(n, str)]
except (json.JSONDecodeError, FileNotFoundError):
    pass

if not role_mcps_has_entry:
    # Substrate role (pi-echo) — no MCPs to render. Exit clean so the
    # rest of the entrypoint proceeds normally.
    raise SystemExit(0)

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

# .mcp.json: claude auto-discovers this file from cwd. Same shape as the
# spawn-mcp.json the role-MCPs render — mcpServers dict, each upstream
# routed via mcp-proxy:8888.
cfg: dict = {"mcpServers": {}}
inventory_rows: list[tuple[str, str, str]] = []  # (name, path, description)
for name in upstream_names:
    e = allow_by_name.get(name)
    if not e:
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

if cfg["mcpServers"]:
    mcp_cfg_path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
else:
    # Empty mcpServers: don't write .mcp.json (claude with no servers is
    # the same as no .mcp.json — no point littering the workspace).
    if mcp_cfg_path.exists():
        mcp_cfg_path.unlink()

# Inventory is always written (even when empty) so role.md's "read your
# inventory" instruction has something to point at.
lines: list[str] = [
    f"# Tools available to pi-{role} in this project",
    "",
    "Rendered at container start by entrypoint.pi.sh from the project's",
    "role-mcps.json (mirrors the worker-facing wrangler's upstream set).",
    "Each tool is reachable via the supervisor's mcp-proxy at",
    "`http://mcp-proxy:8888/<name>/...`; claude auto-discovers .mcp.json",
    "in /workspace/ and wires them for you — call them by tool name.",
    "",
]
if not inventory_rows:
    lines += [
        "**No upstream MCPs allowlisted for this role in this project.**",
        "",
        "Tell the PI to run:",
        f"  research project role-mcp enable <project> {role} --upstream <mcp,...>",
        "and then re-enable this PI role to re-render the inventory.",
        "",
    ]
else:
    lines += [
        "| Name | Path | Description |",
        "|---|---|---|",
    ]
    for name, path, desc in inventory_rows:
        safe_desc = desc.replace("|", "\\|").replace("\n", " ").strip()
        lines.append(f"| `{name}` | `{path}` | {safe_desc} |")
    lines.append("")

inventory_path.write_text("\n".join(lines))
PYEOF
    if [[ -f /workspace/.tools-inventory.md ]]; then
        echo "pi[${RS_PI_ROLE}]: .mcp.json + .tools-inventory.md rendered"
    fi
fi

echo "pi[${RS_PI_ROLE}]: ready (workspace at /workspace, creds staged)"

# --- Keep the container alive ----------------------------------------------
# byobu sessions get created on first webui tab connect via the registered
# `command` field, not here.
exec tail -f /dev/null
