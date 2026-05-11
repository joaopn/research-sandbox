"""mcp_render_config — translate the project's MCP allowlist + role-MCP
registry into the proxy's config.json, then exit. Called once by
entrypoint.supervisor.sh and again by mcp-reload after `research project
mcp allow/deny/sync` or `research project role-mcp enable/disable`.

Stdlib only. Reads:
    /workspace/.orchestrator/mcp-allow.json
        — the per-project allowlist of external + shared MCPs (list of
          {name, kind, transport, ip, port, headers?}).
    /workspace/.orchestrator/role-mcps.json
        — the per-project role-MCP registry (object keyed by role name,
          values {ip, port, upstream_mcps, image}). Role-MCP entries are
          merged in as proxy routes alongside the allowlist so a worker
          calling `mcp-proxy:8888/<role>/mcp` reaches the role-MCP
          container at the role's pinned inner-bridge IP.
        Lives inside the workspace (a directory bind-mount) so atomic-
        rename writes by research.py on the host are visible to the
        supervisor immediately.

Writes:
    /workspace/.orchestrator/mcp-proxy/config.json

Conflict policy: if a name appears in both files (a role-MCP and a
registered MCP share a name), role-MCP wins and a warning prints to
stderr. The two surfaces are managed separately on purpose, but a
name collision means one of them is wrong — surface the surprise.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ALLOWLIST = Path(os.environ.get(
    "MCP_ALLOWLIST", "/workspace/.orchestrator/mcp-allow.json",
))
ROLE_MCPS = Path(os.environ.get(
    "ROLE_MCPS", "/workspace/.orchestrator/role-mcps.json",
))
OUT = Path(os.environ.get(
    "MCP_PROXY_CONFIG_OUT",
    "/workspace/.orchestrator/mcp-proxy/config.json",
))


def _load_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"warning: {path} invalid JSON ({e}); treating as empty",
              file=sys.stderr)
        return None


def render() -> int:
    cfg: dict = {}

    # ---- allowlist (external + shared MCPs from the general registry) ----
    entries = _load_json(ALLOWLIST) or []
    if not isinstance(entries, list):
        print(f"warning: {ALLOWLIST} root must be a list; ignoring",
              file=sys.stderr)
        entries = []
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

    # ---- role-MCPs (project-internal orchestration containers) ----
    role_entries = _load_json(ROLE_MCPS) or {}
    if not isinstance(role_entries, dict):
        print(f"warning: {ROLE_MCPS} root must be an object; ignoring",
              file=sys.stderr)
        role_entries = {}
    for name, e in role_entries.items():
        if not isinstance(name, str) or not isinstance(e, dict):
            continue
        ip = e.get("ip")
        port = e.get("port")
        if not isinstance(ip, str) or not isinstance(port, int):
            continue
        if name in cfg:
            print(f"warning: {name!r} appears in both mcp-allow.json and "
                  f"role-mcps.json; role-MCP wins (re-check your registry)",
                  file=sys.stderr)
        cfg[name] = {"ip": ip, "port": port}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    tmp.replace(OUT)
    print(f"mcp-proxy config: {len(cfg)} route(s) -> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(render())
