#!/usr/bin/env python3
"""research — CLI for Research Sandbox.

Stdlib only. Subcommand layout:

    research start                     # start shared infra (router)
    research stop                      # stop shared infra
    research project create <name> [--data PATHS] [--profile python]
                                   [--dind auto|sysbox|privileged]
                                   [--memory SIZE] [--cpus N]
                                   [--egress open|locked] [--ssh-port N]
    research project attach  <name>
    research project list
    research project status  <name>
    research project stop    <name> | --all
    research project start   <name> | --all
    research project destroy <name>
    research project ssh     <name>

State is stored in ``.env`` beside this script, plus Docker labels on the
supervisor container (``sandbox.project=<name>``). Per-project host
directory ``<PROJECTS_DIR>/<name>/workspace/`` is bind-mounted into the
supervisor's ``/workspace``; named volume ``rs-docker-<name>`` holds the
inner Docker daemon state when ``--dind privileged`` is used (sysbox keeps
it internal to the container).
"""

from __future__ import annotations

import argparse
import base64
import datetime
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# Make cli/ helpers importable.
sys.path.insert(0, str(Path(__file__).resolve().parent / "cli"))
import defaults  # noqa: E402  (host-side default-enablement for worker + sandbox)
import mcp_registry  # noqa: E402
import pi_isolated_registry  # noqa: E402  (BYO sandbox type registry; sub-component of sandbox)
import role_mcp  # noqa: E402
import sandbox  # noqa: E402  (unified PI-driven container surface; absorbs former pi + pi_isolated)

# --- lifecycle core ---------------------------------------------------------
# rscore holds the validated request objects + the lifecycle verbs (one
# implementation shared by the CLI and the future browser surface). The
# star-import re-binds the relocated substrate helpers so the cmd_* below
# resolve them unchanged.
import rscore  # noqa: E402
from rscore import *  # noqa: E402,F401,F403



def confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip() == "yes"
    except EOFError:
        return False


def cmd_images_versions(args: argparse.Namespace) -> None:
    """Print the current image version pins from versions.env."""
    pins = load_versions()
    if not pins:
        die(f"no pins found at {VERSIONS_FILE}")
    width = max(len(k) for k in pins)
    for key, value in pins.items():
        print(f"{key.ljust(width)}  {value}")


def cmd_images_outdated(args: argparse.Namespace) -> None:
    """Query each pin's upstream and report current vs latest. Pull-model
    freshness check — no PR noise, run when a bump is being considered."""
    pins = load_versions()
    if not pins:
        die(f"no pins found at {VERSIONS_FILE}")
    width = max(len(k) for k in pins)
    cur_w = max((len(v) for v in pins.values()), default=7)
    print(f"{'PIN'.ljust(width)}  {'CURRENT'.ljust(cur_w)}  "
          f"{'LATEST'.ljust(cur_w)}  STATUS")
    any_update = False
    for key, current in pins.items():
        source = VERSION_SOURCES.get(key)
        if source is None:
            print(f"{key.ljust(width)}  {current.ljust(cur_w)}  "
                  f"{'?'.ljust(cur_w)}  no source configured")
            continue
        if source["kind"] == "manual":
            print(f"{key.ljust(width)}  {current.ljust(cur_w)}  "
                  f"{'manual'.ljust(cur_w)}  check: {source['url']}")
            continue
        try:
            latest = _latest_version(source)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            print(f"{key.ljust(width)}  {current.ljust(cur_w)}  "
                  f"{'?'.ljust(cur_w)}  unreachable: {e}")
            continue
        if latest == current:
            status = "up to date"
        else:
            status = "UPDATE AVAILABLE"
            any_update = True
            note = source.get("note")
            if note:
                status += f"  ({note})"
        print(f"{key.ljust(width)}  {current.ljust(cur_w)}  "
              f"{latest.ljust(cur_w)}  {status}")
    if any_update:
        print("\nbump a pin in versions.env, then propagate:")
        print("  new projects:      `research start --rebuild`")
        print("  running projects:  `research project update --rebuild <name>` "
              "(recreates the supervisor + re-stages inner images)")


def cmd_start(args: argparse.Namespace) -> None:
    """Bring up shared infra: ensure images + start the router container."""
    _preflight()
    _build_images(force=args.rebuild)
    print("starting router...")
    compose_up = ["docker", "compose", "-f", str(SCRIPT_DIR / "docker-compose.yml"),
                  "up", "-d"]
    # When the user asks for a rebuild, force-rebuild the router image too —
    # otherwise compose reuses the cached image and edits to router/scripts/
    # or router/Dockerfile silently don't reach the running container.
    if args.rebuild:
        compose_up.append("--build")
    compose_up.append("router")
    run_check(compose_up)
    # If the router was recreated (compose --build, or simply a fresh `up`
    # after `stop`), it lost its `docker network connect` attachments to
    # every per-project network. Re-attach so subsequent `project update`
    # calls find rs-router on rs-net-<project>.
    wire_router_to_projects()
    _start_enabled_mcps()
    print("up.")


def cmd_stop(_: argparse.Namespace) -> None:
    """Stop shared infra. Leaves images, volumes, and projects untouched."""
    run_check(["docker", "compose", "-f", str(SCRIPT_DIR / "docker-compose.yml"),
               "stop", "router"])
    print("stopped.")


def _build(reqcls, **kw):
    """Build a validated rscore request from CLI args, mapping the
    input-validation channel (ValidationError) to the terminal's die()."""
    try:
        return reqcls.from_kwargs(**kw)
    except rscore.ValidationError as e:
        die(str(e))


def _print_create_report(res, cfg) -> None:
    """Format rscore.create()'s CreateResult into the terminal report. The
    browser front-end serialises the same dataclass instead."""
    inner_fw = "on" if res.inner_firewall else "off"
    mcps_line = ", ".join(res.mcps) if res.mcps else "(none)"
    role_mcps_line = ", ".join(res.workers) if res.workers else "(none)"
    pi_roles_line = ", ".join(res.sandboxes) if res.sandboxes else "(none)"
    if res.data_mounts:
        data_lines = "\n".join(
            f"             /workspace/shared/data/{b}/  ←  {src}"
            for b, src in sorted(res.data_mounts.items()))
        data_block = f"  Data (RO):\n{data_lines}\n"
    else:
        data_block = ""

    # Webui block — show URL + import string only when rs-webui is up.
    if container_running(WEBUI_CONTAINER):
        webui_bind = read_env_value("WEBUI_BIND") or "127.0.0.1"
        webui_port = read_env_value("WEBUI_PORT") or "7777"
        import_str = _webui_import_string(res.project, res.ssh_password)
        webui_block = (
            f"  Webui:     https://{webui_bind}:{webui_port}\n"
            f"  Import:    {import_str}\n"
        )
    else:
        webui_block = ""

    steps: list[str] = []
    if res.project_type == PROJECT_TYPE_SANDBOX:
        steps.append(
            "  1. Open the Management shell and spin a box:\n"
            f"     `python research.py project attach {res.project}`, then run\n"
            "     `rs-sandbox create` (auto-named) — see `rs-sandbox` for the "
            "cheatsheet.\n"
            "     Boxes are auth-free; run `claude` + /login inside one if you "
            "want an LLM. Management runs no agent by design."
        )
    else:
        steps.append(
            "  1. Authenticate Claude Code inside the supervisor (once per project):\n"
            f"     `python research.py project attach {res.project}`, then in byobu run\n"
            "     `claude` and complete the device-code flow in a local browser."
        )
    steps.append(f"  {len(steps)+1}. Start working on a problem with the supervisor.")
    next_steps = "\n".join(steps)

    print(f"""
Project '{res.project}' is running.

  Container: {res.container}
  Workspace: {res.workspace}
  Network:   {res.network} (egress: {res.egress})
  DIND mode: {res.dind_mode}
  Inner FW:  {inner_fw}
  MCPs:      {mcps_line}
  Role-MCPs: {role_mcps_line}
  PI roles:  {pi_roles_line}
{data_block}  SSH:       research@localhost -p {res.ssh_port}   password: {res.ssh_password}
{webui_block}
Next steps:
{next_steps}
""")


def cmd_project_create(args: argparse.Namespace) -> None:
    cfg = load_config()
    req = _build(
        rscore.CreateRequest,
        name=args.name, type=args.type, egress=args.egress, dind=args.dind,
        profile=args.profile, data=args.data, memory=args.memory,
        cpus=args.cpus, ssh_port=args.ssh_port,
        inner_firewall=args.inner_firewall, enable=args.enable,
        disable=args.disable, role_mcp_upstream=args.role_mcp_upstream,
        mcp=args.mcp,
    )
    res = rscore.create(req, cfg)
    _print_create_report(res, cfg)


def cmd_project_attach(args: argparse.Namespace) -> None:
    container = container_name_for(args.name)
    if not container_running(container):
        die(f"project {args.name!r} is not running (use `research project start` first)")
    # Sandbox-flavor projects: attach lands in the Management console (no
    # agent). Print the rs-sandbox cheatsheet first so SSH/attach matches the
    # webui Management tab. Read the flavor off the container label.
    ptype = run(["docker", "inspect", "-f",
                 f"{{{{index .Config.Labels \"{PROJECT_TYPE_LABEL}\"}}}}", container],
                capture_output=True)
    if ptype.returncode == 0 and ptype.stdout.strip() == PROJECT_TYPE_SANDBOX:
        run(["docker", "exec", container, "rs-sandbox"], capture_output=False)
    # Ensure a session exists (entrypoint creates one, but if byobu was closed…).
    run(["docker", "exec", container, "bash", "-lc",
         "byobu list-sessions 2>/dev/null | grep -q '^main:' || "
         "byobu new-session -d -s main -c /workspace -x 200 -y 50"],
        capture_output=True)
    os.execvp("docker", [
        "docker", "exec", "-it", container,
        "byobu", "attach", "-t", "main",
    ])


def cmd_project_list(_: argparse.Namespace) -> None:
    rows = rscore.list_projects()
    if not rows:
        print("no projects")
        return
    w_name = max(len("PROJECT"), *(len(r.project) for r in rows))
    w_state = max(len("STATE"), *(len(r.state) for r in rows))
    header = f"{'PROJECT':<{w_name}}  {'STATE':<{w_state}}  SSH"
    print(header)
    print("-" * len(header))
    for r in sorted(rows, key=lambda x: x.project):
        print(f"{r.project:<{w_name}}  {r.state:<{w_state}}  {r.ssh or '-'}")


def cmd_project_status(args: argparse.Namespace) -> None:
    req = _build(rscore.StatusRequest, name=args.name)
    s = rscore.status(req)
    print(f"Project:   {s.project}")
    print(f"Container: {s.container}")
    print(f"State:     {s.state}")
    if s.ssh_port:
        print(f"SSH:       localhost:{s.ssh_port}")
    print(f"Workspace: {s.workspace}")
    if s.inner_workers:
        print("\nWorkers (inner docker):")
        for line in s.inner_workers:
            print(f"  {line}")
    if s.registry_count:
        print(f"\nRegistry: {s.registry_count} worker entry(ies) "
              "(see /workspace/.workers/)")


def cmd_project_stop(args: argparse.Namespace) -> None:
    req = _build(rscore.StartStopRequest, name=args.name, all=args.all)
    results = rscore.stop(req)
    if not results:
        print("no projects to act on")
        return
    for r in results:
        if r.outcome == "skip:absent":
            print(f"skip: {r.name} does not exist")
        else:
            print(f"stop: {r.name}")


def cmd_project_start(args: argparse.Namespace) -> None:
    req = _build(rscore.StartStopRequest, name=args.name, all=args.all)
    for r in rscore.start(req):
        if r.outcome == "skip:absent":
            print(f"skip: {r.name} does not exist")
        elif r.outcome == "skip:already":
            print(f"skip: {r.project} already running")
        else:
            print(f"start: {r.project}")


def cmd_project_destroy(args: argparse.Namespace) -> None:
    cfg = load_config()
    req = _build(rscore.DestroyRequest, name=args.name)
    container = container_name_for(req.name)
    if not container_exists(container):
        die(f"project {req.name!r} does not exist")

    project_root = project_root_for(req.name, cfg)
    docker_volume = docker_volume_name_for(req.name)
    network = project_network_for(req.name)

    print(f"=== Destroy: {req.name} ===\n")
    print("This will permanently delete:")
    print(f"  - supervisor container ({container})")
    print(f"  - workspace directory on host: {project_root}")
    print("      (includes .claude/ creds snapshot, plans, all worker outputs)")
    if volume_exists(docker_volume):
        print(f"  - DIND volume ({docker_volume})")
    print(f"  - per-project network ({network})")
    print()
    if not confirm("Type 'yes' to confirm: "):
        print("aborted.")
        return
    rscore.destroy(req, cfg)
    print(f"destroyed {req.name}.")


def cmd_project_ssh(args: argparse.Namespace) -> None:
    container = container_name_for(args.name)
    if not container_running(container):
        die(f"project {args.name!r} is not running")
    port = get_ssh_port(container)
    if not port:
        die("could not resolve SSH port (not published?)")
    print(f"Host:     localhost")
    print(f"Port:     {port}")
    print(f"User:     research")
    print(f"Command:  ssh research@localhost -p {port}")


def cmd_project_update(args: argparse.Namespace) -> None:
    req = _build(
        rscore.UpdateRequest,
        name=args.name, rebuild=args.rebuild, keep_claude=args.keep_claude,
        enable=args.enable, disable=args.disable,
        role_mcp_upstream=args.role_mcp_upstream,
    )
    rscore.update(req)
    print(f"\nproject {req.name!r} updated.")


def cmd_mcp_add(args: argparse.Namespace) -> None:
    entry = _build_mcp_entry(args)
    known_roles = set(role_mcp.ROLE_IMAGES.keys())
    with mcp_registry.lock():
        try:
            data = mcp_registry.load(expand=False, known_roles=known_roles)
        except mcp_registry.RegistryError as e:
            die(str(e))
        if args.name in data["mcps"]:
            die(f"MCP {args.name!r} already registered; remove first")
        data["mcps"][args.name] = entry
        try:
            mcp_registry.save_atomic(data, known_roles=known_roles)
        except mcp_registry.RegistryError as e:
            die(str(e))
    print(f"added MCP {args.name!r} ({entry['kind']})")
    hint = f"  next: `research mcp enable {args.name}`"
    if entry["kind"] == "shared":
        hint += f" then `research mcp start {args.name}`"
    print(hint)


def cmd_mcp_describe(args: argparse.Namespace) -> None:
    new_desc = "" if args.clear else (args.text or "").strip()
    if not args.clear and not new_desc:
        die("description text is required (or pass --clear to remove)")
    with mcp_registry.lock():
        try:
            data = mcp_registry.load(expand=False)
        except mcp_registry.RegistryError as e:
            die(str(e))
        entry = data["mcps"].get(args.name)
        if entry is None:
            die(f"no MCP named {args.name!r}")
        if new_desc:
            entry["description"] = new_desc
        else:
            entry.pop("description", None)
        try:
            mcp_registry.save_atomic(data)
        except mcp_registry.RegistryError as e:
            die(str(e))
    if new_desc:
        print(f"set description for {args.name!r}")
    else:
        print(f"cleared description for {args.name!r}")


def cmd_mcp_set_roles(args: argparse.Namespace) -> None:
    """Replace the MCP's roles list. Empty CSV (``""``) clears it. Roles
    are validated against role_mcp.ROLE_IMAGES at save time, so a typo
    surfaces here rather than as a silent miss in auto-wire derivation."""
    new_roles = _parse_csv_list(args.csv)
    known_roles = set(role_mcp.ROLE_IMAGES.keys())
    unknown = [r for r in new_roles if r not in known_roles]
    if unknown:
        die(f"unknown role(s) {sorted(unknown)}; known: "
            f"{sorted(known_roles) or '(none)'}")
    with mcp_registry.lock():
        try:
            data = mcp_registry.load(expand=False, known_roles=known_roles)
        except mcp_registry.RegistryError as e:
            die(str(e))
        entry = data["mcps"].get(args.name)
        if entry is None:
            die(f"no MCP named {args.name!r}")
        if new_roles:
            entry["roles"] = new_roles
        else:
            entry.pop("roles", None)
        try:
            mcp_registry.save_atomic(data, known_roles=known_roles)
        except mcp_registry.RegistryError as e:
            die(str(e))
    if new_roles:
        print(f"set roles for {args.name!r}: {','.join(new_roles)}")
    else:
        print(f"cleared roles for {args.name!r}")


def cmd_mcp_list(args: argparse.Namespace) -> None:
    try:
        data = mcp_registry.load(expand=False)
    except mcp_registry.RegistryError as e:
        die(str(e))
    if args.json:
        out: dict = {"version": data["version"], "mcps": {}}
        for name, e in data["mcps"].items():
            row = dict(e)
            if e["kind"] == "shared":
                row["running"] = container_running(mcp_container_name_for(name))
            out["mcps"][name] = row
        print(json.dumps(out, indent=2, sort_keys=True))
        return
    rows: list[tuple[str, ...]] = []
    descs: list[str] = []
    for name, e in sorted(data["mcps"].items()):
        kind = e["kind"]
        enabled = "yes" if e.get("enabled", False) else "no"
        if kind == "external":
            target = f"{e.get('host_address', 'host.docker.internal')}:{e['host_port']}"
            status = "-"
        else:
            cname = mcp_container_name_for(name)
            target = f"{cname}:{e['port']}"
            status = "running" if container_running(cname) else "stopped"
        roles_cell = ",".join(sorted(e.get("roles") or [])) or "-"
        rows.append((name, kind, e["transport"], target, enabled, status, roles_cell))
        descs.append(e.get("description", ""))
    if not rows:
        print("(no MCPs registered)")
        return
    headers = ("NAME", "KIND", "TRANSPORT", "TARGET", "ENABLED", "STATUS", "ROLES")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r, d in zip(rows, descs):
        print(fmt.format(*r))
        if d:
            print(f"    {d}")


def cmd_mcp_remove(args: argparse.Namespace) -> None:
    in_use = projects_using_mcp(args.name)
    if in_use and not args.force:
        die(f"MCP {args.name!r} is currently allowed for projects: "
            f"{', '.join(in_use)}.\n"
            f"  Run `research project mcp deny <proj> {args.name}` for each, "
            f"or pass --force.")
    with mcp_registry.lock():
        try:
            data = mcp_registry.load(expand=False)
        except mcp_registry.RegistryError as e:
            die(str(e))
        if args.name not in data["mcps"]:
            die(f"no MCP named {args.name!r}")
        entry = data["mcps"].pop(args.name)
        try:
            mcp_registry.save_atomic(data)
        except mcp_registry.RegistryError as e:
            die(str(e))
    if entry["kind"] == "shared":
        cname = mcp_container_name_for(args.name)
        if container_exists(cname):
            run(["docker", "rm", "-f", cname], capture_output=True)
    print(f"removed MCP {args.name!r}")


def cmd_mcp_enable(args: argparse.Namespace) -> None:
    entry = _set_enabled(args.name, True)
    msg = f"enabled {args.name!r}"
    if entry["kind"] == "shared":
        msg += (f" (use `research mcp start {args.name}` "
                f"or `research start` to launch)")
    print(msg)


def cmd_mcp_disable(args: argparse.Namespace) -> None:
    cfg = load_config()
    affected = projects_using_mcp(args.name)
    if affected and not args.keep_projects:
        denied: list[str] = []
        for project in affected:
            ok, msg = _deny_mcp_for_project(project, cfg, args.name)
            if ok:
                denied.append(project)
            else:
                print(f"warning: deny {args.name!r} from {project!r}: {msg}",
                      file=sys.stderr)
    else:
        denied = []

    entry = _set_enabled(args.name, False)
    suffix = ""
    if entry["kind"] == "shared":
        cname = mcp_container_name_for(args.name)
        if container_running(cname):
            run_check(["docker", "stop", cname])
            suffix = f"; stopped {cname}"

    if affected:
        if args.keep_projects:
            print(f"disabled {args.name!r}{suffix}; --keep-projects: "
                  f"{len(affected)} project(s) still allow it: "
                  f"{', '.join(affected)}")
        else:
            print(f"disabled {args.name!r}{suffix}; denied from "
                  f"{len(denied)} project(s): {', '.join(denied)}")
    else:
        print(f"disabled {args.name!r}{suffix}")


def cmd_worker_list(args: argparse.Namespace) -> None:
    """Host catalog of worker *types*: the always-available analysis worker
    (spawned per-task by the supervisor) + the built-in role-MCP service types
    (enable-able per project). The DEFAULT column marks services flagged for
    auto-enable in new projects (`research worker enable <name>`). Per-project
    lifecycle is `research project worker`."""
    default_on = set(defaults.enabled("worker"))
    cat = [{"name": "analysis", "kind": "task", "default": False,
            "detail": "headless analysis worker (spawned per question by the "
                      "supervisor; no per-project enable)"}]
    for name, image in sorted(role_mcp.ROLE_IMAGES.items()):
        cat.append({"name": name, "kind": "service",
                    "default": name in default_on, "detail": image})
    if args.json:
        print(json.dumps(cat, indent=2, sort_keys=True))
        return
    rows = [(c["name"], c["kind"], "✓" if c["default"] else "-", c["detail"])
            for c in cat]
    headers = ("NAME", "KIND", "DEFAULT", "DETAIL")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))


def cmd_worker_default_enable(args: argparse.Namespace) -> None:
    """Flag a worker service for auto-enable in NEW projects (mirrors
    `mcp enable`). Validates the name is a known service; the always-on
    analysis worker isn't a service and can't be flagged."""
    if args.name not in role_mcp.ROLE_IMAGES:
        die(f"unknown worker service {args.name!r}. Known services: "
            f"{', '.join(sorted(role_mcp.ROLE_IMAGES))}. (The analysis worker "
            f"is always available and isn't a default-enable target.)")
    defaults.set_enabled("worker", args.name, True)
    print(f"worker {args.name!r}: default-enabled (auto-enabled in new "
          f"projects; override per-project with `project create --disable`)")


def cmd_worker_default_disable(args: argparse.Namespace) -> None:
    defaults.set_enabled("worker", args.name, False)
    print(f"worker {args.name!r}: default-disabled (not auto-enabled in new "
          f"projects)")


def cmd_sandbox_add(args: argparse.Namespace) -> None:
    """Register a BYO sandbox type in the host registry. Baked roles
    (echo / wrangler / websearcher) are built-in and need no registration —
    a BYO type may not shadow one."""
    if args.name in sandbox.baked_names():
        die(f"{args.name!r} is a built-in baked sandbox; BYO types can't "
            f"shadow it. Pick a different name.")
    entry: dict = {"root": args.root}
    if args.repo:
        ref = args.ref
        if ref:
            # Explicit ref: verify it resolves (unless suppressed).
            if not args.no_verify:
                _verify_pi_isolated_repo(args.repo, ref)
        else:
            # No ref: resolve the default-branch HEAD now and pin it.
            ref = _resolve_pi_isolated_ref(args.repo)
            print(f"  no --ref given; pinned {args.repo} default-branch HEAD "
                  f"to {ref}")
        entry["repo"] = args.repo
        entry["ref"] = ref
    if args.setup:
        entry["setup"] = args.setup
    if args.mount:
        entry["mount"] = args.mount
    if args.description:
        entry["description"] = args.description.strip()

    with pi_isolated_registry.lock():
        try:
            data = pi_isolated_registry.load(expand=False)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))
        if args.name in data["types"]:
            die(f"sandbox type {args.name!r} already registered; "
                f"remove first or use `sandbox set-root`/`describe`")
        data["types"][args.name] = entry
        try:
            pi_isolated_registry.save_atomic(data)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))
    print(f"added sandbox type {args.name!r} (root {args.root})")
    print(f"  next: `research project create <p> --enable {args.name}` "
          f"(or `project update <p> --enable {args.name}`)")


def cmd_sandbox_list(args: argparse.Namespace) -> None:
    """Full host catalog of sandbox *types*: built-in baked roles + BYO
    registry types. The DEFAULT column marks types flagged for auto-enable in
    new projects (`research sandbox enable <name>`). Visibility surface — what
    can be enabled per project."""
    cat = sandbox.catalog()
    default_on = set(defaults.enabled("sandbox"))
    if args.json:
        for c in cat:
            c["default"] = c["name"] in default_on
        print(json.dumps(cat, indent=2, sort_keys=True))
        return
    if not cat:
        print("(no sandbox types available)")
        return
    # BYO descriptions, fetched once for the annotation lines.
    try:
        byo = pi_isolated_registry.load(expand=False).get("types", {})
    except pi_isolated_registry.RegistryError:
        byo = {}
    rows: list[tuple[str, ...]] = []
    descs: list[str] = []
    for c in cat:
        nm = c["name"]
        if c["kind"] == "baked":
            src = c.get("image") or "-"
            mirror = c.get("mirror_of") or "-"
        else:
            src = c.get("repo") or "(folder-only)"
            mirror = "-"
        # A baked mirror sandbox has no independent default — it comes up iff
        # its worker twin does. echo (no twin) + BYO use the sandbox default set.
        if c["kind"] == "baked" and c.get("mirror_of"):
            default_col = "same as worker"
        else:
            default_col = "✓" if nm in default_on else "-"
        rows.append((nm, c["kind"], default_col, src, mirror))
        descs.append(byo.get(nm, {}).get("description", "")
                     if c["kind"] == "byo" else "")
    headers = ("NAME", "KIND", "DEFAULT", "IMAGE/REPO", "MIRRORS")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r, d in zip(rows, descs):
        print(fmt.format(*r))
        if d:
            print(f"    {d}")


def cmd_sandbox_default_enable(args: argparse.Namespace) -> None:
    """Flag a sandbox for auto-enable in NEW projects (mirrors `mcp enable`).
    Only echo and BYO types are targets — baked mirror roles follow their
    worker twin (see `worker enable`)."""
    known = sandbox.known_type_names()
    if args.name not in known:
        die(f"unknown sandbox {args.name!r}. Known: "
            f"{', '.join(sorted(known)) or '(none)'} "
            f"(baked roles + registered BYO types; add a BYO type with "
            f"`research sandbox add`).")
    _reject_mirror_sandbox_default(args.name)
    defaults.set_enabled("sandbox", args.name, True)
    print(f"sandbox {args.name!r}: default-enabled (auto-enabled in new "
          f"projects; override per-project with `project create --disable`)")


def cmd_sandbox_default_disable(args: argparse.Namespace) -> None:
    _reject_mirror_sandbox_default(args.name)
    defaults.set_enabled("sandbox", args.name, False)
    print(f"sandbox {args.name!r}: default-disabled (not auto-enabled in new "
          f"projects)")


def cmd_sandbox_remove(args: argparse.Namespace) -> None:
    if args.name in sandbox.baked_names():
        die(f"{args.name!r} is a built-in baked sandbox; it can't be removed.")
    in_use = projects_using_sandbox_type(args.name)
    if in_use and not args.force:
        die(f"sandbox type {args.name!r} is enabled for projects: "
            f"{', '.join(in_use)}.\n"
            f"  Run `research project sandbox disable <proj> {args.name}` "
            f"for each, or pass --force.")
    with pi_isolated_registry.lock():
        try:
            data = pi_isolated_registry.load(expand=False)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))
        if args.name not in data["types"]:
            die(f"no sandbox type named {args.name!r}")
        data["types"].pop(args.name)
        try:
            pi_isolated_registry.save_atomic(data)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))
    defaults.set_enabled("sandbox", args.name, False)  # drop any default flag
    print(f"removed sandbox type {args.name!r}")
    if in_use:
        print(f"  note: {len(in_use)} project(s) still have a stale "
              f"sandbox.json entry; they keep running until "
              f"`project sandbox disable`.")


def cmd_sandbox_set_root(args: argparse.Namespace) -> None:
    _sandbox_registry_edit(args.name, lambda e: e.__setitem__("root", args.root))
    print(f"set root for {args.name!r}: {args.root}")
    print("  (existing projects pick up the new root on next "
          "`project update`/recreate)")


def cmd_sandbox_describe(args: argparse.Namespace) -> None:
    new_desc = "" if args.clear else (args.text or "").strip()
    if not args.clear and not new_desc:
        die("description text is required (or pass --clear to remove)")

    def mutate(e: dict) -> None:
        if new_desc:
            e["description"] = new_desc
        else:
            e.pop("description", None)

    _sandbox_registry_edit(args.name, mutate)
    print(f"{'set' if new_desc else 'cleared'} description for {args.name!r}")


def cmd_mcp_start(args: argparse.Namespace) -> None:
    _ensure_router_running()
    if args.name is not None:
        try:
            entry = mcp_registry.entry_for(args.name)
        except mcp_registry.RegistryError as e:
            die(str(e))
        if entry is None:
            die(f"no MCP named {args.name!r}")
        if entry["kind"] != "shared":
            die(f"`start` only applies to shared MCPs (got kind={entry['kind']!r})")
        _spawn_shared_mcp(args.name, entry)
        return
    targets = _shared_mcps(only_enabled=True)
    if not targets:
        try:
            data = mcp_registry.load()
        except mcp_registry.RegistryError as e:
            die(str(e))
        ext = [n for n, e in data["mcps"].items()
               if e.get("enabled", False) and e["kind"] == "external"]
        if ext:
            print(f"(nothing to start: {len(ext)} enabled MCP(s) are "
                  f"external and have no container lifecycle)")
        else:
            print("(no enabled MCPs)")
        return
    for name, entry in targets:
        _spawn_shared_mcp(name, entry)


def cmd_mcp_stop(args: argparse.Namespace) -> None:
    if args.name is not None:
        try:
            entry = mcp_registry.entry_for(args.name, expand=False)
        except mcp_registry.RegistryError as e:
            die(str(e))
        if entry is None:
            die(f"no MCP named {args.name!r}")
        if entry["kind"] != "shared":
            die(f"`stop` only applies to shared MCPs (got kind={entry['kind']!r})")
        targets = [(args.name, entry)]
    else:
        targets = _shared_mcps()
    stopped_any = False
    for name, _entry in targets:
        cname = mcp_container_name_for(name)
        if not container_running(cname):
            continue
        run_check(["docker", "stop", cname])
        print(f"stopped {cname}")
        stopped_any = True
    if not stopped_any:
        print("(no running shared MCPs)")


def cmd_mcp_test(args: argparse.Namespace) -> None:
    try:
        data = mcp_registry.load()
    except mcp_registry.RegistryError as e:
        die(str(e))
    if args.name is not None:
        entry = data["mcps"].get(args.name)
        if entry is None:
            die(f"no MCP named {args.name!r}")
        names = [args.name]
    else:
        names = sorted(data["mcps"].keys())
        if not names:
            print("(no MCPs registered)")
            return
    _ensure_router_running()
    failed = False
    for name in names:
        ok, err = _probe_mcp(name, data["mcps"][name])
        if ok:
            print(f"{name}: reachable")
        else:
            print(f"{name}: unreachable" + (f"\n  {err}" if err else ""),
                  file=sys.stderr)
            failed = True
    if failed:
        sys.exit(1)


def cmd_project_mcp_list(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    _require_project(project)

    allow_entries = load_project_allowlist(project, cfg)
    try:
        registry = mcp_registry.load(expand=False)
    except mcp_registry.RegistryError as e:
        die(str(e))
    reg_mcps = registry["mcps"]

    if args.json:
        out = []
        for e in allow_entries:
            name = e.get("name", "")
            r = reg_mcps.get(name, {})
            row = dict(e)
            row["enabled"] = bool(r.get("enabled", False))
            row["registered"] = name in reg_mcps
            if r.get("kind") == "shared":
                row["running"] = container_running(mcp_container_name_for(name))
            out.append(row)
        print(json.dumps(out, indent=2, sort_keys=True))
        return

    if not allow_entries:
        print(f"(project {project!r} allows no MCPs)")
        return

    rows: list[tuple[str, ...]] = []
    descs: list[str] = []
    for e in sorted(allow_entries, key=lambda x: x.get("name", "")):
        name = e.get("name", "")
        r = reg_mcps.get(name, {})
        kind = e.get("kind") or r.get("kind") or "?"
        if name not in reg_mcps:
            enabled = "missing"
        else:
            enabled = "yes" if r.get("enabled", False) else "no"
        if kind == "shared":
            running = "yes" if container_running(mcp_container_name_for(name)) else "no"
        else:
            running = "-"
        ok, _err = (False, "") if not container_running(ROUTER_CONTAINER) \
            else _probe_mcp(name, r) if name in reg_mcps else (False, "")
        reachable = "yes" if ok else "no"
        rows.append((name, kind, enabled, running, reachable))
        descs.append(e.get("description", "") or r.get("description", ""))

    headers = ("NAME", "KIND", "ENABLED", "RUNNING", "REACHABLE")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r, d in zip(rows, descs):
        print(fmt.format(*r))
        if d:
            print(f"    {d}")


def cmd_project_mcp_allow(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    container_name = _require_project(project)
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research start` first.")
    succeeded, failed = _batch_apply(project, cfg, args.mcp,
                                     _allow_mcp_for_project, "allowed")
    if succeeded:
        _supervisor_mcp_reload(container_name)
    if failed:
        sys.exit(1)


def cmd_project_mcp_deny(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    container_name = _require_project(project)
    succeeded, failed = _batch_apply(project, cfg, args.mcp,
                                     _deny_mcp_for_project, "denied")
    if succeeded:
        _supervisor_mcp_reload(container_name)
    if failed:
        sys.exit(1)


def cmd_project_mcp_sync(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    container_name = _require_project(project)
    try:
        registry = mcp_registry.load(expand=False)
    except mcp_registry.RegistryError as e:
        die(str(e))
    reg_mcps = registry["mcps"]
    enabled = {n for n, e in reg_mcps.items() if e.get("enabled", False)}
    allowed = {e.get("name") for e in load_project_allowlist(project, cfg)
               if e.get("name")}

    to_add = sorted(enabled - allowed)
    # Drop entries that are no longer enabled OR no longer in the registry.
    to_remove = sorted(n for n in allowed
                       if n not in enabled or n not in reg_mcps)

    allow_changed = bool(to_add or to_remove)

    if to_add or to_remove:
        added, _ = _batch_apply(project, cfg, to_add,
                                _allow_mcp_for_project, "allowed")
        removed, _ = _batch_apply(project, cfg, to_remove,
                                  _deny_mcp_for_project, "denied")
        if added or removed:
            _supervisor_mcp_reload(container_name)

    # Phase 3 — re-derive auto-wired role-MCP upstreams against the updated
    # allow list and restart any role-MCP whose upstream set actually
    # changed. PI mirror (if enabled) restarts in lockstep. Skip entries
    # marked `upstream_source=explicit` (operator override). Phase runs
    # AFTER phase 1+2 so _derive_auto_upstreams sees the current allow set.
    workspace_path = workspace_path_for(project, cfg)
    role_entries = role_mcp.load_role_mcps(workspace_path)
    sandbox_entries = sandbox.load(workspace_path)
    role_changes: list[str] = []
    for role, entry in list(role_entries.items()):
        if entry.get("upstream_source") != "auto":
            continue
        old = list(entry.get("upstream_mcps") or [])
        new = _derive_auto_upstreams(role, project, cfg)
        if set(old) == set(new):
            continue
        entry["upstream_mcps"] = new
        role_mcp.save_role_mcps(workspace_path, role_entries)
        _role_mcp_start(container_name, project, cfg, role)
        added_up = sorted(set(new) - set(old))
        removed_up = sorted(set(old) - set(new))
        diff = []
        if added_up:
            diff.append("+" + ",".join(added_up))
        if removed_up:
            diff.append("-" + ",".join(removed_up))
        role_changes.append(f"restarted worker {role!r} ({' '.join(diff)})")
        # The sandbox mirror (if enabled) shares the worker's name; restart
        # it in lockstep so its rendered .mcp.json tracks the new upstreams.
        if role in sandbox_entries:
            _sandbox_start(container_name, project, cfg, role)
            role_changes.append(f"restarted sandbox mirror {role!r} in lockstep")

    if not allow_changed and not role_changes:
        print(f"(project {project!r} already in sync with the registry)")
        return
    if role_changes:
        # mcp-proxy already routes by role-mcp name; restart doesn't
        # change the route table, but reload is cheap and re-affirms.
        _supervisor_mcp_reload(container_name)
        for line in role_changes:
            print(line)


def cmd_project_role_mcp_enable(args: argparse.Namespace) -> None:
    """Three-state upstream semantics:
      --upstream <csv>  → explicit list (survives sync)
      --upstream ""     → explicit empty (survives sync; daemon comes up
                          with no DB MCPs)
      --auto            → re-derive from registry × allow intersection,
                          mark as ``upstream_source: auto``
      (no flag)         → first enable: auto-derive; re-enable: preserve
                          existing source + upstreams (idempotent re-run,
                          no silent flips)
    --upstream and --auto are mutually exclusive (argparse-enforced)."""
    cfg = load_config()
    _require_project(args.project)
    raw_upstream = getattr(args, "upstream", None)
    force_auto = bool(getattr(args, "auto", False))
    if raw_upstream is not None:
        upstreams: list[str] | None = _parse_csv_list(raw_upstream)
    else:
        upstreams = None
    memory = getattr(args, "memory", None)
    mcc_raw = getattr(args, "max_concurrent_calls", None)
    max_concurrent_calls = int(mcc_raw) if mcc_raw is not None else None
    _role_mcp_enable(args.project, cfg, args.role, upstreams,
                     force_auto=force_auto,
                     no_pi_mirror=bool(getattr(args, "no_pi_mirror", False)),
                     memory=memory,
                     max_concurrent_calls=max_concurrent_calls)


def cmd_project_role_mcp_disable(args: argparse.Namespace) -> None:
    cfg = load_config()
    _require_project(args.project)
    _role_mcp_disable(args.project, cfg, args.role)
    print(f"role-mcp {args.role!r}: disabled")


def cmd_project_worker_stop(args: argparse.Namespace) -> None:
    """Gracefully park a registered worker service: refuse if it has
    in-flight send_job calls (read-only check), else `docker stop` it and
    mark the role-mcps.json entry `stopped`. The entry is preserved — the
    worker stays part of the project and the park survives supervisor
    recreate — distinct from `disable`, which removes + deregisters."""
    cfg = load_config()
    supervisor = _require_project(args.project)
    role = args.role
    role_mcp.validate_role(role)
    workspace_path = workspace_path_for(args.project, cfg)
    entries = role_mcp.load_role_mcps(workspace_path)
    if role not in entries:
        die(f"worker {role!r} is not enabled for project {args.project!r}; "
            f"nothing to stop.")

    if container_running(supervisor) and _role_mcp_inner_running(supervisor, role):
        in_flight = _role_mcp_in_flight(workspace_path, role)
        if in_flight and not args.force:
            named = ", ".join(
                f"{e.get('call_id', '?')}({e.get('caller', '?')})"
                for e in in_flight)
            die(f"worker {role!r} has {len(in_flight)} in-flight call(s): "
                f"{named}.\nWait for them to finish (poll with `research "
                f"project worker status {args.project} {role}`), or re-run "
                f"with --force to stop anyway — the in-flight call(s) will be "
                f"lost and marked failed (daemon_restart_orphan) on next "
                f"start.")
        _role_mcp_park(supervisor, role)

    # Re-load before mutating: nothing should have raced us, but the park is
    # a separate process boundary, so read-modify-write the freshest entry.
    entries = role_mcp.load_role_mcps(workspace_path)
    if role in entries:
        entries[role]["stopped"] = True
        role_mcp.save_role_mcps(workspace_path, entries)
    print(f"worker {role!r}: stopped (parked; entry preserved). Restart with "
          f"`research project worker start {args.project} {role}`.")


def cmd_project_worker_start(args: argparse.Namespace) -> None:
    """Restart a parked worker service: respawn the container against its
    preserved role-mcps.json entry (same upstreams, IP, caps, memory) and
    clear the `stopped` flag. Idempotent on an already-running worker."""
    cfg = load_config()
    supervisor = _require_project(args.project)
    role = args.role
    role_mcp.validate_role(role)
    if not container_running(supervisor):
        die(f"project {args.project!r} is not running; bring it up first "
            f"with `research project start {args.project}`.")
    workspace_path = workspace_path_for(args.project, cfg)
    entries = role_mcp.load_role_mcps(workspace_path)
    if role not in entries:
        die(f"worker {role!r} is not enabled for project {args.project!r}; "
            f"enable it first with `research project worker enable "
            f"{args.project} {role}`.")
    _role_mcp_start(supervisor, args.project, cfg, role)
    entries = role_mcp.load_role_mcps(workspace_path)
    if role in entries:
        entries[role]["stopped"] = False
        role_mcp.save_role_mcps(workspace_path, entries)
    print(f"worker {role!r}: started")


def cmd_project_worker_list(args: argparse.Namespace) -> None:
    """Comprehensive worker listing: the enable-able **services** (role-MCPs,
    with upstreams + state) AND the spawned **analysis instances** (ephemeral
    task containers, any state — running / exited / dead). Reads service config
    from the project's host bind-mount so it renders even with the supervisor
    **down**; enriches with live `docker ps -a` state when it's up. The two
    halves are different kinds under one umbrella (see STAGE_CLI_TAXONOMY)."""
    cfg = load_config()
    supervisor = _require_project(args.project)
    workspace_path = workspace_path_for(args.project, cfg)
    services = role_mcp.load_role_mcps(workspace_path)
    up = container_running(supervisor)
    states = _inner_container_states(supervisor)

    def svc_state(role: str) -> str:
        # A deliberately-parked worker (`project worker stop`) reads as
        # `parked` regardless of supervisor/container state — the flag is the
        # disambiguator between "operator stopped it" and "crashed / absent".
        if services.get(role, {}).get("stopped"):
            return "parked"
        if not up:
            return "unknown"
        return states.get(f"rs-{role}", "absent")

    # Analysis instances: ephemeral, label-tracked (research.worker=1), no
    # registry. Enumerated live from `docker ps -a` (all states) when up.
    instances: list[tuple[str, str]] = []  # (name, state)
    if up:
        r = run(["docker", "exec", supervisor, "docker", "ps", "-a",
                 "--filter", "label=research.worker",
                 "--format", "{{.Names}}\t{{.State}}"], capture_output=True)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "\t" in line:
                    n, _, s = line.partition("\t")
                    instances.append((n.strip(), s.strip()))

    if args.json:
        out = {
            "services": [
                {**e, "name": role, "kind": "service", "state": svc_state(role)}
                for role, e in sorted(services.items())
            ],
            "analysis_instances": [
                {"name": n, "kind": "analysis", "state": s}
                for n, s in sorted(instances)
            ],
            "supervisor_up": up,
        }
        print(json.dumps(out, indent=2, sort_keys=True))
        return

    print("Services (enable-able worker-facing role-MCPs):")
    if services:
        rows = []
        for role, e in sorted(services.items()):
            upstreams = ",".join(e.get("upstream_mcps", []) or []) or "-"
            rows.append((role, e.get("ip", "?"), str(e.get("port", "?")),
                         svc_state(role), upstreams))
        headers = ("NAME", "IP", "PORT", "STATE", "UPSTREAMS")
        cols = list(zip(*([headers] + rows)))
        widths = [max(len(str(v)) for v in col) for col in cols]
        fmt = "  ".join(f"{{:<{w}}}" for w in widths)
        print("  " + fmt.format(*headers))
        for r in rows:
            print("  " + fmt.format(*r))
    else:
        print("  (none enabled)")

    print("\nAnalysis instances (ephemeral, supervisor-spawned):")
    if not up:
        print("  (supervisor stopped — start the project to list running tasks)")
    elif instances:
        for n, s in sorted(instances):
            print(f"  {n}  [{s}]")
    else:
        print("  (none)")


def cmd_project_role_mcp_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    supervisor = _require_project(args.project)
    workspace_path = workspace_path_for(args.project, cfg)
    entries = role_mcp.load_role_mcps(workspace_path)
    entry = entries.get(args.role)
    if entry is None:
        die(f"role-mcp {args.role!r} is not enabled for project "
            f"{args.project!r}")

    state = {
        "role": args.role,
        "project": args.project,
        "entry": entry,
        "container": role_mcp.role_container_name(args.role),
        "exists": False,
        "running": False,
    }
    if container_running(supervisor):
        state["exists"] = _role_mcp_inner_exists(supervisor, args.role)
        state["running"] = _role_mcp_inner_running(supervisor, args.role)

    # Per-caller watermark + memory counts. Useful for spotting whether a
    # role-MCP has actually processed any traffic since enable. State lives
    # under .role-mcps/<role>/ (daemon-private); the publish surface at
    # shared/<role>/ is a separate concern surfaced below.
    state_dir = workspace_path / ".role-mcps" / args.role
    wm = state_dir / ".summarize-watermark"
    if wm.is_file():
        try:
            state["watermarks"] = json.loads(wm.read_text())
        except json.JSONDecodeError:
            state["watermarks"] = "(invalid json)"
    else:
        state["watermarks"] = {}
    memories = state_dir / "memories"
    if memories.is_dir():
        state["calls_by_caller"] = {
            p.name: sum(1 for _ in p.glob("*.md"))
            for p in sorted(memories.iterdir()) if p.is_dir()
        }
    else:
        state["calls_by_caller"] = {}

    # Publish-surface presence. Empty dir is normal (echo, possibly
    # websearcher); non-empty (wrangler's extracts/, librarian's refs/)
    # indicates the role is producing artifacts.
    publish_dir = workspace_path / "shared" / args.role
    state["publish_dir"] = str(publish_dir)
    state["publish_present"] = (
        publish_dir.is_dir() and any(publish_dir.iterdir())
    )

    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return

    print(f"role:        {state['role']}")
    print(f"project:     {state['project']}")
    print(f"container:   {state['container']}")
    print(f"  exists:    {state['exists']}")
    print(f"  running:   {state['running']}")
    print(f"ip:          {entry.get('ip')}")
    print(f"port:        {entry.get('port')}")
    print(f"image:       {entry.get('image')}")
    upstreams = entry.get("upstream_mcps") or []
    print(f"upstreams:   {', '.join(upstreams) or '(none)'}")
    print(f"  source:    {entry.get('upstream_source', 'explicit (legacy)')}")
    print(f"watermarks:  {json.dumps(state['watermarks'], sort_keys=True)}")
    print(f"calls:       {json.dumps(state['calls_by_caller'], sort_keys=True)}")
    print(f"publish_dir: {state['publish_dir']}"
          f" ({'has artifacts' if state['publish_present'] else 'empty'})")


def cmd_project_sandbox_enable(args: argparse.Namespace) -> None:
    cfg = load_config()
    _require_project(args.project)
    _sandbox_enable(args.project, cfg, args.name)


def cmd_project_sandbox_disable(args: argparse.Namespace) -> None:
    cfg = load_config()
    _require_project(args.project)
    _sandbox_disable(args.project, cfg, args.name)
    print(f"sandbox {args.name!r}: disabled")


def cmd_project_sandbox_list(args: argparse.Namespace) -> None:
    """Comprehensive listing: every enabled sandbox with its container state
    in any condition (running / exited / dead / absent). Reads config from the
    project's host bind-mount, so it renders even when the supervisor is
    stopped (state shows 'unknown' then); enriches with live `docker ps -a`
    state when the supervisor is up."""
    cfg = load_config()
    supervisor = _require_project(args.project)
    workspace_path = workspace_path_for(args.project, cfg)
    entries = sandbox.load(workspace_path)
    up = container_running(supervisor)
    states = _inner_container_states(supervisor)

    def state_of(nm: str, e: dict) -> str:
        cname = e.get("container") or sandbox.container_name(nm, e.get("kind"))
        if not up:
            return "unknown"
        return states.get(cname, "absent")

    if args.json:
        out = []
        for nm, e in sorted(entries.items()):
            row = dict(e)
            row["name"] = nm
            row["state"] = state_of(nm, e)
            out.append(row)
        print(json.dumps(out, indent=2, sort_keys=True))
        return

    if not entries:
        print(f"(project {args.project!r} has no sandboxes enabled)")
        return

    rows: list[tuple[str, ...]] = []
    for nm, e in sorted(entries.items()):
        src = e.get("repo") or e.get("image") or "-"
        rows.append((nm, e.get("kind", "?"), e.get("ip", "?"),
                     src, state_of(nm, e)))
    headers = ("NAME", "KIND", "IP", "IMAGE/REPO", "STATE")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))
    if not up:
        print("  (supervisor stopped — STATE is config-only; start the "
              "project for live container state)")


def cmd_project_sandbox_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    supervisor = _require_project(args.project)
    workspace_path = workspace_path_for(args.project, cfg)
    entries = sandbox.load(workspace_path)
    entry = entries.get(args.name)
    if entry is None:
        die(f"sandbox {args.name!r} is not enabled for project "
            f"{args.project!r}")
    kind = entry.get("kind")
    state = {
        "name": args.name,
        "project": args.project,
        "kind": kind,
        "entry": entry,
        "container": sandbox.container_name(args.name, kind),
        "exists": False,
        "running": False,
    }
    if container_running(supervisor):
        state["exists"] = _sandbox_inner_exists(supervisor, args.name, kind)
        state["running"] = _sandbox_inner_running(supervisor, args.name, kind)
        if kind == "byo":
            state["supervisor_external_mount"] = _supervisor_has_external_mount(
                supervisor, args.name)

    ws = workspace_path / sandbox.workspace_subdir(args.name, kind)
    state["workspace"] = str(ws)
    state["workspace_present"] = ws.is_dir()
    if ws.is_dir():
        state["workspace_files"] = sorted(
            p.name for p in ws.iterdir() if not p.name.startswith("."))
    else:
        state["workspace_files"] = []

    if kind == "byo":
        repo = entry.get("repo")
        clone_dir = None
        if repo:
            repo_name = repo.rstrip("/").rsplit("/", 1)[-1]
            if repo_name.endswith(".git"):
                repo_name = repo_name[:-4]
            clone_dir = ws / repo_name
        state["clone_dir"] = str(clone_dir) if clone_dir else None
        state["repo_cloned"] = bool(clone_dir and (clone_dir / ".git").is_dir())

    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return

    print(f"name:        {state['name']}")
    print(f"project:     {state['project']}")
    print(f"kind:        {state['kind']}")
    print(f"container:   {state['container']}")
    print(f"  exists:    {state['exists']}")
    print(f"  running:   {state['running']}")
    print(f"ip:          {entry.get('ip')}")
    if kind == "baked":
        print(f"image:       {entry.get('image')}")
        print(f"mirror_of:   {entry.get('mirror_of') or '(none)'}")
    else:
        print(f"repo:        {entry.get('repo') or '(none)'}")
        print(f"ref:         {entry.get('ref') or '(none)'}")
        print(f"mount:       {entry.get('mount')}")
        print(f"root:        {entry.get('root')}")
        print(f"external mount on supervisor: "
              f"{state.get('supervisor_external_mount', False)}")
    print(f"workspace:   {state['workspace']}"
          f" ({'present' if state['workspace_present'] else 'absent'})")
    if state.get("workspace_files"):
        print(f"  files:     {', '.join(state['workspace_files'])}")
    if kind == "byo":
        print(f"clone dir:   {state['clone_dir']}")
        print(f"  repo cloned: {state['repo_cloned']}")


def cmd_project_sandbox_sync_creds(args: argparse.Namespace) -> None:
    """The supervisor→sandbox credential bridge: invoke the supervisor-side
    rs-pi CLI to push the supervisor's current creds into every running
    sandbox container. Operator-initiated only — sandboxes are PI-owned and
    boot un-authed; nothing propagates creds automatically."""
    supervisor = _require_project(args.project)
    r = run(["docker", "exec", supervisor, "rs-pi", "sync-creds"],
            capture_output=False)
    if r.returncode != 0:
        sys.exit(r.returncode)


def cmd_project_update_claude(args: argparse.Namespace) -> None:
    """Interim manual refresh of the supervisor's Claude Code CLI.

    The CLI is baked into the image at build time (Dockerfile.supervisor), so
    a project's interactive `claude` — and the model list it offers — freezes
    at the image's build date and only moves on a `start --rebuild` + project
    recreate. This re-runs the official installer inside the running supervisor
    to pull the latest stable, no rebuild required.

    Scope is the SUPERVISOR only — the surface the PI interacts with. The inner
    fleet (workers, role-MCPs, PI containers) still restores the image-baked
    binary from worker-skel on boot; refreshing those uniformly is the
    per-project agent-dist work (PLAN/STAGE_AGENT_DIST.md). Sandbox-flavor
    (`--type sandbox`) projects have no agent and are refused."""
    supervisor = _require_project(args.name)
    if _container_project_type(supervisor) == PROJECT_TYPE_SANDBOX:
        die(f"project {args.name!r} is a sandbox (agent-less) project — its "
            f"Management console runs no claude to update")
    if not container_running(supervisor):
        die(f"project {args.name!r} is not running "
            f"(use `research project start {args.name}` first)")

    # Login shell + explicit PATH export: install.sh drops claude into
    # ~/.local/bin, which a non-interactive `su -c` won't have on PATH for the
    # version probes (the build-time PATH export lives in ~/.bashrc, not the
    # login profile). Export it inline so before/after checks resolve.
    def _claude_out(cmd: str) -> str:
        r = run(["docker", "exec", supervisor, "su", "-", "research", "-c",
                 f'export PATH="$HOME/.local/bin:$PATH"; {cmd}'],
                capture_output=True)
        return r.stdout.strip()

    before = _claude_out("claude --version 2>/dev/null") or "(none)"
    print(f"current: {before}")
    print("installing latest Claude Code in the supervisor (needs egress)…")
    r = run(["docker", "exec", supervisor, "su", "-", "research", "-c",
             'export PATH="$HOME/.local/bin:$PATH"; '
             'curl -fsSL https://claude.ai/install.sh | bash'],
            capture_output=False)
    if r.returncode != 0:
        die("claude installer failed inside the supervisor (check the "
            "project's egress — locked mode still allows 443)")
    after = _claude_out("claude --version 2>/dev/null") or "(unknown)"
    print(f"updated: {after}")
    if before == after:
        print("(already on the latest stable)")
    print("note: this refreshes the supervisor's interactive session only; "
          "open a fresh claude tab to pick it up. Inner workers / role-MCPs / "
          "PI containers still run the image-baked version.")


def cmd_webui_cert_tailscale() -> None:
    """Stage a Tailscale-issued Let's Encrypt cert into rs-webui-tls,
    replacing the auto-generated self-signed cert. Browser ServiceWorker
    registration (markdown preview, notebook output rendering, Data
    Wrangler webview, …) requires a publicly-trusted cert; Tailscale
    serves this for free on tailnet names with zero per-device CA
    install."""
    if shutil.which("tailscale") is None:
        die("tailscale CLI not on PATH. Install Tailscale first: "
            "https://tailscale.com/download")
    fqdn = _detect_tailscale_fqdn()
    if not fqdn:
        die("could not detect tailnet FQDN. Verify with `tailscale status` "
            "that the daemon is running and the host is registered.")

    print(f"requesting Tailscale cert for {fqdn}...")
    with tempfile.TemporaryDirectory() as td:
        cert_path = Path(td) / "cert.pem"
        key_path = Path(td) / "key.pem"
        r = run([
            "tailscale", "cert",
            "--cert-file", str(cert_path),
            "--key-file", str(key_path),
            fqdn,
        ], capture_output=True)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()
            low = err.lower()
            if "https" in low and ("not enabled" in low or "not configured" in low):
                die("HTTPS is not enabled for your tailnet. Toggle it on at "
                    "https://login.tailscale.com/admin/dns then re-run.\n"
                    f"  tailscale: {err}")
            if "operator" in low or "permission" in low or "denied" in low:
                die("`tailscale cert` requires sudo unless you're the "
                    "configured tailscaled operator. One-time fix:\n"
                    "  sudo tailscale up --operator=$USER\n"
                    f"(raw error: {err})")
            die(f"tailscale cert failed: {err}")
        cert_pem = cert_path.read_bytes()
        key_pem = key_path.read_bytes()

    _stage_webui_cert(cert_pem, key_pem, provider=f"tailscale:{fqdn}")
    print(f"staged Tailscale cert for {fqdn} into rs-webui-tls")

    if container_running(WEBUI_CONTAINER):
        print("recreating webui to pick up new cert...")
        _webui_recreate_in_place()

    bind = read_env_value("WEBUI_BIND") or "127.0.0.1"
    port = read_env_value("WEBUI_PORT") or "7777"
    print()
    if bind in ("127.0.0.1", "::1", "localhost"):
        print(f"  note: WEBUI_BIND={bind} only accepts loopback connections")
        print(f"  to reach via tailnet, run:")
        print(f"    research webui start --bind 0.0.0.0")
        print(f"  then open https://{fqdn}:{port}/")
    elif bind == "0.0.0.0":
        print(f"  open https://{fqdn}:{port}/ — cert is publicly trusted")
    else:
        print(f"  webui bound to {bind}; open https://{fqdn}:{port}/ to use "
              "the trusted cert")
        print(f"  (connections via {bind} directly will see a name-mismatch "
              f"warning — the cert covers {fqdn} only)")


def cmd_webui(args: argparse.Namespace) -> None:
    """Manage the optional webui container (start | stop | status | import |
    cert-tailscale)."""
    action = args.webui_action
    if action == "cert-tailscale":
        cmd_webui_cert_tailscale()
        return
    port = read_env_value("WEBUI_PORT") or "7777"
    bind = read_env_value("WEBUI_BIND") or "127.0.0.1"

    if action == "import":
        target = getattr(args, "project", None)
        if target:
            container = container_name_for(target)
            if not container_exists(container):
                die(f"project {target!r} does not exist")
            ssh_pass = _supervisor_ssh_pass(container)
            if not ssh_pass:
                die(f"could not read SSH password from {container}")
            print(_webui_import_string(target, ssh_pass))
            return
        rows: list[tuple[str, str]] = []
        for c in get_supervisor_containers():
            sp = _supervisor_ssh_pass(c["name"])
            if sp:
                rows.append((c["project"], _webui_import_string(c["project"], sp)))
        if not rows:
            print("no projects with SSH info available.")
            return
        name_w = max(len(p) for p, _ in rows)
        for p, s in rows:
            print(f"{p.ljust(name_w)}  {s}")
        return

    if action == "start":
        new_bind = getattr(args, "bind", None)
        new_port = getattr(args, "port", None)
        rebuild = getattr(args, "rebuild", False)
        recreate = False
        if new_bind and new_bind != bind:
            update_env_key("WEBUI_BIND", new_bind)
            bind = new_bind
            recreate = True
        if new_port and new_port != port:
            update_env_key("WEBUI_PORT", new_port)
            port = new_port
            recreate = True
        os.environ["WEBUI_BIND"] = bind
        os.environ["WEBUI_PORT"] = port
        if (recreate or rebuild) and container_exists(WEBUI_CONTAINER):
            reason = "bind/port changed" if recreate else "rebuild requested"
            print(f"{reason}; recreating webui...")
            run(["docker", "rm", "-f", WEBUI_CONTAINER], capture_output=True)

        if not rebuild and container_running(WEBUI_CONTAINER):
            wire_webui_to_projects()
            print(f"webui already running at https://{bind}:{port}")
            return
        if container_exists(WEBUI_CONTAINER):
            run(["docker", "rm", "-f", WEBUI_CONTAINER], capture_output=True)
        if rebuild or not run_quiet(["docker", "image", "inspect", WEBUI_IMAGE]):
            print("building webui image...")
            docker_compose("--profile", "webui", "build", "webui")
        print(f"starting webui (bind {bind}:{port})...")
        docker_compose("--profile", "webui", "up", "-d", "webui")
        wire_webui_to_projects()
        print(f"webui:  https://{bind}:{port}")
        print(f"  (self-signed cert — your browser will warn on first visit; click through)")
        return

    if action == "stop":
        if not container_exists(WEBUI_CONTAINER):
            print("webui not running.")
            return
        print("stopping webui...")
        run(["docker", "rm", "-f", WEBUI_CONTAINER], capture_output=True)
        print("webui stopped.")
        return

    if action == "status":
        if container_running(WEBUI_CONTAINER):
            print(f"webui: running")
            print(f"  https://{bind}:{port}")
        elif container_exists(WEBUI_CONTAINER):
            print("webui: stopped (container exists)")
        else:
            print(f"webui: not running  (configured bind {bind}:{port})")
        return


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="research", description="Research Sandbox CLI")
    sub = p.add_subparsers(dest="command", required=True)

    st = sub.add_parser("start", help="start shared infra (router); build images if missing")
    st.add_argument("--rebuild", action="store_true",
                    help="force-rebuild supervisor + worker images even if they exist")
    st.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="stop shared infra (router)")
    sp.set_defaults(func=cmd_stop)

    img = sub.add_parser("images", help="image version pins (manifest + freshness)")
    img_sub = img.add_subparsers(dest="subcommand", required=True)
    iv = img_sub.add_parser("versions", help="print current image version pins")
    iv.set_defaults(func=cmd_images_versions)
    io = img_sub.add_parser(
        "outdated", help="query upstreams for newer versions of each pin")
    io.set_defaults(func=cmd_images_outdated)

    proj = sub.add_parser("project", help="per-project operations")
    proj_sub = proj.add_subparsers(dest="subcommand", required=True)

    c = proj_sub.add_parser("create", help="create a new project")
    c.add_argument("name")
    c.add_argument("--type", choices=[PROJECT_TYPE_RESEARCH, PROJECT_TYPE_SANDBOX],
                   default=PROJECT_TYPE_RESEARCH,
                   help="project flavor. 'research' (default): supervisor agent "
                        "+ workers. 'sandbox': agent-less collection of isolated "
                        "boxes managed from the in-supervisor `rs-sandbox` CLI "
                        "(inner firewall on by default; egress-OFF boxes get no "
                        "outbound network).")
    c.add_argument("--data", metavar="PATHS",
                   help="comma-separated host paths, each mounted RO at "
                        "/workspace/shared/data/<basename>/ (e.g. "
                        "`--data /home/me/raw,/srv/parsed` lands as "
                        "/workspace/shared/data/raw and .../parsed). "
                        "Missing paths are mkdir -p'd. Basename collisions "
                        "across the list are a hard error.")
    c.add_argument("--profile", default="python", help="supervisor image profile")
    c.add_argument("--dind", choices=["auto", "sysbox", "privileged"],
                   help="DIND mode (default: auto — sysbox if available, else privileged)")
    c.add_argument("--memory", help="memory limit (e.g. 8g)")
    c.add_argument("--cpus", help="cpu limit (e.g. 4)")
    c.add_argument("--egress", choices=["open", "locked"],
                   help="router egress policy (default from .env, usually open)")
    c.add_argument("--ssh-port", type=int, help="explicit SSH host port")
    c.add_argument("--inner-firewall", action="store_true",
                   help="enable defense-in-depth iptables ACL on the supervisor's "
                        "rs-inner bridge (workers can only reach mcp-proxy + DNS)")
    c.add_argument("--enable", metavar="IDS",
                   help=f"comma-separated tokens to force-enable. Tokens "
                        f"are matched by name against three registries: "
                        f"services ({','.join(KNOWN_SERVICES)}), workers "
                        f"({','.join(sorted(role_mcp.ROLE_IMAGES))}), and "
                        f"sandboxes ({','.join(sandbox.baked_names())} + BYO "
                        f"types). Worker tokens sugar for `project worker "
                        f"enable`; sandbox tokens for `project sandbox "
                        f"enable`. A worker that has a baked sandbox twin "
                        f"(wrangler / websearcher) auto-enables that mirror; "
                        f"the bare sentinel `no-sandbox-mirror` suppresses "
                        f"that for every worker token.")
    c.add_argument("--role-mcp-upstream", metavar="ROLE=CSV",
                   action="append",
                   help="repeatable: pin an explicit upstream list for a "
                        "role-mcp activated via `--enable <role>` "
                        "(e.g. `--role-mcp-upstream wrangler=postgres-mcp"
                        ",mongo-mcp`). Without this flag, the role-mcp "
                        "auto-derives upstreams from the registry × allow "
                        "intersection. Empty CSV (e.g. `wrangler=`) means "
                        "explicit no-upstreams.")
    c.add_argument("--disable", metavar="IDS",
                   help="comma-separated tokens to disable for this project, "
                        "overruling the host default-enable sets: service ids "
                        "(supervisor is always-on), worker services, and "
                        "sandboxes. e.g. `--disable websearcher` skips a "
                        "default-enabled worker for this one project.")
    c.add_argument("--mcp", metavar="NAMES", default="all-enabled",
                   help="MCPs to auto-allow at create time: 'all-enabled' "
                        "(default — every currently-enabled MCP), 'none', or a "
                        "comma-separated list of registry names. Per-MCP failures "
                        "(disabled, container not running, host unreachable) print "
                        "a warning and skip; the project still comes up. Add or "
                        "remove later with `research project mcp allow|deny|sync`.")
    c.set_defaults(func=cmd_project_create)

    a = proj_sub.add_parser("attach", help="docker exec + byobu attach")
    a.add_argument("name")
    a.set_defaults(func=cmd_project_attach)

    ls = proj_sub.add_parser("list", help="list all projects")
    ls.set_defaults(func=cmd_project_list)

    st = proj_sub.add_parser("status", help="show project status")
    st.add_argument("name")
    st.set_defaults(func=cmd_project_status)

    for op in ("stop", "start"):
        sp = proj_sub.add_parser(op, help=f"{op} the supervisor")
        g = sp.add_mutually_exclusive_group(required=True)
        g.add_argument("name", nargs="?")
        g.add_argument("--all", action="store_true")
        sp.set_defaults(func=(cmd_project_stop if op == "stop" else cmd_project_start))

    d = proj_sub.add_parser("destroy", help="remove container + volume + network")
    d.add_argument("name")
    d.set_defaults(func=cmd_project_destroy)

    u = proj_sub.add_parser(
        "update",
        help="push edited code into a project; preserves workspace + creds",
        description="Recreates the supervisor container (rm + create + start; "
                    "the only safe shape on sysbox), preserving workspace, "
                    "creds, network, SSH port, env, mounts, memory/CPU limits. "
                    "Without --rebuild, edited supervisor-baked files (cli/, "
                    "container/supervisor/*, container/analysis/CLAUDE.md.template, "
                    "agent/entrypoint.supervisor.sh) are docker-cp'd into the "
                    "freshly-created container before its first start. With "
                    "--rebuild, all three images are rebuilt first and the "
                    "new container comes from the rebuilt image. After the "
                    "recreate, /workspace/.claude/ template copies (CLAUDE.md, "
                    "slash commands, logbook templates) are refreshed from "
                    "/opt/claude-templates/* unless --keep-claude is given.",
    )
    u.add_argument("name")
    u.add_argument("--rebuild", action="store_true",
                   help="rebuild images first (required for Dockerfile / "
                        "mcp-proxy / worker-image changes)")
    u.add_argument("--keep-claude", action="store_true",
                   help="preserve the project's /workspace/.claude/* files "
                        "instead of overwriting them with the latest "
                        "templates (default: refresh, so role-doc and "
                        "slash-command edits propagate)")
    u.add_argument("--enable", metavar="IDS",
                   help=f"comma-separated tokens to enable. Matched by "
                        f"name against services ({','.join(KNOWN_SERVICES)}), "
                        f"workers ({','.join(sorted(role_mcp.ROLE_IMAGES))}), "
                        f"and sandboxes ({','.join(sandbox.baked_names())} + "
                        f"BYO types). Bare sentinel `no-sandbox-mirror` "
                        f"suppresses the baked-sandbox-mirror auto-enable for "
                        f"every worker token.")
    u.add_argument("--role-mcp-upstream", metavar="ROLE=CSV",
                   action="append",
                   help="repeatable: pin an explicit upstream list for a "
                        "worker activated via `--enable <role>` "
                        "(see `project create --help` for full semantics).")
    u.add_argument("--disable", metavar="IDS",
                   help="comma-separated service ids to disable "
                        "(supervisor is always-on and cannot be "
                        "disabled). Also accepts sandbox names to "
                        "disable a sandbox container (workspace "
                        "preserved).")
    u.set_defaults(func=cmd_project_update)

    uc = proj_sub.add_parser(
        "update-claude",
        help="refresh the supervisor's Claude Code CLI in place (no image "
             "rebuild) so the interactive session's model list isn't frozen "
             "at image-build time")
    uc.add_argument("name")
    uc.set_defaults(func=cmd_project_update_claude)

    sh = proj_sub.add_parser("ssh", help="print SSH connection info")
    sh.add_argument("name")
    sh.set_defaults(func=cmd_project_ssh)

    pm = proj_sub.add_parser("mcp",
                             help="per-project MCP allowlist (list / allow / deny / sync)")
    pm_sub = pm.add_subparsers(dest="mcp_action", required=True)

    pml = pm_sub.add_parser("list",
                            help="show every MCP allowed for the project, "
                                 "with kind / enabled / running / reachable / description")
    pml.add_argument("project")
    pml.add_argument("--json", action="store_true")
    pml.set_defaults(func=cmd_project_mcp_list)

    pma = pm_sub.add_parser("allow",
                            help="grant the project access to one or more registered MCPs "
                                 "(per-MCP failures warn + skip; supervisor reload runs once)")
    pma.add_argument("project")
    pma.add_argument("mcp", nargs="+")
    pma.set_defaults(func=cmd_project_mcp_allow)

    pmd = pm_sub.add_parser("deny",
                            help="revoke the project's access to one or more allowed MCPs")
    pmd.add_argument("project")
    pmd.add_argument("mcp", nargs="+")
    pmd.set_defaults(func=cmd_project_mcp_deny)

    pms = pm_sub.add_parser("sync",
                            help="reconcile the project's allowlist against the registry: "
                                 "add every newly-enabled MCP, remove any that are no longer "
                                 "enabled or have been deregistered")
    pms.add_argument("project")
    pms.set_defaults(func=cmd_project_mcp_sync)

    # ----- Role-MCP lifecycle (B.0) --------------------------------------
    rm = proj_sub.add_parser(
        "worker",
        help="per-project worker lifecycle (enable / disable / list / "
             "status). Workers are pipeline-side agents: the role-MCP "
             "services workers call via the proxy, plus (in `list`) the "
             "ephemeral analysis containers the supervisor spawns. Not "
             "PI-driven — see `project sandbox` for those.",
    )
    rm_sub = rm.add_subparsers(dest="role_action", required=True)

    rme = rm_sub.add_parser("enable",
                            help="bring up a worker service container in the "
                                 "supervisor's inner dockerd")
    rme.add_argument("project")
    rme.add_argument("role",
                     help="worker service name (e.g. echo-mcp, wrangler, "
                          "websearcher) — see `research worker list`.")
    rme_src = rme.add_mutually_exclusive_group()
    rme_src.add_argument("--upstream",
                         help="comma-separated upstream MCP names the role-"
                              "worker's spawned `claude -p` will see (must "
                              "already be allowed via `project mcp allow`). "
                              "Pins the entry as `upstream_source=explicit` "
                              "so `project mcp sync` leaves it alone. Pass "
                              "an empty string ('') for an explicit no-"
                              "upstreams entry.")
    rme_src.add_argument("--auto", action="store_true",
                         help="(re-)derive the upstream set from the "
                              "registry × allow intersection (MCPs with "
                              "matching `roles`) and mark the entry as "
                              "`upstream_source=auto` so `project mcp sync` "
                              "keeps it current. Use this to flip a pinned "
                              "entry back to auto-derive.")
    rme.add_argument("--no-sandbox-mirror", dest="no_pi_mirror",
                     action="store_true",
                     help="suppress the matching sandbox mirror's auto-enable. "
                          "Default: when a baked sandbox shares this worker's "
                          "name (wrangler / websearcher), its container is "
                          "enabled in lockstep.")
    rme.add_argument("--memory",
                     help="per-worker-service container memory cap (docker syntax, "
                          "e.g. 4g). Persists in role-mcps.json and survives "
                          "_recreate_supervisor. Default from "
                          "DEFAULT_ROLE_MCP_MEMORY in .env (2g if unset).")
    rme.add_argument("--max-concurrent-calls", type=int,
                     help="daemon-side cap on in-flight send_job calls; "
                          "beyond this the daemon returns an MCP tool "
                          "error with structured concurrency_limit payload "
                          "immediately (no spawn). 0 disables the cap. "
                          "Default from DEFAULT_ROLE_MCP_MAX_CONCURRENT_CALLS "
                          "in .env (3 if unset).")
    rme.set_defaults(func=cmd_project_role_mcp_enable)

    rmd = rm_sub.add_parser("disable",
                            help="stop + remove the worker service container "
                                 "(workspace state under shared/<role> "
                                 "survives)")
    rmd.add_argument("project")
    rmd.add_argument("role")
    rmd.set_defaults(func=cmd_project_role_mcp_disable)

    rmstop = rm_sub.add_parser(
        "stop",
        help="gracefully PARK a running worker service (docker stop, NOT "
             "remove): the role-mcps.json entry is preserved so the worker "
             "stays part of the project and the park survives supervisor "
             "recreate. Refuses if the worker has in-flight send_job calls "
             "unless --force. Contrast `disable`, which removes + "
             "deregisters the worker entirely.")
    rmstop.add_argument("project")
    rmstop.add_argument("role")
    rmstop.add_argument(
        "--force", action="store_true",
        help="stop even with in-flight calls; the in-flight call(s) are lost "
             "and marked failed (daemon_restart_orphan) on next start.")
    rmstop.set_defaults(func=cmd_project_worker_stop)

    rmstart = rm_sub.add_parser(
        "start",
        help="restart a PARKED worker service (see `worker stop`). Respawns "
             "the container against its preserved entry (same upstreams, IP, "
             "caps) and clears the parked flag.")
    rmstart.add_argument("project")
    rmstart.add_argument("role")
    rmstart.set_defaults(func=cmd_project_worker_start)

    rml = rm_sub.add_parser("list",
                            help="show worker services (running state + "
                                 "upstreams) AND, with --instances would be "
                                 "implicit, the spawned analysis containers "
                                 "in every state; works supervisor up or down")
    rml.add_argument("project")
    rml.add_argument("--json", action="store_true")
    rml.set_defaults(func=cmd_project_worker_list)

    rms = rm_sub.add_parser("status",
                            help="deep-print one worker service's container "
                                 "state, watermarks, and per-caller call "
                                 "counts")
    rms.add_argument("project")
    rms.add_argument("role")
    rms.add_argument("--json", action="store_true")
    rms.set_defaults(func=cmd_project_role_mcp_status)

    # ----- per-project sandbox lifecycle (STAGE_CLI_TAXONOMY) -----------
    # Merges the former `project pi` (baked roles) + `project pi-isolated`
    # (BYO agents) into one surface: PI-driven, webui-tab-able containers.
    sb = proj_sub.add_parser(
        "sandbox",
        help="per-project sandbox lifecycle (enable / disable / list / "
             "status / sync-creds). Sandboxes are PI-driven containers with "
             "webui tabs: baked roles (echo / wrangler / websearcher) and "
             "BYO skill-repo agents (see `research sandbox list`). They live "
             "in sandbox.json. PI-owned auth (in-tab /login or sync-creds).",
    )
    sb_sub = sb.add_subparsers(dest="sandbox_action", required=True)

    sbe = sb_sub.add_parser("enable",
                            help="bring up a sandbox container in the "
                                 "supervisor's inner dockerd (recreates the "
                                 "supervisor first if a BYO external folder "
                                 "isn't mounted yet)")
    sbe.add_argument("project")
    sbe.add_argument("name", help="sandbox name: a baked role (echo / "
                                  "wrangler / websearcher) or a BYO type "
                                  "(see `research sandbox list`)")
    sbe.set_defaults(func=cmd_project_sandbox_enable)

    sbd = sb_sub.add_parser("disable",
                            help="stop + remove the sandbox container "
                                 "(workspace, and any BYO external folder, "
                                 "survive)")
    sbd.add_argument("project")
    sbd.add_argument("name")
    sbd.set_defaults(func=cmd_project_sandbox_disable)

    sbl = sb_sub.add_parser("list",
                            help="show every sandbox enabled for the project "
                                 "with its container state in any condition "
                                 "(running / stopped / dead); works "
                                 "supervisor up or down")
    sbl.add_argument("project")
    sbl.add_argument("--json", action="store_true")
    sbl.set_defaults(func=cmd_project_sandbox_list)

    sbs = sb_sub.add_parser("status",
                            help="deep-print one sandbox's container state + "
                                 "workspace (and, for BYO, clone) presence")
    sbs.add_argument("project")
    sbs.add_argument("name")
    sbs.add_argument("--json", action="store_true")
    sbs.set_defaults(func=cmd_project_sandbox_status)

    sbsc = sb_sub.add_parser("sync-creds",
                             help="push the supervisor's creds into every "
                                  "running sandbox (operator-initiated; "
                                  "sandboxes are otherwise PI-authed via "
                                  "in-tab /login)")
    sbsc.add_argument("project")
    sbsc.set_defaults(func=cmd_project_sandbox_sync_creds)

    # ----- MCP registry (Stage 2.1) -------------------------------------
    mcp = sub.add_parser("mcp", help="MCP registry operations")
    mcp_sub = mcp.add_subparsers(dest="subcommand", required=True)

    a = mcp_sub.add_parser("add", help="register an MCP in the host registry")
    a.add_argument("name")
    a.add_argument("--kind", choices=("external", "shared"), required=True)
    a.add_argument("--transport", choices=("http", "sse"), default="http")
    a.add_argument("--host", metavar="HOST:PORT",
                   help="(external) HOST:PORT the supervisor reaches the MCP at "
                        "(use host.docker.internal:<port> for a service on the docker host)")
    a.add_argument("--header", action="append", default=[],
                   metavar="K=V",
                   help="(external) HTTP header to inject (repeatable)")
    a.add_argument("--image",
                   help="(shared) docker image of the MCP server")
    a.add_argument("--port", type=int,
                   help="(shared) port the MCP listens on inside its container")
    a.add_argument("--env", action="append", default=[],
                   metavar="K=V",
                   help="(shared) env var passed to the MCP container (repeatable)")
    a.add_argument("--path", default=mcp_registry.DEFAULT_PATH,
                   help=f"upstream URL path the MCP listens on "
                        f"(default {mcp_registry.DEFAULT_PATH!r}, the SDK convention)")
    a.add_argument("--description",
                   help="PI-authored project-level intent for what this MCP "
                        "gives access to (e.g. 'parsed event aggregates'); "
                        "propagates into per-project allowlists and into each "
                        "worker's CLAUDE.md when granted")
    a.add_argument("--roles", metavar="CSV",
                   help=f"comma-separated role-MCP affinities — declares "
                        f"that this MCP serves the named role-MCP(s) as an "
                        f"upstream. Used by `--enable <role>` (sugar for "
                        f"`project role-mcp enable`) and `project mcp sync` "
                        f"to auto-wire the per-project upstream set. Known "
                        f"roles: "
                        f"{','.join(sorted(role_mcp.ROLE_IMAGES)) or '(none)'}")
    a.set_defaults(func=cmd_mcp_add)

    md_d = mcp_sub.add_parser("describe",
                              help="set or clear an MCP's description")
    md_d.add_argument("name")
    g = md_d.add_mutually_exclusive_group(required=True)
    g.add_argument("text", nargs="?", help="new description text")
    g.add_argument("--clear", action="store_true",
                   help="remove the existing description")
    md_d.set_defaults(func=cmd_mcp_describe)

    sr = mcp_sub.add_parser("set-workers",
                            help="replace an MCP's worker affinities — which "
                                 "worker services auto-wire this MCP as an "
                                 "upstream (comma-separated; empty CSV clears)")
    sr.add_argument("name")
    sr.add_argument("csv", help="comma-separated worker service names, or "
                                "empty string to clear")
    sr.set_defaults(func=cmd_mcp_set_roles)

    ml = mcp_sub.add_parser("list", help="list registered MCPs")
    ml.add_argument("--json", action="store_true")
    ml.set_defaults(func=cmd_mcp_list)

    mr = mcp_sub.add_parser("remove", help="remove MCP from registry (and stop its container)")
    mr.add_argument("name")
    mr.add_argument("--force", action="store_true",
                    help="remove even if projects currently allow it (Stage 2.2 state)")
    mr.set_defaults(func=cmd_mcp_remove)

    me = mcp_sub.add_parser("enable",
                            help="mark an MCP as enabled (required for `project mcp allow`; "
                                 "shared MCPs auto-start on `research start`)")
    me.add_argument("name")
    me.set_defaults(func=cmd_mcp_enable)

    md = mcp_sub.add_parser("disable",
                            help="clear the enabled flag, deny from every project that "
                                 "currently allows it, and (if shared and running) stop "
                                 "the container")
    md.add_argument("name")
    md.add_argument("--keep-projects", action="store_true",
                    help="leave per-project allowlists untouched; project workers "
                         "keep the stale wiring until you `project mcp deny` or "
                         "`project mcp sync` them yourself")
    md.set_defaults(func=cmd_mcp_disable)

    msp = mcp_sub.add_parser("start", help="(shared) start MCP containers (defaults to all enabled)")
    msp.add_argument("name", nargs="?",
                     help="MCP name; omit to start every enabled shared MCP")
    msp.set_defaults(func=cmd_mcp_start)

    mst = mcp_sub.add_parser("stop", help="(shared) stop MCP containers (defaults to all running)")
    mst.add_argument("name", nargs="?",
                     help="MCP name; omit to stop every running shared MCP")
    mst.set_defaults(func=cmd_mcp_stop)

    mts = mcp_sub.add_parser("test", help="probe reachability of MCPs (defaults to every registered MCP)")
    mts.add_argument("name", nargs="?",
                     help="MCP name; omit to test every registered MCP")
    mts.set_defaults(func=cmd_mcp_test)

    # ----- worker catalog (host visibility) -----------------------------
    wk = sub.add_parser(
        "worker",
        help="worker-side agents: catalog of built-in service types "
             "(role-MCPs) the supervisor can run, plus the analysis worker "
             "baseline. Per-project lifecycle is `research project worker`.")
    wk_sub = wk.add_subparsers(dest="subcommand", required=True)
    wkl = wk_sub.add_parser("list", help="list available worker types")
    wkl.add_argument("--json", action="store_true")
    wkl.set_defaults(func=cmd_worker_list)
    wke = wk_sub.add_parser("enable",
                            help="flag a worker service for auto-enable in new "
                                 "projects (like `mcp enable`)")
    wke.add_argument("name")
    wke.set_defaults(func=cmd_worker_default_enable)
    wkd = wk_sub.add_parser("disable",
                            help="clear a worker service's default-enable flag")
    wkd.add_argument("name")
    wkd.set_defaults(func=cmd_worker_default_disable)

    # ----- sandbox type registry (STAGE_CLI_TAXONOMY) -------------------
    # Host catalog of sandbox types: built-in baked roles (constants) +
    # operator-registered BYO skill-repo types (the host registry, formerly
    # `pi-isolated`). Per-project lifecycle is `research project sandbox`.
    sbr = sub.add_parser(
        "sandbox",
        help="host catalog of sandbox types: built-in baked roles + BYO "
             "skill-repo agents (reusable repo + root-folder definitions "
             "enabled per-project with `project create|update --enable`)")
    sbr_sub = sbr.add_subparsers(dest="subcommand", required=True)

    pa = sbr_sub.add_parser("add", help="register a BYO sandbox type")
    pa.add_argument("name")
    pa.add_argument("--root", required=True, metavar="HOST_DIR",
                    help="host folder for this type; the per-project subdir "
                         "<root>/<project>/ is the RW external mount. May use "
                         "~ and ${VAR}.")
    pa.add_argument("--repo", help="git URL cloned into the container at "
                                   "enable time (omit for a pure-folder agent)")
    pa.add_argument("--ref", help="commit/tag to check out. Optional: if "
                                  "omitted, the repo's default-branch HEAD is "
                                  "resolved and pinned now (still no drift — "
                                  "you just don't supply the SHA). Requires "
                                  "git on the host when omitted.")
    pa.add_argument("--setup", help="shell command run in the clone dir after "
                                    "checkout (e.g. 'bash setup.sh'); runs on "
                                    "every boot, must be idempotent")
    pa.add_argument("--mount", metavar="CONTAINER_PATH",
                    help=f"absolute container path the external folder lands "
                         f"at (default {pi_isolated_registry.DEFAULT_MOUNT!r}; "
                         f"keep under /workspace/)")
    pa.add_argument("--description",
                    help="operator note surfaced in `sandbox list`")
    pa.add_argument("--no-verify", action="store_true",
                    help="skip the `git ls-remote` repo/ref check at add time")
    pa.set_defaults(func=cmd_sandbox_add)

    pl = sbr_sub.add_parser("list",
                            help="list all sandbox types (built-in baked + BYO)")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_sandbox_list)

    pr = sbr_sub.add_parser("remove", help="remove a BYO type from the registry")
    pr.add_argument("name")
    pr.add_argument("--force", action="store_true",
                    help="remove even if projects currently enable it")
    pr.set_defaults(func=cmd_sandbox_remove)

    sbe = sbr_sub.add_parser("enable",
                             help="flag a sandbox (baked or BYO) for auto-enable "
                                  "in new projects (like `mcp enable`)")
    sbe.add_argument("name")
    sbe.set_defaults(func=cmd_sandbox_default_enable)
    sbd = sbr_sub.add_parser("disable",
                             help="clear a sandbox's default-enable flag")
    sbd.add_argument("name")
    sbd.set_defaults(func=cmd_sandbox_default_disable)

    psr = sbr_sub.add_parser("set-root", help="change a BYO type's host root folder")
    psr.add_argument("name")
    psr.add_argument("root", metavar="HOST_DIR")
    psr.set_defaults(func=cmd_sandbox_set_root)

    pds = sbr_sub.add_parser("describe", help="set or clear a BYO type's description")
    pds.add_argument("name")
    pdg = pds.add_mutually_exclusive_group(required=True)
    pdg.add_argument("text", nargs="?", help="new description text")
    pdg.add_argument("--clear", action="store_true", help="remove the description")
    pds.set_defaults(func=cmd_sandbox_describe)

    # ----- webui (browser SSH multiplexer) ------------------------------
    wu = sub.add_parser("webui", help="manage the optional browser UI container")
    wu_sub = wu.add_subparsers(dest="webui_action", required=True)

    wus = wu_sub.add_parser("start",
                            help="start the webui (builds image if missing)")
    wus.add_argument("--bind",
                     help="host IP to bind to (default 127.0.0.1; set to 0.0.0.0 "
                          "to expose on LAN; updates .env, regenerates TLS cert SAN)")
    wus.add_argument("--port",
                     help="host port to bind to (default 7777; updates .env). "
                          "Container always listens on 7777 internally")
    wus.add_argument("--rebuild", action="store_true",
                     help="rebuild the webui image and recreate the container "
                          "(use after editing webui/server.py, app.js, etc.)")
    wus.set_defaults(func=cmd_webui)

    wup = wu_sub.add_parser("stop", help="remove the webui container")
    wup.set_defaults(func=cmd_webui)

    wut = wu_sub.add_parser("status", help="show webui status")
    wut.set_defaults(func=cmd_webui)

    wui = wu_sub.add_parser(
        "import",
        help="print the base64 import string the SPA's add-project form auto-fills from",
    )
    wui.add_argument("project", nargs="?",
                     help="project name; omit to list every project that has SSH info")
    wui.set_defaults(func=cmd_webui)

    wct = wu_sub.add_parser(
        "cert-tailscale",
        help="replace the self-signed cert with a Tailscale-issued "
             "Let's Encrypt cert (fixes browser ServiceWorker / webview)",
        description="Auto-detects the host's tailnet FQDN, runs "
                    "`tailscale cert <fqdn>`, stages the resulting "
                    "cert+key into the rs-webui-tls volume, and recreates "
                    "the webui container so the new cert takes effect. "
                    "Requires HTTPS enabled in your tailnet's admin UI "
                    "(login.tailscale.com/admin/dns) and either sudo or "
                    "`sudo tailscale up --operator=$USER` set once.",
    )
    wct.set_defaults(func=cmd_webui)

    return p


def main() -> None:
    load_config()
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
