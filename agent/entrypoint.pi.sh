#!/usr/bin/env bash
# entrypoint.pi.sh — PI-role container boot.
#
# Expected mounts (provided by the supervisor's `pi enable` call):
#   /workspace          — per-role RW state (role.md, .claude/, sessions/, …)
#
# Auth: PI containers are PI-owned and boot un-authed. No creds are staged
# here — the PI authenticates in the tab (`/login`), or the operator pushes
# the supervisor's creds in via `rs-pi sync-creds` (docker cp + install, no
# mount). bypassPermissions config is baked into rs-pi-base's settings.json.
#
# Expected environment:
#   RS_PI_ROLE          — e.g. echo, wrangler, librarian, websearcher
#
# Startup order:
#   1. Restore home skel (volume hides image contents).
#   2. Stage role.md from /opt/pi-templates/<role>/role.md if absent.
#   3. tail -f /dev/null — keep the container up; byobu sessions are
#      created on first webui tab connect.

set -euo pipefail

: "${RS_PI_ROLE:?RS_PI_ROLE must be set}"

# --- Restore home skel on first boot ---------------------------------------
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/worker-skel/. ~/
fi

# --- Deploy the agent (claude) from the supervisor-staged dist -------------
# Own writable ~/.local (no bake; STAGE_AGENT_DIST slice 2). Absence-guarded so a
# restart preserves an autoupdater bump. The PI's interactive `claude` (byobu tab)
# finds it via ~/.local/bin on PATH — self-heal the .bashrc export below.
if [[ -d /opt/agent-dist && ! -e ~/.local/bin/claude ]]; then
    mkdir -p ~/.local
    cp -a /opt/agent-dist/. ~/.local/
fi
if ! grep -q '\.local/bin' ~/.bashrc 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
fi

# --- code-server editor (dist) — STAGE_EDITOR_DIST. Deploy + lazy-launch from
#     /opt/editor-dist when the project enabled the editor (RS_SERVICE_CODE_SERVER
#     forwarded from its flag), the mount is populated, and no system bake exists
#     (the coexistence guard). The deploy/launch logic is ONE shared script in the
#     dist (no per-entrypoint duplication). Non-fatal: the editor is optional.
if [[ "${RS_SERVICE_CODE_SERVER:-disabled}" == "enabled" ]] \
   && [[ -e /opt/editor-dist/.local/bin/code-server ]] \
   && [[ ! -e /usr/bin/code-server ]]; then
    bash /opt/editor-dist/tools/code-server-deploy.sh || true
fi

# Role marker for the byobu status-bar plugin (~/.byobu/bin/60_rolename).
# Written every boot — see entrypoint.supervisor.sh for the rationale.
echo "${RS_PI_ROLE}" > ~/.rs-role

# Artifact-contract surface (STAGE_SANDBOX_ARTIFACTS): published/ is what the
# supervisor reads; internal/ is private scratch. Created every boot (idempotent);
# both are subdirs of the project-volume /workspace bind-mount, so published/ is
# visible to the supervisor for free. The Stop-hook gate (baked into rs-pi-base's
# settings.json) reconciles published/ against the manifest on idle.
mkdir -p /workspace/published /workspace/internal

# --- Auth: PI-owned, no staging --------------------------------------------
# Nothing to do here. PI containers boot un-authed; the PI runs `/login` in
# the tab, or the operator pushes the supervisor's creds in via
# `rs-pi sync-creds`. settings.json (bypassPermissions) is baked into the
# rs-pi-base image, so interactive claude doesn't permission-prompt.

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

# --- Render .mcp.json + .tools-inventory.md from two sources ---------------
# Mirror of entrypoint.role-mcp.sh's render block. Two sources feed the
# rendered config + inventory:
#
#   1. Proxy-routed upstreams from /etc/orchestrator/role-mcps.json ∩
#      mcp-allow.json — the same intersection the worker-facing role-MCP
#      uses (single source of truth, no PI/worker drift).
#   2. Image-baked stdio MCPs from /opt/pi-templates/<short>/extra-mcps.json
#      — per-role tooling bundled with the image (Playwright in
#      pi-websearcher; parallel to entrypoint.role-mcp.sh's
#      /opt/role-mcp/role/extra-mcps.json hook in rs-websearcher).
#
# Outcomes:
#   - Both sources empty (pi-echo): no .mcp.json written, no inventory.
#   - Only proxy-routed (pi-wrangler with allowlisted DBs): one section.
#   - Only image-baked (pi-websearcher with no operator-added upstreams):
#     image-baked section with live tools/list query results.
#   - Both: both sections; name-collision between sources is a hard error
#     (boot fails) so an operator can't shadow an image-baked tool.
python3 - <<'PYEOF'
import json
import os
import subprocess
import sys
from pathlib import Path

role = os.environ["RS_PI_ROLE"]
orch = Path("/etc/orchestrator")
mcp_cfg_path = Path("/workspace/.mcp.json")
inventory_path = Path("/workspace/.tools-inventory.md")

role_mcps_path = orch / "role-mcps.json"
allow_path = orch / "mcp-allow.json"
extras_path = Path(f"/opt/pi-templates/{role}/extra-mcps.json")


def query_image_baked_tools(command, cmd_args, timeout=5):
    """Spawn an image-baked stdio MCP, send initialize + tools/list, parse
    the response. Same contract as entrypoint.role-mcp.sh's helper. On any
    failure (spawn, timeout, parse) returns (None, "<why>") — the renderer
    surfaces a placeholder in the inventory but does NOT hard-fail the
    container, since the PI's claude may still know the tools from role.md."""
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

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "rs-pi-entrypoint", "version": "0"}}},
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


# --- Source 1: proxy-routed upstreams from role-mcps.json ∩ mcp-allow.json
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

cfg: dict = {"mcpServers": {}}
proxy_inventory_rows: list[tuple[str, str, str]] = []  # (name, path, description)
for name in upstream_names:
    e = allow_by_name.get(name)
    if not e:
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

# --- Source 2: image-baked stdio MCPs from /opt/pi-templates/<short>/extra-mcps.json
# Parallel to entrypoint.role-mcp.sh's image-baked merge. Collisions with
# proxy-routed names are hard-failures — an operator can't silently shadow
# an image-baked tool with a proxy one. Tool names rendered into the
# inventory come from a live tools/list query so they stay current across
# MCP server version bumps.
image_baked_inventory: list[tuple[str, list[str], str]] = []  # (name, tool_names, note)
if extras_path.is_file():
    try:
        extras_data = json.loads(extras_path.read_text())
    except json.JSONDecodeError as e:
        print(f"pi[{role}]: extra-mcps.json invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    extras_servers = extras_data.get("mcpServers") if isinstance(extras_data, dict) else None
    if extras_servers is None:
        extras_servers = {}
    if not isinstance(extras_servers, dict):
        print(f"pi[{role}]: extra-mcps.json mcpServers must be an object",
              file=sys.stderr)
        sys.exit(1)

    collisions = sorted(n for n in extras_servers if n in cfg["mcpServers"])
    if collisions:
        names_csv = ", ".join(repr(n) for n in collisions)
        print(f"pi[{role}]: image-baked / proxy-routed name collision: "
              f"{names_csv} is both image-baked AND proxy-routed; refusing "
              f"to start. Rename or remove the operator-registered MCP, "
              f"then re-enable this PI role.",
              file=sys.stderr)
        sys.exit(1)

    for name, server_cfg in extras_servers.items():
        if not isinstance(server_cfg, dict):
            image_baked_inventory.append((name, [], "(invalid server config in extra-mcps.json)"))
            continue
        # The render-extra-mcps.py output carries {command, args} only;
        # claude's .mcp.json reader requires an explicit `type` field for
        # stdio entries to skip its URL-based defaulting. Set it here so
        # interactive PI claude picks up the image-baked MCP cleanly.
        merged_cfg = dict(server_cfg)
        merged_cfg.setdefault("type", "stdio")
        cfg["mcpServers"][name] = merged_cfg
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
            print(f"pi[{role}]: image-baked MCP {name!r} tools/list "
                  f"failed: {err}", file=sys.stderr)
        else:
            tool_names = [t.get("name") for t in tools if isinstance(t, dict)
                          and isinstance(t.get("name"), str)]
            image_baked_inventory.append((name, tool_names, ""))

# --- Nothing to render? Exit clean (pi-echo case) --------------------------
if not role_mcps_has_entry and not extras_path.is_file():
    raise SystemExit(0)

# --- Write .mcp.json ------------------------------------------------------
if cfg["mcpServers"]:
    mcp_cfg_path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
else:
    if mcp_cfg_path.exists():
        mcp_cfg_path.unlink()

# --- Render the inventory --------------------------------------------------
# Image-baked tools section first when present — they're role-defining for
# PI roles that bake their primary tool (pi-websearcher: Playwright IS the
# point); proxy-routed are project-supplemental.
lines: list[str] = [
    f"# Tools available to pi-{role} in this project",
    "",
    "Rendered at container start by entrypoint.pi.sh from the project's",
    "role-mcps.json and (if present) /opt/pi-templates/<role>/extra-mcps.json.",
    "Proxy-routed upstreams reach via the supervisor's mcp-proxy; image-",
    "baked stdio MCPs are launched directly by this interactive claude",
    "session. claude auto-discovers /workspace/.mcp.json and wires them",
    "for you — call them by tool name.",
    "",
]

if image_baked_inventory:
    lines += [
        "## Image-baked tools",
        "",
        "Stdio MCPs bundled with this role's image. Launched directly by",
        "your claude session (no proxy involved). Tool names below come",
        "from a live tools/list query at container start — they reflect",
        "the actual API of the installed MCP version, not a static template.",
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

proxy_header = "## Proxy-routed upstreams"
if not proxy_inventory_rows:
    proxy_header += " (none allowlisted)"
lines += [proxy_header, ""]

if not proxy_inventory_rows:
    if image_baked_inventory:
        lines += [
            "No proxy-routed MCPs allowlisted for this role in this project.",
            "Expected if this role's primary tools are image-baked (see above).",
            "",
        ]
    else:
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
        "Reach each tool via `http://mcp-proxy:8888/<name>/...` — claude",
        "wires this automatically from .mcp.json; call by tool name.",
        "",
        "| Name | Path | Description |",
        "|---|---|---|",
    ]
    for name, path, desc in proxy_inventory_rows:
        safe_desc = desc.replace("|", "\\|").replace("\n", " ").strip()
        lines.append(f"| `{name}` | `{path}` | {safe_desc} |")
    lines.append("")

inventory_path.write_text("\n".join(lines))
PYEOF
if [[ -f /workspace/.tools-inventory.md ]]; then
    echo "pi[${RS_PI_ROLE}]: .mcp.json + .tools-inventory.md rendered"
fi

echo "pi[${RS_PI_ROLE}]: ready (workspace at /workspace; PI-authed via /login or rs-pi sync-creds)"

# --- Keep the container alive ----------------------------------------------
# byobu sessions get created on first webui tab connect via the registered
# `command` field, not here.
exec tail -f /dev/null
