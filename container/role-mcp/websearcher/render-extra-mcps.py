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

After writing the three files it CHECKS THE TOOL LIST: it starts each
server exactly as rendered, asks for its tools over stdio (initialize, then
tools/list — no browser is launched for that), and fails when a tool is in
neither `denied_tools` nor `allowed_tools`. So a Playwright bump that adds a
tool fails the image build until the YAML reviews it.

Stdlib + pyyaml. Errors are fatal (non-zero exit) so a build with a
malformed YAML or an unreviewed tool fails loudly.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

PLACEHOLDER_BIN = "__PLAYWRIGHT_MCP_BIN__"
PLACEHOLDER_CONFIG = "__CONFIG_PATH__"

# How long the build waits for a server's whole tool list (start, initialize,
# every tools/list page). Measured 0.25-0.3 s in both images that run this,
# offline, as root and as worker. A false failure here fails the WHOLE image
# rebuild, while waiting costs nothing in a non-interactive build, so the bound
# is generous: 60 s covers a cold node start on a loaded build host by two
# orders of magnitude and still turns a hung server into a failed build within
# a minute rather than a build that never ends.
TOOLS_LIST_TIMEOUT_S = 60
# How long a server gets to exit after its input closes and it is sent SIGTERM,
# before the SIGKILL backstop. dumb-init forwards SIGTERM to node's own process
# group (node runs in a separate session dumb-init made, out of reach of a
# group kill aimed at dumb-init); measured gone within 10 ms. 5 s is two
# orders of magnitude over that and only ever spent on a server that ignores
# the signal, at which point the backstop fires.
TERM_GRACE_S = 5
MCP_PROTOCOL_VERSION = "2025-03-26"


class ToolsCheckError(Exception):
    """The tool list could not be read, or it holds an unreviewed tool."""


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
    allowed_tools = spec.get("allowed_tools") or {}
    for key, mapping in (("denied_tools", denied_tools), ("allowed_tools", allowed_tools)):
        if not isinstance(mapping, dict) or not all(
                isinstance(k, str) and isinstance(v, str) and v.strip()
                for k, v in mapping.items()):
            raise ValueError(f"{key} must be a mapping of tool-name -> non-empty reason")
    both = sorted(set(denied_tools) & set(allowed_tools))
    if both:
        raise ValueError(f"tools in both denied_tools and allowed_tools: {', '.join(both)}")
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


def reviewed_tools(yaml_text: str) -> tuple[set[str], set[str]]:
    """(denied, allowed) tool names from the YAML (render() validated them)."""
    spec = yaml.safe_load(yaml_text)
    return set(spec.get("denied_tools") or {}), set(spec.get("allowed_tools") or {})


def list_server_tools(command: str, args: list[str],
                      timeout: float = TOOLS_LIST_TIMEOUT_S) -> list[str]:
    """Start one stdio MCP server, return the names of every tool it lists
    (following tools/list pagination). Fails closed: a server that does not
    start, exits, errors, answers nothing in time, or lists no tools raises
    ToolsCheckError. Teardown: close the server's input, SIGTERM it (dumb-init
    forwards that to node, which runs in a session of its own), wait
    TERM_GRACE_S, then SIGKILL the server's process group as a backstop. The
    backstop reaches only children that stayed in the server's group; a
    wrapper that moves its child elsewhere and does not forward SIGTERM can
    leave it behind (at image build the RUN step's teardown removes it)."""
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as errf:
        try:
            proc = subprocess.Popen([command, *args], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=errf,
                                    start_new_session=True)
        except OSError as e:
            raise ToolsCheckError(f"could not start {command}: {e}") from e
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        buf = b""

        def stderr_tail() -> str:
            errf.seek(0)
            return errf.read().decode(errors="replace").strip()

        def send(msg: dict) -> None:
            try:
                proc.stdin.write((json.dumps(msg) + "\n").encode())
                proc.stdin.flush()
            except OSError as e:
                raise ToolsCheckError(f"{command} closed its input: {e}; "
                                      f"stderr: {stderr_tail()}") from e

        def recv(req_id: int) -> dict:
            nonlocal buf
            while True:
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:                # bad JSON or bad UTF-8:
                        continue                      # a stray non-protocol line
                    if isinstance(msg, dict) and msg.get("id") == req_id:
                        if "error" in msg:
                            raise ToolsCheckError(f"{command} answered request "
                                                  f"{req_id} with an error: {msg['error']}")
                        result = msg.get("result")
                        if not isinstance(result, dict):
                            raise ToolsCheckError(f"{command} answered request "
                                                  f"{req_id} without a result object")
                        return result
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ToolsCheckError(f"{command} gave no tool list within "
                                          f"{TOOLS_LIST_TIMEOUT_S} s; stderr: {stderr_tail()}")
                if not sel.select(left):
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)   # read size only; lines reassemble in buf
                if not chunk:
                    raise ToolsCheckError(f"{command} exited before answering "
                                          f"(code {proc.poll()}); stderr: {stderr_tail()}")
                buf += chunk

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                             "clientInfo": {"name": "render-extra-mcps", "version": "1"}}})
            recv(1)
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            names: list[str] = []
            cursor, req_id = None, 2
            while True:
                send({"jsonrpc": "2.0", "id": req_id, "method": "tools/list",
                      "params": {"cursor": cursor} if cursor else {}})
                result = recv(req_id)
                tools = result.get("tools")
                if not isinstance(tools, list):
                    raise ToolsCheckError(f"{command}: tools/list returned no tools array")
                names += [t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]
                cursor = result.get("nextCursor")
                if not cursor:
                    break
                req_id += 1
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=TERM_GRACE_S)
                except subprocess.TimeoutExpired:
                    pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
            sel.close()
            try:
                proc.stdout.close()
            except OSError:
                pass
    if not names:
        raise ToolsCheckError(f"{command} listed no tools")
    return names


def check_tools(extra: dict, denied: set[str], allowed: set[str]) -> list[str]:
    """Check every rendered server's real tool list against the YAML's two
    mappings. Returns report lines; raises ToolsCheckError on an unreviewed
    tool (named on the error's last line)."""
    report: list[str] = []
    for name, server in sorted(extra["mcpServers"].items()):
        exposed = set(list_server_tools(server["command"], server["args"]))
        unreviewed = sorted(exposed - denied - allowed)
        gone = sorted((denied | allowed) - exposed)
        report.append(f"{name}: {len(exposed)} tools exposed, "
                      f"{len(exposed & denied)} denied, {len(exposed & allowed)} allowed")
        if gone:
            report.append(f"{name}: listed in the YAML but not exposed: {', '.join(gone)}")
        if unreviewed:
            raise ToolsCheckError(
                "\n".join(report) + "\n"
                f"{name}: tools exposed but in neither denied_tools nor allowed_tools: "
                f"{', '.join(unreviewed)}")
    return report


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

    # Fail LOUD at build if --bin resolved empty. The Dockerfiles pass
    # `$(command -v playwright-mcp)`, which yields "" (not an error) if the
    # @playwright/mcp CLI is renamed upstream (it went mcp-server-playwright →
    # playwright-mcp after 0.0.41). argparse `required` only checks presence, and an
    # empty value would template into the MCP `args` (a valid string) → a silent
    # empty command slot → `dumb-init -- "" …` → JSON-RPC -32000 at runtime. Catch
    # it here, the single choke point both websearcher Dockerfiles call.
    if not args.bin.strip():
        p.error("--bin resolved empty; the @playwright/mcp binary name likely "
                "changed (check `command -v playwright-mcp` in the Dockerfile)")

    yaml_text = args.yaml.read_text()
    extra, mcp_config, managed_settings = render(
        yaml_text, args.bin, str(args.out_config))
    for path, data in (
            (args.out_extra, extra),
            (args.out_config, mcp_config),
            (args.out_managed_settings, managed_settings)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    # After the writes: the server reads its --config file at start.
    denied, allowed = reviewed_tools(yaml_text)
    try:
        for line in check_tools(extra, denied, allowed):
            print(f"render-extra-mcps: {line}")
    except ToolsCheckError as e:
        print(f"render-extra-mcps: tool check failed:\n{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
