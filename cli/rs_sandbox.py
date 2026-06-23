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
here — it is the project-wide router policy (a sandbox project defaults to
``locked``: 80/443/53 + ICMP, RFC1918 blocked — usable but contained).

Self-contained by necessity (only this file is baked, like rs-worker), so the
box-pool bounds + sandbox.json shape are duplicated here. Stdlib only; shells
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
SANDBOX_JSON = ORCH / "sandbox.json"
PROJECT_JSON = ORCH / "project.json"

INNER_NETWORK = "rs-inner"
# Management supervisor stages the dist here at create; RO copy-source the box's
# entrypoint cp's into its own ~/.local (no bake; STAGE_AGENT_DIST slice 2).
AGENT_DIST_MOUNT = "/opt/agent-dist"
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

  rs-sandbox create [name] [--browser]  spin a blank box (auto-named box-N);
                                        --browser bundles playwright + Chromium
  rs-sandbox list [--json]    show boxes (+ any baked sandboxes) and their state
  rs-sandbox stop <name>      stop a box (keeps its workspace; start to resume)
  rs-sandbox start <name>     (re)start a stopped box from its saved entry
  rs-sandbox discard <name>   stop the box AND wipe its workspace

Boxes carry NO credentials — run `claude` then /login inside one if you need an
LLM. Outbound network is the project's router policy (sandbox projects default
to 'locked': 80/443/53 + ping only). This Management shell has authority over
every box, so it deliberately runs no agent — never paste box artifacts into
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


def _require_sandbox_project() -> None:
    """rs-sandbox is only meaningful in a --type sandbox project."""
    try:
        ptype = json.loads(PROJECT_JSON.read_text()).get("type")
    except (OSError, json.JSONDecodeError):
        ptype = None
    if ptype != "sandbox":
        die("not a sandbox-flavor project (.orchestrator/project.json type != "
            "'sandbox'). Create one with "
            "`research project create <name> --type sandbox`.")


# --- sandbox.json -----------------------------------------------------------


def load() -> dict[str, dict]:
    if not SANDBOX_JSON.is_file():
        return {}
    try:
        data = json.loads(SANDBOX_JSON.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict[str, dict]) -> None:
    # Atomic-rename; parent dir is bind-mounted, so the write is visible to the
    # host + webui immediately (parent-dir mount, not file).
    ORCH.mkdir(parents=True, exist_ok=True)
    tmp = SANDBOX_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(SANDBOX_JSON)


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


# --- run / teardown ---------------------------------------------------------


def _run_box(name: str, ip: str, browser: bool = False) -> None:
    """docker run a blank box in the local inner dockerd. No mounts beyond the
    box's own workspace, no creds, no repo. ``browser`` selects the
    Chromium-equipped image (the box's claude gets a playwright MCP). The
    workspace dir is pre-created uid-1000-owned so dockerd's auto-create on -v
    doesn't land it root-owned."""
    sub = f"pi-isolated/{name}"
    (WORKSPACE / sub).mkdir(parents=True, exist_ok=True)
    cname = box_container(name)
    image = BOX_IMAGE_BROWSER if browser else BOX_IMAGE
    _docker("rm", "-f", cname)  # idempotent
    # Guard the mount so a missing source can't turn a box run into a cryptic mount
    # error; the box's entrypoint absence-guard surfaces it as "claude not found".
    agent_mount = (["-v", f"{AGENT_DIST_MOUNT}:{AGENT_DIST_MOUNT}:ro"]
                   if os.path.isdir(AGENT_DIST_MOUNT) else [])
    r = _docker(
        "run", "-d",
        "--name", cname,
        "--network", INNER_NETWORK,
        "--ip", ip,
        "--restart", "unless-stopped",
        "-v", f"{WORKSPACE}/{sub}:/workspace",
        *agent_mount,
        "-e", f"RS_SANDBOX_NAME={name}",
        "--label", "research.sandbox=1",
        "--label", f"research.box={name}",
        image,
    )
    if r.returncode != 0:
        die(f"docker run failed for box {name!r}:\n"
            f"{(r.stderr or r.stdout).strip()}")


def _box_entry(entries: dict[str, dict], name: str) -> dict:
    """Fetch a kind="sandbox" box entry or die (used by stop/start/discard,
    which only act on boxes this CLI owns — not baked/byo sandboxes)."""
    entry = entries.get(name)
    if entry is None or entry.get("kind") != KIND:
        die(f"no sandbox box named {name!r} "
            f"(baked/byo sandboxes are managed with `research project sandbox`)")
    return entry


def cmd_create(args: argparse.Namespace) -> None:
    _require_sandbox_project()
    entries = load()
    name = args.name
    if name is None:
        name = auto_name(entries)
    elif not _NAME_RE.match(name):
        die(f"invalid box name {name!r} (must match {_NAME_RE.pattern})")
    elif name in entries:
        die(f"sandbox {name!r} already exists; discard it or pick another name")
    ip = allocate_ip(entries)
    entries[name] = {"kind": KIND, "ip": ip, "container": box_container(name),
                     "browser": bool(args.browser)}
    save(entries)
    _run_box(name, ip, browser=bool(args.browser))
    print(json.dumps({"name": name, "ip": ip, "browser": bool(args.browser),
                      "container": box_container(name)}, indent=2))


def cmd_restart(args: argparse.Namespace) -> None:
    """Re-run a box from its sandbox.json entry. Called by the host
    _recreate_supervisor restart loop (the inner dockerd is fresh after a
    sysbox recreate) — delegated here so the docker-run logic lives in one
    place. Identical to `start` for a gone container; named `restart` for the
    recreate-loop caller's clarity."""
    _require_sandbox_project()
    entry = _box_entry(load(), args.name)
    _run_box(args.name, entry["ip"], browser=bool(entry.get("browser")))
    print(f"box {args.name!r}: restarted at {entry['ip']}")


def cmd_stop(args: argparse.Namespace) -> None:
    _require_sandbox_project()
    _box_entry(load(), args.name)  # validate it's our box
    r = _docker("stop", box_container(args.name))
    if r.returncode != 0:
        die(f"failed to stop box {args.name!r}: {(r.stderr or r.stdout).strip()}")
    print(f"box {args.name!r}: stopped (workspace preserved; "
          f"`rs-sandbox start {args.name}` to resume)")


def cmd_start(args: argparse.Namespace) -> None:
    """Resume a stopped box, or re-run it from its entry if the container is
    gone (e.g. after a recreate)."""
    _require_sandbox_project()
    entry = _box_entry(load(), args.name)
    cname = box_container(args.name)
    exists = _docker("container", "inspect", cname).returncode == 0
    if exists:
        r = _docker("start", cname)
        if r.returncode != 0:
            die(f"failed to start box {args.name!r}: "
                f"{(r.stderr or r.stdout).strip()}")
    else:
        _run_box(args.name, entry["ip"], browser=bool(entry.get("browser")))
    print(f"box {args.name!r}: started at {entry['ip']}")


def cmd_discard(args: argparse.Namespace) -> None:
    _require_sandbox_project()
    entries = load()
    _box_entry(entries, args.name)
    _docker("rm", "-f", box_container(args.name))
    # Full teardown — a box is disposable: drop the entry AND wipe its workspace.
    del entries[args.name]
    save(entries)
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
        rows.append({"name": name, "kind": e.get("kind"), "ip": e.get("ip"),
                     "browser": bool(e.get("browser")),
                     "state": states.get(cname, "absent")})
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("no sandboxes. create a box: rs-sandbox create [name] [--browser]")
        return
    print(f"{'NAME':<16} {'KIND':<9} {'IP':<16} {'BROWSER':<8} STATE")
    for row in rows:
        print(f"{row['name']:<16} {row['kind'] or '-':<9} "
              f"{row['ip'] or '-':<16} {'yes' if row['browser'] else '-':<8} "
              f"{row['state']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rs-sandbox",
        description="Sandbox-project box lifecycle (runs inside the supervisor).")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="spin a blank isolated box")
    c.add_argument("name", nargs="?", default=None,
                   help="box name (default: auto-named box-N)")
    c.add_argument("--browser", action="store_true",
                   help="bundle a browser: @playwright/mcp + Chromium wired "
                        "into the box's claude (heavier image)")
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
