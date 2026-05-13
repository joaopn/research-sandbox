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
import subprocess
import sys
from pathlib import Path

role = os.environ["RS_ROLE_NAME"]
orch = Path("/etc/orchestrator")
spawn_cfg_path = Path("/etc/role-mcp/spawn-mcp.json")
inventory_path = Path("/workspace/.tools-inventory.md")
spawn_cfg_path.parent.mkdir(parents=True, exist_ok=True)
inventory_path.parent.mkdir(parents=True, exist_ok=True)


def query_image_baked_tools(command, cmd_args, timeout=5):
    """Spawn an image-baked stdio MCP, send initialize + tools/list, parse
    the response. Returns (tool_dicts, err_message). On any failure
    (spawn, timeout, parse) returns (None, "<why>") — the entrypoint
    renders a placeholder in the inventory and logs the failure, but does
    NOT hard-fail the container (a buggy stdio MCP shouldn't block the
    daemon from starting; the role-worker may still know the tools from
    role.md). Timeout is per-MCP; 5s is generous for a local stdio
    roundtrip — most servers respond in <100 ms."""
    try:
        proc = subprocess.Popen(
            [command, *list(cmd_args)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, FileNotFoundError) as e:
        return None, f"spawn failed: {e}"

    # MCP wire: initialize (request) + notifications/initialized + tools/list.
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "rs-entrypoint", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"

    try:
        stdout, stderr = proc.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return None, f"tools/list timed out after {timeout}s"

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id") == 2 and isinstance(obj.get("result"), dict):
            tools = obj["result"].get("tools")
            if isinstance(tools, list):
                return tools, ""

    stderr_tail = (stderr or "")[-200:].replace("\n", " ").strip()
    return None, f"no tools/list result (stderr tail: {stderr_tail!r})"


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

# Every spawned claude -p reaches its proxy-routed upstreams through
# mcp-proxy:8888, the same path workers use. The /<name>/ prefix is the
# proxy's routing key; the proxy strips it and forwards.
cfg: dict = {"mcpServers": {}}
proxy_inventory_rows: list[tuple[str, str, str]] = []  # (name, path, description)
for name in upstream_names:
    e = allow_by_name.get(name)
    if not e:
        # Validation should prevent this at enable time; tolerate at boot
        # by skipping the entry so the daemon still comes up. Surface the
        # missing-allow in the inventory so the role-worker can flag it.
        proxy_inventory_rows.append(
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
    proxy_inventory_rows.append((name, path, desc))

# --- Image-baked extras (substrate B.1-substrate SB1 + SB2) ----------------
# /opt/role-mcp/role/extra-mcps.json declares per-role stdio MCPs bundled
# with the image (e.g. Playwright in B.1's websearcher). Merged into
# spawn-mcp.json so each spawned claude sees them through the same
# --mcp-config path as proxy upstreams. Collisions with proxy-routed
# names are hard-failures — an operator can't silently shadow an image-
# baked tool with a proxy one. Tool names rendered into the inventory
# come from a live tools/list query (NOT a static template), so they
# stay current across pre-1.0 MCP server renames.
extras_path = Path("/opt/role-mcp/role/extra-mcps.json")
image_baked_inventory: list[tuple[str, list[str], str]] = []  # (name, tool_names, note)
if extras_path.is_file():
    try:
        extras_data = json.loads(extras_path.read_text())
    except json.JSONDecodeError as e:
        print(f"role-mcp[{role}]: extra-mcps.json invalid JSON: {e}",
              file=sys.stderr)
        sys.exit(1)

    extras_servers = extras_data.get("mcpServers") if isinstance(extras_data, dict) else None
    if extras_servers is None:
        extras_servers = {}
    if not isinstance(extras_servers, dict):
        print(f"role-mcp[{role}]: extra-mcps.json mcpServers must be an object",
              file=sys.stderr)
        sys.exit(1)

    collisions = sorted(n for n in extras_servers if n in cfg["mcpServers"])
    if collisions:
        names_csv = ", ".join(repr(n) for n in collisions)
        print(f"role-mcp[{role}]: image-baked / proxy-routed name collision: "
              f"{names_csv} is both image-baked AND proxy-routed; refusing "
              f"to start. Rename or remove the operator-registered MCP, "
              f"then re-enable the role-MCP.",
              file=sys.stderr)
        sys.exit(1)

    for name, server_cfg in extras_servers.items():
        if not isinstance(server_cfg, dict):
            image_baked_inventory.append((name, [], "(invalid server config in extra-mcps.json)"))
            continue
        cfg["mcpServers"][name] = server_cfg
        command = server_cfg.get("command")
        cmd_args = server_cfg.get("args") or []
        if not isinstance(command, str) or not command:
            image_baked_inventory.append((name, [], "(missing 'command' in server config)"))
            continue
        if not isinstance(cmd_args, list):
            image_baked_inventory.append((name, [], "(invalid 'args' in server config)"))
            continue
        tools, err = query_image_baked_tools(command, cmd_args, timeout=5)
        if tools is None:
            image_baked_inventory.append(
                (name, [], f"(tools/list failed: {err})")
            )
            print(f"role-mcp[{role}]: image-baked MCP {name!r} tools/list "
                  f"failed: {err}", file=sys.stderr)
        else:
            tool_names = [t.get("name") for t in tools if isinstance(t, dict)
                          and isinstance(t.get("name"), str)]
            image_baked_inventory.append((name, tool_names, ""))

# --- Write spawn-mcp.json -----------------------------------------------
# Empty mcpServers => write "{}" so spawn.sh's empty-config skip kicks in.
if not cfg["mcpServers"]:
    spawn_cfg_path.write_text("{}\n")
else:
    spawn_cfg_path.write_text(
        json.dumps(cfg, indent=2, sort_keys=True) + "\n")

# --- Render the human-facing inventory ----------------------------------
lines: list[str] = [
    f"# Tools available to {role} in this project",
    "",
    "Rendered at container start by entrypoint.role-mcp.sh from the project's",
    "mcp-allow.json and (if present) /opt/role-mcp/role/extra-mcps.json.",
    "Proxy-routed upstreams reach via the supervisor's mcp-proxy; image-",
    "baked stdio MCPs are launched directly by each spawned claude session.",
    "",
]

proxy_header = "## Proxy-routed upstreams"
if not proxy_inventory_rows:
    proxy_header += " (none allowlisted)"
lines += [proxy_header, ""]

if not proxy_inventory_rows:
    lines += [
        "No upstream MCPs allowlisted for this role in this project.",
        "",
    ]
    if not image_baked_inventory:
        lines += [
            "Surface this as a configuration problem in your per-call log",
            "(`outcome: needs_human`, naming the operator action required",
            "— `research project role-mcp enable <project> <role> "
            "--upstream <mcp,...>`).",
            "",
        ]
    else:
        lines += [
            "This may be normal — if this role's primary tools are image-",
            "baked (see below), the absence of proxy upstreams is expected.",
            "",
        ]
else:
    lines += [
        "Reach each tool via `http://mcp-proxy:8888/<name>/...` — your MCP",
        "config wires this automatically; call by tool name like any MCP.",
        "",
        "| Name | Path | Description |",
        "|---|---|---|",
    ]
    for name, path, desc in proxy_inventory_rows:
        safe_desc = desc.replace("|", "\\|").replace("\n", " ").strip()
        lines.append(f"| `{name}` | `{path}` | {safe_desc} |")
    lines.append("")

if image_baked_inventory:
    lines += [
        "## Image-baked tools",
        "",
        "Stdio MCPs bundled with this role's image. Launched directly by",
        "each spawned claude session (no proxy involved). Tool names below",
        "come from a live tools/list query at container start — they",
        "reflect the actual API of the installed MCP version, not a static",
        "template.",
        "",
        "| Name | Transport | Tools |",
        "|---|---|---|",
    ]
    for name, tool_names, note in image_baked_inventory:
        if note:
            tools_cell = note.replace("|", "\\|")
        elif not tool_names:
            tools_cell = "(no tools reported)"
        else:
            tools_cell = ", ".join(f"`{n}`" for n in tool_names)
        lines.append(f"| `{name}` | stdio (image-baked) | {tools_cell} |")
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
