#!/usr/bin/env python3
"""Render /workspace/.mcp.json (and pi's wiring) inside a sandbox box.

Moved verbatim out of agent/entrypoint.sandbox-box.sh so it has ONE source and
two callers: the box image bakes it and the entrypoint runs the baked copy at
every boot, and the host streams THIS SAME FILE into a RUNNING box (`docker exec
-i … python3 -`) when an agent change gives the box pi for the first time — so a
box created from an older pinned box image is wired too. Reads only absolute
paths and RS_PI_AGENT_DIR (set when pi's launcher is present); idempotent
(regenerates everything from its sources every run). Runs as the box user.

Regenerated WHOLESALE every run from three sources:
  source-1 = /workspace/.mcp-proxy.json  — proxy MCP servers the host wrote at
             create/restart from the project allowlist (may be absent/empty).
  source-2 = /opt/sandbox-box/extra-mcps.json — image-baked stdio MCPs (the
             browser box's Playwright, restricted: started through the
             environment-clearing wrapper); base boxes don't carry the file.
             A TRUSTED browser box (RS_BOX_BROWSER=trusted, which rs-sandbox
             sets only on an image that supports it) uses
             /opt/sandbox-box/extra-mcps.trusted.json instead — the same
             declaration without the wrapper; with the env but without that
             file (impossible through rs-sandbox) the box stays restricted.
  source-3 = /workspace/.mcp-preset.json — preset-declared stdio MCPs.
A name collision is a hard error (exit 1: the boot refuses to start) so a
project MCP cannot silently shadow the baked browser tooling.

In a restricted browser box that deploys pi, the image's denied tools
(/opt/sandbox-box/denied-tools.json, {server: [tool, ...]} — the same list
claude's managed settings deny: nine risky Playwright tools, two on an image
built before trusted boxes) are marked `hidden` in that server's
`toolExposure`, so pi registers them but can never call them. claude ignores
the key. No file (a base box) means no hiding; an unreadable one in a
restricted pi box stops the render before anything is written (running pi with
the deny silently off is the failure this prevents). A trusted box never reads
the file. A browser box's tool inventory gains a "Browser" section naming the
mode and, when restricted, the blocked tools (read best-effort for that: a
claude-only box never stops over it).
"""
import json
import os
import sys
from pathlib import Path

proxy_path = Path("/workspace/.mcp-proxy.json")
restricted_extras_path = Path("/opt/sandbox-box/extra-mcps.json")
trusted_extras_path = Path("/opt/sandbox-box/extra-mcps.trusted.json")
# Trusted only when BOTH say so: the box's run (rs-sandbox sets the variable
# only for a trusted box on an image that supports it) and the image (the
# trusted declaration exists). Anything else — absent, empty, another value, an
# image without the file — is the restricted default.
browser_trusted = (os.environ.get("RS_BOX_BROWSER") == "trusted"
                   and trusted_extras_path.is_file())
extras_path = trusted_extras_path if browser_trusted else restricted_extras_path
mcp_path = Path("/workspace/.mcp.json")
inv_path = Path("/workspace/.tools-inventory.md")

servers: dict = {}

# Source 1: proxy MCPs (host-resolved, may be absent/empty).
try:
    data = json.loads(proxy_path.read_text())
    s = data.get("mcpServers") if isinstance(data, dict) else None
    if isinstance(s, dict):
        servers.update(s)
except (OSError, json.JSONDecodeError):
    pass

# Source 2: image-baked stdio MCPs (browser box only).
if extras_path.is_file():
    try:
        extras = json.loads(extras_path.read_text())
    except json.JSONDecodeError as e:
        print(f"sandbox-box: extra-mcps.json invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    es = extras.get("mcpServers") if isinstance(extras, dict) else None
    if es is None:
        es = {}
    if not isinstance(es, dict):
        print("sandbox-box: extra-mcps.json mcpServers must be an object",
              file=sys.stderr)
        sys.exit(1)
    collisions = sorted(n for n in es if n in servers)
    if collisions:
        print(f"sandbox-box: image-baked / project MCP name collision: "
              f"{', '.join(map(repr, collisions))}; refusing to start. "
              f"Rename or drop the colliding project MCP.", file=sys.stderr)
        sys.exit(1)
    for n, cfg in es.items():
        if isinstance(cfg, dict):
            cfg = dict(cfg)
            cfg.setdefault("type", "stdio")
        servers[n] = cfg

# Source 3: preset-declared stdio MCPs (host-staged from the box catalog into
# /workspace/.mcp-preset.json). Values are copied VERBATIM — ${FIELD}
# references are expanded by the AGENT at config load, never by this merge.
# Checked against the UNION of sources 1+2 (must run AFTER source 2, or a
# preset-vs-baked collision slips through this backstop — the host pre-flight
# normally refuses first, but a backstop nobody exercises is one nobody
# notices is broken). Collision policy: refuse-to-start, same as source 2.
preset_path = Path("/workspace/.mcp-preset.json")
if preset_path.is_file():
    try:
        pre = json.loads(preset_path.read_text())
    except json.JSONDecodeError as e:
        print(f"sandbox-box: .mcp-preset.json invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    ps = pre.get("mcpServers") if isinstance(pre, dict) else None
    if ps is None:
        ps = {}
    if not isinstance(ps, dict):
        print("sandbox-box: .mcp-preset.json mcpServers must be an object",
              file=sys.stderr)
        sys.exit(1)
    collisions = sorted(n for n in ps if n in servers)
    if collisions:
        print(f"sandbox-box: preset / baked-or-project MCP name collision: "
              f"{', '.join(map(repr, collisions))}; refusing to start. "
              f"Rename the preset's server or drop the colliding source.",
              file=sys.stderr)
        sys.exit(1)
    for n, cfg in ps.items():
        if isinstance(cfg, dict):
            cfg = dict(cfg)
            cfg.setdefault("type", "stdio")
        servers[n] = cfg

def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file + rename: an agent session reading the file while a
    LIVE re-render runs (an agent added to a running box) must never see it
    half-written."""
    tmp = path.with_name(path.name + ".rs-tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


pi_link = Path("/workspace/.pi/mcp.json")
PI_LINK_TARGET = "../.mcp.json"
pi_agent_dir = os.environ.get("RS_PI_AGENT_DIR") or ""


def pi_link_is_ours() -> bool:
    return pi_link.is_symlink() and os.readlink(pi_link) == PI_LINK_TARGET


def pi_link_set() -> None:
    """Point /workspace/.pi/mcp.json at the rendered file — idempotent across
    boots. Anything else at that path (a file the PI made while no link existed,
    a link of their own) is the PI's: never touched, the link is skipped and pi
    uses theirs. Never fatal (set -e would crash-loop the box over pi wiring)."""
    try:
        if pi_link_is_ours():
            return
        if pi_link.is_symlink() or pi_link.exists() or (
                pi_link.parent.exists() and not pi_link.parent.is_dir()):
            print(f"sandbox-box: {pi_link} is the PI's own file; pi will not see "
                  f"the box's MCP servers (remove it to restore the link)", file=sys.stderr)
            return
        pi_link.parent.mkdir(exist_ok=True)
        tmp = pi_link.with_name(pi_link.name + ".rs-tmp")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(PI_LINK_TARGET, tmp)
        os.replace(tmp, pi_link)
    except OSError as e:
        print(f"sandbox-box: could not link {pi_link}: {e}", file=sys.stderr)


def pi_link_clear() -> None:
    """Remove OUR link (never a PI's file), and the .pi/ folder when that leaves
    it empty. Never fatal."""
    try:
        if pi_link_is_ours():
            pi_link.unlink()
        if pi_link.parent.is_dir() and not pi_link.parent.is_symlink() \
                and not any(pi_link.parent.iterdir()):
            pi_link.parent.rmdir()
    except OSError as e:
        print(f"sandbox-box: could not remove {pi_link}: {e}", file=sys.stderr)


def pi_trust_workspace() -> None:
    """Record /workspace trusted in pi's trust.json (merged; a decision already
    there for /workspace is the PI's and stays). Only in a box that deploys pi.
    Never fatal."""
    if not pi_agent_dir:
        return
    trust = Path(pi_agent_dir) / "trust.json"
    try:
        data = json.loads(trust.read_text()) if trust.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        print(f"sandbox-box: leaving {trust} alone ({e})", file=sys.stderr)
        return
    if "/workspace" in data:
        return
    data["/workspace"] = True
    try:
        trust.parent.mkdir(parents=True, exist_ok=True)
        tmp = trust.with_name(trust.name + ".rs-tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, trust)
    except OSError as e:
        print(f"sandbox-box: could not write {trust}: {e}", file=sys.stderr)


# The image's denied tools, for pi only (claude's deny is the image's managed
# settings). Read BEFORE pi_trust_workspace(): the host's live re-render keys
# "wired" on trust.json, so a refused render must leave it unwritten.
denied_path = Path("/opt/sandbox-box/denied-tools.json")
denied: dict = {}
if pi_agent_dir and not browser_trusted and denied_path.is_file():
    try:
        denied = json.loads(denied_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"sandbox-box: denied-tools.json unreadable: {e}; refusing to start",
              file=sys.stderr)
        sys.exit(1)
    if not isinstance(denied, dict) or not all(
            isinstance(v, list) and all(isinstance(t, str) and t for t in v)
            for v in denied.values()):
        print("sandbox-box: denied-tools.json must map each server to a list of "
              "tool names; refusing to start", file=sys.stderr)
        sys.exit(1)

def browser_section() -> str:
    """The tool inventory's "Browser" section in a browser box (the image's
    baked declaration exists), "" otherwise. Restricted names the blocked
    tools from the image's own list, so an older image's shorter list reads
    as what it is. Best-effort: an absent or unreadable list never stops the
    render (the pi refusal above is the only one)."""
    if not restricted_extras_path.is_file():
        return ""
    if browser_trusted:
        return ("\n## Browser\n\nTrusted: every browser tool is available, "
                "for every agent in this box.\n")
    blocked: set = set()
    try:
        data = json.loads(denied_path.read_text())
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    blocked.update(x for x in v if isinstance(x, str) and x)
    except (OSError, UnicodeDecodeError, ValueError):
        pass
    if not blocked:
        return ("\n## Browser\n\nRestricted: the list of blocked browser tools "
                "could not be read.\n")
    names = ", ".join(f"`{x}`" for x in sorted(blocked))
    return ("\n## Browser\n\nRestricted: these browser tools are blocked for "
            f"every agent in this box (a call is refused): {names}.\n")


pi_trust_workspace()
if servers:
    if pi_agent_dir:
        for n, cfg in servers.items():
            if isinstance(cfg, dict):
                cfg.setdefault("exposure", "direct")
                if denied.get(n):
                    te = cfg.get("toolExposure")
                    cfg["toolExposure"] = {**(te if isinstance(te, dict) else {}),
                                           **dict.fromkeys(denied[n], "hidden")}
    write_atomic(mcp_path, json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n")
    if pi_agent_dir:
        pi_link_set()
    else:
        pi_link_clear()
    rows = []
    for n, cfg in sorted(servers.items()):
        t = cfg.get("type", "?") if isinstance(cfg, dict) else "?"
        loc = cfg.get("url", "(stdio)") if isinstance(cfg, dict) else "?"
        rows.append(f"| `{n}` | {t} | {loc} |")
    write_atomic(
        inv_path,
        f"# Tools wired into this box\n\n"
        f"Rendered at boot from .mcp-proxy.json (project MCPs) + image-baked + preset tools.\n"
        f"claude auto-discovers /workspace/.mcp.json"
        + (f" (pi reads it through /workspace/.pi/mcp.json)" if pi_agent_dir else "")
        + f" — call tools by name. /workspace/.mcp.json is rewritten at every "
        f"boot: a server added to it"
        + (f" (also by `pi mcp add -l`, which writes through the link)" if pi_agent_dir else "")
        + f" lasts until the box restarts.\n\n"
        f"| Name | Type | Location |\n|---|---|---|\n" + "\n".join(rows) + "\n"
        + browser_section())
else:
    for p in (mcp_path, inv_path):
        if p.exists():
            p.unlink()
    pi_link_clear()
