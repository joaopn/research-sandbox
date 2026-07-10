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
# Per-box editor host-port pool. When a box opts into the editor, the inner
# `docker run -p <port>:8443` publishes the box's code-server stub onto the
# SUPERVISOR's network namespace (the inner dockerd's host), so the webui reaches
# it at rs-project-<proj>:<port>. Distinct from the box-internal 8443/8444 (those
# live in the box netns); 100 ports ≫ the 12-IP box ceiling. Allocated
# sequentially, the same discipline as the IP pool.
BOX_EDITOR_PORT_LO = 8500
BOX_EDITOR_PORT_HI = 8599  # inclusive
KIND = "sandbox"

# --- dev-lane constants (STAGE_DEV_GITEA) ------------------------------------
# A dev box runs on its OWN bridge (one /24 per box — "agent + nothing else":
# no L2 adjacency between agents, and no path to mcp-proxy on rs-inner, which is
# why --mcps is rejected for dev boxes). The subnets are explicitly PINNED: the
# inner daemon's default pools start at 172.17/16 and the OUTER per-project
# bridge draws from the host daemon's same 172.16/12 default pools — an unpinned
# dev bridge can land on the outer subnet, and its connected route would then
# swallow gitea-bound traffic. 192.168.100+ mirrors the rs-inner 192.168.99.0/24
# pin; 12 subnets match the 12-box IP-pool ceiling above.
DEV_NET_PREFIX = "rs-dev-"
DEV_SUBNET_BASE = 100                 # 192.168.<base+n>.0/24
DEV_SUBNET_COUNT = 12
# The box's pinned address on its own /24 (.1 is the bridge gateway). A static
# --ip is valid here because the dev bridge has a user-configured subnet.
DEV_BOX_HOST_OCTET = 2
# Container hardening for dev boxes, ported verbatim from the agentic-dev-sandbox
# agent containers: drop all capabilities, re-add only what an unprivileged
# interactive dev container needs; bound the pid count (fork-bomb backstop).
# Spike-verified: entrypoint, git, byobu/tmux, and the claude binary all run
# under this set.
DEV_CAPS = [
    "--cap-drop=ALL",
    "--cap-add=CHOWN", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER",
    "--cap-add=SETGID", "--cap-add=SETUID", "--cap-add=KILL",
    "--cap-add=FSETID", "--cap-add=AUDIT_WRITE", "--cap-add=NET_RAW",
]
DEV_PIDS_LIMIT = "512"
# Host-staged dev wiring: the non-secret half (gitea_ip + attached repos) lives
# in the bind-mounted .orchestrator/ (webui-readable — NEVER a token); the token
# is staged separately into the supervisor's own fs, OUTSIDE the workspace.
DEV_GITEA_JSON = ORCH / "dev-gitea.json"
DEV_TOKENS_DIR = Path(os.environ.get("HOME", "/home/research")) / ".dev-tokens"
# In-box gitea URL: the box resolves the NAME via its --add-host entry.
DEV_GITEA_URL = "http://rs-gitea:3000"

# Box names: lowercase, must match the webui tab-id regex so the tab
# synthesizer renders a tab for them. Auto-named box-N.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_AUTO_RE = re.compile(r"^box-(\d+)$")

CHEATSHEET = """\
rs-sandbox — isolated boxes for running un-vetted code

  rs-sandbox create [name] [--preset TYPE] [--agent claude|none] [--editor]
                           [--mcps a,b] [--repo URL --ref REF --setup CMD]
                              spin a box (auto-named box-N). --preset picks the box
                              type (empty, dev, websearcher, data-wrangler, byo, or
                              an operator-registered type); the agent defaults per
                              preset; --mcps wires project MCPs (forces the agent on);
                              --repo/--ref/--setup seed a byo box from a repo. A dev
                              box takes --repo <name> (an attached gitea repo) and
                              boots with the fork cloned + hardened.
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


def allocate_dev_subnet(entries: dict[str, dict]) -> tuple[str, str]:
    """A free (subnet, box_ip) pair for a dev box's dedicated bridge — the
    sequential-scan discipline of allocate_ip, keyed on the stored dev_subnet."""
    taken = {e.get("dev_subnet") for e in entries.values() if isinstance(e, dict)}
    for n in range(DEV_SUBNET_COUNT):
        octet = DEV_SUBNET_BASE + n
        subnet = f"192.168.{octet}.0/24"
        if subnet not in taken:
            return subnet, f"192.168.{octet}.{DEV_BOX_HOST_OCTET}"
    die(f"dev subnet pool exhausted (192.168.{DEV_SUBNET_BASE}-"
        f"{DEV_SUBNET_BASE + DEV_SUBNET_COUNT - 1}.0/24); discard an unused dev box first")


def allocate_editor_port(entries: dict[str, dict]) -> int:
    taken = {e.get("editor_port") for e in entries.values() if isinstance(e, dict)}
    for port in range(BOX_EDITOR_PORT_LO, BOX_EDITOR_PORT_HI + 1):
        if port not in taken:
            return port
    die(f"box editor-port pool exhausted "
        f"({BOX_EDITOR_PORT_LO}-{BOX_EDITOR_PORT_HI}); discard an unused box first")


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


def _dev_run_info(repo: str) -> dict:
    """Resolve the CURRENT gitea wiring for a dev box run: the host-staged
    non-secret dev-gitea.json + the separately-staged token file. Read at EVERY
    run/re-run, so a recreate/restart always picks up the current gitea IP (the
    stale-IP heal is exactly `rs-sandbox restart` after the host re-stages)."""
    try:
        data = json.loads(DEV_GITEA_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        die("no dev wiring staged (.orchestrator/dev-gitea.json missing or invalid); "
            "attach a repo first: research dev attach <project> --repo <repo>")
    repos = {r.get("repo"): r for r in (data.get("repos") or [])
             if isinstance(r, dict) and r.get("repo")}
    if repo not in repos:
        die(f"repo {repo!r} is not attached to this project "
            f"(attached: {sorted(repos) or 'none'}); run "
            f"`research dev attach <project> --repo {repo}` first")
    gitea_ip = (data.get("gitea_ip") or "").strip()
    if not gitea_ip:
        die("staged dev wiring carries no gitea_ip; re-run `research dev attach`")
    token_path = DEV_TOKENS_DIR / f"agent-{repo}.token"
    try:
        token = token_path.read_text().strip()
    except OSError:
        token = ""
    if not token:
        die(f"agent token missing at {token_path}; re-run `research dev attach` "
            f"(it re-stages the token)")
    return {"gitea_ip": gitea_ip, "repo": repo, "token": token,
            "user": repos[repo].get("user") or f"agent-{repo}"}


def _ensure_dev_bridge(name: str, subnet: str) -> str:
    """Idempotently create the box's dedicated bridge (fresh inner dockerd after
    a supervisor recreate has no networks). Returns the network name."""
    net = f"{DEV_NET_PREFIX}{name}"
    if _docker("network", "inspect", net).returncode != 0:
        r = _docker("network", "create", "--subnet", subnet, net)
        if r.returncode != 0:
            die(f"could not create dev bridge {net!r}: "
                f"{(r.stderr or r.stdout).strip()}")
    return net


# The universal fetch surface's supervisor-side halves (STAGE_DEV_GITEA S3):
# the host stages both into THIS supervisor; every box run copies them in.
RS_FETCH_BIN = "/usr/local/bin/rs-fetch"
OPERATOR_TOKEN_FILE = DEV_TOKENS_DIR / "operator.token"


def _staged_gitea_ip() -> str:
    """The project's gitea address from the host-staged non-secret wiring file,
    or "" (pre-gitea project / not wired). Tolerant read — a plain box run must
    never fail because the dev lane doesn't exist."""
    try:
        data = json.loads(DEV_GITEA_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    ip = data.get("gitea_ip") if isinstance(data, dict) else ""
    return ip.strip() if isinstance(ip, str) else ""


def _stage_box_fetch(cname: str) -> None:
    """Copy the universal fetch surface into a just-run box: the rs-fetch tool
    (from this supervisor's own staged copy, root-owned 0755) + the READ-ONLY
    operator token (0600, stdin as the box user — never argv, never the
    workspace). SILENT skip when the supervisor halves aren't staged (pre-gitea
    project — an ordinary restart after the dev lane exists heals it, the
    greenfield posture); a staging FAILURE warns and leaves the box usable
    without fetch (never die — the box itself is fine)."""
    try:
        tok = OPERATOR_TOKEN_FILE.read_text().strip()
    except OSError:
        return
    if not tok or not os.path.isfile(RS_FETCH_BIN):
        return
    r = subprocess.run(
        ["docker", "exec", "-i", "-u", "0", cname, "sh", "-c",
         "cat > /usr/local/bin/rs-fetch && chmod 755 /usr/local/bin/rs-fetch"],
        input=Path(RS_FETCH_BIN).read_bytes(), capture_output=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or b"").decode(errors="replace").strip()
        print(f"warning: staging rs-fetch into box {cname!r} failed: "
              f"{err or 'cat returned non-zero'}", file=sys.stderr)
    r = subprocess.run(
        ["docker", "exec", "-i", "-u", "worker", cname, "sh", "-c",
         'umask 077 && mkdir -p "$HOME/.dev-tokens" && '
         'cat > "$HOME/.dev-tokens/operator.token"'],
        input=(tok + "\n").encode(), capture_output=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or b"").decode(errors="replace").strip()
        print(f"warning: operator-token staging into box {cname!r} failed: "
              f"{err}", file=sys.stderr)


def _run_box(name: str, ip: str, *, browser: bool = False, agent: str = "none",
             editor: bool = False, editor_port: int = 0, clone_repo: str = "",
             clone_ref: str = "", clone_setup: str = "",
             dev: dict | None = None, dev_subnet: str = "") -> None:
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
    # Publish the box's code-server stub (8443) onto the supervisor netns at
    # editor_port so the webui can reach it at rs-project-<proj>:<editor_port>.
    editor_publish = (["-p", f"{editor_port}:8443"]
                      if (editor and editor_port) else [])
    clone_env: list[str] = []
    if clone_repo:
        clone_env = ["-e", f"RS_BOX_CLONE_REPO={clone_repo}",
                     "-e", f"RS_BOX_CLONE_REF={clone_ref}",
                     "-e", f"RS_BOX_CLONE_SETUP={clone_setup}"]
    if dev:
        # Dev lane: dedicated pinned bridge instead of rs-inner, the gitea name
        # staged via --add-host (inner-container DNS cannot resolve outer-bridge
        # names), the ADS hardening set, and the GITEA_* env the entrypoint's dev
        # clone block + repo-watch consume. Token via env = ADS parity (visible
        # only to inner `docker inspect`, i.e. this already-authoritative shell).
        net = _ensure_dev_bridge(name, dev_subnet)
        net_args = ["--network", net, "--ip", ip,
                    "--add-host", f"rs-gitea:{dev['gitea_ip']}",
                    *DEV_CAPS, f"--pids-limit={DEV_PIDS_LIMIT}"]
        dev_env = ["-e", f"GITEA_URL={DEV_GITEA_URL}",
                   "-e", f"GITEA_USER={dev['user']}",
                   "-e", f"GITEA_TOKEN={dev['token']}",
                   "-e", f"REPO_NAME={dev['repo']}"]
    else:
        net_args = ["--network", INNER_NETWORK, "--ip", ip]
        # Universal fetch wiring (STAGE_DEV_GITEA S3): every box resolves
        # rs-gitea once the project is wired (inner-container DNS cannot
        # resolve outer-bridge names, so the staged address rides --add-host —
        # fixed at run; a stale address heals by restart, greenfield posture).
        # The dev branch above injects its own from _dev_run_info (strict).
        gitea_ip = _staged_gitea_ip()
        if gitea_ip:
            net_args += ["--add-host", f"rs-gitea:{gitea_ip}"]
        dev_env = []
    r = _docker(
        "run", "-d",
        "--name", cname,
        *net_args,
        "--restart", "unless-stopped",
        "-v", f"{WORKSPACE}/{sub}:/workspace",
        *agent_mount,
        *editor_mount,
        *editor_publish,
        "-e", f"RS_SERVICE_CODE_SERVER={'enabled' if editor else 'disabled'}",
        "-e", f"RS_SANDBOX_NAME={name}",
        "-e", f"RS_BOX_AGENT={agent}",
        *clone_env,
        *dev_env,
        "--label", "research.sandbox=1",
        "--label", f"research.box={name}",
        image,
    )
    if r.returncode != 0:
        die(f"docker run failed for box {name!r}:\n"
            f"{(r.stderr or r.stdout).strip()}")
    # Universal fetch surface: rs-fetch + the operator token into the fresh box
    # (silent no-op until the dev lane exists; warn-not-die on failure).
    _stage_box_fetch(cname)


def _rerun_box(name: str, entry: dict) -> None:
    """Re-run a box from its saved entry (restart/start after a recreate). Re-
    renders the proxy MCP source (allowlist may have changed) non-strictly, then
    re-runs from the stored axes. CLAUDE.md persists on the volume (no-clobber).
    A dev entry re-resolves its gitea wiring FRESH (staged file + token) so the
    re-run bakes the CURRENT gitea IP into --add-host — this is the stale-IP heal."""
    mcps = [m for m in (entry.get("upstream_mcps") or []) if isinstance(m, str)]
    ws = WORKSPACE / f"pi-isolated/{name}"
    ws.mkdir(parents=True, exist_ok=True)
    servers = _build_proxy_mcps(mcps, strict=False)
    (ws / ".mcp-proxy.json").write_text(
        json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n")
    is_dev = bool(entry.get("dev"))
    _run_box(name, entry["ip"], browser=bool(entry.get("browser")),
             agent=entry.get("agent", "none"), editor=bool(entry.get("editor")),
             editor_port=int(entry.get("editor_port") or 0),
             clone_repo=(entry.get("repo") or "") if not is_dev else "",
             clone_ref=entry.get("ref") or "",
             clone_setup=entry.get("setup") or "",
             dev=_dev_run_info(entry["repo"]) if is_dev else None,
             dev_subnet=entry.get("dev_subnet") or "")


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
    is_dev = bool(preset.get("dev"))
    # A clone preset may bake its repo URL (e.g. paper-orchestra); an explicit
    # --repo overrides it. ref/setup stay caller-supplied (no baked-ref presets yet).
    # A DEV preset reuses --repo with NAME semantics (an attached gitea repo);
    # ref/setup/mcps are rejected there — the dev clone is the authed gitea lane
    # (entrypoint-driven), and the dedicated dev bridge has no path to mcp-proxy.
    repo = (args.repo or "").strip() or (preset.get("repo") or "").strip()
    ref, setup = (args.ref or "").strip(), (args.setup or "").strip()
    mcps = _parse_csv(args.mcps)
    dev_info: dict | None = None
    if is_dev:
        if not repo:
            die(f"the {args.preset!r} preset requires --repo <name> (a repo "
                f"attached via `research dev attach <project> --repo <repo>`)")
        if ref or setup:
            die("--ref/--setup are not valid for a dev box (the fork's default "
                "branch is checked out; setup runs are the agent's own work)")
        if mcps:
            die("--mcps is not valid for a dev box (its dedicated bridge has no "
                "path to mcp-proxy)")
        dev_info = _dev_run_info(repo)   # dies with the attach remedy if unstaged
    elif (repo or setup) and not is_clone:
        die(f"--repo/--setup are only valid for a clone (BYO) preset; "
            f"preset {args.preset!r} does not clone")
    # Agent: explicit override, else the preset default; selecting any MCP forces
    # the agent on (nothing else can reach an MCP — STAGE_BOX_EXT_UX D-B).
    agent = args.agent or ("claude" if preset.get("agent_default") else "none")
    if mcps:
        agent = "claude"
    browser = preset.get("image") == "browser"
    editor = bool(args.editor)
    dev_subnet = ""
    if is_dev:
        # Dev boxes live on their own pinned /24, NOT the rs-inner pool: no
        # .14-.25 slot is consumed, and entry["ip"] is the box's REAL pinned
        # address on that bridge (list/restart/start stay truthful).
        dev_subnet, ip = allocate_dev_subnet(entries)
    else:
        ip = allocate_ip(entries)
    # A box editor publishes onto a per-box supervisor-netns port (0 = no editor).
    editor_port = allocate_editor_port(entries) if editor else 0
    # Stage CLAUDE.md + .mcp-proxy.json BEFORE the run (M2: the entrypoint reads
    # them at boot). strict=True → die on an MCP not in the project allowlist.
    _stage_box_workspace(name, preset, mcps, strict=True)
    entry = {"kind": KIND, "ip": ip, "container": box_container(name),
             "preset": args.preset, "browser": browser, "agent": agent,
             "editor": editor, "upstream_mcps": mcps}
    if editor:
        entry["editor_port"] = editor_port
    if is_clone:
        entry.update({"repo": repo, "ref": ref, "setup": setup})
    if is_dev:
        entry.update({"dev": True, "repo": repo, "dev_subnet": dev_subnet})
    entries[name] = entry
    save(entries)
    _run_box(name, ip, browser=browser, agent=agent, editor=editor,
             editor_port=editor_port, clone_repo=repo if is_clone else "",
             clone_ref=ref, clone_setup=setup,
             dev=dev_info, dev_subnet=dev_subnet)
    print(json.dumps({"name": name, "ip": ip, "preset": args.preset,
                      "browser": browser, "agent": agent, "editor": editor,
                      "editor_port": editor_port or None,
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
    gone (e.g. after a recreate). A parked DEV box whose baked --add-host no
    longer matches the currently staged gitea address is RE-RUN instead of
    plain-started — ExtraHosts is fixed at docker run, so a `docker start`
    would boot it stale (the workspace persists either way)."""
    _require_dind_project()
    entry = _box_entry(load(), args.name)
    cname = box_container(args.name)
    exists = _docker("container", "inspect", cname).returncode == 0
    if exists and entry.get("dev"):
        dev = _dev_run_info(entry["repo"])   # dies with the attach remedy if unstaged
        ins = _docker("inspect", "-f", "{{json .HostConfig.ExtraHosts}}", cname)
        # Quoted JSON form — a bare substring would false-match a prefix ip.
        if f"\"rs-gitea:{dev['gitea_ip']}\"" not in (ins.stdout or ""):
            _rerun_box(args.name, entry)
            print(f"box {args.name!r}: re-run at {entry['ip']} "
                  f"(gitea address changed while parked)")
            return
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
    entry = _box_entry(entries, args.name)
    _docker("rm", "-f", box_container(args.name))
    if entry.get("dev"):
        # The box's dedicated bridge goes with it (best-effort — a fresh inner
        # dockerd after a recreate may never have re-created it).
        _docker("network", "rm", f"{DEV_NET_PREFIX}{args.name}")
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
                   help="box type: empty, dev, websearcher, data-wrangler, byo, or "
                        "an operator-registered type (default empty)")
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
