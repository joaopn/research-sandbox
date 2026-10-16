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
import stat
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
# Node seed (STAGE_NODE_SEED), staged into the supervisor the same way. MIRROR of
# rscore.NODE_DIST_MOUNT (pytest-pinned). A box whose preset asks for node
# RO-mounts it and copies it into its own ~/.local as the first step of its
# run-once setup (_NODE_SETUP_STEP), so the box images carry no node of their own.
NODE_DIST_MOUNT = "/opt/node-dist"
# The populated-seed sentinel (a regular file in the seed, never a link).
NODE_DIST_BIN = f"{NODE_DIST_MOUNT}/.local/bin/node"
# The RS-owned step composed in front of a node box's preset setup, at every
# run (never stored on the entry). No-clobber: a node the box already has —
# e.g. one it upgraded itself — is kept. A failed copy ends the setup with a
# non-zero status, so the run-once sentinel is not written and the box retries,
# like any failed setup. npm's global prefix follows the node binary
# (~/.local), so `npm install -g` needs no sudo.
_NODE_SETUP_STEP = (
    f'[ -e "$HOME/.local/bin/node" ] || '
    f'{{ mkdir -p "$HOME/.local" && cp -a {NODE_DIST_MOUNT}/.local/. "$HOME/.local/"; }} '
    f'|| exit 1')
# --- Gitea history (OK_BOX slice 3) ----------------------------------------
# A history box's notes/ is a clone of its private notes repo on the shared
# Gitea; OpenKnowledge's own sync pushes it. The host provisions the repo, the
# box's own account and its token (staged into this project container at
# DEV_TOKENS_DIR/<user>.token); this tool copies the token into the box's home
# BEFORE first start (never `docker -e` — it would sit in `docker inspect`, the
# run's command line and every box process's environment) and composes an
# RS-owned setup step that wires notes/ to the repo. Only non-secret names ride
# the environment.
HISTORY_TOKEN_IN_BOX = "/home/worker/.rs-history.token"
# The Gitea's port inside the install — MIRROR of gitea.GITEA_INNER_PORT
# (pytest-pinned; this file cannot import cli/gitea.py).
HISTORY_GITEA_PORT = "3000"
_HISTORY_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
_IPV4_RE = re.compile(r"\A(?:\d{1,3}\.){3}\d{1,3}\Z")
# The clone of a notes repo and its first push (one empty commit) are small:
# 60 s covers a slow link with room to spare, and bounds a Gitea that accepts
# the connection but stalls; at expiry the step records clone-/push-failed
# (never fatal) and the next fresh start of the box retries it.
HISTORY_PUSH_TIMEOUT_S = 60
# The step's shell. Reads only the environment and the 0600 token file; writes
# the 0600 include file (by temp file + rename, only when absent in setup mode —
# a heal replaces it), the commit identity, the clone, a fixed status CODE in
# ~/.rs-history-status, and a fixed paragraph in the box's terminal message
# (replaced, never appended). git's own output is never captured anywhere.
# RS_HIST_MODE: "setup" (once per container fs, in front of the preset's setup)
# or "heal" (a running/started box after the Gitea's address changed).
_HISTORY_SCRIPT = r"""
T="$HOME/.rs-history.token"; S="$HOME/.rs-history-status"; N=/workspace/notes
INC="$HOME/.rs-history.gitconfig"
URL="http://rs-gitea:@@PORT@@/notes/$RS_HISTORY_REPO.git"
_rs_hist_text() {
  case "$1" in
    on) printf '%s\n' "History: on. These notes are kept in the Gitea repo notes/$RS_HISTORY_REPO (this box's (GIT) tab in the webui)." "Turn on \"pull and push\" sync in OpenKnowledge's settings once, after ok init, so edits are pushed.";;
    off:no-address|off:no-token) printf '%s\n' "History: off. This box started without its Gitea access; restart the project to restore it.";;
    off:clone-failed) printf '%s\n' "History: off. The notes repo could not be cloned; restart the project before writing notes (a fresh box retries the clone).";;
    off:not-a-clone) printf '%s\n' "History: off. /workspace/notes already holds files that are not a clone of the notes repo; move the whole notes folder aside in this box (mv /workspace/notes /workspace/notes.old), then restart the project.";;
    off:push-failed) printf '%s\n' "History: off for now. The first push to the notes repo failed; it is retried at the next fresh start of this box.";;
    *) printf '%s\n' "History: off.";;
  esac
}
_rs_hist_greet() {
  G=/workspace/.rs-greeting
  [ -f "$G" ] && [ ! -L "$G" ] || return 0
  tmp=$(mktemp /workspace/.rs-greeting.XXXXXX) || return 0
  awk '/^--- Gitea history ---$/{skip=1} !skip{print} /^--- end of Gitea history ---$/{skip=0}' "$G" > "$tmp" 2>/dev/null
  { printf '%s\n' "--- Gitea history ---"; _rs_hist_text "$1"; printf '%s\n' "--- end of Gitea history ---"; } >> "$tmp"
  mv -f "$tmp" "$G"
}
_rs_hist_status() { printf '%s\n' "$1" > "$S"; _rs_hist_greet "$1"; }
[ -n "$RS_HISTORY_GITEA" ] || { _rs_hist_status off:no-address; exit 0; }
[ -s "$T" ] || { _rs_hist_status off:no-token; exit 0; }
if [ "$RS_HIST_MODE" = heal ] || [ ! -e "$INC" ]; then
  tmp=$(mktemp "$INC.XXXXXX") || { _rs_hist_status off:no-token; exit 0; }
  chmod 600 "$tmp"
  printf '[url "http://%s:%s@%s:@@PORT@@/"]\n\tinsteadOf = http://rs-gitea:@@PORT@@/\n' "$RS_HISTORY_USER" "$(cat "$T")" "$RS_HISTORY_GITEA" > "$tmp"
  mv -f "$tmp" "$INC"
fi
git config --global --get-all include.path 2>/dev/null | grep -qxF "$INC" || git config --global --add include.path "$INC"
git config --global user.name "$RS_HISTORY_USER"
git config --global user.email "$RS_HISTORY_USER@rs.invalid"
# A heal before the box's own setup finished touches only the address: the
# setup is still cloning/pushing and owns the status.
if [ "$RS_HIST_MODE" = heal ] && [ ! -f "$HOME/.rs-box-setup-done" ]; then exit 0; fi
# Clone only into NOTHING or a real empty folder — never over a file or a link
# named notes (anything else falls to the not-a-clone check below).
if [ "$RS_HIST_MODE" != heal ] && { { [ ! -e "$N" ] && [ ! -L "$N" ]; } || { [ -d "$N" ] && [ ! -L "$N" ] && [ -z "$(ls -A "$N" 2>/dev/null)" ]; }; }; then
  # Clone under a FRESH hidden name, then move it into place with one rename
  # (`mv -T`): the rename lands only onto an absent path or a still-empty
  # folder and refuses if anything was written into notes/ meanwhile, so the
  # operator's files are never touched. git cleans up only the folder it
  # created itself on a failure; a clone killed outright stays under its hidden
  # name (nothing here deletes anything) and the next fresh start clones anew.
  TC=$(mktemp -u /workspace/.rs-notes-clone.XXXXXX) || { _rs_hist_status off:clone-failed; exit 0; }
  GIT_TERMINAL_PROMPT=0 timeout @@PUSH@@ git clone -q "$URL" "$TC" >/dev/null 2>&1 || { _rs_hist_status off:clone-failed; exit 0; }
  git -C "$TC" rev-parse --verify -q HEAD >/dev/null 2>&1 || git -C "$TC" symbolic-ref HEAD refs/heads/main
  mv -T "$TC" "$N" 2>/dev/null || { _rs_hist_status off:not-a-clone; exit 0; }
fi
# A heal never clones: an empty notes/ without a clone stays clone-failed (the
# setup's verdict), never relabelled as "not a clone".
if [ "$RS_HIST_MODE" = heal ] && [ ! -d "$N/.git" ] && [ -z "$(ls -A "$N" 2>/dev/null)" ]; then
  _rs_hist_status off:clone-failed; exit 0
fi
[ "$(git -C "$N" config --get remote.origin.url 2>/dev/null)" = "$URL" ] || { _rs_hist_status off:not-a-clone; exit 0; }
if ! git -C "$N" rev-parse -q --verify main@{u} >/dev/null 2>&1; then
  git -C "$N" rev-parse --verify -q HEAD >/dev/null 2>&1 || git -C "$N" commit -q --allow-empty -m "Start the notes" >/dev/null 2>&1
  GIT_TERMINAL_PROMPT=0 timeout @@PUSH@@ git -C "$N" push -q -u origin main >/dev/null 2>&1 || { _rs_hist_status off:push-failed; exit 0; }
fi
_rs_hist_status on
""".replace("@@PORT@@", HISTORY_GITEA_PORT).replace("@@PUSH@@", str(HISTORY_PUSH_TIMEOUT_S))
# Composed into the setup as a SUBSHELL with `|| true`: an exit inside it ends
# only the subshell, so the preset's own setup still runs after it and the
# step can never be the setup's failing status.
_HISTORY_SETUP_STEP = "( RS_HIST_MODE=setup\n" + _HISTORY_SCRIPT + ") || true"

# The box's greeting, printed by its terminal tab when it opens a session
# (webui services.pi_isolated_service → greet-and-shell.sh <this path>).
GREETING_FILE = ".rs-greeting"
# Read-only folders: a top-level project folder ticked for a box is mounted
# read-only at FOLDER_MOUNT_ROOT/<name>. MIRROR of rscore.BOX_FOLDER_NAME_RE
# (pytest-pinned equal; this file cannot import rscore). Names reach the
# runtime's --mount syntax, where "," and "=" carry meaning, so the charset is
# closed; "." and ".." are refused separately (valid_folder_name).
_FOLDER_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
FOLDER_MOUNT_ROOT = "/mnt"
BOX_IMAGE = "rs-sandbox-box:latest"
# Browser variant: + @playwright/mcp + Chromium, wired into the box's claude.
BOX_IMAGE_BROWSER = "rs-sandbox-box-browser:latest"
# The browser image's baked stdio MCP server name(s). ⚠ MIRROR of
# cli/box_catalog.py::BAKED_BROWSER_MCPS — this file is staged standalone into
# the supervisor (no cli/ package in-image), so it CANNOT import the canonical
# constant; a host pytest pins the two equal. Used by cmd_create's collision
# die (two-gate lockstep with box_add's host-side pre-flight).
BAKED_BROWSER_MCPS = ("playwright",)
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
  rs-sandbox create … --folders=a,b
                              mount top-level project folders read-only in the
                              box at /mnt/<folder> (not on a dev box)
  rs-sandbox folders <name> [--set=a,b]
                              show or replace a box's read-only folders (a
                              change restarts the box fresh; --set= clears)
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
    _write_mcp_preset(ws, preset)
    _write_greeting(ws, preset)


def _write_greeting(ws: Path, preset: dict) -> None:
    """Write the preset's greeting to the box's /workspace/.rs-greeting (create
    and every re-run, so a catalog edit lands at restart). NEVER removed: no
    product path wrote this file before presets could carry a greeting, so a
    box without one may hold a greeting someone wrote by hand."""
    text = preset.get("greeting") or ""
    if not text.strip():
        return
    # The folder is writable from inside the box, so never OPEN what sits at the
    # path (a planted link could point at any supervisor file; a planted FIFO
    # would block this write — and the project restart it runs in — forever):
    # write a fresh temp file (O_EXCL|O_NOFOLLOW refuses anything pre-planted
    # there) and rename it over the path, which replaces a link, FIFO or device
    # node without opening it. A directory at the path fails the rename → warn.
    path = ws / GREETING_FILE
    tmp = ws / (GREETING_FILE + ".tmp")
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, path)
    except OSError as e:
        print(f"rs-sandbox: warning: could not write the box greeting: {e}",
              file=sys.stderr)


def _compose_setup(preset_setup: str, node: bool, history: bool = False) -> str:
    """The setup text a box runs once per container filesystem: the RS node
    step, then the RS history step, in front of the preset's own setup; a box
    with neither runs its preset setup byte-identically."""
    steps = ([_NODE_SETUP_STEP] if node else []) + \
            ([_HISTORY_SETUP_STEP] if history else [])
    if not steps:
        return preset_setup
    return "\n".join(steps + ([preset_setup] if preset_setup else []))


def valid_folder_name(name: object) -> bool:
    return (isinstance(name, str) and bool(_FOLDER_NAME_RE.match(name))
            and name not in (".", ".."))


def _parse_folders(raw: str | None) -> list[str]:
    """A --folders/--set list → sorted unique names, dying on a bad name before
    any state write. Sorted so a reordered request compares equal to the record."""
    names = _parse_csv(raw)
    for n in names:
        if not valid_folder_name(n):
            die(f"invalid folder name {n[:64]!r} (letters, digits, '.', '_' "
                f"or '-' only)")
    return sorted(set(names))


def _entry_history(entry: dict) -> dict | None:
    """A box record's validated history names, or None (never for a dev box,
    and never names the pattern refuses — the record is container-writable)."""
    h = entry.get("history")
    if entry.get("dev") or not isinstance(h, dict):
        return None
    user, repo = h.get("user"), h.get("repo")
    if not all(isinstance(v, str) and _HISTORY_NAME_RE.match(v) for v in (user, repo)):
        return None
    return {"user": user, "repo": repo}


def _entry_folders(entry: dict) -> list[str]:
    """The folder names a box's record carries (never for a dev box: dev boxes
    take no folders, so a tampered dev entry mounts nothing)."""
    if entry.get("dev"):
        return []
    raw = entry.get("folders")
    return [f for f in raw if isinstance(f, str)] if isinstance(raw, list) else []


def _folder_mounts(folders: list[str]) -> tuple[list[str], list[str], list[dict]]:
    """Check each ticked folder NOW and build its read-only mount: (argv, the
    names kept, warnings). A name that is not a real top-level folder at this
    moment — gone, a link, a file, unreadable, or a name the pattern refuses —
    is skipped with a structured warning ({folder, reason}; the folder omitted
    for a bad name) and never reaches the runtime. lstat, never stat: a link is
    refused, not followed (its target resolves differently on the host). The
    mount is --mount, not -v: a source deleted after this check fails the run
    instead of being re-created empty and root-owned."""
    argv: list[str] = []
    kept: list[str] = []
    warnings: list[dict] = []
    for name in folders:
        if not valid_folder_name(name):
            warnings.append({"kind": "folder", "reason": "bad-name"})
            continue
        src = WORKSPACE / name
        try:
            st = os.lstat(src)
        except FileNotFoundError:
            warnings.append({"kind": "folder", "folder": name, "reason": "missing"})
            continue
        except OSError:
            warnings.append({"kind": "folder", "folder": name, "reason": "unreadable"})
            continue
        if stat.S_ISLNK(st.st_mode):
            warnings.append({"kind": "folder", "folder": name, "reason": "link"})
            continue
        if not stat.S_ISDIR(st.st_mode):
            warnings.append({"kind": "folder", "folder": name, "reason": "not-a-folder"})
            continue
        argv += ["--mount", f"type=bind,source={src},"
                            f"target={FOLDER_MOUNT_ROOT}/{name},readonly"]
        kept.append(name)
    return argv, kept, warnings


def _warning_text(name: str, w: dict) -> str:
    """Human line for one warning (the in-box CLI's stderr only; the host
    builds its own wording from the structured form)."""
    if w.get("kind") == "history":
        return (f"warning: box {name!r} starts without Gitea history "
                f"({w.get('reason')})")
    folder = w.get("folder")
    what = f"folder {folder!r}" if folder else "a folder with an invalid name"
    return (f"warning: box {name!r} starts without read-only {what} "
            f"({w.get('reason')})")


def _write_mcp_preset(ws: Path, preset: dict) -> None:
    """Write /workspace/.mcp-preset.json — source-3 of the entrypoint's
    wholesale .mcp.json regen — from the preset's declared mcp_servers;
    REMOVE the file when the preset declares none (a preset that drops the key
    must not leave a stale merge source behind). The webui's RO /projects mount
    can read this file, so a field VALUE must NEVER enter it — the writer only
    handles catalog manifest data by construction; ${FIELD} references (never
    expanded values) are what land here, expanded by the agent at config load."""
    mp = ws / ".mcp-preset.json"
    servers = preset.get("mcp_servers") or {}
    if servers:
        mp.write_text(json.dumps({"mcpServers": servers}, indent=2,
                                 sort_keys=True) + "\n")
    elif mp.exists():
        mp.unlink()


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
    never fail because the dev lane doesn't exist. Only a dotted IPv4 address is
    returned: the file is writable from the project container, and the value
    lands in --add-host and in a history box's git settings."""
    try:
        data = json.loads(DEV_GITEA_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    ip = data.get("gitea_ip") if isinstance(data, dict) else ""
    ip = ip.strip() if isinstance(ip, str) else ""
    return ip if _IPV4_RE.match(ip) else ""


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
    """Install the dev tooling launchers (rs-repo-watch + rs-wt + rs-land)
    into a DEV box. The box image bakes the dev tooling at /opt/dev only
    (command-relevance: a non-dev box carries no dev commands on PATH), and
    the box entrypoint runs as `worker`, which cannot write /usr/local/bin —
    so the launchers land via a root exec here, right after the dev box's
    docker run (the rs-fetch staging idiom). Best-effort per launcher: a
    failure warns; each stays manually startable via its /opt/dev path."""
    for tool in ("rs-repo-watch", "rs-wt", "rs-land"):
        r = _docker("exec", "-u", "0", cname, "ln", "-sf",
                    f"/opt/dev/{tool}", f"/usr/local/bin/{tool}")
        if r.returncode != 0:
            print(f"warning: installing {tool} into dev box {cname!r} "
                  f"failed: {(r.stderr or r.stdout).strip()}", file=sys.stderr)


# Per-agent env for a BOX (home /home/worker) — MIRROR of rscore._agent_env
# (this file is staged into the supervisor standalone and cannot import rscore;
# the pytest pins the two equal after home substitution). Registry order; claude
# needs none. pi: its relocated agent dir + telemetry off (pi runs online).
_AGENT_BOX_ENV: dict[str, list[tuple[str, str]]] = {
    "pi": [("PI_CODING_AGENT_DIR", "/home/worker/.local/share/pi-agent/agent"),
           ("PI_TELEMETRY", "0")],
}


def _project_agents() -> list[str]:
    """The project's deployed agent set from the marker (STAGE_PI_AGENT),
    read verbatim like _project_box_pair; absent/legacy → []."""
    try:
        data = json.loads((WORKSPACE / ".orchestrator" / "project.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    agents = data.get("agents") if isinstance(data, dict) else None
    return [a for a in agents if isinstance(a, str)] if isinstance(agents, list) else []


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
             clone_ref: str = "", clone_setup: str = "", preset_setup: str = "",
             dev: dict | None = None, dev_subnet: str = "", branch: str = "",
             loopback_ports: list[dict] | None = None,
             model: str = "", effort: str = "", fetch: bool = False,
             node: bool = False, folders: list[str] | None = None,
             history: dict | None = None) -> list[dict]:
    """docker run a box in the local inner dockerd. ``browser`` selects the
    Chromium-equipped image; ``agent`` (any staged agent|none): any agent mounts
    the project's MERGED copy-source and exports RS_BOX_AGENT=claude (the baked
    entrypoint gate), so the box receives every agent the project carries, still
    auth-free; ``editor`` → the box's OWN
    RS_SERVICE_CODE_SERVER (box-level toggle, default off — decoupled from the
    project's editor); ``clone_*`` (BYO) → RS_BOX_CLONE_* the entrypoint clones +
    runs (as box-shell env argv, never a host shell); ``model``/``effort`` → the
    agent env this box's claude reads (a box tab runs a login shell, not the
    agent, so there is no argv to inject — it must ride the environment). The
    workspace dir is pre-staged + uid-1000-owned (see _stage_box_workspace) so
    dockerd's auto-create on -v doesn't land it root-owned. ``folders`` → one
    read-only /mnt/<name> mount per folder that passes its check now; returns
    the structured warnings for the ones skipped (data only — never printed
    here: a failed op relays this command's stderr verbatim)."""
    sub = f"pi-isolated/{name}"
    (WORKSPACE / sub).mkdir(parents=True, exist_ok=True)
    cname = box_container(name)
    image = BOX_IMAGE_BROWSER if browser else BOX_IMAGE
    _docker("rm", "-f", cname)  # idempotent
    # Guard the mount so a missing source can't turn a box run into a cryptic mount
    # error; the box's entrypoint absence-guard surfaces it as "claude not found".
    # Any agent selection mounts the project's MERGED copy-source (every staged
    # agent arrives in one cp); the baked entrypoint's gate is the literal
    # RS_BOX_AGENT=claude, so that is what an agent-bearing box exports below.
    agent_mount = (["-v", f"{AGENT_DIST_MOUNT}:{AGENT_DIST_MOUNT}:ro"]
                   if (agent != "none" and os.path.isdir(AGENT_DIST_MOUNT)) else [])
    # Per-agent config env for the project's deployed set (pi's relocated agent
    # dir + telemetry off): only when the box mounts the dist. (A box that existed before an add is re-run with this env
    # while its absence-guarded entrypoint keeps its old ~/.local — the env then
    # names a file that is not there, inert because the binary is not there
    # either; a re-created box gets both.) The box user is `worker`.
    agent_env: list[str] = []
    if agent != "none":
        staged = _project_agents()
        for a, pairs in _AGENT_BOX_ENV.items():
            if a in staged:
                for k, v in pairs:
                    agent_env += ["-e", f"{k}={v}"]
    # Editor dist mount, gated on the box's OWN editor toggle (STAGE_EDITOR_DIST +
    # STAGE_BOX_EXT_UX). Only mount when this box opted in AND the dist is staged.
    editor_mount = (["-v", f"{EDITOR_DIST_MOUNT}:{EDITOR_DIST_MOUNT}:ro"]
                    if (editor and os.path.isdir(EDITOR_DIST_MOUNT)) else [])
    # Node seed mount, only for a box whose entry asks for node AND only when the
    # staged seed is populated: a bare -v on a missing source would make the
    # inner dockerd create an empty root-owned /opt/node-dist, which a later
    # on-demand stage would replace — leaving this box mounted on the deleted
    # folder. Without the mount the setup's node step fails and the box retries
    # its setup until a restart after the seed is staged.
    node_mount: list[str] = []
    if node:
        if os.path.isfile(NODE_DIST_BIN):
            node_mount = ["-v", f"{NODE_DIST_MOUNT}:{NODE_DIST_MOUNT}:ro"]
        else:
            print(f"rs-sandbox: warning: box {name!r} needs node, but the node "
                  f"runtime is not staged in this project; its setup cannot "
                  f"finish until the project is restarted with node pulled",
                  file=sys.stderr)
    # Read-only folders, re-checked at every run (skip + warn). Never for a dev
    # box (it takes none). Empty for a box without folders, so its argv is
    # byte-identical to a folder-less run.
    folder_mounts, _kept, folder_warnings = (
        _folder_mounts(folders) if (folders and not dev) else ([], [], []))
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
    # The pair is claude's (ANTHROPIC_MODEL / CLAUDE_CODE_EFFORT_LEVEL); an
    # non-claude agent's box carries no RS-side model (the PI picks in-app).
    model_env: list[str] = []
    if model and agent == "claude":
        model_env = ["-e", f"ANTHROPIC_MODEL={model}"]
        if effort:
            model_env += ["-e", f"CLAUDE_CODE_EFFORT_LEVEL={effort}"]
    # Preset setup script rides env argv deliberately: it is user-VISIBLE
    # catalog data (the window shows it), never a field value.
    # Gitea history: only non-secret names ride the environment (the address,
    # the account, the repo); the token is a 0600 file copied in before the
    # first start, below. A missing address or token never stops the box — it
    # starts without history and says so (a `history` warning; the in-box step
    # records the same reason). Never for a dev box.
    hist = history if (history and not dev) else None
    hist_env: list[str] = []
    hist_token = None
    history_warnings: list[dict] = []
    if hist:
        hist_ip = _staged_gitea_ip()
        hist_env = ["-e", f"RS_HISTORY_GITEA={hist_ip}",
                    "-e", f"RS_HISTORY_USER={hist['user']}",
                    "-e", f"RS_HISTORY_REPO={hist['repo']}"]
        tok = DEV_TOKENS_DIR / f"{hist['user']}.token"
        try:
            hist_token = tok if tok.read_text().strip() else None
        except OSError:
            hist_token = None
        if not hist_ip:
            history_warnings.append({"kind": "history", "reason": "no-address"})
        elif hist_token is None:
            history_warnings.append({"kind": "history", "reason": "no-token"})
    # Preset setup script rides env argv deliberately: it is user-VISIBLE
    # catalog data (the window shows it), never a field value.
    setup = _compose_setup(preset_setup, node, history=bool(hist))
    setup_env = (["-e", f"RS_BOX_SETUP={setup}"] if setup else [])
    common = [
        "--name", cname,
        *net_args,
        "--restart", "unless-stopped",
        "-v", f"{WORKSPACE}/{sub}:/workspace",
        *agent_mount,
        *editor_mount,
        *node_mount,
        *folder_mounts,
        *editor_publish,
        *loopback_publish,
        "-e", f"RS_SERVICE_CODE_SERVER={'enabled' if editor else 'disabled'}",
        "-e", f"RS_SANDBOX_NAME={name}",
        # The entrypoint gates the dist cp on the literal "claude" (baked); every
        # agent-bearing box exports it and receives the merged set. The box's
        # own `agent` stays the selected name on its entry.
        "-e", f"RS_BOX_AGENT={'claude' if agent != 'none' else 'none'}",
        *model_env,
        *agent_env,
        *setup_env,
        *hist_env,
        *clone_env,
        *dev_env,
        "--label", "research.sandbox=1",
        "--label", f"research.box={name}",
        image,
    ]
    env_src = Path.home() / ".rs-box-env" / f"{name}.env"
    if env_src.is_file() or hist_token is not None:
        # Preset input fields: deliver ~/.rs-box.env BEFORE first boot via
        # create → cp → start — never -e (inner `docker inspect` would carry
        # the values) and never the workspace (host/webui-readable). The cp is
        # safe by construction: supervisor→inner has NO userns shift (research
        # 1000 → worker 1000) and docker cp preserves mode, so the 0600 file
        # lands owned by the box user; the box's home is image-resident (the
        # skel restore never fires there), so nothing clobbers it at boot.
        # Each step checks its own rc; a cp/start failure removes the created
        # container so no Created husk sits behind the already-saved entry
        # (cmd_create writes the entry pre-run).
        r = _docker("create", *common)
        if r.returncode != 0:
            die(f"docker create failed for box {name!r}:\n"
                f"{(r.stderr or r.stdout).strip()}")
        if hist_token is not None:
            # The history token: never fatal — a failed copy starts the box
            # without history (its step then records off:no-token).
            rt = _docker("cp", str(hist_token), f"{cname}:{HISTORY_TOKEN_IN_BOX}")
            if rt.returncode != 0:
                history_warnings.append({"kind": "history",
                                         "reason": "token-copy-failed"})
        r = _docker("cp", str(env_src), f"{cname}:/home/worker/.rs-box.env") \
            if env_src.is_file() else _docker("container", "inspect", cname)
        if r.returncode == 0:
            r = _docker("start", cname)
        if r.returncode != 0:
            _docker("rm", "-f", cname)
            die(f"failed to stage/start box {name!r} with its env file:\n"
                f"{(r.stderr or r.stdout).strip()}")
    else:
        # No env file ⇒ the pre-existing single-step run, byte-identical.
        r = _docker("run", "-d", *common)
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
    return folder_warnings + history_warnings


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


def _rerun_box(name: str, entry: dict) -> list[dict]:
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
    # Refresh the preset MCP source alongside the proxy source (write + remove
    # at BOTH sites): a catalog edit lands at restart, like an allowlist edit.
    # Tolerant — a preset since removed from the catalog keeps the existing
    # staged file rather than failing the restart (today a restart never
    # resolves the preset at all; that must not regress).
    try:
        preset = resolve_preset(entry.get("preset") or "empty")
        _write_mcp_preset(ws, preset)
        _write_greeting(ws, preset)
    except SystemExit:
        print(f"rs-sandbox: warning: preset {entry.get('preset')!r} no longer "
              f"resolvable; keeping the box's existing .mcp-preset.json",
              file=sys.stderr)
    is_dev = bool(entry.get("dev"))
    return _run_box(name, entry["ip"], browser=bool(entry.get("browser")),
             agent=entry.get("agent", "none"), editor=bool(entry.get("editor")),
             editor_port=int(entry.get("editor_port") or 0),
             clone_repo=(entry.get("repo") or "") if not is_dev else "",
             clone_ref=entry.get("ref") or "",
             clone_setup=entry.get("setup") or "",
             # Snapshot from the entry (a recreate wipes the fs + the run-once
             # sentinel, so the setup must re-run on the fresh filesystem).
             preset_setup=entry.get("preset_setup") or "",
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
             fetch=bool(entry.get("fetch")),
             # From the STORED entry: the node step is composed at each run.
             node=bool(entry.get("node")),
             # Every recorded name, re-checked at this run (skip + warn).
             folders=_entry_folders(entry),
             history=_entry_history(entry))


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
    # Read-only folders: the shape is refused here, before any state write; the
    # real-folder check runs at every box run (skip + warn), this one included.
    folders = _parse_folders(args.folders)
    # Gitea history: the host provisions the account/repo/token first and passes
    # the names; refused on a dev preset, a preset without gitea_history, and
    # together with --fetch (two-gate lockstep with box_add).
    h_user = (getattr(args, "history_user", "") or "").strip()
    h_repo = (getattr(args, "history_repo", "") or "").strip()
    history = None
    if h_user or h_repo:
        if not (h_user and h_repo and _HISTORY_NAME_RE.match(h_user)
                and _HISTORY_NAME_RE.match(h_repo)):
            die("history needs both a valid account and repo name")
        if is_dev:
            die("a dev box cannot keep notes history on Gitea")
        if not preset.get("gitea_history"):
            die(f"box type {args.preset!r} does not keep history on Gitea")
        if args.fetch:
            die("a box keeping history on Gitea cannot also take rs-fetch")
        history = {"user": h_user, "repo": h_repo}
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
        if folders:
            die("a dev box does not take read-only folders")
        # Two-gate lockstep with box_add: base-image only (the catalog forces
        # dev presets to image:'base'). Only an explicit --browser dies;
        # --no-browser/absent resolve to base below anyway.
        if args.browser:
            die("--browser is not valid for a dev box (dev presets are "
                "base-image only)")
        # The catalog validator forbids these on dev presets; a tampered staged
        # catalog is the only way here — die rather than run half-wired.
        if any(preset.get(k) for k in ("fields", "setup", "mcp_servers",
                                       "node", "greeting")):
            die("a dev preset cannot carry fields/setup/mcp_servers/node/"
                "greeting; fix the box type in the catalog")
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
    if mcps and agent == "none":
        agent = "claude"
    # Only an agent this project staged can reach the box (the merged copy-source
    # is the project's set + claude); refuse by name, before any side effect.
    carried = ["claude", *[a for a in _project_agents() if a != "claude"]]
    if agent != "none" and agent not in carried:
        die(f"this project carries {', '.join(carried)} — add {agent!r} to the "
            f"project first")
    # The model pair is claude's: refuse an explicit pair on any OTHER agent
    # rather than record one the box would silently ignore. An agent-less box
    # keeps accepting one (base behaviour; _run_box drops it where no claude
    # runs). LOCKSTEP with rscore.BoxAddRequest.from_kwargs's predicate.
    if agent not in ("claude", "none") and ((args.model or "").strip() or (args.effort or "").strip()):
        die(f"a model applies to a claude box; agent {agent!r} picks its model in-app")
    # Spawn-time tri-state (the host box_add threads --browser/--no-browser only
    # when the caller was explicit): None ⇒ the preset's image default. The
    # resolved value is persisted on the entry below, so _rerun_box re-applies
    # it across restarts/recreates with no further logic.
    browser = (args.browser if args.browser is not None
               else preset.get("image") == "browser")
    # Two-gate lockstep with box_add's host-side collision pre-flight (same
    # mirrored constant): a preset MCP name colliding with the browser image's
    # baked tools or a selected MCP would crash-loop the box at boot (the
    # entrypoint's merge refuses under --restart unless-stopped) — die here.
    preset_mcp_names = set(preset.get("mcp_servers") or {})
    clash = sorted(preset_mcp_names & set(BAKED_BROWSER_MCPS)) if browser else []
    if clash:
        die(f"preset MCP server name(s) {', '.join(clash)} collide with the "
            f"browser image's baked tools; rename them or drop the browser")
    clash = sorted(preset_mcp_names & set(mcps))
    if clash:
        die(f"preset MCP server name(s) {', '.join(clash)} collide with "
            f"selected MCP(s); deselect or rename them")
    # A fields preset needs its env file staged BEFORE the run. The sanctioned
    # path (the project's Add-box window) stages it; a direct in-supervisor run
    # is the power path — stage ~/.rs-box-env/<name>.env yourself.
    preset_setup = (preset.get("setup") or "").strip()
    if preset.get("fields"):
        if not (Path.home() / ".rs-box-env" / f"{name}.env").is_file():
            die(f"this box type takes input fields, but no env file is staged "
                f"for {name!r}; add the box from the project's Add-box window")
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
    # A node box copies the project's staged node seed at boot. Refuse before any
    # state write when it is missing (the host box add stages it on demand, so
    # this is reached only by a direct run or a seed removed since). Webui-safe:
    # the text reaches the browser through the op tail.
    node = bool(preset.get("node"))
    if node and not os.path.isfile(NODE_DIST_BIN):
        die("this box type needs node, but node is not staged in this project "
            "— pull node under Management → Software, then retry")
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
    _pair = _project_box_pair() if agent == "claude" else {}
    model = (args.model or "").strip() or (_pair.get("model") or "")
    effort = (args.effort or "").strip() or (_pair.get("effort") or "")
    entry = {"kind": KIND, "ip": ip, "container": box_container(name),
             "preset": args.preset, "browser": browser, "agent": agent,
             "editor": editor, "upstream_mcps": mcps,
             "model": model, "effort": effort}
    if editor:
        entry["editor_port"] = editor_port
    if node:
        # The node step is composed at every run from this flag (_compose_setup),
        # never stored: preset_setup stays the preset's own text.
        entry["node"] = True
    if preset_setup:
        # Snapshot (like the clone entries' setup): _rerun_box re-passes it so
        # a recreate — which wipes the container fs and the run-once sentinel —
        # re-runs the setup on the fresh filesystem.
        entry["preset_setup"] = preset_setup
    if fetch:
        # Persisted so _rerun_box re-applies the add-host + re-copies the fetch
        # halves on every restart/recreate (docker run wiring is fixed at run),
        # and so the host-side reconciles can find this box.
        entry["fetch"] = True
    if folders:
        # Every ticked name is kept (sorted): each run re-checks each one, so a
        # folder that is gone now and comes back is mounted again at a restart.
        entry["folders"] = folders
    if history:
        # Names only — the token is re-read from the staged file at every run.
        entry["history"] = history
    if is_clone:
        entry.update({"repo": repo, "ref": ref, "setup": setup})
    if is_dev:
        entry.update({"dev": True, "repo": repo, "dev_subnet": dev_subnet,
                      "branch": branch,
                      "gitea_user": args.gitea_user.strip()})
    entries[name] = entry
    save(entries)
    warnings = _run_box(name, ip, browser=browser, agent=agent, editor=editor,
                        editor_port=editor_port,
                        clone_repo=repo if is_clone else "",
                        clone_ref=ref, clone_setup=setup,
                        preset_setup=preset_setup, dev=dev_info,
                        dev_subnet=dev_subnet, branch=branch, model=model,
                        effort=effort, fetch=fetch, node=node, folders=folders,
                        history=history)
    print(json.dumps({"name": name, "ip": ip, "preset": args.preset,
                      "browser": browser, "agent": agent, "editor": editor,
                      "editor_port": editor_port or None, "fetch": fetch,
                      "folders": folders, "warnings": warnings,
                      "container": box_container(name)}, indent=2))


def cmd_restart(args: argparse.Namespace) -> None:
    """Re-run a box from its extensions.json entry. Called by the host
    _recreate_supervisor restart loop (the inner dockerd is fresh after a
    sysbox recreate) — delegated here so the docker-run logic lives in one
    place. Identical to `start` for a gone container; named `restart` for the
    recreate-loop caller's clarity."""
    _require_dind_project()
    entry = _box_entry(load(), args.name)
    warnings = _rerun_box(args.name, entry)
    # JSON: the host relaunch loop reads the folder warnings from it (its two
    # callers otherwise check only the exit status).
    print(json.dumps({"name": args.name, "ip": entry["ip"],
                      "warnings": warnings}))


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
            print(json.dumps({"pub_super": lp["pub_super"], "app_port": app_port,
                              "warnings": []}))
            return
    pub = allocate_loopback_pub(entries)
    lp_list.append({"app_port": app_port, "pub_super": pub})
    entry["loopback_ports"] = lp_list
    entries[args.name] = entry
    save(entries)
    warnings = _rerun_box(args.name, entry)
    print(json.dumps({"pub_super": pub, "app_port": app_port,
                      "warnings": warnings}))


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
        print(json.dumps({"ok": True, "app_port": args.app_port, "changed": False,
                          "warnings": []}))
        return
    entry["loopback_ports"] = lp_list
    entries[args.name] = entry
    save(entries)
    warnings = _rerun_box(args.name, entry)
    print(json.dumps({"ok": True, "app_port": args.app_port, "changed": True,
                      "warnings": warnings}))


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
                _print_warnings(args.name, _rerun_box(args.name, entry))
                print(f"box {args.name!r}: re-run at {entry['ip']} "
                      f"(gitea address changed while parked)")
                return
    # Read-only folders are re-checked at every start: a parked box whose folder
    # mounts no longer match what the check keeps now (a folder gone, now a
    # link, or back again) is re-run — its mounts are fixed at docker run.
    folders = _entry_folders(entry)
    if exists and folders:
        _argv, kept, skipped = _folder_mounts(folders)
        want = {f"{FOLDER_MOUNT_ROOT}/{n}" for n in kept}
        if _container_folder_mounts(cname) != want:
            _print_warnings(args.name, _rerun_box(args.name, entry))
            print(f"box {args.name!r}: re-run at {entry['ip']} "
                  f"(its read-only folders changed while parked)")
            return
        # Mounts already match; still say which ticked folders it lacks.
        _print_warnings(args.name, skipped)
    if exists:
        r = _docker("start", cname)
        if r.returncode != 0:
            die(f"failed to start box {args.name!r}: "
                f"{(r.stderr or r.stdout).strip()}")
        # A plain `docker start` keeps the -p publishes but the exec'd forwarder
        # processes died with the stop — relaunch them (F3 Slice 2b). The
        # _rerun_box branches above/below relaunch via _run_box.
        _launch_box_forwarders(cname, entry.get("loopback_ports") or [])
        # A parked history box missed any Gitea address change: rewrite its
        # address in place and retry a missing first push (no re-run).
        hist = _entry_history(entry)
        if hist:
            _history_heal(cname, hist)
    else:
        _print_warnings(args.name, _rerun_box(args.name, entry))
    print(f"box {args.name!r}: started at {entry['ip']}")


def _print_warnings(name: str, warnings: list[dict]) -> None:
    for w in warnings:
        print(_warning_text(name, w), file=sys.stderr)


def _container_folder_mounts(cname: str) -> set[str] | None:
    """The read-only folder mounts an existing box container carries: the
    destinations of its bind mounts under FOLDER_MOUNT_ROOT. None — which never
    equals a wanted set, so the caller re-runs — when the container cannot be
    read or carries a folder mount that is not a read-only bind."""
    r = _docker("container", "inspect", "-f", "{{json .Mounts}}", cname)
    if r.returncode != 0:
        return None
    try:
        mounts = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(mounts, list):
        return None
    out: set[str] = set()
    for m in mounts:
        if not isinstance(m, dict):
            return None
        dest = m.get("Destination")
        if isinstance(dest, str) and dest.startswith(FOLDER_MOUNT_ROOT + "/"):
            if m.get("Type") != "bind" or m.get("RW") is not False:
                return None
            out.add(dest)
    return out


def _history_heal(cname: str, hist: dict) -> bool:
    """Rewrite a RUNNING history box's Gitea address in place (the 0600
    include file, by temp file + rename) and retry a missing first push — the
    same step as the setup's, in heal mode. Only the address, account and repo
    ride the command (`-e`); the token is read inside the box from its own
    0600 file. OpenKnowledge and the agent keep running."""
    ip = _staged_gitea_ip()
    if not ip:
        return False
    r = _docker("exec", "-u", "worker", "-e", "RS_HIST_MODE=heal",
                "-e", f"RS_HISTORY_GITEA={ip}",
                "-e", f"RS_HISTORY_USER={hist['user']}",
                "-e", f"RS_HISTORY_REPO={hist['repo']}",
                cname, "bash", "-c", _HISTORY_SCRIPT)
    return r.returncode == 0


def cmd_history_heal(args: argparse.Namespace) -> None:
    """Host-driven heal after the Gitea's address changed: every RUNNING box
    of this name with history gets the new address in place."""
    _require_dind_project()
    if not _NAME_RE.match(args.name):
        die("invalid box name")
    entry = load().get(args.name)
    hist = _entry_history(entry) if isinstance(entry, dict) else None
    if not hist:
        die(f"box {args.name!r} keeps no history")
    cname = box_container(args.name)
    running = _docker("container", "inspect", "-f", "{{.State.Running}}",
                      cname).stdout.strip() == "true"
    healed = running and _history_heal(cname, hist)
    print(json.dumps({"name": args.name, "healed": bool(healed)}))


def cmd_folders(args: argparse.Namespace) -> None:
    """Show (no --set) or replace (--set=a,b; --set= clears) a box's read-only
    folders. A change is saved and the box re-run (its mounts are fixed at
    docker run); an unchanged set on a running box does nothing, while a box
    whose container is not running is re-run (the recovery for a box a daemon
    restart left down on a deleted folder). Text reaching the browser names no
    command (the host relays a failure verbatim)."""
    _require_dind_project()
    if not _NAME_RE.match(args.name):
        die("invalid box name")
    entries = load()
    entry = entries.get(args.name)
    if not isinstance(entry, dict) or entry.get("kind") != KIND:
        die(f"no box named {args.name!r} in this project")
    if args.set is None:
        print(json.dumps({"name": args.name, "folders": _entry_folders(entry)}))
        return
    if entry.get("dev"):
        die("a dev box does not take read-only folders")
    new = _parse_folders(args.set)
    old = sorted(set(_entry_folders(entry)))
    running = _docker("container", "inspect", "-f", "{{.State.Running}}",
                      box_container(args.name)).stdout.strip() == "true"
    if new == old and running:
        print(json.dumps({"name": args.name, "folders": new, "changed": False,
                          "warnings": []}))
        return
    if new:
        entry["folders"] = new
    else:
        entry.pop("folders", None)
    entries[args.name] = entry
    save(entries)
    warnings = _rerun_box(args.name, entry)
    print(json.dumps({"name": args.name, "folders": new, "changed": True,
                      "warnings": warnings}))


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
                     "dev": bool(e.get("dev")), "folders": _entry_folders(e),
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
    # LOCKSTEP with rscore._BOX_AGENTS (every known agent + none).
    c.add_argument("--agent", choices=["claude", "pi", "none"], default=None,
                   help="override the preset's agent default; any agent cp's the "
                        "project's whole agent set in (still auth-free — sign in "
                        "inside: `claude` + /login, or `pi` + /login with an API key)")
    c.add_argument("--model", default="",
                   help="agent model for this box (default: the project's box "
                        "default, set at project create)")
    c.add_argument("--effort", default="",
                   help="agent effort level for this box (default: the project's "
                        "box default; ignored for a model with no effort levels)")
    c.add_argument("--editor", action="store_true",
                   help="bundle the code-server editor into this box")
    # Tri-state on purpose (first BooleanOptionalAction in this codebase):
    # --browser / --no-browser override the preset's image, ABSENT (None) keeps
    # the preset default — a store_true could not express "not specified".
    c.add_argument("--browser", action=argparse.BooleanOptionalAction, default=None,
                   help="override the preset's image: --browser runs the "
                        "Playwright+Chromium box image, --no-browser the base "
                        "image (default: the preset's image)")
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
    c.add_argument("--history-user", default="", dest="history_user",
                   help="(gitea_history preset) the box's Gitea account, minted "
                        "host-side by box add; pass with --history-repo")
    c.add_argument("--history-repo", default="", dest="history_repo",
                   help="(gitea_history preset) the box's notes repo name")
    c.add_argument("--folders", default="",
                   help="comma-separated top-level project folders to mount "
                        "read-only at /mnt/<folder> (pass as --folders=a,b; "
                        "rejected on a dev preset)")
    c.set_defaults(func=cmd_create)

    fo = sub.add_parser("folders",
                        help="show or replace a box's read-only folders")
    fo.add_argument("name")
    fo.add_argument("--set", default=None,
                    help="the new comma-separated folder list (--set=a,b; "
                         "--set= clears); restarts the box fresh when it changes")
    fo.set_defaults(func=cmd_folders)

    hh = sub.add_parser("history-heal",
                        help="rewrite a running history box's Gitea address in place")
    hh.add_argument("name")
    hh.set_defaults(func=cmd_history_heal)

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
