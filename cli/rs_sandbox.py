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
# Per-box loopback publish-port pool (F3 Slice 2b). When a box exposes a
# 127.0.0.1 service, `docker run -p <pub_super>:<app_port>` publishes it onto the
# SUPERVISOR's netns (like the editor pool) and an in-box forwarder bridges
# lo→eth0 so the DNAT reaches it. A DISTINCT band from the editor pool (both live
# on the supervisor netns; disjoint so they never collide); 100 ports ≫ the 12-IP
# box ceiling. Lockstep: rscore._EXPORT_BOX_LOOPBACK_PORT_LO/HI reserves this same
# band from operator-registerable top-level ports.
BOX_LOOPBACK_PORT_LO = 8600
BOX_LOOPBACK_PORT_HI = 8699  # inclusive
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

# The placeholder the dev instructions carry for the box's base branch,
# substituted at the CLAUDE.md write so no template token reaches the agent.
# MIRROR-PAIR LOCKSTEP with cli/rscore.py's BASE_BRANCH_TOKEN — this file is
# streamed into the supervisor standalone and cannot import rscore.
BASE_BRANCH_TOKEN = "{{BASE_BRANCH}}"

# Box names: lowercase, must match the webui tab-id regex so the tab
# synthesizer renders a tab for them. Auto-named box-N.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_AUTO_RE = re.compile(r"^box-(\d+)$")

CHEATSHEET = """\
rs-sandbox — isolated boxes for running un-vetted code

  rs-sandbox create [name] [--preset TYPE] [--agent claude|none] [--editor]
                           [--fetch] [--mcps a,b] [--repo URL --ref REF --setup CMD]
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


def allocate_loopback_pub(entries: dict[str, dict]) -> int:
    """Lowest free supervisor-netns publish port for an exposed box loopback port
    (F3 Slice 2b). Scans every box's loopback_ports list (a box can expose more
    than one), the editor-pool sibling for the per-box publish surface."""
    taken: set[int] = set()
    for e in entries.values():
        if not isinstance(e, dict):
            continue
        for lp in e.get("loopback_ports") or []:
            if isinstance(lp, dict) and isinstance(lp.get("pub_super"), int):
                taken.add(lp["pub_super"])
    for port in range(BOX_LOOPBACK_PORT_LO, BOX_LOOPBACK_PORT_HI + 1):
        if port not in taken:
            return port
    die(f"box loopback publish-port pool exhausted "
        f"({BOX_LOOPBACK_PORT_LO}-{BOX_LOOPBACK_PORT_HI}); unexpose a port first")


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
                         *, strict: bool, base_branch: str = "") -> None:
    """Write the box's CLAUDE.md (instructions) + .mcp-proxy.json (the stable
    proxy source-1) into the box's workspace dir BEFORE the container starts —
    the entrypoint reads them at boot. CLAUDE.md is first-boot no-clobber (a PI
    edit survives a restart); .mcp-proxy.json is overwritten every create/restart
    so an allowlist change re-renders. The entrypoint regenerates /workspace/.mcp.json
    wholesale from .mcp-proxy.json + the image-baked stdio MCPs (idempotent across
    reboots — a fresh proxy-only source each boot).

    ``base_branch`` (dev boxes) is substituted for BASE_BRANCH_TOKEN in the
    instructions, so the agent reads a literal branch name rather than a
    template. Empty for every other preset, whose text carries no token."""
    ws = WORKSPACE / f"pi-isolated/{name}"
    ws.mkdir(parents=True, exist_ok=True)
    instr = (preset.get("instructions") or "").strip()
    if base_branch:
        instr = instr.replace(BASE_BRANCH_TOKEN, base_branch)
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


def _dev_run_info(repo: str, gitea_user: str) -> dict:
    """Resolve the CURRENT gitea wiring for a dev box run: the host-staged
    non-secret dev-gitea.json (the gitea IP) + the BOX's OWN consumer identity
    (per-consumer forks: ``gitea_user`` from the box's entry, minted host-side
    at box add; its token staged as ``~/.dev-tokens/<user>.token``). Read at
    EVERY run/re-run, so a recreate/restart always picks up the current gitea
    IP (the stale-IP heal is exactly `rs-sandbox restart` after the host
    re-stages)."""
    if not gitea_user:
        die("this dev box carries no gitea identity (pre-fork-model entry); "
            "discard it and re-add the box")
    try:
        data = json.loads(DEV_GITEA_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        die("no dev wiring staged (.orchestrator/dev-gitea.json missing or invalid); "
            "re-add the box (host-side box add stages the wiring)")
    repos = {r.get("repo"): r for r in (data.get("repos") or [])
             if isinstance(r, dict) and r.get("repo")}
    if repo not in repos:
        die(f"repo {repo!r} carries no staged wiring for this project "
            f"(staged: {sorted(repos) or 'none'}); re-add the box")
    gitea_ip = (data.get("gitea_ip") or "").strip()
    if not gitea_ip:
        die("staged dev wiring carries no gitea_ip; restart the project or "
            "re-add the box")
    token_path = DEV_TOKENS_DIR / f"{gitea_user}.token"
    try:
        token = token_path.read_text().strip()
    except OSError:
        token = ""
    if not token:
        die(f"consumer token missing at {token_path}; restart the project "
            f"(the host re-stages consumer tokens) or re-add the box")
    return {"gitea_ip": gitea_ip, "repo": repo, "token": token,
            "user": gitea_user}


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


# The opt-in fetch surface's supervisor-side halves: the host stages these
# into THIS supervisor when the project carries rs-fetch consumers; an
# rs-fetch-ENABLED box run copies them in (universal staging is retired).
RS_FETCH_BIN = "/usr/local/bin/rs-fetch"
OPERATOR_TOKEN_FILE = DEV_TOKENS_DIR / "operator.token"
FETCH_WIRING_FILE = DEV_TOKENS_DIR / "fetch-wiring.json"


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
    """Copy the opt-in fetch surface into a just-run rs-fetch-ENABLED box: the
    rs-fetch tool (from this supervisor's own staged copy, root-owned 0755) +
    the READ-ONLY operator token (0600, stdin as the box user — never argv,
    never the workspace) + the global fetch wiring (non-secret repo->owner
    rows, to ~/.dev-tokens/fetch-wiring.json where rs-fetch reads it). Called
    ONLY for a fetch-flagged box now, so missing supervisor halves are a LOUD
    warning (the surface was explicitly requested — the retired universal
    staging skipped silently): the heal is a project stop/start (the host
    re-stages the halves for fetch-consumer projects), then `rs-sandbox
    restart` of this box. A staging FAILURE warns and leaves the box usable
    without fetch (never die — the box itself is fine)."""
    try:
        tok = OPERATOR_TOKEN_FILE.read_text().strip()
    except OSError:
        tok = ""
    if not tok or not os.path.isfile(RS_FETCH_BIN):
        print(f"warning: rs-fetch box {cname!r}: the supervisor-side fetch "
              f"halves are not staged (tool "
              f"{'present' if os.path.isfile(RS_FETCH_BIN) else 'missing'}, "
              f"operator token {'present' if tok else 'missing'}); stop/start "
              f"the project to re-stage them, then restart this box",
              file=sys.stderr)
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
    # Global fetch wiring (non-secret; rs-fetch's fork-owner source in a box).
    # Warn-skip when the supervisor copy is absent — an older staging; heals
    # the same way as the halves above.
    try:
        wiring = FETCH_WIRING_FILE.read_text()
    except OSError:
        wiring = ""
    if not wiring:
        print(f"warning: fetch wiring missing at {FETCH_WIRING_FILE}; rs-fetch "
              f"in box {cname!r} cannot resolve fork owners — stop/start the "
              f"project to re-stage it, then restart this box", file=sys.stderr)
        return
    r = subprocess.run(
        ["docker", "exec", "-i", "-u", "worker", cname, "sh", "-c",
         'umask 077 && mkdir -p "$HOME/.dev-tokens" && '
         'cat > "$HOME/.dev-tokens/fetch-wiring.json"'],
        input=wiring.encode(), capture_output=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or b"").decode(errors="replace").strip()
        print(f"warning: fetch-wiring staging into box {cname!r} failed: "
              f"{err}", file=sys.stderr)


def _install_repo_watch(cname: str) -> None:
    """Install the rs-repo-watch launcher into a DEV box. The box image bakes
    the dev tooling at /opt/dev only (command-relevance: a non-dev box carries
    no dev commands on PATH), and the box entrypoint runs as `worker`, which
    cannot write /usr/local/bin — so the launcher lands via a root exec here,
    right after the dev box's docker run (the rs-fetch staging idiom).
    Best-effort: a failure warns; the watcher stays manually startable via
    /opt/dev/rs-repo-watch."""
    r = _docker("exec", "-u", "0", cname, "ln", "-sf",
                "/opt/dev/rs-repo-watch", "/usr/local/bin/rs-repo-watch")
    if r.returncode != 0:
        print(f"warning: installing rs-repo-watch into dev box {cname!r} "
              f"failed: {(r.stderr or r.stdout).strip()}", file=sys.stderr)


def _project_box_pair() -> dict:
    """The project's default (model, effort) pair for a BOX, from the marker the
    host wrote at create (STAGE_MODEL_SELECT). Read VERBATIM: this CLI is staged
    into the supervisor as a standalone stdlib file — there is no `cli/` package
    and no model catalog here — so it cannot resolve or validate anything. The
    host guarantees the pair is already resolved and mutually valid (an
    effort-less model never carries an effort), which is exactly why the marker is
    written with all four types drop-resolved. Absent/legacy → {} → no flags."""
    try:
        data = json.loads((WORKSPACE / ".orchestrator" / "project.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    models = data.get("models")
    if not isinstance(models, dict):
        return {}
    pair = models.get("box")
    return pair if isinstance(pair, dict) else {}


def _run_box(name: str, ip: str, *, browser: bool = False, agent: str = "none",
             editor: bool = False, editor_port: int = 0, clone_repo: str = "",
             clone_ref: str = "", clone_setup: str = "",
             dev: dict | None = None, dev_subnet: str = "", branch: str = "",
             loopback_ports: list[dict] | None = None,
             model: str = "", effort: str = "", fetch: bool = False) -> None:
    """docker run a box in the local inner dockerd. ``browser`` selects the
    Chromium-equipped image; ``agent`` (claude|none) → RS_BOX_AGENT (entrypoint
    deploys claude only for "claude", still auth-free); ``editor`` → the box's OWN
    RS_SERVICE_CODE_SERVER (box-level toggle, default off — decoupled from the
    project's editor); ``clone_*`` (BYO) → RS_BOX_CLONE_* the entrypoint clones +
    runs (as box-shell env argv, never a host shell); ``model``/``effort`` → the
    agent env this box's claude reads (a box tab runs a login shell, not the
    agent, so there is no argv to inject — it must ride the environment). The
    workspace dir is pre-staged + uid-1000-owned (see _stage_box_workspace) so
    dockerd's auto-create on -v doesn't land it root-owned."""
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
    # F3 Slice 2b: publish each exposed loopback port onto the supervisor netns.
    # -p <pub_super>:<app_port> DNATs to the box's eth0:<app_port>, where the in-box
    # forwarder (launched below) listens and relays to 127.0.0.1:<app_port>.
    lp_list = loopback_ports or []
    loopback_publish = [x for lp in lp_list
                        for x in ("-p", f"{lp['pub_super']}:{lp['app_port']}")]
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
        # The base branch rides its OWN parameter, never the `dev` dict: that
        # dict is _dev_run_info()'s return, read from the staged dev-gitea.json,
        # and will never carry a branch. Consumed by the entrypoint's first
        # clone and exported to the agent by rs-repo-watch.
        if branch:
            dev_env += ["-e", f"GITEA_BRANCH={branch}"]
    else:
        net_args = ["--network", INNER_NETWORK, "--ip", ip]
        # Opt-in rs-fetch wiring (the universal add-host is RETIRED): only an
        # rs-fetch-enabled box resolves rs-gitea (inner-container DNS cannot
        # resolve outer-bridge names, so the staged address rides --add-host —
        # fixed at run; a stale address is auto-healed by the host reconcile /
        # `rs-sandbox start`, both of which re-run on mismatch). Warn-not-die
        # on missing staging: a sick gitea must not brick a box restart — the
        # box runs fetch-less until the next healthy re-run. The dev branch
        # above injects its own from _dev_run_info (strict).
        if fetch:
            gitea_ip = _staged_gitea_ip()
            if gitea_ip:
                net_args += ["--add-host", f"rs-gitea:{gitea_ip}"]
            else:
                print(f"warning: no staged gitea address for rs-fetch box "
                      f"{name!r}; it runs without fetch until a restart after "
                      f"the project wiring is staged", file=sys.stderr)
        dev_env = []
    # Agent model + effort (STAGE_MODEL_SELECT). Fixed at `docker run` — a plain
    # `docker restart` does NOT re-evaluate env — so _rerun_box re-applies them
    # from the stored entry on every restart / supervisor recreate.
    model_env: list[str] = []
    if model:
        model_env = ["-e", f"ANTHROPIC_MODEL={model}"]
        if effort:
            model_env += ["-e", f"CLAUDE_CODE_EFFORT_LEVEL={effort}"]
    r = _docker(
        "run", "-d",
        "--name", cname,
        *net_args,
        "--restart", "unless-stopped",
        "-v", f"{WORKSPACE}/{sub}:/workspace",
        *agent_mount,
        *editor_mount,
        *editor_publish,
        *loopback_publish,
        "-e", f"RS_SERVICE_CODE_SERVER={'enabled' if editor else 'disabled'}",
        "-e", f"RS_SANDBOX_NAME={name}",
        "-e", f"RS_BOX_AGENT={agent}",
        *model_env,
        *clone_env,
        *dev_env,
        "--label", "research.sandbox=1",
        "--label", f"research.box={name}",
        image,
    )
    if r.returncode != 0:
        die(f"docker run failed for box {name!r}:\n"
            f"{(r.stderr or r.stdout).strip()}")
    # Opt-in fetch surface: rs-fetch + the operator token + the global fetch
    # wiring into the fresh box — ONLY when this box opted in (the universal
    # staging is retired; a dev box never gets it — the flag is rejected on
    # the dev preset at both gates).
    if fetch:
        _stage_box_fetch(cname)
    # Dev tooling on PATH for a DEV box only (the image bakes it at /opt/dev;
    # command-relevance — see _install_repo_watch).
    if dev:
        _install_repo_watch(cname)
    # F3 Slice 2b: launch the in-box loopback forwarders for the freshly-run box.
    _launch_box_forwarders(cname, lp_list)


def _launch_box_forwarders(cname: str, loopback_ports: list[dict]) -> None:
    """Launch the baked rs-loopback-fwd (root, so any app_port ≥1 binds) for each
    exposed loopback port. No liveness guard needed: a double launch self-resolves
    — the second forwarder's eth0 bind fails and it exits 0 with no pidfile
    (reuse_address=False). Best-effort; a hiccup warns, the box is otherwise fine."""
    for lp in loopback_ports:
        ap = lp.get("app_port")
        if not isinstance(ap, int):
            continue
        r = _docker("exec", "-d", "-u", "0", cname, "rs-loopback-fwd", str(ap))
        if r.returncode != 0:
            print(f"warning: launching loopback forwarder for app-port {ap} in "
                  f"box {cname!r} failed: {(r.stderr or r.stdout).strip()}",
                  file=sys.stderr)


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
             dev=(_dev_run_info(entry["repo"], entry.get("gitea_user") or "")
                  if is_dev else None),
             dev_subnet=entry.get("dev_subnet") or "",
             # From the STORED entry (env is fixed at docker run). Only matters
             # if the workspace was wiped and the entrypoint re-clones; on a
             # normal re-run the clone already exists and the agent's own
             # checkout is left alone.
             branch=entry.get("branch") or "",
             # Re-apply the exposed loopback publishes + forwarders from the stored
             # entry: -p is fixed at docker run, so a restart/recreate must re-run
             # them (F3 Slice 2b), same reason as editor_port/model above.
             loopback_ports=entry.get("loopback_ports") or [],
             # From the STORED entry, not re-derived: env is fixed at docker run,
             # so a restart (and the supervisor-recreate relaunch loop, which
             # lands here) must re-apply the box's own pair or it evaporates.
             model=entry.get("model") or "",
             effort=entry.get("effort") or "",
             # Same reasoning: the rs-fetch add-host + staging must re-apply
             # from the stored entry on every re-run.
             fetch=bool(entry.get("fetch")))


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
    branch = (args.branch or "").strip()
    mcps = _parse_csv(args.mcps)
    if branch and not is_dev:
        die("--branch is only valid for a dev box; a byo box pins its clone "
            "with --ref (which also accepts a tag or commit)")
    dev_info: dict | None = None
    if is_dev:
        if not repo:
            die(f"the {args.preset!r} preset requires --repo <name> (an added "
                f"dev repo)")
        if ref or setup:
            die("--ref/--setup are not valid for a dev box (use --branch to "
                "pick the base branch; setup runs are the agent's own work)")
        if mcps:
            die("--mcps is not valid for a dev box (its dedicated bridge has no "
                "path to mcp-proxy)")
        if args.fetch:
            die("--fetch is not valid for a dev box (it works its own fork; "
                "the read-only fetch surface is for non-dev boxes)")
        # Per-consumer forks: the box's gitea identity is minted HOST-side
        # (research/webui box add) before this runs and arrives as
        # --gitea-user; a bare in-supervisor `rs-sandbox create` cannot mint
        # it (the gitea admin token is host-only).
        if not (args.gitea_user or "").strip():
            die("a dev box is created via `research project box add` (or the "
                "webui box window), which mints its gitea identity — "
                "--gitea-user is required here")
        # Same reasoning as --gitea-user: the host resolves the base branch
        # against the mirror (existence-checked, ""-means-repo-default) BEFORE
        # this runs and passes it concretely. Requiring it here is what makes
        # the token-survives case UNREACHABLE: an empty branch would skip
        # _stage_box_workspace's substitution and write a CLAUDE.md whose every
        # git recipe reads `git checkout {{BASE_BRANCH}}` — a failure visible
        # only inside the agent's own session.
        if not branch:
            die("a dev box is created via `research project box add` (or the "
                "webui box window), which resolves its base branch against the "
                "mirror — --branch is required here")
        dev_info = _dev_run_info(repo, args.gitea_user.strip())
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
    fetch = bool(args.fetch)
    # Fail LOUD when the editor is requested but the dist is not staged — the
    # silent alternative (_run_box skips the mount, the entrypoint skips the
    # deploy, the box boots editor-less with no error anywhere) is the exact
    # defect this kills. POPULATED sentinel, never a bare isdir: a partial/
    # interrupted stage leaves the dir without the launcher, and isfile follows
    # the relativized launcher symlink, which resolves inside a complete staged
    # tree by construction. Pre-state-write: no entry, no port, no container
    # exists yet. Wording is webui-safe (die text reaches the browser via the
    # op tail): both remedies are webui-doable.
    if editor and not os.path.isfile(
            os.path.join(EDITOR_DIST_MOUNT, ".local/bin/code-server")):
        die("this box requests the editor, but the editor files are not "
            "staged in this project — install the editor from the Software "
            "page if it is missing on this host, then stop and start the "
            "project to stage it, and retry")
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
    _stage_box_workspace(name, preset, mcps, strict=True, base_branch=branch)
    # Agent model + effort (STAGE_MODEL_SELECT). Explicit flags win; otherwise the
    # project's `box` default from the marker, read verbatim (the host resolved and
    # effort-drop-checked it at create — nothing here can validate). Persisted on
    # the entry so _rerun_box re-applies it: docker run env is fixed at run.
    _pair = _project_box_pair()
    model = (args.model or "").strip() or (_pair.get("model") or "")
    effort = (args.effort or "").strip() or (_pair.get("effort") or "")
    entry = {"kind": KIND, "ip": ip, "container": box_container(name),
             "preset": args.preset, "browser": browser, "agent": agent,
             "editor": editor, "upstream_mcps": mcps,
             "model": model, "effort": effort}
    if editor:
        entry["editor_port"] = editor_port
    if fetch:
        # Persisted so _rerun_box re-applies the add-host + re-copies the fetch
        # halves on every restart/recreate (docker run wiring is fixed at run),
        # and so the host-side reconciles can find this box.
        entry["fetch"] = True
    if is_clone:
        entry.update({"repo": repo, "ref": ref, "setup": setup})
    if is_dev:
        entry.update({"dev": True, "repo": repo, "dev_subnet": dev_subnet,
                      "branch": branch,
                      "gitea_user": args.gitea_user.strip()})
    entries[name] = entry
    save(entries)
    _run_box(name, ip, browser=browser, agent=agent, editor=editor,
             editor_port=editor_port, clone_repo=repo if is_clone else "",
             clone_ref=ref, clone_setup=setup,
             dev=dev_info, dev_subnet=dev_subnet, branch=branch,
             model=model, effort=effort, fetch=fetch)
    print(json.dumps({"name": name, "ip": ip, "preset": args.preset,
                      "browser": browser, "agent": agent, "editor": editor,
                      "editor_port": editor_port or None, "fetch": fetch,
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


def cmd_expose(args: argparse.Namespace) -> None:
    """Expose a box's 127.0.0.1:<app_port> as a webui tab (F3 Slice 2b). Allocate
    a supervisor-netns publish port, record it on the entry, re-run the box (so the
    new -p publish + forwarder take effect — -p is fixed at docker run), and print
    the pub_super for the host to register. Idempotent on app_port."""
    _require_dind_project()
    entries = load()
    entry = _box_entry(entries, args.name)
    app_port = args.app_port
    lp_list = entry.get("loopback_ports") or []
    for lp in lp_list:
        if isinstance(lp, dict) and lp.get("app_port") == app_port:
            print(json.dumps({"pub_super": lp["pub_super"], "app_port": app_port}))
            return
    pub = allocate_loopback_pub(entries)
    lp_list.append({"app_port": app_port, "pub_super": pub})
    entry["loopback_ports"] = lp_list
    entries[args.name] = entry
    save(entries)
    _rerun_box(args.name, entry)
    print(json.dumps({"pub_super": pub, "app_port": app_port}))


def cmd_unexpose(args: argparse.Namespace) -> None:
    """Drop an exposed loopback port (F3 Slice 2b): remove it from the entry and
    re-run the box (rm+run without the -p — the removed port's forwarder dies with
    the old container). A no-op if the app_port wasn't exposed."""
    _require_dind_project()
    entries = load()
    entry = _box_entry(entries, args.name)
    old = entry.get("loopback_ports") or []
    lp_list = [lp for lp in old
               if not (isinstance(lp, dict) and lp.get("app_port") == args.app_port)]
    if len(lp_list) == len(old):
        # Nothing was exposed on this app_port — a true no-op, don't restart the box.
        print(json.dumps({"ok": True, "app_port": args.app_port, "changed": False}))
        return
    entry["loopback_ports"] = lp_list
    entries[args.name] = entry
    save(entries)
    _rerun_box(args.name, entry)
    print(json.dumps({"ok": True, "app_port": args.app_port, "changed": True}))


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
        dev = _dev_run_info(entry["repo"], entry.get("gitea_user") or "")
        ins = _docker("inspect", "-f", "{{json .HostConfig.ExtraHosts}}", cname)
        # Quoted JSON form — a bare substring would false-match a prefix ip.
        if f"\"rs-gitea:{dev['gitea_ip']}\"" not in (ins.stdout or ""):
            _rerun_box(args.name, entry)
            print(f"box {args.name!r}: re-run at {entry['ip']} "
                  f"(gitea address changed while parked)")
            return
    if exists and entry.get("fetch"):
        # Same reconcile for an rs-fetch box: its baked rs-gitea add-host is
        # fixed at docker run. Guard on a NON-EMPTY staged ip — with "" the
        # quoted form below never matches, so every start of a parked fetch
        # box on a wiring-less project would pointlessly re-run it.
        gitea_ip = _staged_gitea_ip()
        if gitea_ip:
            ins = _docker("inspect", "-f", "{{json .HostConfig.ExtraHosts}}", cname)
            # Quoted JSON form — a bare substring would false-match a prefix ip.
            if f"\"rs-gitea:{gitea_ip}\"" not in (ins.stdout or ""):
                _rerun_box(args.name, entry)
                print(f"box {args.name!r}: re-run at {entry['ip']} "
                      f"(gitea address changed while parked)")
                return
    if exists:
        r = _docker("start", cname)
        if r.returncode != 0:
            die(f"failed to start box {args.name!r}: "
                f"{(r.stderr or r.stdout).strip()}")
        # A plain `docker start` keeps the -p publishes but the exec'd forwarder
        # processes died with the stop — relaunch them (F3 Slice 2b). The
        # _rerun_box branches above/below relaunch via _run_box.
        _launch_box_forwarders(cname, entry.get("loopback_ports") or [])
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
    c.add_argument("--model", default="",
                   help="agent model for this box (default: the project's box "
                        "default, set at project create)")
    c.add_argument("--effort", default="",
                   help="agent effort level for this box (default: the project's "
                        "box default; ignored for a model with no effort levels)")
    c.add_argument("--editor", action="store_true",
                   help="bundle the code-server editor into this box")
    c.add_argument("--fetch", action="store_true",
                   help="wire this box for rs-fetch (read-only fetch of "
                        "dev-lane agent commits from the shared gitea); the "
                        "host box-add path stages the fetch surface this "
                        "copies from — rejected on a dev preset")
    c.add_argument("--mcps", default="",
                   help="comma-separated project MCP names to wire into the box "
                        "(forces the agent on)")
    c.add_argument("--repo", default="",
                   help="(byo preset) git repo URL to clone into the box at boot")
    c.add_argument("--ref", default="",
                   help="(byo preset) git ref to check out")
    c.add_argument("--branch", default="",
                   help="(dev preset) base branch the agent works; the host "
                        "box-add path resolves and validates it against the "
                        "mirror before this runs")
    c.add_argument("--setup", default="",
                   help="(byo preset) setup command to run in the clone")
    c.add_argument("--gitea-user", default="", dest="gitea_user",
                   help="(dev preset) the box's gitea consumer username, minted "
                        "host-side by box add (per-consumer forks)")
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

    ex = sub.add_parser("expose",
                        help="publish a box's 127.0.0.1:<app_port> as a webui tab")
    ex.add_argument("name")
    ex.add_argument("app_port", type=int)
    ex.set_defaults(func=cmd_expose)

    ux = sub.add_parser("unexpose", help="drop an exposed loopback port")
    ux.add_argument("name")
    ux.add_argument("app_port", type=int)
    ux.set_defaults(func=cmd_unexpose)
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
