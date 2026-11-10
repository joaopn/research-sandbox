#!/usr/bin/env python3
"""Render /workspace/.mcp.json (and pi's home MCP settings) inside a sandbox box.

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

In a box that deploys pi (RS_PI_AGENT_DIR set), pi reads the same servers from
its home MCP settings, <agent dir>/mcp.json (read in every folder, no trust
needed). RS merges its entries there by name, each marked "rsManaged": true:
every run replaces exactly the marked entries (its stale names removed), the
user's own entries are kept, and an unmarked entry with an RS server's name is
the user's — left alone and named in a warning. An unreadable or non-object
file is left alone with a warning; the render never fails over pi's file.
Nothing is written into /workspace/.pi/. While the project marks this box's
folder as still holding RS's old files (/opt/rs-box-skills/.old-files), pi's
home file is not written: RS writes no new copy while an old one is there.

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
    half-written. The temp name is unique to this run (the boot's render and a
    live one can overlap; both compute the same result)."""
    tmp = path.with_name(f"{path.name}.rs-tmp.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


pi_agent_dir = os.environ.get("RS_PI_AGENT_DIR") or ""
# RS's mark on its own entries in pi's home MCP settings.
RS_MARK = "rsManaged"
# The mark the project's MCP gateway sync (agent/rs-mcp-sync.py) puts on the
# gateway servers it adds to the same file through `pi mcp add`: a header.
# Those entries are that writer's alone — never pruned, replaced or rewritten
# here, whatever else they carry. MIRROR of rs-mcp-sync's MARK_HEADER (pinned).
GATEWAY_MARK_HEADER = "X-RS-MCP"


def is_gateway(entry) -> bool:
    headers = entry.get("headers") if isinstance(entry, dict) else None
    return isinstance(headers, dict) and any(
        isinstance(h, str) and h.lower() == GATEWAY_MARK_HEADER.lower() for h in headers)
old_files_mark = Path("/opt/rs-box-skills/.old-files")


def pi_home_merge(rs_servers: dict) -> bool:
    """Merge RS's servers into pi's home mcp.json by name: drop every entry
    RS marked before, add each of `rs_servers` marked, keep everything else.
    A gateway entry (the project's MCP gateway sync marks it with its header)
    is never dropped or replaced, even if it also carried this mark. An
    unmarked entry with an RS server's name is the user's or the gateway's:
    kept, RS's is not added, and the name is warned about (a gateway clash is
    also reported by the gateway sync, as a conflict, at its next run: the
    server's name is declared in /workspace/.mcp.json). No file and nothing to add: no
    file is made. Returns True when the file now holds exactly RS's current
    entries (nothing to do counts); False, with a warning, when it was left
    alone. Never raises."""
    path = Path(pi_agent_dir) / "mcp.json"
    data: dict = {}
    try:
        if path.is_symlink() or path.exists():
            data = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as e:
        print(f"sandbox-box: pi's MCP settings {path} could not be read ({e}); "
              f"left alone, pi does not see the box's MCP servers", file=sys.stderr)
        return False
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        print(f"sandbox-box: pi's MCP settings {path} are not the expected shape; "
              f"left alone, pi does not see the box's MCP servers", file=sys.stderr)
        return False
    current = data.get("mcpServers", {})
    merged = {n: c for n, c in current.items()
              if is_gateway(c) or not (isinstance(c, dict) and c.get(RS_MARK) is True)}
    clashes = sorted(n for n in rs_servers if n in merged and not is_gateway(merged[n]))
    gateway = sorted(n for n in rs_servers if n in merged and is_gateway(merged[n]))
    for n, cfg in rs_servers.items():
        if n in merged:
            continue
        merged[n] = {**(cfg if isinstance(cfg, dict) else {}), RS_MARK: True}
    if clashes:
        print(f"sandbox-box: pi's MCP settings already hold your own "
              f"{', '.join(map(repr, clashes))}; kept, so pi does not get the "
              f"box's server(s) of that name", file=sys.stderr)
    if gateway:
        print(f"sandbox-box: pi's MCP settings already hold the project's MCP "
              f"gateway server(s) {', '.join(map(repr, gateway))}, wired from the MCP "
              f"page; kept, so pi does not get the box's own server(s) of that name",
              file=sys.stderr)
    if merged == current and (path.exists() or not merged):
        return True
    data["mcpServers"] = merged
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(data, indent=2) + "\n")
    except OSError as e:
        print(f"sandbox-box: could not write pi's MCP settings {path}: {e}",
              file=sys.stderr)
        return False
    return True


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
    except (OSError, ValueError, RecursionError) as e:
        print(f"sandbox-box: leaving {trust} alone ({e})", file=sys.stderr)
        return
    if "/workspace" in data:
        return
    data["/workspace"] = True
    try:
        trust.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(trust, json.dumps(data, indent=2) + "\n")
    except OSError as e:
        print(f"sandbox-box: could not write {trust}: {e}", file=sys.stderr)


# The image's denied tools, for pi only (claude's deny is the image's managed
# settings). Read BEFORE pi's home settings and trust record are written: the
# host's live re-render keys "wired" on trust.json, so a refused render must
# leave it unwritten.
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


def pi_servers() -> dict:
    """RS's servers as pi reads them: each with `exposure: direct` and, in a
    restricted browser box, the denied tools hidden — copies, so
    /workspace/.mcp.json (claude's) carries none of pi's keys."""
    out: dict = {}
    for n, cfg in servers.items():
        c = dict(cfg) if isinstance(cfg, dict) else {}
        c.setdefault("exposure", "direct")
        if denied.get(n):
            te = c.get("toolExposure")
            c["toolExposure"] = {**(te if isinstance(te, dict) else {}),
                                 **dict.fromkeys(denied[n], "hidden")}
        out[n] = c
    return out


if servers:
    write_atomic(mcp_path, json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n")
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
        + (f"; pi reads the same servers from its own MCP settings in its home "
           f"folder (`mcp.json` in pi's agent folder), where RS's entries carry "
           f"`\"rsManaged\": true` and are replaced at every boot while your own "
           f"entries are kept" if pi_agent_dir else "")
        + f" — call tools by name. /workspace/.mcp.json is rewritten at every "
        f"boot: a server added to it lasts until the box restarts.\n\n"
        f"| Name | Type | Location |\n|---|---|---|\n" + "\n".join(rows) + "\n"
        + browser_section())
else:
    for p in (mcp_path, inv_path):
        if p.exists():
            p.unlink()
# pi's home settings, then its trust record — only once the merge succeeded
# (the trust record is what the host reads as "wired"). Never while the box's
# folder still holds RS's old files. Never a reason for a non-zero exit.
if pi_agent_dir and not old_files_mark.exists():
    if pi_home_merge(pi_servers()):
        pi_trust_workspace()
