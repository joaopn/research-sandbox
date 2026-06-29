#!/opt/conda/bin/python
"""rs-sandbox — sandbox-project box lifecycle, inside the supervisor.

The agent-less "sandbox project" flavor (STAGE_SANDBOX_PROJECT.md) is a
collection of blank, isolated boxes for running un-vetted code (e.g.
security-checking a repo before cloning it into a real project). This CLI is
the single owner of that box lifecycle: it runs INSIDE the substrate (baked
into the supervisor image, like ``rs-worker``), talks to the local inner
Docker daemon, and is driven by a human in the non-agent **Management** tab —
never by an LLM. That is the authority-without-agency invariant: the surface
that can create/discard boxes and read their artifacts holds authority over
everything, so it must hold zero agency.

Boxes run the clean ``rs-sandbox-box`` image (FROM rs-analysis-base — python,
claude, byobu, git, ping; NO PI artifact-contract). They are auth-free (run
``claude`` + ``/login`` inside one if you want an LLM). Egress is NOT gated
here — it is the project-wide router policy (a sandbox-dind project defaults to
``locked``: 80/443/53 + ICMP, RFC1918 blocked — usable but contained).

Self-contained by necessity (only this file is baked, like rs-worker), so the
box-pool bounds + extensions.json shape are duplicated here. Stdlib only; shells
out to the ``docker`` CLI (static-IP pinning is awkward through docker-py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

WORKSPACE = Path(os.environ.get("RS_WORKSPACE", "/workspace"))
ORCH = WORKSPACE / ".orchestrator"
EXTENSIONS_JSON = ORCH / "extensions.json"
PROJECT_JSON = ORCH / "project.json"
# The host stages the resolved box-preset catalog here (cli/box_catalog.load_catalog,
# STAGE_BOX_EXT_UX) so this in-supervisor CLI resolves presets offline; refreshed at
# every box_add (Q1→live: operator-registered types usable on existing projects).
BOX_CATALOG_JSON = ORCH / "box-catalog.json"
# The per-project MCP allowlist (rscore.project_allowlist_path) — the supervisor's
# own .orchestrator/, readable here (rs-sandbox runs in the supervisor). Source for
# a box's proxy MCP wiring.
MCP_ALLOW_JSON = ORCH / "mcp-allow.json"

INNER_NETWORK = "rs-inner"
# Management supervisor stages the dist here at create; RO copy-source the box's
# entrypoint cp's into its own ~/.local (no bake; STAGE_AGENT_DIST slice 2).
AGENT_DIST_MOUNT = "/opt/agent-dist"
# Editor (code-server) dist (STAGE_EDITOR_DIST), staged the same way. Re-declared
# (this in-supervisor CLI can't import rscore — drags docker/yaml in), mirroring
# AGENT_DIST_MOUNT. The box's entrypoint deploys it only when RS_SERVICE_CODE_SERVER
# is enabled (forwarded from the project's flag) AND the mount is populated.
EDITOR_DIST_MOUNT = "/opt/editor-dist"
BOX_IMAGE = "rs-sandbox-box:latest"
# Browser variant: + @playwright/mcp + Chromium, wired into the box's claude.
BOX_IMAGE_BROWSER = "rs-sandbox-box-browser:latest"
IP_PREFIX = "192.168.99."
# Box IP pool — the .14-.25 PI sub-range (shared with BYO sandboxes; the whole
# .10-.25 PI range is ACCEPTed by the inner firewall, so no per-box rule).
BOX_IP_LO = 14
BOX_IP_HI = 25  # inclusive
KIND = "sandbox"

# Box names: lowercase, must match the webui tab-id regex so the tab
# synthesizer renders a tab for them. Auto-named box-N.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_AUTO_RE = re.compile(r"^box-(\d+)$")

CHEATSHEET = """\
rs-sandbox — isolated boxes for running un-vetted code

  rs-sandbox create [name] [--preset TYPE] [--agent claude|none] [--editor]
                           [--mcps a,b] [--repo URL --ref REF --setup CMD]
                              spin a box (auto-named box-N). --preset picks the box
                              type (empty, websearcher, data-wrangler, byo, or an
                              operator-registered type); the agent defaults per
                              preset; --mcps wires project MCPs (forces the agent on);
                              --repo/--ref/--setup seed a byo box from a repo.
  rs-sandbox list [--json]    show boxes (+ any baked extensions) and their state
  rs-sandbox stop <name>      stop a box (keeps its workspace; start to resume)
  rs-sandbox start <name>     (re)start a stopped box from its saved entry
  rs-sandbox discard <name>   stop the box AND wipe its workspace

Boxes are auth-free: an agent box still has NO credentials — run `claude` then
/login inside. Outbound network is the project's router policy (sandbox projects
default to 'locked': 80/443/53 + ping only). This Management shell has authority
over every box, so it deliberately runs no agent — never paste box artifacts into
an LLM here.
"""


def die(msg: str) -> NoReturn:
    print(f"rs-sandbox: {msg}", file=sys.stderr)
    raise SystemExit(1)


def box_container(name: str) -> str:
    # iso- family: reuses the webui tab synthesis (pi_isolated_service) and the
    # _recreate_supervisor restart delegation, which both key on this name.
    return f"rs-pi-iso-{name}"


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _require_dind_project() -> None:
    """rs-sandbox (the box harness) runs in ANY dind supervisor — research OR
    sandbox-dind (STAGE_DIND_UNIFY, the harness is a standing dind utility now).
    Only the docker containment substrate has no inner dockerd, so reject just that;
    a missing/legacy marker defaults to dind (greenfield). Mirrors the host-side
    `_running_dind_supervisor` gate (substrate, not flavor)."""
    try:
        substrate = json.loads(PROJECT_JSON.read_text()).get("substrate")
    except (OSError, json.JSONDecodeError):
        substrate = None
    if substrate == "docker":
        die("the docker containment substrate has no inner dockerd; the rs-sandbox "
            "box harness is unavailable here (it is a dind feature — research or "
            "sandbox-dind).")


# --- extensions.json --------------------------------------------------------


def load() -> dict[str, dict]:
    if not EXTENSIONS_JSON.is_file():
        return {}
    try:
        data = json.loads(EXTENSIONS_JSON.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict[str, dict]) -> None:
    # Atomic-rename; parent dir is bind-mounted, so the write is visible to the
    # host + webui immediately (parent-dir mount, not file).
    ORCH.mkdir(parents=True, exist_ok=True)
    tmp = EXTENSIONS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(EXTENSIONS_JSON)


# --- allocation -------------------------------------------------------------


def allocate_ip(entries: dict[str, dict]) -> str:
    taken = {e.get("ip") for e in entries.values() if isinstance(e, dict)}
    for octet in range(BOX_IP_LO, BOX_IP_HI + 1):
        ip = f"{IP_PREFIX}{octet}"
        if ip not in taken:
            return ip
    die(f"box IP pool exhausted ({IP_PREFIX}{BOX_IP_LO}-{IP_PREFIX}{BOX_IP_HI}); "
        f"discard an unused box first")


def auto_name(entries: dict[str, dict]) -> str:
    used = {int(m.group(1)) for n in entries if (m := _AUTO_RE.match(n))}
    i = 1
    while i in used:
        i += 1
    return f"box-{i}"


# --- preset catalog + MCP wiring (STAGE_BOX_EXT_UX) -------------------------

# A box that can always be made blank even if the host never staged a catalog.
_EMPTY_PRESET = {"name": "empty", "image": "base", "agent_default": False,
                 "clone": False, "instructions": ""}


def load_box_catalog() -> dict[str, dict]:
    """Resolved box-preset catalog the host stages into .orchestrator/
    (cli/box_catalog.load_catalog → a list). Keyed by name; {} if absent."""
    try:
        data = json.loads(BOX_CATALOG_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, list):
        return {e["name"]: e for e in data
                if isinstance(e, dict) and isinstance(e.get("name"), str)}
    return {}


def resolve_preset(name: str) -> dict:
    """Resolve a preset name against the staged catalog. 'empty' always resolves
    (blank-box floor) even with no staged catalog; anything else must be staged."""
    catalog = load_box_catalog()
    if name in catalog:
        return catalog[name]
    if name == "empty":
        return _EMPTY_PRESET
    die(f"unknown box preset {name!r} (available: {sorted(catalog) or ['empty']})")


def _parse_csv(s: str | None) -> list[str]:
    return [t.strip() for t in (s or "").split(",") if t.strip()]


def _build_proxy_mcps(mcps: list[str], *, strict: bool) -> dict:
    """{name: server-cfg} proxy entries for the selected MCPs, resolved against
    the supervisor's mcp-allow.json — source-1 for the box's .mcp.json. ``strict``
    (create): die on an MCP not in the allowlist; non-strict (restart re-render):
    skip it with a warning, so a since-de-allowed MCP can't break a recreate."""
    allow: dict[str, dict] = {}
    try:
        rows = json.loads(MCP_ALLOW_JSON.read_text())
        if isinstance(rows, list):
            for e in rows:
                if isinstance(e, dict) and isinstance(e.get("name"), str):
                    allow[e["name"]] = e
    except (OSError, json.JSONDecodeError):
        pass
    servers: dict[str, dict] = {}
    for name in mcps:
        e = allow.get(name)
        if not e:
            if strict:
                die(f"MCP {name!r} is not allowed for this project; allow it "
                    f"first (`research project mcp allow ...`) or omit it")
            print(f"rs-sandbox: warning: MCP {name!r} no longer allowed; "
                  f"dropping it from the box", file=sys.stderr)
            continue
        path = e.get("path") or "/mcp"
        server: dict = {"type": "http", "url": f"http://mcp-proxy:8888/{name}{path}"}
        headers = e.get("headers")
        if isinstance(headers, dict) and headers:
            server["headers"] = headers
        servers[name] = server
    return servers


def _stage_box_workspace(name: str, preset: dict, mcps: list[str],
                         *, strict: bool) -> None:
    """Write the box's CLAUDE.md (instructions) + .mcp-proxy.json (the stable
    proxy source-1) into the box's workspace dir BEFORE the container starts —
    the entrypoint reads them at boot. CLAUDE.md is first-boot no-clobber (a PI
    edit survives a restart); .mcp-proxy.json is overwritten every create/restart
    so an allowlist change re-renders. The entrypoint regenerates /workspace/.mcp.json
    wholesale from .mcp-proxy.json + the image-baked stdio MCPs (idempotent across
    reboots — a fresh proxy-only source each boot)."""
    ws = WORKSPACE / f"pi-isolated/{name}"
    ws.mkdir(parents=True, exist_ok=True)
    instr = (preset.get("instructions") or "").strip()
    claude_md = ws / "CLAUDE.md"
    if instr and not claude_md.exists():
        claude_md.write_text(instr + "\n")
    # bypassPermissions for the box editor's in-IDE claude — the VS Code Claude
    # extension reads PROJECT settings from the open folder's .claude/, not just
    # ~/.claude. No hooks (the box is the security boundary); no-clobber.
    settings = ws / ".claude" / "settings.json"
    if not settings.exists():
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(
            {"permissions": {"defaultMode": "bypassPermissions"}, "theme": "dark"},
            indent=2) + "\n")
    servers = _build_proxy_mcps(mcps, strict=strict)
    (ws / ".mcp-proxy.json").write_text(
        json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n")


# --- run / teardown ---------------------------------------------------------


def _run_box(name: str, ip: str, *, browser: bool = False, agent: str = "none",
             editor: bool = False, clone_repo: str = "", clone_ref: str = "",
             clone_setup: str = "") -> None:
    """docker run a box in the local inner dockerd. ``browser`` selects the
    Chromium-equipped image; ``agent`` (claude|none) → RS_BOX_AGENT (entrypoint
    deploys claude only for "claude", still auth-free); ``editor`` → the box's OWN
    RS_SERVICE_CODE_SERVER (box-level toggle, default off — decoupled from the
    project's editor); ``clone_*`` (BYO) → RS_BOX_CLONE_* the entrypoint clones +
    runs (as box-shell env argv, never a host shell). The workspace dir is
    pre-staged + uid-1000-owned (see _stage_box_workspace) so dockerd's auto-create
    on -v doesn't land it root-owned."""
    sub = f"pi-isolated/{name}"
    (WORKSPACE / sub).mkdir(parents=True, exist_ok=True)
    cname = box_container(name)
    image = BOX_IMAGE_BROWSER if browser else BOX_IMAGE
    _docker("rm", "-f", cname)  # idempotent
    # Guard the mount so a missing source can't turn a box run into a cryptic mount
    # error; the box's entrypoint absence-guard surfaces it as "claude not found".
    agent_mount = (["-v", f"{AGENT_DIST_MOUNT}:{AGENT_DIST_MOUNT}:ro"]
                   if (agent == "claude" and os.path.isdir(AGENT_DIST_MOUNT)) else [])
    # Editor dist mount, gated on the box's OWN editor toggle (STAGE_EDITOR_DIST +
    # STAGE_BOX_EXT_UX). Only mount when this box opted in AND the dist is staged.
    editor_mount = (["-v", f"{EDITOR_DIST_MOUNT}:{EDITOR_DIST_MOUNT}:ro"]
                    if (editor and os.path.isdir(EDITOR_DIST_MOUNT)) else [])
    clone_env: list[str] = []
    if clone_repo:
        clone_env = ["-e", f"RS_BOX_CLONE_REPO={clone_repo}",
                     "-e", f"RS_BOX_CLONE_REF={clone_ref}",
                     "-e", f"RS_BOX_CLONE_SETUP={clone_setup}"]
    r = _docker(
        "run", "-d",
        "--name", cname,
        "--network", INNER_NETWORK,
        "--ip", ip,
        "--restart", "unless-stopped",
        "-v", f"{WORKSPACE}/{sub}:/workspace",
        *agent_mount,
        *editor_mount,
        "-e", f"RS_SERVICE_CODE_SERVER={'enabled' if editor else 'disabled'}",
        "-e", f"RS_SANDBOX_NAME={name}",
        "-e", f"RS_BOX_AGENT={agent}",
        *clone_env,
        "--label", "research.sandbox=1",
        "--label", f"research.box={name}",
        image,
    )
    if r.returncode != 0:
        die(f"docker run failed for box {name!r}:\n"
            f"{(r.stderr or r.stdout).strip()}")


def _rerun_box(name: str, entry: dict) -> None:
    """Re-run a box from its saved entry (restart/start after a recreate). Re-
    renders the proxy MCP source (allowlist may have changed) non-strictly, then
    re-runs from the stored axes. CLAUDE.md persists on the volume (no-clobber)."""
    mcps = [m for m in (entry.get("upstream_mcps") or []) if isinstance(m, str)]
    ws = WORKSPACE / f"pi-isolated/{name}"
    ws.mkdir(parents=True, exist_ok=True)
    servers = _build_proxy_mcps(mcps, strict=False)
    (ws / ".mcp-proxy.json").write_text(
        json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n")
    _run_box(name, entry["ip"], browser=bool(entry.get("browser")),
             agent=entry.get("agent", "none"), editor=bool(entry.get("editor")),
             clone_repo=entry.get("repo") or "", clone_ref=entry.get("ref") or "",
             clone_setup=entry.get("setup") or "")


def _box_entry(entries: dict[str, dict], name: str) -> dict:
    """Fetch a kind="sandbox" box entry or die (used by stop/start/discard,
    which only act on boxes this CLI owns — not baked/byo sandboxes)."""
    entry = entries.get(name)
    if entry is None or entry.get("kind") != KIND:
        die(f"no sandbox box named {name!r} "
            f"(baked/byo sandboxes are managed with `research project extension`)")
    return entry


def cmd_create(args: argparse.Namespace) -> None:
    _require_dind_project()
    entries = load()
    name = args.name
    if name is None:
        name = auto_name(entries)
    elif not _NAME_RE.match(name):
        die(f"invalid box name {name!r} (must match {_NAME_RE.pattern})")
    elif name in entries:
        die(f"sandbox {name!r} already exists; discard it or pick another name")
    preset = resolve_preset(args.preset)
    is_clone = bool(preset.get("clone"))
    repo, ref, setup = (args.repo or "").strip(), (args.ref or "").strip(), \
        (args.setup or "").strip()
    if (repo or setup) and not is_clone:
        die(f"--repo/--setup are only valid for a clone (BYO) preset; "
            f"preset {args.preset!r} does not clone")
    # Agent: explicit override, else the preset default; selecting any MCP forces
    # the agent on (nothing else can reach an MCP — STAGE_BOX_EXT_UX D-B).
    mcps = _parse_csv(args.mcps)
    agent = args.agent or ("claude" if preset.get("agent_default") else "none")
    if mcps:
        agent = "claude"
    browser = preset.get("image") == "browser"
    editor = bool(args.editor)
    ip = allocate_ip(entries)
    # Stage CLAUDE.md + .mcp-proxy.json BEFORE the run (M2: the entrypoint reads
    # them at boot). strict=True → die on an MCP not in the project allowlist.
    _stage_box_workspace(name, preset, mcps, strict=True)
    entry = {"kind": KIND, "ip": ip, "container": box_container(name),
             "preset": args.preset, "browser": browser, "agent": agent,
             "editor": editor, "upstream_mcps": mcps}
    if is_clone:
        entry.update({"repo": repo, "ref": ref, "setup": setup})
    entries[name] = entry
    save(entries)
    _run_box(name, ip, browser=browser, agent=agent, editor=editor,
             clone_repo=repo if is_clone else "", clone_ref=ref, clone_setup=setup)
    print(json.dumps({"name": name, "ip": ip, "preset": args.preset,
                      "browser": browser, "agent": agent, "editor": editor,
                      "container": box_container(name)}, indent=2))


def cmd_restart(args: argparse.Namespace) -> None:
    """Re-run a box from its extensions.json entry. Called by the host
    _recreate_supervisor restart loop (the inner dockerd is fresh after a
    sysbox recreate) — delegated here so the docker-run logic lives in one
    place. Identical to `start` for a gone container; named `restart` for the
    recreate-loop caller's clarity."""
    _require_dind_project()
    entry = _box_entry(load(), args.name)
    _rerun_box(args.name, entry)
    print(f"box {args.name!r}: restarted at {entry['ip']}")


def cmd_stop(args: argparse.Namespace) -> None:
    _require_dind_project()
    _box_entry(load(), args.name)  # validate it's our box
    r = _docker("stop", box_container(args.name))
    if r.returncode != 0:
        die(f"failed to stop box {args.name!r}: {(r.stderr or r.stdout).strip()}")
    print(f"box {args.name!r}: stopped (workspace preserved; "
          f"`rs-sandbox start {args.name}` to resume)")


def cmd_start(args: argparse.Namespace) -> None:
    """Resume a stopped box, or re-run it from its entry if the container is
    gone (e.g. after a recreate)."""
    _require_dind_project()
    entry = _box_entry(load(), args.name)
    cname = box_container(args.name)
    exists = _docker("container", "inspect", cname).returncode == 0
    if exists:
        r = _docker("start", cname)
        if r.returncode != 0:
            die(f"failed to start box {args.name!r}: "
                f"{(r.stderr or r.stdout).strip()}")
    else:
        _rerun_box(args.name, entry)
    print(f"box {args.name!r}: started at {entry['ip']}")


def cmd_discard(args: argparse.Namespace) -> None:
    _require_dind_project()
    entries = load()
    _box_entry(entries, args.name)
    _docker("rm", "-f", box_container(args.name))
    # Drop the entry (box is gone either way). --keep-workspace leaves the
    # box's artifacts on disk under pi-isolated/<name>/ for later retrieval;
    # the default is full teardown — a box is disposable.
    del entries[args.name]
    save(entries)
    if args.keep_workspace:
        print(f"box {args.name!r}: discarded (workspace preserved at "
              f"pi-isolated/{args.name}/)")
    else:
        shutil.rmtree(WORKSPACE / f"pi-isolated/{args.name}", ignore_errors=True)
        print(f"box {args.name!r}: discarded")


def cmd_list(args: argparse.Namespace) -> None:
    # Show every sandbox in the project — boxes (kind="sandbox") AND any baked
    # sandbox (e.g. websearcher), so the Management surface sees them all.
    entries = {n: e for n, e in load().items() if isinstance(e, dict)}
    states: dict[str, str] = {}
    r = _docker("ps", "-a", "--format", "{{.Names}}\t{{.State}}")
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            if "\t" in line:
                cn, _, st = line.partition("\t")
                states[cn.strip()] = st.strip()
    rows = []
    for name, e in sorted(entries.items()):
        cname = e.get("container") or box_container(name)
        # The agent axis only applies to kind="sandbox" boxes; baked/byo
        # sandboxes carry no agent key, so render their column as `-`.
        agent = e.get("agent", "none") if e.get("kind") == KIND else None
        rows.append({"name": name, "kind": e.get("kind"), "ip": e.get("ip"),
                     "browser": bool(e.get("browser")), "agent": agent,
                     "state": states.get(cname, "absent")})
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("no sandboxes. create a box: "
              "rs-sandbox create [name] [--preset TYPE] [--agent claude]")
        return
    print(f"{'NAME':<16} {'KIND':<9} {'IP':<16} {'BROWSER':<8} {'AGENT':<8} STATE")
    for row in rows:
        print(f"{row['name']:<16} {row['kind'] or '-':<9} "
              f"{row['ip'] or '-':<16} {'yes' if row['browser'] else '-':<8} "
              f"{row['agent'] or '-':<8} {row['state']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rs-sandbox",
        description="Sandbox-project box lifecycle (runs inside the supervisor).")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="spin an isolated box")
    c.add_argument("name", nargs="?", default=None,
                   help="box name (default: auto-named box-N)")
    c.add_argument("--preset", default="empty",
                   help="box type: empty, websearcher, data-wrangler, byo, or an "
                        "operator-registered type (default empty)")
    c.add_argument("--agent", choices=["claude", "none"], default=None,
                   help="override the preset's agent default; 'claude' cp's the "
                        "binary in (still auth-free — run `claude` + /login inside)")
    c.add_argument("--editor", action="store_true",
                   help="bundle the code-server editor into this box")
    c.add_argument("--mcps", default="",
                   help="comma-separated project MCP names to wire into the box "
                        "(forces the agent on)")
    c.add_argument("--repo", default="",
                   help="(byo preset) git repo URL to clone into the box at boot")
    c.add_argument("--ref", default="",
                   help="(byo preset) git ref to check out")
    c.add_argument("--setup", default="",
                   help="(byo preset) setup command to run in the clone")
    c.set_defaults(func=cmd_create)

    lst = sub.add_parser("list", help="list sandboxes (boxes + baked)")
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_list)

    sp = sub.add_parser("stop", help="stop a box (keeps its workspace)")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_stop)

    sr = sub.add_parser("start", help="(re)start a stopped box")
    sr.add_argument("name")
    sr.set_defaults(func=cmd_start)

    d = sub.add_parser("discard", help="stop a box and wipe its workspace")
    d.add_argument("name")
    d.add_argument("--keep-workspace", action="store_true",
                   help="remove the box but leave its workspace artifacts on disk")
    d.set_defaults(func=cmd_discard)

    rt = sub.add_parser("restart", help="re-run a box from its saved entry")
    rt.add_argument("name")
    rt.set_defaults(func=cmd_restart)
    return p


def main() -> None:
    # Bare invocation (the Management tab's spawn line) prints the cheatsheet.
    if len(sys.argv) == 1:
        print(CHEATSHEET)
        return
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
