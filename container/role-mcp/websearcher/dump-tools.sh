#!/usr/bin/env bash
# dump-tools.sh — print the Playwright MCP's tools/list verbatim.
#
# Token-free verification helper for Phase 7 of the B.1 acceptance
# runbook (PLAN/STAGE_BACKEND_MCP_B1_test.md). Spawns the image-baked
# Playwright stdio MCP, sends the standard initialize -> initialized ->
# tools/list handshake, parses the tools/list response, and prints one
# tool name per line. Used to enumerate the Playwright MCP's tool
# surface without paying for a real claude spawn. NOTE: this reports
# what Playwright EXPOSES — denied tools (via /etc/claude-code/
# managed-settings.json) still appear here. The deny happens at the
# Claude Code client layer, not at the MCP source.
#
# Reads /opt/role-mcp/role/extra-mcps.json (rendered at image build by
# render-extra-mcps.py from extra-mcps.yaml). Iterates every mcpServer
# entry, in case the role grows additional image-baked MCPs later.

set -euo pipefail

EXTRAS="${EXTRAS:-/opt/role-mcp/role/extra-mcps.json}"
TIMEOUT="${TIMEOUT:-15}"

if [[ ! -f "${EXTRAS}" ]]; then
    echo "dump-tools: ${EXTRAS} missing — image build did not render the JSON?" >&2
    exit 2
fi

exec /opt/conda/bin/python3 - "${EXTRAS}" "${TIMEOUT}" <<'PYEOF'
import json
import subprocess
import sys

extras_path = sys.argv[1]
timeout = float(sys.argv[2])

with open(extras_path) as f:
    cfg = json.load(f)

servers = cfg.get("mcpServers") or {}
if not servers:
    print("dump-tools: no mcpServers in extra-mcps.json", file=sys.stderr)
    sys.exit(1)

rc = 0
for name, server in servers.items():
    command = server.get("command")
    args = server.get("args") or []
    if not command:
        print(f"# {name}: no command", file=sys.stderr)
        rc = 1
        continue
    try:
        proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, FileNotFoundError) as e:
        print(f"# {name}: spawn failed: {e}", file=sys.stderr)
        rc = 1
        continue

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "dump-tools", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"

    try:
        out, err = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        print(f"# {name}: tools/list timed out after {timeout}s", file=sys.stderr)
        rc = 1
        continue

    tools = None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id") == 2 and isinstance(obj.get("result"), dict):
            tools = obj["result"].get("tools")
            break

    if tools is None:
        stderr_tail = (err or "")[-200:].replace("\n", " ").strip()
        print(f"# {name}: no tools/list result (stderr tail: {stderr_tail!r})",
              file=sys.stderr)
        rc = 1
        continue

    print(f"# {name}: {len(tools)} tools")
    for t in tools:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            print(t["name"])

sys.exit(rc)
PYEOF
