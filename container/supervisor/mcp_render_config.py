"""mcp_render_config — translate the project's MCP allowlist into the
proxy's config.json, then exit. Called once by entrypoint.supervisor.sh
and again by mcp-reload after `research project mcp allow/deny/sync`.

Stdlib only. Reads:
    /workspace/.orchestrator/mcp-allow.json
        — the per-project allowlist (list of
          {name, kind, transport, ip, port, headers?}).
        Lives inside the workspace (a directory bind-mount) so atomic-
        rename writes by research.py on the host are visible to the
        supervisor immediately.
Writes:
    /workspace/.orchestrator/mcp-proxy/config.json
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ALLOWLIST = Path(os.environ.get(
    "MCP_ALLOWLIST", "/workspace/.orchestrator/mcp-allow.json",
))
OUT = Path(os.environ.get(
    "MCP_PROXY_CONFIG_OUT",
    "/workspace/.orchestrator/mcp-proxy/config.json",
))


def render() -> int:
    if ALLOWLIST.is_file():
        try:
            entries = json.loads(ALLOWLIST.read_text())
        except json.JSONDecodeError as e:
            print(f"warning: {ALLOWLIST} invalid JSON ({e}); empty config",
                  file=sys.stderr)
            entries = []
    else:
        entries = []
    if not isinstance(entries, list):
        print(f"warning: {ALLOWLIST} root must be a list; empty config",
              file=sys.stderr)
        entries = []

    cfg: dict = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        ip = e.get("ip")
        port = e.get("port")
        if not isinstance(name, str) or not isinstance(ip, str) or not isinstance(port, int):
            continue
        route = {"ip": ip, "port": port}
        headers = e.get("headers")
        if isinstance(headers, dict) and headers:
            route["headers"] = headers
        cfg[name] = route

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    tmp.replace(OUT)
    print(f"mcp-proxy config: {len(cfg)} route(s) -> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(render())
