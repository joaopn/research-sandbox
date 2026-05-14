#!/usr/bin/env python3
"""Render the websearcher role's three image-baked JSON artifacts.

Run at image build time by Dockerfile.websearcher. Reads the YAML
source-of-truth (extra-mcps.yaml) and emits:

  --out-extra <path>             The substrate's image-baked merge
                                 hook input (spawn-mcp.json's source
                                 for the playwright entry).

  --out-config <path>            The Playwright MCP config file
                                 referenced via `--config <path>` from
                                 the MCP's CLI args. Carries
                                 browser.launchOptions.args from
                                 YAML's chromium_args block (Chromium-
                                 level flags can't ride on the MCP
                                 CLI directly in 0.0.41+).

  --out-managed-settings <path>  Claude Code managed-settings file
                                 (the highest-precedence permission
                                 scope per Claude Code's docs). Carries
                                 permissions.deny derived from YAML's
                                 denied_tools mapping, prefixed with
                                 `mcp__<mcp-server-name>__`.

Placeholders resolved at render time:
  __PLAYWRIGHT_MCP_BIN__  -> --bin <path>
  __CONFIG_PATH__         -> --out-config <path>

Stdlib + pyyaml. Errors are fatal (non-zero exit) so a build with a
malformed YAML fails loudly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

PLACEHOLDER_BIN = "__PLAYWRIGHT_MCP_BIN__"
PLACEHOLDER_CONFIG = "__CONFIG_PATH__"


def render(yaml_text: str, bin_path: str, config_path: str
           ) -> tuple[dict, dict, dict]:
    spec = yaml.safe_load(yaml_text)
    if not isinstance(spec, dict):
        raise ValueError("YAML root must be a mapping")
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        raise ValueError("YAML must define a non-empty mcpServers mapping")

    def sub(s: str) -> str:
        return s.replace(PLACEHOLDER_BIN, bin_path) \
                .replace(PLACEHOLDER_CONFIG, config_path)

    out_servers: dict[str, dict] = {}
    for name, server in servers.items():
        if not isinstance(server, dict):
            raise ValueError(f"mcpServers.{name} must be a mapping")
        command = server.get("command")
        args = server.get("args") or []
        if not isinstance(command, str) or not command:
            raise ValueError(f"mcpServers.{name}.command must be a non-empty string")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError(f"mcpServers.{name}.args must be a list of strings")
        out_servers[name] = {
            "command": sub(command),
            "args": [sub(a) for a in args],
        }
    extra = {"mcpServers": out_servers}

    chromium_args = spec.get("chromium_args") or []
    if not isinstance(chromium_args, list) \
            or not all(isinstance(a, str) for a in chromium_args):
        raise ValueError("chromium_args must be a list of strings")
    # The @playwright/mcp config schema accepts browser.launchOptions.args
    # — the standard Playwright LaunchOptions field that surfaces Chromium
    # command-line flags. (Verified via config.d.ts in the installed package.)
    mcp_config = {
        "browser": {
            "launchOptions": {
                "args": list(chromium_args),
            },
        },
    }

    denied_tools = spec.get("denied_tools") or {}
    if not isinstance(denied_tools, dict) \
            or not all(isinstance(k, str) for k in denied_tools):
        raise ValueError("denied_tools must be a mapping of tool-name -> rationale")
    # Tool-restriction rules in Claude Code use the format `mcp__<server>__<tool>`
    # where <server> is the key under spawn-mcp.json's mcpServers (here:
    # whatever the YAML's mcpServers key is — typically "playwright"). Build
    # the deny array by Cartesian product of mcpServers names × denied_tools.
    deny_rules: list[str] = []
    for server_name in sorted(out_servers):
        for tool_name in sorted(denied_tools):
            deny_rules.append(f"mcp__{server_name}__{tool_name}")
    managed_settings = {
        "permissions": {
            "deny": deny_rules,
        },
    }
    return extra, mcp_config, managed_settings


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--yaml", required=True, type=Path,
                   help="Source YAML, e.g. container/role-mcp/websearcher/extra-mcps.yaml")
    p.add_argument("--bin", required=True,
                   help="Resolved absolute path of the MCP binary")
    p.add_argument("--out-extra", required=True, type=Path,
                   help="Destination for the substrate's extra-mcps.json")
    p.add_argument("--out-config", required=True, type=Path,
                   help="Destination for the Playwright MCP config JSON, "
                        "ALSO the value substituted into __CONFIG_PATH__")
    p.add_argument("--out-managed-settings", required=True, type=Path,
                   help="Destination for the Claude Code managed-settings JSON")
    args = p.parse_args(argv)

    yaml_text = args.yaml.read_text()
    extra, mcp_config, managed_settings = render(
        yaml_text, args.bin, str(args.out_config))
    for path, data in (
            (args.out_extra, extra),
            (args.out_config, mcp_config),
            (args.out_managed_settings, managed_settings)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
