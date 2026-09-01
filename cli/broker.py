"""broker — host-side daemon that exposes a closed lifecycle verb vocabulary
over a unix-domain socket, so a future browser surface can drive project
lifecycle without ever holding a docker socket.

Runs as the user, with the user's existing docker access. Opt-in
(`research broker start`); with it stopped the CLI-first system is unchanged.

Wire protocol — length-prefixed JSON over a unix socket (no HTTP, no network
listener). One request per connection:

    →  [4-byte big-endian length] [UTF-8 JSON: {"verb": <str>, "args": {…}, "token"?: <str>}]
    ←  [4-byte big-endian length] [UTF-8 JSON reply]
        success:  {"ok": true,  "result": <verb result>}
        failure:  {"ok": false, "error": {"kind": <str>, "message": <str>}}

  `token` is optional and backward-compatible: read verbs (`list`/`status`)
  omit it; write verbs require it; `login` returns a fresh one.

Containment, by construction:
  * **Closed verb allowlist** (`VERBS`) — a request can only name a verb in
    this dict; never a docker passthrough. **Deny-by-default gating**: a verb
    in `VERBS` but not in the `OPEN_VERBS` read allowlist requires a valid
    session token, so a newly-added verb is gated automatically. `login`
    (auth, no docker capability) lives outside `VERBS` on a distinct path.
  * **Filesystem-gated** — the socket lives 0600 in the user's own tree; the
    server additionally verifies the peer's uid via SO_PEERCRED (defence in
    depth atop the file perms).
  * **Bounded input** — a request frame larger than MAX_REQUEST_BYTES is
    rejected before its body is read.
  * **Verb failures don't kill the daemon** — a verb that calls rscore.die()
    raises SystemExit; the dispatcher catches it and returns an error envelope
    (the same SystemExit channel the CLI lets exit the process).

Requests are handled one at a time (a single local operator; lifecycle ops are
infrequent). Serial handling also keeps the stderr-capture below race-free.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import re
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

# broker lives in cli/; make sibling modules importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rscore  # noqa: E402
import broker_auth  # noqa: E402
import gitea  # noqa: E402  (S4: the reviewer-stash path for pre-spawn checks)
import workflow  # noqa: E402
import model_catalog  # noqa: E402  (agent model/effort catalog + per-type defaults)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Same host-side tree as the MCP registry (~/.research-sandbox/). User-owned.
BROKER_DIR = Path.home() / ".research-sandbox"
# The socket lives in a dedicated subdir so the webui can bind-mount *only* the
# socket (parent-dir mount — inode-safe across daemon restarts) WITHOUT exposing
# the rest of ~/.research-sandbox (the password hash, MCP/sandbox registries,
# audit log) to a network-facing container.
BROKER_RUN_DIR = BROKER_DIR / "run"
BROKER_SOCKET = BROKER_RUN_DIR / "broker.sock"
BROKER_PIDFILE = BROKER_DIR / "broker.pid"
BROKER_LOG = BROKER_DIR / "broker.log"

# Per-operation progress logs (WEBUI_OPLOG). Two dirs straddling the webui mount
# boundary on purpose:
#   * View log → under run/ (RO-mounted into the webui at /run/rs-broker). Holds
#     only allowlist-emit milestones (step key + safe description). Webui-visible.
#   * Full log → under BROKER_DIR (the parent — HOST-ONLY, never mounted). Holds
#     the verb's raw stdout+stderr, which carries host paths ("created data
#     directory: <abs path>", "=== Creating project: … ==="). Must stay outside
#     the mount.
BROKER_OPLOG_DIR = BROKER_RUN_DIR / "oplogs"        # .view.log — webui-visible
BROKER_FULLLOG_DIR = BROKER_DIR / "oplogs-full"     # .full.log — host-only

# Verbs that get a per-op progress log: the long-running lifecycle writes. Reads
# (OPEN_VERBS) and auth verbs never produce one. op_id-driven from the webui.
PROGRESS_VERBS = frozenset({"create", "update", "destroy", "start", "stop",
                            "box_add", "box_remove", "dev_gitea_start",
                            "dev_passwd", "dev_repo_remove",
                            "dev_purge_consumer"})

# op_id names a file, so it is validated as a safe basename before it ever does:
# first char alnum, rest alnum/dot/dash/underscore — no path separator, no
# leading dot (so "..", "/", absolute paths, and traversal are all rejected).
# The webui generates "<project>-<action>-<ts>-<rand>"; this is the containment
# check on that. Length is bounded already (the 64 KiB request frame caps it;
# the OS caps the filename) so no separate numeric limit is invented here.
_OP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Max bytes in a single request frame. Lifecycle requests are tiny (a verb name
# + a handful of short fields/lists); even a create with many --enable tokens
# and several long --data host paths stays well under a few KiB. 64 KiB leaves
# generous headroom for any legitimate request while bounding the memory a
# hostile peer can force from one frame. At ~6 KiB a legit create with several
# long paths could brush the ceiling; at ~640 KiB we'd be allocating for input
# that, for this closed verb set, is certainly malformed.
MAX_REQUEST_BYTES = 64 * 1024

# The build lock (host-only, NOT under run/) serialises the build lane: one
# agent-pull / editor-pull / fleet-rebuild at a time. It records the detached
# child's pid so a second request tells a LIVE build from a crashed one
# (stale-pid recovery — a dead pid is reclaimable). Content: {pid, op_id, verb}.
BUILD_LOCK = BROKER_DIR / "build.lock"

# Per-op liveness markers for the PARALLEL review lane (STAGE_DEV_GITEA S4).
# Reviews deliberately do NOT share BUILD_LOCK (they run N-at-a-time by PI
# decision, and run_build's finally releases the GLOBAL lock — a review riding
# that lane would unlink a concurrent build's lock). One {pid} file per op_id,
# written by the PARENT after a successful spawn, unlinked by the child's
# finally — build_alive covers review ops through these. Host-only, never
# under run/ (same reasoning as BUILD_LOCK).
REVIEW_LOCKS_DIR = BROKER_DIR / "review-locks"

# Per-op liveness markers for the PARALLEL detached dev-box lane (webui-first
# B) — the review-locks sibling: one {pid} file per op_id, parent-written after
# a successful spawn, child-unlinked in finally; build_alive covers dev-box ops
# through these. Host-only, never under run/.
DEV_BOX_LOCKS_DIR = BROKER_DIR / "dev-box-locks"

# Max bytes returned per op_full_tail poll. Bounds ONE reply frame — a fleet-build
# full log grows to MBs, and the whole point of tailing is not to ship it all at
# once. The webui advances its `from` byte cursor and polls again, so this is
# NEVER truncation-loss (the cursor is authoritative): at half this value the
# webui just polls twice as often; at 10× a single reply is larger but still one
# frame. 64 KiB mirrors the request-frame ceiling, keeping replies bounded.
OP_TAIL_MAX_BYTES = 64 * 1024

# 4-byte unsigned big-endian length prefix on every frame, both directions.
_LEN = struct.Struct(">I")
_LEN_SIZE = _LEN.size

# Seconds to wait for the socket to appear after `start` (resp. for the process
# to exit after `stop`). Startup is an import + bind (sub-second); 5 s is
# comfortable headroom on a loaded host and still fails fast if it crashed on
# boot. Polled in 0.1 s steps.
BROKER_WAIT_S = 5

# Default client read timeout. A read-only verb is instant; 10 s tolerates a
# momentarily-busy daemon (serial handling) without hanging a caller forever.
CLIENT_TIMEOUT_S = 10


# ---------------------------------------------------------------------------
# Per-operation progress logs (WEBUI_OPLOG)
# ---------------------------------------------------------------------------


class _Tee:
    """Fan one text stream out to several sinks. Used to send the verb's
    captured stdout/stderr to the host-only full log while stderr also keeps
    feeding the StringIO the dispatcher reads to build a die() error envelope."""

    def __init__(self, *sinks):
        self._sinks = sinks

    def write(self, s: str) -> int:
        for snk in self._sinks:
            snk.write(s)
        return len(s)

    def flush(self) -> None:
        for snk in self._sinks:
            with contextlib.suppress(Exception):
                snk.flush()


class _Progress:
    """Allowlist-EMIT milestone sink for one webui-fired op. Writes JSONL
    milestones to the view log (webui-visible) and flushes after each, so a
    live tail sees progress without unbuffering the whole daemon.

    Positive-emit by construction: only the fields written here reach the file
    (op_id, project, action, step key, status, a safe description, ts) — never a
    host path, inner IP, or credential. That is what makes the view log safe to
    expose, not a blocklist filter over the firehose."""

    def __init__(self, op_id: str, project, action: str, view_path: Path):
        self.op_id = op_id
        self.project = project
        self.action = action
        # Truncate: one op per op_id, so a reused handle starts clean.
        self._f = open(view_path, "w")

    def _emit(self, status: str, key: str, msg: str) -> None:
        rec = {"op_id": self.op_id, "project": self.project,
               "action": self.action, "step": key, "status": status,
               "msg": msg, "ts": time.time()}
        self._f.write(json.dumps(rec) + "\n")
        self._f.flush()

    def step(self, key: str, msg: str = "") -> None:
        self._emit("step", key, msg)

    def done(self, msg: str = "") -> None:
        self._emit("done", "done", msg)

    def fail(self, msg: str = "") -> None:
        self._emit("failed", "failed", msg)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._f.close()


class _OpLog:
    """Bundles the two log streams for one op: the view sink (`.progress`) and
    the host-only full-log file handle (`.full`). The dispatcher owns the
    terminal done()/fail() (so a verb that die()s still lands a terminal record)
    and closes both in a finally."""

    def __init__(self, op_id: str, action: str, project):
        BROKER_OPLOG_DIR.mkdir(parents=True, exist_ok=True)
        BROKER_FULLLOG_DIR.mkdir(parents=True, exist_ok=True)
        # Open the view sink first; if the full-log open then fails, close it so
        # the half-built _OpLog leaks no handle (dispatch maps the OSError to
        # bad_request and never sees an `op` to .close()).
        self.progress = _Progress(op_id, project, action,
                                  BROKER_OPLOG_DIR / f"{op_id}.view.log")
        try:
            self.full = open(BROKER_FULLLOG_DIR / f"{op_id}.full.log", "w")
        except OSError:
            self.progress.close()
            raise

    def close(self) -> None:
        self.progress.close()
        with contextlib.suppress(Exception):
            self.full.close()


def make_oplog(op_id: str, verb: str, args: dict) -> _OpLog:
    """Real op-log factory the daemon injects into dispatch. Validates op_id as
    a safe basename (it names a file) before opening anything. Raises ValueError
    on a malformed op_id so the dispatcher can answer bad_request without
    creating a file."""
    if not isinstance(op_id, str) or not _OP_ID_RE.match(op_id):
        raise ValueError(f"op_id is not a safe basename: {op_id!r}")
    return _OpLog(op_id, verb, args.get("name"))


# ---------------------------------------------------------------------------
# Verb dispatch (the closed vocabulary)
# ---------------------------------------------------------------------------


# Every verb fn takes (args, progress). progress is the op-log milestone sink
# for write verbs fired with an op_id; the read verbs accept and ignore it
# (None for CLI/socket-direct callers). The lifecycle verbs forward it to rscore
# as the optional third arg, which the CLI omits — backward-compatible.
def _verb_list(_args: dict, _progress=None) -> list[dict]:
    return [dataclasses.asdict(s) for s in rscore.list_projects()]


def _verb_status(args: dict, _progress=None) -> dict:
    req = rscore.StatusRequest.from_kwargs(**args)   # may raise ValidationError
    return dataclasses.asdict(rscore.status(req))    # may die() → SystemExit


def _verb_models(_args: dict, _progress=None) -> dict:
    """The agent model catalog + the merged per-container-type defaults, for the
    webui's model pickers (create dialog, the per-project config box, the box
    window, the dev card). The webui cannot read `models/` itself — the directory
    sits at the repo root, OUTSIDE the scoped ./webui build context — so this
    in-broker read (host python, stdlib-only `model_catalog`) is its only source,
    and it pre-selects every control from the same `defaults` the verbs fall back
    to, so the browser and the CLI cannot diverge.

    Token-gated (NOT in OPEN_VERBS) — deny-by-default like every non-list/status
    verb. A malformed catalog or a bad operator override raises ModelCatalogError,
    which dispatch does NOT catch (only Validation/Harness/SystemExit) and would
    escape into socketserver as a TRUNCATED reply — so map it to ValidationError
    here for a clean envelope (the _verb_workflows discipline)."""
    try:
        return model_catalog.payload()
    except model_catalog.ModelCatalogError as e:
        raise rscore.ValidationError(str(e))


def _verb_model_default_set(args: dict, _progress=None) -> dict:
    """Write (or clear) one container type's default (model, effort) pair in
    the operator's untracked model-defaults override — the write half of
    `models` (the Management → Infrastructure → Reviewer control). Token-gated
    (NOT in OPEN_VERBS); inline is fine — a sub-ms validated file write, no
    docker, no network. The request layer + verb map every catalog/OS error
    onto ValidationError, so nothing but the three dispatch-caught exceptions
    can escape."""
    safe = {k: v for k, v in args.items() if k in MODEL_DEFAULT_WEBUI_FIELDS}
    req = rscore.ModelDefaultSetRequest.from_kwargs(**safe)
    return rscore.model_default_set(req)


def _verb_workflows(_args: dict, _progress=None) -> dict:
    """The store catalog for the webui create form: built-ins + BYO, plus the
    agent enum and the default workflow. The webui image carries no `cli/`, so
    this in-broker read (host python, stdlib-only `workflow`) is its only catalog
    source. Token-gated (NOT in OPEN_VERBS) — deny-by-default, like every other
    non-`list`/`status` verb. A malformed BYO registry raises WorkflowError,
    which dispatch does NOT catch (only Validation/Harness/SystemExit) and would
    escape into socketserver as a truncated reply — so map it to ValidationError
    here for a clean envelope."""
    try:
        catalog = workflow.load_catalog()
    except workflow.WorkflowError as e:
        raise rscore.ValidationError(str(e))
    # Tag each entry with whether it runs the worker/extension enable cone, so the
    # create form can show the --enable presets only where they take effect
    # (research flavor) instead of offering a silent no-op on a docker box or sandbox-dind host.
    for m in catalog:
        m["has_worker_layer"] = rscore.workflow_has_worker_layer(m)
        # box_capable (STAGE_SANDBOX_DIND_AGENT): the sandbox-dind flavor shows the
        # agents + light-path group in the create dialog. Derived from data.
        m["box_capable"] = rscore.workflow_is_sandbox_dind(m)
    return {
        "workflows": catalog,                     # full manifests (substrate,
                                                  # repo/ref/setup presets, source,
                                                  # has_worker_layer)
        # Each known agent + whether its host dist is pulled (STAGE_MULTI_AGENT). The
        # create form offers ONLY staged agents as on/off boxes — never an agent it
        # can't deploy — so a relayed `agents` set always validates in from_kwargs.
        "agents": [{"name": a, "staged": rscore.dist_present(a)}
                   for a in rscore.KNOWN_AGENTS],
        # Whether the node seed is cached (STAGE_NODE_SEED) — the create form's Node
        # tickbox is an affordance off this bit (disabled + hinted when absent); the
        # real gate is from_kwargs' node floor. Same "only offer what's deployable"
        # motive as `agents` staged-state.
        "node_present": rscore.node_dist_present(),
        "default_workflow": rscore.DEFAULT_WORKFLOW,
    }


def _verb_software_status(_args: dict, _progress=None) -> dict:
    """Read-only status of the host's agent/editor dists, the built image fleet,
    and the effective version pins (base + untracked override) — the data behind
    the webui Software panel. Token-gated (NOT in OPEN_VERBS) — deny-by-default,
    like every other non-`list`/`status` verb. `software_status` is crash-proof by
    construction (guarded reads, degrade-not-raise); the belt-and-braces wrap maps
    any residual exception to a clean ValidationError envelope rather than letting
    it escape dispatch as a truncated reply (the _verb_workflows discipline)."""
    try:
        return rscore.software_status()
    except (rscore.ValidationError, rscore.HarnessError, SystemExit):
        raise
    except Exception as e:                       # pragma: no cover — defensive
        raise rscore.ValidationError(f"software status unavailable: {e}")


# The dist-refresh PREVIEW reads: resolve the upstream `latest` for a dist so the
# webui can show current→latest before the operator confirms the bump+rebuild.
# Token-gated (in VERBS, NOT OPEN_VERBS), and DELIBERATELY NOT in BUILD_DISPATCH —
# they are fast(ish) reads, not builds; the actual bump+rebuild is the separate
# BUILD_DISPATCH `agent_refresh`/`editor_refresh` lane verb, which re-resolves
# child-side (this preview value never crosses back into the apply path). The
# underlying resolve runs an in-container `curl` bounded by
# rscore._UPSTREAM_RESOLVE_MAX_TIME_S so this inline (serial-daemon) read can't
# hang the accept thread. rscore.die() on a network/upstream failure → SystemExit →
# clean failed envelope (the established path).
def _verb_agent_refresh_check(args: dict, _progress=None) -> dict:
    agent = args.get("agent", "claude")
    if agent not in rscore.KNOWN_AGENTS:
        raise rscore.ValidationError(
            f"unknown agent {agent!r} (known: {', '.join(rscore.KNOWN_AGENTS)})")
    current, latest, ext_current, ext_latest = rscore.agent_refresh_check(agent)
    # ext_* are absent-as-"" for an agent declaring no companion extension; the
    # webui branches on ext_latest being truthy, so an ext-less agent renders
    # exactly as it does today.
    return {"current": current, "latest": latest,
            "ext_current": ext_current, "ext_latest": ext_latest}


def _verb_editor_refresh_check(_args: dict, _progress=None) -> dict:
    current, latest = rscore.editor_refresh_check()
    return {"current": current, "latest": latest}


def _verb_reader_refresh_check(_args: dict, _progress=None) -> dict:
    current, latest = rscore.reader_refresh_check()
    return {"current": current, "latest": latest}


def _verb_op_full_tail(args: dict, _progress=None) -> dict:
    """Token-gated tail of a build op's HOST-ONLY full log — the raw docker/build
    firehose that carries host paths, deliberately never mounted, served ONLY
    through this authenticated relay (a compromised webui with no token can't
    reach it). Mirrors broker_op_log_handler's discipline: _OP_ID_RE basename
    guard (it names a file), guarded read, missing file → exists:false, per-poll
    byte cap (OP_TAIL_MAX_BYTES). A fast read → runs INLINE, so it stays in VERBS
    (unlike the build verbs, which spawn a child)."""
    op_id = args.get("op_id")
    if not isinstance(op_id, str) or not _OP_ID_RE.match(op_id):
        raise rscore.ValidationError(f"op_id is not a safe basename: {op_id!r}")
    try:
        frm = max(0, int(args.get("from", 0)))
    except (TypeError, ValueError):
        frm = 0
    path = BROKER_FULLLOG_DIR / f"{op_id}.full.log"
    try:
        with open(path, "rb") as f:
            f.seek(frm)
            chunk = f.read(OP_TAIL_MAX_BYTES)
    except FileNotFoundError:
        return {"exists": False, "from": frm, "next": frm, "data": ""}
    except OSError as e:
        raise rscore.ValidationError(f"cannot read op log: {e}")
    return {"exists": True, "from": frm, "next": frm + len(chunk),
            "data": chunk.decode("utf-8", "replace")}


def _verb_build_alive(args: dict, _progress=None) -> dict:
    """Token-gated liveness probe for a build op: {alive:true} iff the build lock
    holds THIS op_id with a LIVE pid. The terminal-first build tail has no other
    liveness signal, so a child hard-killed (OOM / kill -9 / power-loss) before its
    finally writes a view-log terminal would wedge the browser modal forever; this
    lets the tail detect that and stop. A safe read (in VERBS, runs inline). NB:
    run_build writes the terminal BEFORE releasing the lock, so a normal finish is
    seen as a terminal first — alive:false only ever means 'no longer running',
    after which the tail drains once more before declaring an interruption."""
    op_id = args.get("op_id")
    if not isinstance(op_id, str) or not _OP_ID_RE.match(op_id):
        raise rscore.ValidationError(f"op_id is not a safe basename: {op_id!r}")
    holder = _build_lock_holder()
    pid = holder.get("pid") if holder else None
    alive = (holder is not None and holder.get("op_id") == op_id
             and isinstance(pid, int) and _alive(pid))
    # Review + dev-box ops: the parallel lanes have no global lock — liveness
    # comes from the per-op lock files (same terminal-first property: the
    # children write done/fail before their finally unlinks the lock).
    if not alive:
        alive = _review_lock_alive(op_id) or _dev_box_lock_alive(op_id)
    return {"alive": bool(alive)}


def _verb_stop(args: dict, progress=None) -> list[dict]:
    req = rscore.StartStopRequest.from_kwargs(**args)  # may raise ValidationError
    return [dataclasses.asdict(r) for r in rscore.stop(req, progress=progress)]


def _verb_start(args: dict, progress=None) -> list[dict]:
    req = rscore.StartStopRequest.from_kwargs(**args)  # may raise ValidationError
    return [dataclasses.asdict(r) for r in rscore.start(req, progress=progress)]


# The webui-settable subset of CreateRequest fields — the broker's input
# boundary for `create`. Only these names are forwarded to from_kwargs, so a
# relayed request can NEVER set a bind-mount source (`data`), pin a host
# `ssh_port`, toggle `inner_firewall`, supply a `role_mcp_upstream`, or reach
# any future path/host-shaped field — those stay CLI-only. Deny-by-default,
# mirroring OPEN_VERBS: a new CreateRequest field is unreachable from the webui
# until someone explicitly adds it here. from_kwargs still validates these
# (name regex, workflow membership, egress enum, enable/disable token lists).
# `workflow` is the user-facing selector (substrate + flavor are derived from its
# manifest, never relayed); the old `type` flag is gone (WORKFLOW_TAXONOMY_S3).
CREATE_WEBUI_FIELDS = frozenset({
    "name", "workflow", "egress", "enable", "disable", "memory", "cpus",
    # Light-path harness payload (WORKFLOW_TAXONOMY_S4). repo/ref/setup are
    # in-box fields (they act inside the locked runc box, not on the host — see
    # the boundary note in rscore), preset by the workflow + overridable here.
    # github_pat is a SECRET in-box field: forwarded over the request envelope,
    # never logged/persisted off the box, never a CreateResult field.
    "repo", "ref", "setup", "github_pat",
    # GitHub SSH auth + git commit identity for the light-path box. IN-BOX, like
    # the PAT: they configure git INSIDE the container (~/.ssh, ~/.gitconfig) and
    # are not path/host-shaped — no bind-mount source, no host port, no daemon
    # wiring. github_ssh_key is a SECRET and satisfies the three conditions that
    # let a secret be relayed at all: it reaches the container over `docker exec`
    # STDIN (never argv/env, so it cannot surface in `docker inspect` or
    # /proc/<pid>/cmdline), it is repr=False and never a CreateResult field, and
    # from_kwargs validates it pre-side-effect (shape, mutual exclusion with
    # github_pat, GitHub-host gate).
    "github_ssh_key", "git_user_name", "git_user_email",
    # agents: the agent-dist SET — in-box field (STAGE_MULTI_AGENT; was the single
    # `agent`). A docker box deploys the full set at boot; sandbox-dind honors
    # claude on/off for its supervisor (an explicit [] = agent-less, distinct from
    # an ABSENT key = the workflow's preset — from_kwargs keeps the two apart).
    # Per-agent dist-must-exist is enforced in from_kwargs. Lockstep boundary
    # edit: the webui form POSTs `agents:[...]` when its agent cards are shown
    # (a field not in this set is silently dropped).
    "agents",
    # Agent model + effort per container type (STAGE_MODEL_SELECT). IN-BOX fields:
    # they select which model the agent INSIDE the container talks to — no path, no
    # host port, no bind-mount source — so they are openable under the host-root
    # boundary, the same class as repo/setup. Validated + resolved against the
    # catalog in from_kwargs (an unknown alias or an impossible model/effort pair is
    # refused pre-side-effect). There is deliberately no `box_*` here: a box's pair
    # is chosen per-box at box-add.
    "supervisor_model", "supervisor_effort",
    "worker_model", "worker_effort",
    "role_model", "role_effort",
    # Opt-in rs-fetch (any non-dev workflow): an IN-BOX capability toggle —
    # stages the read-only fetch tool + operator token + wiring INSIDE the
    # container (the supervisor, on dind); no path, no host port, no mount.
    # from_kwargs refuses it on the dev workflow and floors it on gitea
    # bootstrap (fail-early).
    "fetch",
})

# The webui-settable subset of UpdateRequest fields. `rebuild`/`keep_claude` are
# dropped on purpose: a webui update is the fast file-only recreate, and an
# image rebuild (`rebuild=True`) would block the serially-handled daemon for the
# whole multi-minute build — a self-inflicted DoS from the browser. Rebuilds
# stay a deliberate CLI action.
UPDATE_WEBUI_FIELDS = frozenset({
    "name", "enable", "disable", "role_mcp_upstream",
    # Agent model + effort (STAGE_MODEL_SELECT). On UPDATE an unset field means
    # LEAVE UNCHANGED — not "reset to the default" (see the UpdateRequest note).
    # A worker-only change is the cheapest write in the system: it touches no
    # container at all.
    "supervisor_model", "supervisor_effort",
    "worker_model", "worker_effort",
    "role_model", "role_effort",
})


def _verb_create(args: dict, progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in CREATE_WEBUI_FIELDS}
    req = rscore.CreateRequest.from_kwargs(**safe)  # may raise ValidationError
    return dataclasses.asdict(rscore.create(req, progress=progress))


def _verb_attach(args: dict, _progress=None) -> dict:
    """JIT keyring: return a running project's SSH coordinates (incl. password,
    in-memory). Token-gated like a write precisely because it returns the
    credential. Not in PROGRESS_VERBS — it is a fast keyring fetch, no op log."""
    req = rscore.AttachRequest.from_kwargs(**args)    # may raise ValidationError
    return dataclasses.asdict(rscore.webui_attach_info(req))  # may die() → SystemExit


def _verb_update(args: dict, progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in UPDATE_WEBUI_FIELDS}
    req = rscore.UpdateRequest.from_kwargs(**safe)   # may raise ValidationError
    return dataclasses.asdict(rscore.update(req, progress=progress))


def _verb_destroy(args: dict, progress=None) -> dict:
    """Tear a project down. Step-up re-auth (see STEP_UP_VERBS) is enforced in
    dispatch BEFORE this runs; the request's `proof` is consumed there and
    never reaches rscore (DestroyRequest reads only `name`)."""
    req = rscore.DestroyRequest.from_kwargs(**args)  # may raise ValidationError
    rscore.destroy(req, progress=progress)           # may die() → SystemExit
    return {"destroyed": req.name}


# The webui-settable subset of the box verbs' inputs — the input boundary for
# box add/remove/list, mirroring CREATE_WEBUI_FIELDS. Every field acts INSIDE the
# locked-egress, credential-free inner box (project is name-regex'd; name is
# box-regex'd; agent ∈ {claude,none}; browser is a bool) — none is host-shaped, so
# they are relayable. from_kwargs still validates them. The step-up `proof`
# for box_remove is NOT here: it is verified + consumed in dispatch, never
# forwarded to rscore.
# preset = box TYPE (catalog-gated in box_add); agent overrides the preset default;
# editor bundles code-server; fetch wires the box's opt-in read-only rs-fetch
# surface (in-box; rejected on a dev preset + gitea-bootstrap-floored in box_add);
# mcps are project MCP names (⊆ allow, gated in box_add);
# repo/ref/setup seed a `byo` box and run INSIDE it (in-box, relayable per
# WORKFLOW_TAXONOMY_S4). `browser` is a TRI-STATE spawn toggle: true/false override
# the preset's image (base vs Playwright+Chromium), absent/None keeps the preset
# default — an in-box image choice, not host-shaped; rejected (true) on dev
# presets in box_add.
BOX_ADD_WEBUI_FIELDS = frozenset({"project", "name", "preset", "agent", "editor",
                                  "browser",
                                  # Values for the preset's DECLARED input
                                  # fields ({NAME: value}; may carry secrets —
                                  # request field is repr=False, values reach
                                  # only a private env file, never argv/env
                                  # config/results/logs). Gated in box_add:
                                  # names must EQUAL the preset's declaration.
                                  "field_values",
                                  "fetch", "mcps", "repo", "ref", "setup",
                                  # Base branch for a DEV box (in-box: it selects
                                  # which branch of the agent's own fork gets
                                  # cloned). Rejected on non-dev presets in
                                  # box_add; resolved + existence-checked there.
                                  "branch",
                                  # This box's own agent model/effort; unset ⇒ the
                                  # project's `box` default from the marker.
                                  "model", "effort"})
BOX_TARGET_WEBUI_FIELDS = frozenset({"project", "name"})
# box_remove additionally accepts keep_workspace (a bool, not host-shaped): when
# set the box is removed but its artifacts stay on disk. The step-up `proof`
# stays OUT of the set (verified + consumed in dispatch, never forwarded).
BOX_REMOVE_WEBUI_FIELDS = BOX_TARGET_WEBUI_FIELDS | {"keep_workspace"}


def _verb_box_add(args: dict, progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in BOX_ADD_WEBUI_FIELDS}
    req = rscore.BoxAddRequest.from_kwargs(**safe)   # may raise ValidationError
    return dataclasses.asdict(rscore.box_add(req, progress=progress))


def _verb_box_remove(args: dict, progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in BOX_REMOVE_WEBUI_FIELDS}
    req = rscore.BoxRemoveRequest.from_kwargs(**safe)  # may raise ValidationError
    return dataclasses.asdict(rscore.box_remove(req, progress=progress))


def _verb_box_list(args: dict, _progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in BOX_TARGET_WEBUI_FIELDS}
    req = rscore.BoxListRequest.from_kwargs(**safe)   # may raise ValidationError
    return dataclasses.asdict(rscore.box_list(req))


def _verb_box_presets(args: dict, _progress=None) -> dict:
    # project-only read; box_presets maps a malformed box-registry.json to
    # ValidationError internally (verb-fn-raise), so nothing extra to catch here.
    safe = {k: v for k, v in args.items() if k == "project"}
    req = rscore.BoxPresetsRequest.from_kwargs(**safe)  # may raise ValidationError
    return dataclasses.asdict(rscore.box_presets(req))


# Exported ports (STAGE_EXPORTED_PORTS) + pass-through claims
# (STAGE_PASSTHROUGH_PORTS): every field acts on the project's own supervisor
# netns or the webui's own already-published block — none is host-shaped (cf.
# the host-root boundary). Deny-by-default field allowlist, mirroring
# BOX_ADD_WEBUI_FIELDS. Token-gated (not in OPEN_VERBS), no step-up (a tab
# carries no data), no progress (instant file write — synchronous relay).
PORT_ADD_WEBUI_FIELDS = frozenset({"project", "port", "label", "box",
                                   "passthrough", "host_port", "count"})
PORT_TARGET_WEBUI_FIELDS = frozenset({"project", "port", "passthrough"})


def _verb_port_add(args: dict, _progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in PORT_ADD_WEBUI_FIELDS}
    req = rscore.PortAddRequest.from_kwargs(**safe)   # may raise ValidationError
    return dataclasses.asdict(rscore.port_add(req))


def _verb_port_remove(args: dict, _progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in PORT_TARGET_WEBUI_FIELDS}
    req = rscore.PortRemoveRequest.from_kwargs(**safe)  # may raise ValidationError
    return dataclasses.asdict(rscore.port_remove(req))


def _verb_port_list(args: dict, _progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k == "project"}
    req = rscore.PortListRequest.from_kwargs(**safe)  # may raise ValidationError
    return dataclasses.asdict(rscore.port_list(req))


# Dev lane (STAGE_DEV_GITEA S1). Deny-by-default field allowlists, mirroring the
# port verbs. `url`/`repo`/`project` act inside gitea or on the project's own
# network — none is host-shaped (no bind-mount source, no host port). Repo
# remove joins STEP_UP_VERBS (deletes agent work). None joins OPEN_VERBS.
# There is NO inline repo-add verb: broker-side repo-add exists only inside the
# detached dev-box lane (the ~120s migrate must never run on the accept
# thread); the CLI calls rscore.dev_repo_add directly.
# The review lane's input boundary (S4). Consumed by the dispatch review
# branch + _verb_review_pr, NOT by any VERBS entry — review_pr lives in
# REVIEW_DISPATCH only (see the review-lane section). `commit` is the
# single-commit locator (exactly one of pr/commit, enforced in from_kwargs).
REVIEW_WEBUI_FIELDS = frozenset({"repo", "pr", "commit"})
# klass is GONE (STAGE_DEV_GITEA S3): attachments are agent-class only — the
# fetch surface went universal and the former "control" class was retired.
DEV_ATTACH_WEBUI_FIELDS = frozenset({"project", "repo"})
DEV_DETACH_WEBUI_FIELDS = frozenset({"project", "repo"})
DEV_TARGET_WEBUI_FIELDS = frozenset({"repo"})
# The active-fork selector (per-consumer forks): a repo name + a gitea agent
# username, both shape-validated in from_kwargs and re-gated against the
# repo's LIVE forks in the verb — neither is host-shaped.
DEV_ACTIVE_FORK_WEBUI_FIELDS = frozenset({"repo", "user"})
# Purging a retired dev identity: the gitea agent username ONLY. Shape +
# agent-prefix floor in from_kwargs, then re-gated in the verb against the
# attachment ledger AND the identity's live forks. Nothing host-shaped, and no
# repo field — the purge is user-scoped by construction.
DEV_PURGE_WEBUI_FIELDS = frozenset({"user"})
# The per-row commit dropdown read (lazy, click-triggered): a repo name + ONE
# of pr/branch (exactly-one enforced in from_kwargs, which also normalizes
# ""-vs-absent — a webui query miss must not read as a phantom field) + the
# 1-based `page` the dropdown's "show more" walks. None is host-shaped: the
# branch name is URL-quoted at the gitea client, and `page` reaches only a
# gitea query string as a from_kwargs-validated positive int.
DEV_COMMITS_WEBUI_FIELDS = frozenset({"repo", "pr", "branch", "page"})
# The dev lane's input boundaries (webui-first B; the lane keeps its dev-box
# mechanism names while carrying BOTH dev provision verbs — see
# _DEV_LANE_VERBS). Consumed by the dispatch dev-lane branch + the _verb_*
# fns, NOT by any VERBS entry — the verbs live in DEV_BOX_DISPATCH only. None
# is host-shaped: url/name/agent/editor act inside gitea / the inner box;
# workflow/egress/enable/disable are the already-relayable create policy
# fields (a subset of CREATE_WEBUI_FIELDS' vocabulary); `pat` is a SECRET
# that reaches only gitea's migrate auth_token (per-repo transient — RS stores
# no PAT): repr=False on the requests, off every Result, never argv (child
# args ride stdin), never a durable sink.
DEV_BOX_WEBUI_FIELDS = frozenset({"project", "url", "pat", "name", "agent",
                                  "editor",
                                  # The base branch this box's agent works —
                                  # in-box, and validated against the mirror
                                  # before the box is created.
                                  "branch",
                                  # The dev box's agent model/effort. Without these
                                  # the box window's dev preset would have its model
                                  # dropped HERE and again in the verb's kwarg list,
                                  # and the box would silently run the default —
                                  # on the box type where the choice matters most.
                                  "model", "effort"})
# The dev-project provision boundary (the Workflows page's dev card): minimal
# and deliberate. Adding a field here is a boundary edit — never open a
# path/host-shaped one. `workflow` threads the manifest the dialog was opened
# from (a BYO dev-flagged workflow creates what ITS manifest says); fail-closed
# in from_kwargs (dev_repo is rejected outside a dev-flagged workflow).
DEV_PROJECT_WEBUI_FIELDS = frozenset({"name", "workflow", "url", "pat",
                                      # The base branch the project's agent
                                      # works; validated against the mirror
                                      # pre-side-effect in create().
                                      "branch",
                                      "egress", "enable", "disable",
                                      # A dev project has no worker/role layer, so
                                      # the dev card carries ONE model picker — the
                                      # agent the researcher talks to. Same as any
                                      # other non-research workflow's create dialog.
                                      "supervisor_model", "supervisor_effort"})
# The gitea-password verb's input boundary: ONE field, a SECRET (repr=False on
# the request, off the Result, never a durable sink). The step-up `proof` is
# consumed in dispatch and never reaches this filter.
DEV_PASSWD_WEBUI_FIELDS = frozenset({"password"})
# Set/clear one container type's model+effort default in the operator's
# untracked override file (the Management → Infrastructure → Reviewer
# control). Name-shaped catalog tokens + a bool only — nothing host-shaped;
# the contract is full-pair-or-clear (no partial writes).
MODEL_DEFAULT_WEBUI_FIELDS = frozenset({"type", "model", "effort", "clear"})


def _verb_dev_repo_remove(args: dict, progress=None) -> dict:
    # Step-up gated (STEP_UP_VERBS) + tailed (PROGRESS_VERBS): the delete makes
    # N bounded gitea calls (enumerate + delete each retired fork + the mirror),
    # so it runs as an op rather than inside the webui's 30s relay window.
    # `progress` is forwarded so the fork-gate / delete milestones reach the tail.
    safe = {k: v for k, v in args.items() if k in DEV_TARGET_WEBUI_FIELDS}
    req = rscore.DevRepoRemoveRequest.from_kwargs(**safe)  # may raise ValidationError
    return dataclasses.asdict(rscore.dev_repo_remove(req, progress))


def _verb_dev_repo_list(_args: dict, _progress=None) -> dict:
    req = rscore.DevRepoListRequest.from_kwargs()
    return dataclasses.asdict(rscore.dev_repo_list(req))


def _verb_dev_attach(args: dict, _progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in DEV_ATTACH_WEBUI_FIELDS}
    req = rscore.DevAttachRequest.from_kwargs(**safe)    # may raise ValidationError
    return dataclasses.asdict(rscore.dev_attach(req))


def _verb_dev_detach(args: dict, _progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in DEV_DETACH_WEBUI_FIELDS}
    req = rscore.DevDetachRequest.from_kwargs(**safe)    # may raise ValidationError
    return dataclasses.asdict(rscore.dev_detach(req))


def _verb_dev_sync(args: dict, _progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in DEV_TARGET_WEBUI_FIELDS}
    req = rscore.DevSyncRequest.from_kwargs(**safe)      # may raise ValidationError
    return dataclasses.asdict(rscore.dev_sync(req))


def _verb_dev_set_active_fork(args: dict, _progress=None) -> dict:
    # Management's global per-repo active-fork selector (the ≥2-live-forks
    # edge). A user-initiated WRITE (dev_sync posture: resumes an enabled
    # gitea, never creates); re-stages the wiring on attached projects.
    safe = {k: v for k, v in args.items() if k in DEV_ACTIVE_FORK_WEBUI_FIELDS}
    req = rscore.DevSetActiveForkRequest.from_kwargs(**safe)  # may raise ValidationError
    return dataclasses.asdict(rscore.dev_set_active_fork(req))


def _verb_dev_purge_consumer(args: dict, progress=None) -> dict:
    # Delete a RETIRED dev identity (gitea user + every fork it owns) so its
    # name can be reused. STEP-UP gated (it deletes history irreversibly) and a
    # TAILED op — hence dev_purge_consumer sits in BOTH STEP_UP_VERBS and
    # PROGRESS_VERBS; dispatch gates op-log creation on the latter, so a tailed
    # op outside it would write no file and the browser would tail nothing.
    safe = {k: v for k, v in args.items() if k in DEV_PURGE_WEBUI_FIELDS}
    req = rscore.DevPurgeConsumerRequest.from_kwargs(**safe)  # may raise ValidationError
    return dataclasses.asdict(rscore.dev_purge_consumer(req, progress=progress))


def _verb_dev_commits(args: dict, _progress=None) -> dict:
    # The Fetch tab's lazy per-row commit list (PR expanders + branch
    # expanders). A bounded READ on the accept thread (one fork-list GET + one
    # commits GET, both API_TIMEOUT_S=15 < the webui's 30s relay window);
    # never starts gitea.
    safe = {k: v for k, v in args.items() if k in DEV_COMMITS_WEBUI_FIELDS}
    req = rscore.DevCommitsRequest.from_kwargs(**safe)   # may raise ValidationError
    return dataclasses.asdict(rscore.dev_commits(req))


def _verb_dev_status(_args: dict, _progress=None) -> dict:
    # The Development-page read: no fields; NEVER starts gitea (a stopped one
    # reports running:false immediately — dev_gitea_start is the explicit verb).
    req = rscore.DevStatusRequest.from_kwargs()
    return dataclasses.asdict(rscore.dev_status(req))


def _verb_dev_repo_status(args: dict, _progress=None) -> dict:
    # ONE repo's Development status (the per-project Fetch tab) — the
    # repo-scoped sibling of dev_status: same never-starts-gitea posture, one
    # repo_status read + host-file reviews/attachments. Repo-targeted like
    # dev_sync, so it shares DEV_TARGET_WEBUI_FIELDS.
    safe = {k: v for k, v in args.items() if k in DEV_TARGET_WEBUI_FIELDS}
    req = rscore.DevRepoStatusRequest.from_kwargs(**safe)  # may raise ValidationError
    return dataclasses.asdict(rscore.dev_repo_status(req))


def _verb_dev_gitea_start(_args: dict, progress=None) -> dict:
    # Deliberate gitea enable/provision (the Management Infrastructure button).
    # No fields. A tailed op (PROGRESS_VERBS) — thread `progress` so the one-time
    # image pull streams milestones to the view log.
    req = rscore.DevGiteaStartRequest.from_kwargs()
    return dataclasses.asdict(rscore.dev_gitea_start(req, progress))


def _verb_dev_passwd(args: dict, progress=None) -> dict:
    # Set the sandbox-admin gitea password (the Management → Infrastructure
    # dialog). Step-up gated (STEP_UP_VERBS): a stolen session cookie must not
    # suffice to mint a gitea-admin credential. The password is the single
    # allow-listed field; a change-password failure raises HarnessError so the
    # detail never reaches the durable full log unscrubbed.
    safe = {k: v for k, v in args.items() if k in DEV_PASSWD_WEBUI_FIELDS}
    req = rscore.DevPasswdRequest.from_kwargs(**safe)    # may raise ValidationError
    return dataclasses.asdict(rscore.dev_passwd(req, progress))


# The closed lifecycle vocabulary — the host-root boundary. Adding a verb here
# is a deliberate, security-reviewed edit; never a docker passthrough.
VERBS = {
    "list": _verb_list,
    "status": _verb_status,
    "workflows": _verb_workflows,
    "models": _verb_models,
    "model_default_set": _verb_model_default_set,
    "software_status": _verb_software_status,
    "agent_refresh_check": _verb_agent_refresh_check,
    "editor_refresh_check": _verb_editor_refresh_check,
    "reader_refresh_check": _verb_reader_refresh_check,
    "op_full_tail": _verb_op_full_tail,
    "build_alive": _verb_build_alive,
    "stop": _verb_stop,
    "start": _verb_start,
    "create": _verb_create,
    "attach": _verb_attach,
    "update": _verb_update,
    "destroy": _verb_destroy,
    "box_add": _verb_box_add,
    "box_remove": _verb_box_remove,
    "box_list": _verb_box_list,
    "box_presets": _verb_box_presets,
    "port_add": _verb_port_add,
    "port_remove": _verb_port_remove,
    "port_list": _verb_port_list,
    "dev_repo_remove": _verb_dev_repo_remove,
    "dev_repo_list": _verb_dev_repo_list,
    "dev_attach": _verb_dev_attach,
    "dev_detach": _verb_dev_detach,
    "dev_sync": _verb_dev_sync,
    "dev_status": _verb_dev_status,
    "dev_repo_status": _verb_dev_repo_status,
    "dev_gitea_start": _verb_dev_gitea_start,
    "dev_passwd": _verb_dev_passwd,
    "dev_set_active_fork": _verb_dev_set_active_fork,
    "dev_purge_consumer": _verb_dev_purge_consumer,
    "dev_commits": _verb_dev_commits,
}

# Verbs requiring step-up re-auth: a FRESH login proof (derived client-side
# from the re-typed master password) in the request, not just a live session
# token, so a stolen token alone cannot trigger them. `destroy` is
# the data-destroying verb; this is the cheap half of its gate (the recoverable
# soft-delete + rate-limit land before the webui is exposed beyond localhost).
STEP_UP_VERBS = frozenset({"destroy", "box_remove", "dev_repo_remove",
                           "dev_passwd", "dev_purge_consumer"})

# Deny-by-default gating: a verb in VERBS but NOT in this read allowlist
# requires a valid session token. Inverting the set (vs an explicit *gated*
# set) means any verb added to VERBS is gated automatically unless someone
# *explicitly* opens it — the safe direction for a host-root-equivalent
# surface. `list`/`status` expose no SSH password, so they stay open (a
# same-uid peer already reads them via the CLI).
OPEN_VERBS = frozenset({"list", "status"})

# Auth verbs live OUTSIDE VERBS — they grant no docker capability, so they are
# handled on a distinct path in dispatch and never reach rscore. `logout`
# revokes the presented token (idempotent; you can only revoke your own).
AUTH_VERBS = frozenset({"login", "logout"})


# ---------------------------------------------------------------------------
# Build lane (F2 Slice 2)
#
# Long builds (agent/editor dist pulls, a full fleet rebuild) are multi-minute —
# running one inline on the serial UnixStreamServer would freeze every other
# verb. So build verbs live in a SEPARATE dispatch table (BUILD_DISPATCH),
# DELIBERATELY absent from VERBS: the inline-exec path (dispatch's `table.get`
# over VERBS) physically cannot resolve a build verb, so no future edit can run a
# build on the accept thread. dispatch's build branch is the sole entry — it
# token-gates, validates, checks the lock, spawns a DETACHED child, and returns
# started:true at once. The child (run_build) owns the op-log, points fd 1/2 at
# the HOST-ONLY full log (the raw firehose the authenticated op_full_tail relays),
# emits coarse view-log milestones, and always releases the lock.
# ---------------------------------------------------------------------------

# Only `agent` is browser-relayable (an enum); editor/rebuild take no args.
AGENT_PULL_WEBUI_FIELDS = frozenset({"agent"})


def _verb_agent_pull(args: dict, progress=None) -> dict:
    agent = args.get("agent", "claude")
    if agent not in rscore.KNOWN_AGENTS:          # defense-in-depth (parent re-checks pre-spawn)
        raise rscore.ValidationError(
            f"unknown agent {agent!r} (known: {', '.join(rscore.KNOWN_AGENTS)})")
    return rscore.agent_pull(agent=agent, progress=progress)


def _verb_editor_pull(_args: dict, progress=None) -> dict:
    return rscore.editor_pull(progress=progress)


def _verb_agent_refresh(args: dict, progress=None) -> dict:
    agent = args.get("agent", "claude")
    if agent not in rscore.KNOWN_AGENTS:          # defense-in-depth (parent re-checks pre-spawn)
        raise rscore.ValidationError(
            f"unknown agent {agent!r} (known: {', '.join(rscore.KNOWN_AGENTS)})")
    return rscore.agent_refresh(agent=agent, progress=progress)


def _verb_editor_refresh(_args: dict, progress=None) -> dict:
    return rscore.editor_refresh(progress=progress)


def _verb_reader_pull(_args: dict, progress=None) -> dict:
    return rscore.reader_pull(progress=progress)


def _verb_reader_refresh(_args: dict, progress=None) -> dict:
    return rscore.reader_refresh(progress=progress)


def _verb_node_pull(_args: dict, progress=None) -> dict:
    # Node is a SEED, not a managed dist — there is deliberately NO node_refresh
    # verb (STAGE_NODE_SEED). This pull is the whole build-lane surface.
    return rscore.node_pull(progress=progress)


def _verb_rebuild(_args: dict, progress=None) -> dict:
    rscore.rebuild(progress=progress)
    return {"rebuilt": True}


# The child-only build vocabulary — NOT in VERBS (see the section header).
BUILD_DISPATCH = {
    "agent_pull": _verb_agent_pull,
    "editor_pull": _verb_editor_pull,
    "reader_pull": _verb_reader_pull,
    "node_pull": _verb_node_pull,
    "agent_refresh": _verb_agent_refresh,
    "editor_refresh": _verb_editor_refresh,
    "reader_refresh": _verb_reader_refresh,
    "rebuild": _verb_rebuild,
}


def _build_lock_holder() -> dict | None:
    """The current build.lock {pid, op_id, verb}, or None if absent/unreadable."""
    try:
        return json.loads(BUILD_LOCK.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _build_in_progress() -> bool:
    """True iff a build lock is held by a LIVE process. A lock whose pid is dead
    is a crashed child → stale → reclaimable (False)."""
    holder = _build_lock_holder()
    if holder is None:
        return False
    pid = holder.get("pid")
    return isinstance(pid, int) and _alive(pid)


def _write_build_lock(pid: int, op_id: str, verb: str) -> None:
    BROKER_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_LOCK.write_text(json.dumps({"pid": pid, "op_id": op_id, "verb": verb}))


def _release_build_lock() -> None:
    with contextlib.suppress(FileNotFoundError):
        BUILD_LOCK.unlink()


def _spawn_build_child(op_id: str, verb: str, args: dict) -> int:
    """Spawn the DETACHED build child (`broker __run-build`). Mirrors start()'s
    Popen: start_new_session=True so a SIGTERM to the daemon's process group (or a
    broker restart) doesn't kill an in-flight build; stdout/stderr → BROKER_LOG is
    only a startup backstop — run_build re-points fd 1/2 at its host-only full log
    once it has an _OpLog. Returns the child pid the parent records in build.lock."""
    BROKER_DIR.mkdir(parents=True, exist_ok=True)
    research_py = rscore.SCRIPT_DIR / "research.py"
    log = open(BROKER_LOG, "a")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(research_py), "broker", "__run-build",
             op_id, verb, json.dumps(args)],
            stdout=log, stderr=log, start_new_session=True,
            cwd=str(rscore.SCRIPT_DIR))
    finally:
        log.close()                               # the child holds its own dup'd fd
    return proc.pid


def run_build(op_id: str, verb: str, args_json: str) -> None:
    """The DETACHED build child (invoked by `broker __run-build`). Owns the op-log
    for one build: points fd 1/2 at the HOST-ONLY full log (so docker build's
    inherited stdout/stderr + all prints stream there for the authenticated tail),
    runs the build verb DIRECTLY from BUILD_DISPATCH (NEVER dispatch() — that would
    re-hit the build branch and fork-bomb), writes a terminal done/fail to the
    mounted view-log, and ALWAYS releases the lock. The view-log fail reason is a
    COARSE allowlist-safe token, NEVER str(exception) — a build error can carry a
    host path, and _Progress flushes straight to the mounted file; the raw detail's
    only home is the full log (already streamed via the fd redirect). A RAW
    exception prints nothing on its own, so its arm print_exc()s to that full log
    FIRST — without it a crashed child leaves an EMPTY log and is undiagnosable
    from the browser tail AND the host."""
    try:
        args = json.loads(args_json)
    except ValueError:
        args = {}
    try:
        op = make_oplog(op_id, verb, args)
    except (ValueError, OSError):
        _release_build_lock()
        return
    try:
        # fd-level redirect: contextlib.redirect_stdout swaps only sys.stdout and
        # misses the subprocess's inherited OS fd 1/2, so use dup2 on the real fds,
        # then rebind Python's line-buffered wrappers so print()s also stream.
        os.dup2(op.full.fileno(), 1)
        os.dup2(op.full.fileno(), 2)
        sys.stdout = os.fdopen(1, "w", buffering=1, closefd=False)
        sys.stderr = os.fdopen(2, "w", buffering=1, closefd=False)
        BUILD_DISPATCH[verb](args, op.progress)
        op.progress.done()
    except rscore.ValidationError:
        op.progress.fail("invalid request")
    except SystemExit:
        op.progress.fail("build failed")          # die() already printed to the full log
    except (rscore.HarnessError, Exception):
        # A RAW exception prints nothing on its own — without this the full log is
        # EMPTY and the failure is undiagnosable from the browser tail AND the host.
        # Stdout/stderr are the fd-redirected HOST-ONLY full log here; print_exc
        # emits frames + source lines, never locals.
        traceback.print_exc()
        op.progress.fail("build failed")          # coarse token; detail is in the full log
    finally:
        op.close()
        _release_build_lock()


# ---------------------------------------------------------------------------
# Review lane (STAGE_DEV_GITEA S4)
#
# The PARALLEL sibling of the build lane: each PR review runs in its own
# detached child (`broker __run-review`) with NO shared lock — reviews are
# ephemeral locked containers and may run N-at-a-time (PI decision). review_pr
# lives in its OWN table, absent from VERBS (the inline path structurally
# cannot run a multi-minute review on the accept thread) AND absent from
# BUILD_DISPATCH (that lane serialises on BUILD_LOCK, and run_build's finally
# releases the GLOBAL lock — a review there would unlink a concurrent build's
# lock). Per-op review-lock files give build_alive its liveness signal.
# ---------------------------------------------------------------------------


def _verb_review_pr(args: dict, progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in REVIEW_WEBUI_FIELDS}
    req = rscore.ReviewRequest.from_kwargs(**safe)   # may raise ValidationError
    return rscore.review_pr(req, progress)


# The child-only review vocabulary — NOT in VERBS, NOT in BUILD_DISPATCH.
REVIEW_DISPATCH = {
    "review_pr": _verb_review_pr,
}


def _review_lock_path(op_id: str) -> Path:
    return REVIEW_LOCKS_DIR / f"{op_id}.json"


def _write_review_lock(op_id: str, pid: int) -> None:
    REVIEW_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    _review_lock_path(op_id).write_text(json.dumps({"pid": pid}))


def _release_review_lock(op_id: str) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        _review_lock_path(op_id).unlink()


def _review_lock_alive(op_id: str) -> bool:
    """True iff this op's review lock holds a LIVE pid (a dead pid is a crashed
    child — reads as not-alive, the wedge-escape signal)."""
    try:
        holder = json.loads(_review_lock_path(op_id).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return False
    pid = holder.get("pid") if isinstance(holder, dict) else None
    return isinstance(pid, int) and _alive(pid)


def _spawn_review_child(op_id: str, args: dict) -> int:
    """Spawn the DETACHED review child (`broker __run-review`). Mirrors
    _spawn_build_child: start_new_session so a daemon restart doesn't kill an
    in-flight review; BROKER_LOG is only the startup backstop — run_review
    re-points fd 1/2 at its host-only full log."""
    BROKER_DIR.mkdir(parents=True, exist_ok=True)
    research_py = rscore.SCRIPT_DIR / "research.py"
    log = open(BROKER_LOG, "a")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(research_py), "broker", "__run-review",
             op_id, json.dumps(args)],
            stdout=log, stderr=log, start_new_session=True,
            cwd=str(rscore.SCRIPT_DIR))
    finally:
        log.close()                               # the child holds its own dup'd fd
    return proc.pid


def run_review(op_id: str, args_json: str) -> None:
    """The DETACHED review child (invoked by `broker __run-review`). Mirrors
    run_build: owns the op-log, points fd 1/2 at the HOST-ONLY full log (review
    detail can carry host + gitea paths; the review-specific coarse reason
    lives in the LEDGER entry review_pr writes), calls the review verb DIRECTLY
    from REVIEW_DISPATCH (never dispatch() — re-entry), writes a terminal
    done/fail to the mounted view-log, and unlinks ITS OWN review lock in
    finally (never BUILD_LOCK). Terminal-first: done/fail lands before the lock
    release, so build_alive's alive:false always trails a written terminal on
    every in-process path. View-log fail reasons are COARSE tokens; the full log
    gets the detail — a RAW exception print_exc()s there (it prints nothing on
    its own, so without it a crashed child is undiagnosable), and the
    ValidationError arm prints str(e) there (_verb_review_pr runs from_kwargs
    INSIDE this try, so a malformed request lands here and would otherwise leave
    no reason anywhere)."""
    try:
        args = json.loads(args_json)
    except ValueError:
        args = {}
    try:
        op = make_oplog(op_id, "review_pr", args)
    except (ValueError, OSError):
        _release_review_lock(op_id)
        return
    try:
        os.dup2(op.full.fileno(), 1)
        os.dup2(op.full.fileno(), 2)
        sys.stdout = os.fdopen(1, "w", buffering=1, closefd=False)
        sys.stderr = os.fdopen(2, "w", buffering=1, closefd=False)
        REVIEW_DISPATCH["review_pr"](args, op.progress)
        op.progress.done()
    except rscore.ValidationError as e:
        print(f"validation: {e}")                 # full log — ReviewRequest is repo+pr, secret-free
        op.progress.fail("invalid request")
    except SystemExit:
        op.progress.fail("review failed")         # die() already printed to the full log
    except (rscore.HarnessError, Exception):
        traceback.print_exc()                     # a raw exception prints nothing on its own
        op.progress.fail("review failed")         # coarse token; detail is in the full log
    finally:
        op.close()
        _release_review_lock(op_id)


# ---------------------------------------------------------------------------
# Dev provision lane (STAGE_DEV_GITEA webui-first B; dev-project create added)
#
# The review lane's parallel sibling for the URL-driven dev surfaces: the box
# window's dev box (mirror+box-create) AND the Workflows page's dev project
# (mirror+project-create), each one detached child (`broker __run-dev-box`).
# A GitHub migrate can block ~120s and must never sit on the serial accept
# thread, so both verbs live in their OWN table — absent from VERBS (the
# inline path structurally cannot reach them), absent from BUILD_DISPATCH
# (that lane serialises on the GLOBAL build.lock, which run_build's finally
# releases unconditionally), absent from REVIEW_DISPATCH (that branch filters
# review fields). Per-op locks give build_alive its liveness signal, exactly
# like reviews. ONE deliberate deviation from the review-lane mirror: the
# child args carry the per-repo GitHub PAT, so they ride the child's STDIN —
# never argv, which is world-readable in /proc/<pid>/cmdline for the child's
# lifetime. (The lane predates the second verb; it keeps its dev-box
# mechanism names — table, locks, child subcommand — by deliberate choice.)
# ---------------------------------------------------------------------------


def _verb_dev_box_provision(args: dict, progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in DEV_BOX_WEBUI_FIELDS}
    req = rscore.DevBoxProvisionRequest.from_kwargs(**safe)  # may raise ValidationError
    return rscore.dev_box_provision(req, progress)


def _verb_dev_project_provision(args: dict, progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in DEV_PROJECT_WEBUI_FIELDS}
    req = rscore.DevProjectProvisionRequest.from_kwargs(**safe)  # may raise ValidationError
    return rscore.dev_project_provision(req, progress)


# The lane's SINGLE source of truth: verb → (webui field allowlist, request
# class for the dispatch-side PRE-SPAWN shape validation, verb fn the child
# executes). DEV_BOX_DISPATCH is DERIVED below so the two key sets cannot
# drift — a verb added here is automatically dispatchable, and there is no
# hand-maintained second table to forget (a mismatch would KeyError in
# dispatch and escape as a truncated reply).
_DEV_LANE_VERBS = {
    "dev_box_provision": (
        DEV_BOX_WEBUI_FIELDS, rscore.DevBoxProvisionRequest,
        _verb_dev_box_provision),
    "dev_project_provision": (
        DEV_PROJECT_WEBUI_FIELDS, rscore.DevProjectProvisionRequest,
        _verb_dev_project_provision),
}

# The child-only vocabulary — NOT in VERBS / BUILD_DISPATCH / REVIEW_DISPATCH.
DEV_BOX_DISPATCH = {v: spec[2] for v, spec in _DEV_LANE_VERBS.items()}


def _dev_box_lock_path(op_id: str) -> Path:
    return DEV_BOX_LOCKS_DIR / f"{op_id}.json"


def _write_dev_box_lock(op_id: str, pid: int) -> None:
    DEV_BOX_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    _dev_box_lock_path(op_id).write_text(json.dumps({"pid": pid}))


def _release_dev_box_lock(op_id: str) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        _dev_box_lock_path(op_id).unlink()


def _dev_box_lock_alive(op_id: str) -> bool:
    """True iff this op's dev-box lock holds a LIVE pid (mirrors
    _review_lock_alive — a dead pid reads as not-alive, the wedge-escape)."""
    try:
        holder = json.loads(_dev_box_lock_path(op_id).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return False
    pid = holder.get("pid") if isinstance(holder, dict) else None
    return isinstance(pid, int) and _alive(pid)


def _spawn_dev_box_child(op_id: str, verb: str, args: dict) -> int:
    """Spawn the DETACHED dev-lane child (`broker __run-dev-box <op_id>
    <verb>`). Mirrors _spawn_review_child EXCEPT the args JSON rides the
    child's STDIN, never argv: the args carry the per-repo GitHub PAT, and an
    argv value is world-readable in /proc/<pid>/cmdline for the child's
    lifetime. The VERB NAME does ride argv — it is fixed lane vocabulary, not
    a secret, and the child needs it before it can trust anything on stdin.
    The write+close is BrokenPipe-tolerant: a child dying at exec must not
    raise on the serial accept thread (an uncaught error would escape dispatch
    into socketserver and truncate the client reply) — the op then degrades
    through the never-alive per-op lock into the tail's wedge-escape."""
    BROKER_DIR.mkdir(parents=True, exist_ok=True)
    research_py = rscore.SCRIPT_DIR / "research.py"
    log = open(BROKER_LOG, "a")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(research_py), "broker", "__run-dev-box",
             op_id, verb],
            stdin=subprocess.PIPE, stdout=log, stderr=log,
            start_new_session=True, cwd=str(rscore.SCRIPT_DIR))
    finally:
        log.close()                               # the child holds its own dup'd fd
    with contextlib.suppress(BrokenPipeError, OSError):
        proc.stdin.write(json.dumps(args).encode())
    with contextlib.suppress(BrokenPipeError, OSError):
        proc.stdin.close()
    return proc.pid


def run_dev_box(op_id: str, verb: str) -> None:
    """The DETACHED dev-lane child (invoked by `broker __run-dev-box <op_id>
    <verb>`). Mirrors run_review: owns the op-log, points fd 1/2 at the
    HOST-ONLY full log, calls the verb DIRECTLY from DEV_BOX_DISPATCH (never
    dispatch() — re-entry), writes a terminal done/fail to the mounted
    view-log, and unlinks ITS OWN dev-box lock in finally (never BUILD_LOCK).
    Terminal-first: done/fail lands before the lock release. The args JSON
    arrives on STDIN (never argv — it carries the PAT; see
    _spawn_dev_box_child); the verb rides argv (fixed lane vocabulary). An
    unknown verb releases the lock and returns BEFORE make_oplog — the parent
    only spawns known verbs, and a bogus one must write no op files
    (gate-before-sink). View-log fail reasons are COARSE tokens; the
    ValidationError arm additionally prints str(e) to the full log FIRST — a
    bad PAT or a private-without-PAT source surfaces as
    GiteaError→ValidationError, and without that print it would be
    undiagnosable from the browser AND the full log. Safe by construction:
    GiteaError carries method/path/status only, never a body or the PAT."""
    if verb not in DEV_BOX_DISPATCH:
        _release_dev_box_lock(op_id)
        return
    try:
        args = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    try:
        op = make_oplog(op_id, verb, args)
    except (ValueError, OSError):
        _release_dev_box_lock(op_id)
        return
    try:
        os.dup2(op.full.fileno(), 1)
        os.dup2(op.full.fileno(), 2)
        sys.stdout = os.fdopen(1, "w", buffering=1, closefd=False)
        sys.stderr = os.fdopen(2, "w", buffering=1, closefd=False)
        DEV_BOX_DISPATCH[verb](args, op.progress)
        op.progress.done()
    except rscore.ValidationError as e:
        print(f"validation: {e}")                 # full log — status-only, PAT-free
        op.progress.fail("invalid request")
    except SystemExit:
        op.progress.fail("provision failed")      # die() already printed to the full log
    except (rscore.HarnessError, Exception):
        # A RAW exception prints nothing on its own — without this the full log
        # is EMPTY and the failure is undiagnosable (bit the first acceptance
        # run: a NameError died silently behind the coarse token). Stdout/stderr
        # are the fd-redirected HOST-ONLY full log here, the sanctioned home for
        # host detail; print_exc emits frames + source lines, never locals, so
        # the PAT (never in an exception message by the GiteaError/HarnessError
        # constructions) cannot ride it.
        traceback.print_exc()
        op.progress.fail("provision failed")      # coarse token; detail is in the full log
    finally:
        op.close()
        _release_dev_box_lock(op_id)


def _err(kind: str, message: str) -> dict:
    return {"ok": False, "error": {"kind": kind, "message": message}}


def _auth_login(args: dict, tokens) -> dict:
    """Verify the operator login proof and issue a session token. Never calls
    rscore / docker. `proof` carries the client-side login derivation
    (broker_auth.derive_login_proof) — the raw master password never transits;
    the browser derives it, and `broker passwd` stores scrypt(proof). An
    absent/wrong proof (or no secret set, which fails closed) → kind 'auth'."""
    proof = args.get("proof")
    if not isinstance(proof, str) or not broker_auth.verify_password(proof):
        return _err("auth", "invalid credentials")
    token, expires_at = tokens.issue(broker_auth.DEFAULT_PRINCIPAL)
    return {"ok": True, "result": {
        "token": token, "expires_at": expires_at,
        "principal": broker_auth.DEFAULT_PRINCIPAL}}


def dispatch(verb, args, token=None, tokens=None, *, op_id=None,
             verbs: dict | None = None, audit=None, oplog=None,
             spawn_build=None, spawn_review=None, spawn_dev_box=None) -> dict:
    """Resolve and run one verb, mapping every failure mode to a reply dict.
    Pure by default (no socket; no file I/O unless an `audit` or `oplog` sink is
    passed) so it is unit-testable on its own.

    `tokens` is the daemon's TokenStore (needed for `login` + gated verbs).
    `audit`, if given, is called audit(principal, verb, outcome) for auth +
    write events; left None in tests to keep dispatch side-effect-free.
    `oplog`, if given, is the op-log factory oplog(op_id, verb, args) → _OpLog;
    created AFTER the gates so a rejected caller writes no file (and never
    learns the step-up gate exists), and only for a write verb fired with an
    `op_id`. Left None in tests for the same side-effect-free reason as `audit`.
    `spawn_build(op_id, verb, args) → child pid` launches the detached build
    child for a BUILD_DISPATCH verb; the daemon injects `_spawn_build_child`,
    tests pass None (the branch then gates + validates but never spawns).
    `spawn_review(op_id, args) → child pid` is the review lane's analog
    (daemon injects `_spawn_review_child`; tests pass None likewise), and
    `spawn_dev_box(op_id, verb, args) → child pid` the dev lane's (daemon
    injects `_spawn_dev_box_child`; the lane carries both dev provision
    verbs, so the verb threads through to the child's argv).
    """
    def _audited(principal, outcome, reply):
        if audit is not None:
            audit(principal, verb, outcome)
        return reply

    table = VERBS if verbs is None else verbs

    # Auth path — distinct from the lifecycle vocabulary, no docker capability.
    if verb in AUTH_VERBS:
        if not isinstance(args, dict):
            return _err("bad_request", "args must be a JSON object")
        if verb == "login":
            reply = _auth_login(args, tokens)
            if reply.get("ok"):
                return _audited(reply["result"]["principal"], "ok", reply)
            return _audited(None, "auth_fail", reply)
        # logout: revoke the presented token. Idempotent — an absent/unknown
        # token is a no-op success; you can only revoke the token you hold.
        principal = tokens.principal_for(token) if tokens is not None else None
        if tokens is not None and isinstance(token, str):
            tokens.revoke(token)
        return _audited(principal, "logout",
                        {"ok": True, "result": {"logged_out": True}})

    # Build lane — the SOLE entry for a build verb (BUILD_DISPATCH is absent from
    # VERBS, so the inline-exec path below can never reach one). Placed before the
    # VERBS `table.get`: token-gate → op_id + pre-spawn arg validation → lock →
    # spawn a DETACHED child → return started:true at once. The child owns the
    # op-log and releases the lock; nothing here runs the build on the accept
    # thread. Uses `verbs is None` so an alternate table passed by a test can't
    # accidentally re-route builds (the daemon always passes the real BUILD path).
    if verbs is None and verb in BUILD_DISPATCH:
        if not isinstance(args, dict):
            return _err("bad_request", "args must be a JSON object")
        principal = tokens.principal_for(token) if tokens is not None else None
        if principal is None:
            return _audited(None, "unauthorized",
                            _err("unauthorized",
                                 "a valid session token is required; call login"))
        if not isinstance(op_id, str) or not _OP_ID_RE.match(op_id):
            return _audited(principal, "bad_request",
                            _err("bad_request", "a valid op_id is required"))
        # Pre-spawn arg validation: a bad agent must NOT spawn a child — the client
        # already holds started:true and would never see a child-side failure.
        if verb in ("agent_pull", "agent_refresh"):
            agent = args.get("agent", "claude")
            if agent not in rscore.KNOWN_AGENTS:
                return _audited(principal, "validation",
                                _err("validation",
                                     f"unknown agent {agent!r} (known: "
                                     f"{', '.join(rscore.KNOWN_AGENTS)})"))
        if _build_in_progress():
            return _audited(principal, "busy",
                            _err("busy", "a build is already running"))
        if spawn_build is None:              # tests: gate + validate, never spawn
            return _audited(principal, "ok",
                            {"ok": True, "result": {"op_id": op_id, "started": True}})
        pid = spawn_build(op_id, verb, args)     # detached child
        _write_build_lock(pid, op_id, verb)      # PARENT writes, in the serial section
        return _audited(principal, "ok",
                        {"ok": True, "result": {"op_id": op_id, "started": True}})

    # Review lane (S4) — the SOLE entry for review_pr (REVIEW_DISPATCH is
    # absent from VERBS and BUILD_DISPATCH). Same gate order as the build
    # branch but NO lock check — reviews run in PARALLEL; the parent writes a
    # PER-OP review lock only AFTER a successful spawn, so a rejected caller
    # leaves no lock and no op-log (gate-before-sink).
    if verbs is None and verb in REVIEW_DISPATCH:
        if not isinstance(args, dict):
            return _err("bad_request", "args must be a JSON object")
        principal = tokens.principal_for(token) if tokens is not None else None
        if principal is None:
            return _audited(None, "unauthorized",
                            _err("unauthorized",
                                 "a valid session token is required; call login"))
        if not isinstance(op_id, str) or not _OP_ID_RE.match(op_id):
            return _audited(principal, "bad_request",
                            _err("bad_request", "a valid op_id is required"))
        # Pre-spawn validation: a bad request must NOT spawn a child — the
        # client already holds started:true and would never see a child-side
        # failure. Shape via from_kwargs + two sub-ms host file gates.
        safe = {k: v for k, v in args.items() if k in REVIEW_WEBUI_FIELDS}
        try:
            rscore.ReviewRequest.from_kwargs(**safe)
        except rscore.ValidationError as e:
            return _audited(principal, "validation",
                            _err("validation", str(e)))
        # The CLI mention below is a SANCTIONED bootstrap exemption (PI
        # ruling: the token store is one-time initial setup, the broker-passwd
        # tier) — the one class of remedy the webui-never-names-CLI rule
        # permits a browser-reachable string to carry.
        if not gitea.REVIEWER_TOKEN_PATH.is_file():
            return _audited(principal, "validation",
                            _err("validation",
                                 "no reviewer token — mint one with `claude "
                                 "setup-token` (any project terminal) and "
                                 "store it with `research dev reviewer-token` "
                                 "(one-time setup)"))
        if not rscore.dist_present(rscore.DEFAULT_AGENT):
            return _audited(principal, "validation",
                            _err("validation",
                                 "no cached agent dist — run `research agent "
                                 "pull` on the host first"))
        if spawn_review is None:            # tests: gate + validate, never spawn
            return _audited(principal, "ok",
                            {"ok": True, "result": {"op_id": op_id, "started": True}})
        pid = spawn_review(op_id, safe)          # detached child, FILTERED args
        _write_review_lock(op_id, pid)           # PARENT writes, post-spawn only
        return _audited(principal, "ok",
                        {"ok": True, "result": {"op_id": op_id, "started": True}})

    # Dev provision lane (webui-first B) — the SOLE entry for the
    # _DEV_LANE_VERBS vocabulary (dev_box_provision + dev_project_provision;
    # DEV_BOX_DISPATCH is derived from the same map and absent from every
    # other table). Mirrors the review branch: token → op_id → per-verb
    # pre-spawn validation → spawn → the parent writes a PER-OP lock only
    # AFTER a successful spawn (gate-before-sink). NO lock check — provisions
    # run N-at-a-time; a same-repo double-submit is the documented
    # migrate-resume quirk (the duplicate-review posture). The spawn passes
    # the FILTERED args over the child's stdin (they carry the PAT — see
    # _spawn_dev_box_child); the verb rides the child's argv.
    if verbs is None and verb in DEV_BOX_DISPATCH:
        if not isinstance(args, dict):
            return _err("bad_request", "args must be a JSON object")
        principal = tokens.principal_for(token) if tokens is not None else None
        if principal is None:
            return _audited(None, "unauthorized",
                            _err("unauthorized",
                                 "a valid session token is required; call login"))
        if not isinstance(op_id, str) or not _OP_ID_RE.match(op_id):
            return _audited(principal, "bad_request",
                            _err("bad_request", "a valid op_id is required"))
        # Pre-spawn validation: per-verb shape via from_kwargs + one cheap
        # file gate — a bad request must NOT spawn a child (the client already
        # holds started:true and would never see a child-side failure).
        # Everything docker-shaped stays child-side.
        fields, req_cls, _fn = _DEV_LANE_VERBS[verb]
        safe = {k: v for k, v in args.items() if k in fields}
        try:
            req_cls.from_kwargs(**safe)
        except rscore.ValidationError as e:
            return _audited(principal, "validation",
                            _err("validation", str(e)))
        if not gitea.bootstrap_present():
            return _audited(principal, "validation",
                            _err("validation",
                                 "Gitea isn't enabled — enable it under "
                                 "Management → Infrastructure (or `research "
                                 "dev gitea-enable`) first"))
        if spawn_dev_box is None:           # tests: gate + validate, never spawn
            return _audited(principal, "ok",
                            {"ok": True, "result": {"op_id": op_id, "started": True}})
        pid = spawn_dev_box(op_id, verb, safe)   # detached child, args via stdin
        _write_dev_box_lock(op_id, pid)          # PARENT writes, post-spawn only
        return _audited(principal, "ok",
                        {"ok": True, "result": {"op_id": op_id, "started": True}})

    fn = table.get(verb)
    if fn is None:
        return _err("unknown_verb",
                    f"unknown verb {verb!r}; allowed: {sorted(table)}")
    if not isinstance(args, dict):
        return _err("bad_request", "args must be a JSON object")

    # Deny-by-default: anything not explicitly open needs a valid token.
    principal = None
    if verb not in OPEN_VERBS:
        principal = tokens.principal_for(token) if tokens is not None else None
        if principal is None:
            return _audited(None, "unauthorized",
                            _err("unauthorized",
                                 "a valid session token is required; call login"))

    # Step-up re-auth for the data-destroying verbs. Checked AFTER the token
    # gate, so an unauthenticated caller gets a plain 'unauthorized' and never
    # learns the step-up exists. The fresh proof is verified here and never
    # reaches rscore's request fields (from_kwargs / the WEBUI_FIELDS
    # allow-lists read only their own keys).
    if verb in STEP_UP_VERBS:
        step_proof = args.get("proof")
        if not isinstance(step_proof, str) or not broker_auth.verify_password(step_proof):
            return _audited(principal, "step_up_fail",
                            _err("step_up_required",
                                 "this action requires re-entering your password"))

    # Per-op progress log: write verbs only, and only when the caller supplied
    # an op_id (the webui does; the CLI/socket-direct callers don't). Created
    # HERE — after the token + step-up gates — so a rejected caller leaves no
    # file behind. A malformed op_id (it names a file) → bad_request, no file.
    op = None
    if (oplog is not None and op_id is not None
            and verb in PROGRESS_VERBS and verb not in OPEN_VERBS
            and verb not in AUTH_VERBS):
        try:
            op = oplog(op_id, verb, args)
        except (ValueError, OSError) as e:
            return _audited(principal, "bad_request",
                            _err("bad_request", f"invalid op_id: {e}"))
    progress = op.progress if op is not None else None

    # A verb that calls die() raises SystemExit after printing to stderr; capture
    # that text so the failure message reaches the caller instead of the daemon
    # log. When an op log is active, stdout+stderr also tee to the host-only
    # full log — stderr to BOTH buf (for the die() envelope below) and the full
    # log, stdout to the full log only. Serial request handling makes the global
    # stream swap race-free.
    buf = io.StringIO()
    out_stream = _Tee(op.full) if op is not None else None
    err_stream = _Tee(buf, op.full) if op is not None else buf
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stderr(err_stream))
            if out_stream is not None:
                stack.enter_context(contextlib.redirect_stdout(out_stream))
            result = fn(args, progress)
        reply, outcome = {"ok": True, "result": result}, "ok"
        if op is not None:
            op.progress.done()
    except rscore.ValidationError as e:
        reply, outcome = _err("validation", str(e)), "validation"
        if op is not None:
            op.progress.fail(str(e))
    except rscore.HarnessError as e:
        # Split-sink (WORKFLOW_TAXONOMY_S4 rule 4): the light-path clone/setup
        # failed. client_detail (token-scrubbed git/setup stderr) goes to the
        # CLIENT envelope ONLY; log_msg (step name only) goes to the DURABLE
        # full log + view log — NEVER through _Tee (the ExitStack has already
        # restored the streams here, so op.full.write is an un-redirected plain
        # write). The command argv (which carries the PAT) is never stringified.
        reply, outcome = _err("failed", e.client_detail or e.log_msg), "failed"
        if op is not None:
            op.full.write(e.log_msg + "\n")
            op.progress.fail(e.log_msg)
    except SystemExit:
        msg = buf.getvalue().strip() or "operation failed"
        # die() prints "error: <msg>"; trim the prefix for a clean envelope.
        msg = msg.split("error: ", 1)[-1]
        reply, outcome = _err("failed", msg), "failed"
        if op is not None:
            op.progress.fail(msg)
    finally:
        if op is not None:
            op.close()

    # Audit write verbs only (reads stay open + low-value/noisy).
    if verb not in OPEN_VERBS:
        return _audited(principal, outcome, reply)
    return reply


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def _peer_is_self(conn: socket.socket) -> bool:
    """Verify the connecting process's uid == ours, via SO_PEERCRED. Defence in
    depth on top of the 0600 socket perms."""
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
    except AttributeError:
        return True                      # no SO_PEERCRED here → rely on perms
    except OSError:
        return False                     # supported but unverifiable → deny
    if uid == os.getuid():
        return True
    # Self-diagnosing: the wire reply stays generic ("forbidden"), but the log
    # names the mismatch so a host whose webui runs under a different uid (the
    # uid-equality contract) has a clear signal instead of a silent 503.
    print(f"peer reject: uid {uid} != owner {os.getuid()}", flush=True)
    return False


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        if not _peer_is_self(self.connection):
            self._send(_err("forbidden", "peer is not the owning user"))
            return
        hdr = self.rfile.read(_LEN_SIZE)
        if len(hdr) != _LEN_SIZE:
            return                        # client closed / sent nothing
        (length,) = _LEN.unpack(hdr)
        if length > MAX_REQUEST_BYTES:
            self._send(_err("bad_request",
                            f"request too large: {length} > {MAX_REQUEST_BYTES}"))
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._send(_err("bad_request", "truncated request body"))
            return
        try:
            msg = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._send(_err("bad_request", f"invalid JSON: {e}"))
            return
        if not isinstance(msg, dict):
            self._send(_err("bad_request", "request must be a JSON object"))
            return
        self._send(dispatch(msg.get("verb"), msg.get("args") or {},
                            msg.get("token"), self.server.tokens,
                            op_id=msg.get("op_id"),
                            audit=broker_auth.audit_event,
                            oplog=make_oplog,
                            spawn_build=_spawn_build_child,
                            spawn_review=_spawn_review_child,
                            spawn_dev_box=_spawn_dev_box_child))

    def _send(self, reply: dict) -> None:
        data = json.dumps(reply).encode()
        self.wfile.write(_LEN.pack(len(data)) + data)


class _Server(socketserver.UnixStreamServer):
    def server_bind(self) -> None:
        # Clear a stale socket left by a crashed daemon, then bind with a umask
        # that makes the socket 0600 race-free (vs a post-bind chmod window).
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.server_address)
        old = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(old)


def serve() -> None:
    """Run the daemon loop in the foreground (what `start` spawns detached)."""
    BROKER_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        BROKER_DIR.chmod(0o700)
    BROKER_RUN_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        BROKER_RUN_DIR.chmod(0o700)
    # Pre-create the op-log dirs (the factory also does, lazily). View log lives
    # under run/ (webui-mounted RO); full log under BROKER_DIR (host-only).
    BROKER_OPLOG_DIR.mkdir(parents=True, exist_ok=True)
    BROKER_FULLLOG_DIR.mkdir(parents=True, exist_ok=True)
    server = _Server(str(BROKER_SOCKET), _Handler)
    # In-daemon session-token store: issued on login, never persisted, flushed
    # by construction on restart. The handler reaches it via self.server.tokens.
    server.tokens = broker_auth.TokenStore()

    def _stop(_signum, _frame):
        # shutdown() must run off the serve_forever() thread to avoid deadlock.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"broker listening on {BROKER_SOCKET}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(BROKER_SOCKET)
        print("broker stopped", flush=True)


# ---------------------------------------------------------------------------
# Client (reusable: the test + the future webui relay)
# ---------------------------------------------------------------------------


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    got = 0
    while got < n:
        chunk = sock.recv(n - got)
        if not chunk:
            raise ConnectionError("broker closed the connection mid-frame")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def client_call(verb: str, args: dict | None = None, *,
                token: str | None = None, op_id: str | None = None,
                socket_path=None, timeout: float = CLIENT_TIMEOUT_S) -> dict:
    """Send one framed request and return the parsed reply dict. `token` is
    attached only when given (read verbs omit it; write verbs need it). `op_id`,
    likewise optional + backward-compatible, keys the per-op progress log for a
    write verb."""
    path = str(socket_path or BROKER_SOCKET)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        payload = {"verb": verb, "args": args or {}}
        if token is not None:
            payload["token"] = token
        if op_id is not None:
            payload["op_id"] = op_id
        req = json.dumps(payload).encode()
        s.sendall(_LEN.pack(len(req)) + req)
        hdr = _recv_exact(s, _LEN_SIZE)
        (length,) = _LEN.unpack(hdr)
        return json.loads(_recv_exact(s, length))


# ---------------------------------------------------------------------------
# Lifecycle (research broker {start,stop,status})
# ---------------------------------------------------------------------------


def _read_pid():
    try:
        return int(BROKER_PIDFILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _running() -> bool:
    pid = _read_pid()
    return pid is not None and _alive(pid)


def _clear_pidfile() -> None:
    with contextlib.suppress(FileNotFoundError):
        BROKER_PIDFILE.unlink()


def start() -> None:
    if _running():
        print(f"broker already running (pid {_read_pid()}, socket {BROKER_SOCKET})")
        return
    BROKER_DIR.mkdir(parents=True, exist_ok=True)
    research_py = rscore.SCRIPT_DIR / "research.py"
    log = open(BROKER_LOG, "a")
    proc = subprocess.Popen(
        [sys.executable, str(research_py), "broker", "serve"],
        stdout=log, stderr=log, start_new_session=True, cwd=str(rscore.SCRIPT_DIR))
    BROKER_PIDFILE.write_text(str(proc.pid))
    for _ in range(BROKER_WAIT_S * 10):
        if BROKER_SOCKET.exists():
            print(f"broker started (pid {proc.pid}, socket {BROKER_SOCKET})")
            return
        if proc.poll() is not None:
            _clear_pidfile()
            print(f"broker failed to start (exited {proc.returncode}); see {BROKER_LOG}")
            return
        time.sleep(0.1)
    print(f"broker started (pid {proc.pid}) but socket did not appear within "
          f"{BROKER_WAIT_S}s; see {BROKER_LOG}")


def stop() -> None:
    pid = _read_pid()
    if pid is None or not _alive(pid):
        _clear_pidfile()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(BROKER_SOCKET)
        print("broker not running")
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    for _ in range(BROKER_WAIT_S * 10):
        if not _alive(pid):
            break
        time.sleep(0.1)
    _clear_pidfile()
    with contextlib.suppress(FileNotFoundError):
        os.unlink(BROKER_SOCKET)
    print("broker stopped")


def status() -> None:
    pid = _read_pid()
    if pid is not None and _alive(pid):
        missing = "" if BROKER_SOCKET.exists() else "  (MISSING!)"
        print(f"broker running (pid {pid})")
        print(f"  socket: {BROKER_SOCKET}{missing}")
    else:
        print("broker not running")


def passwd() -> None:
    """Set/replace the single operator secret (interactive, double-entry).
    Unified login: this must be the SAME password as the webui vault's master
    password — the browser derives a login proof from it client-side and the
    raw password never transits; what we store is scrypt(proof). Hashing +
    derivation live in broker_auth; this is the thin terminal wrapper."""
    import getpass
    print("This password must match your webui vault (master) password —")
    print("one password unlocks the vault and logs into Management.")
    try:
        pw = getpass.getpass("New broker password: ")
        pw2 = getpass.getpass("Confirm: ")
    except (EOFError, KeyboardInterrupt):
        print("\naborted")
        return
    if len(pw) < broker_auth.MIN_PASSWORD_LENGTH:
        print(f"aborted: password must be at least "
              f"{broker_auth.MIN_PASSWORD_LENGTH} characters (the webui vault "
              f"enforces the same minimum)")
        return
    if pw != pw2:
        print("aborted: passwords do not match")
        return
    broker_auth.set_password(broker_auth.derive_login_proof(pw))
    print(f"broker password set ({broker_auth.PASSWD_FILE})")
    # Tokens live only in the running daemon's memory and are not bound to the
    # password — rotating the secret does NOT drop sessions already issued.
    if _running():
        print("note: the broker is running — existing session tokens stay valid "
              "until they expire or the broker is restarted. To revoke live "
              "sessions now: research broker stop && research broker start")
