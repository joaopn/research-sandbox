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
from pathlib import Path

# broker lives in cli/; make sibling modules importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rscore  # noqa: E402
import broker_auth  # noqa: E402
import workflow  # noqa: E402
import pi_isolated_registry  # noqa: E402  (for _verb_ext_enable's RegistryError catch)

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
                            "box_add", "box_remove",
                            "ext_enable", "ext_disable"})

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
    return {
        "workflows": catalog,                     # full manifests (substrate,
                                                  # repo/ref/setup presets, source,
                                                  # has_worker_layer)
        # Each known agent + whether its host dist is pulled (STAGE_MULTI_AGENT). The
        # create form offers ONLY staged agents as on/off boxes — never an agent it
        # can't deploy — so a relayed `agents` set always validates in from_kwargs.
        "agents": [{"name": a, "staged": rscore.dist_present(a)}
                   for a in rscore.KNOWN_AGENTS],
        "default_workflow": rscore.DEFAULT_WORKFLOW,
    }


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
    # agents: the agent-dist SET a docker box deploys at boot — in-box field
    # (STAGE_MULTI_AGENT; was the single `agent`). Per-agent dist-must-exist is
    # enforced in from_kwargs. The rename is a lockstep boundary edit: the webui
    # form POSTs `agents:[...]` (a field not in this set is silently dropped).
    "agents",
})

# The webui-settable subset of UpdateRequest fields. `rebuild`/`keep_claude` are
# dropped on purpose: a webui update is the fast file-only recreate, and an
# image rebuild (`rebuild=True`) would block the serially-handled daemon for the
# whole multi-minute build — a self-inflicted DoS from the browser. Rebuilds
# stay a deliberate CLI action.
UPDATE_WEBUI_FIELDS = frozenset({
    "name", "enable", "disable", "role_mcp_upstream",
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
    dispatch BEFORE this runs; the request's `password` is consumed there and
    never reaches rscore (DestroyRequest reads only `name`)."""
    req = rscore.DestroyRequest.from_kwargs(**args)  # may raise ValidationError
    rscore.destroy(req, progress=progress)           # may die() → SystemExit
    return {"destroyed": req.name}


# The webui-settable subset of the box verbs' inputs — the input boundary for
# box add/remove/list, mirroring CREATE_WEBUI_FIELDS. Every field acts INSIDE the
# locked-egress, credential-free inner box (project is name-regex'd; name is
# box-regex'd; agent ∈ {claude,none}; browser is a bool) — none is host-shaped, so
# they are relayable. from_kwargs still validates them. The step-up `password`
# for box_remove is NOT here: it is verified + consumed in dispatch, never
# forwarded to rscore.
BOX_ADD_WEBUI_FIELDS = frozenset({"project", "name", "browser", "agent"})
BOX_TARGET_WEBUI_FIELDS = frozenset({"project", "name"})
# box_remove additionally accepts keep_workspace (a bool, not host-shaped): when
# set the box is removed but its artifacts stay on disk. The step-up `password`
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


# The webui-settable subset of the extension verbs' inputs. `upstream` is a list
# of project-scoped MCP names (allowlist-validated in _extension_enable, only ever
# written to extensions.json / rendered as mcp-proxy:8888/<name>) — NOT host-shaped,
# so relayable; `name` is catalog-gated; `auto` is a bool. No path/host field.
EXT_ENABLE_WEBUI_FIELDS = frozenset({"project", "name", "upstream", "auto"})
EXT_TARGET_WEBUI_FIELDS = frozenset({"project", "name"})


def _verb_ext_enable(args: dict, progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in EXT_ENABLE_WEBUI_FIELDS}
    req = rscore.ExtEnableRequest.from_kwargs(**safe)  # may raise ValidationError
    # The BYO branch reaches pi_isolated_registry.entry_for(expand=True), which can
    # raise RegistryError (e.g. an unset ${VAR} during expansion). dispatch only
    # catches ValidationError/HarnessError/SystemExit, so re-raise it as one
    # (mirrors _verb_workflows' WorkflowError→ValidationError).
    try:
        return dataclasses.asdict(rscore.ext_enable(req, progress=progress))
    except pi_isolated_registry.RegistryError as e:
        raise rscore.ValidationError(str(e))


def _verb_ext_disable(args: dict, progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in EXT_TARGET_WEBUI_FIELDS}
    req = rscore.ExtDisableRequest.from_kwargs(**safe)  # may raise ValidationError
    return dataclasses.asdict(rscore.ext_disable(req, progress=progress))


def _verb_ext_list(args: dict, _progress=None) -> dict:
    safe = {k: v for k, v in args.items() if k in EXT_TARGET_WEBUI_FIELDS}
    req = rscore.ExtListRequest.from_kwargs(**safe)    # may raise ValidationError
    return dataclasses.asdict(rscore.ext_list(req))


# The closed lifecycle vocabulary — the host-root boundary. Adding a verb here
# is a deliberate, security-reviewed edit; never a docker passthrough.
VERBS = {
    "list": _verb_list,
    "status": _verb_status,
    "workflows": _verb_workflows,
    "stop": _verb_stop,
    "start": _verb_start,
    "create": _verb_create,
    "attach": _verb_attach,
    "update": _verb_update,
    "destroy": _verb_destroy,
    "box_add": _verb_box_add,
    "box_remove": _verb_box_remove,
    "box_list": _verb_box_list,
    "ext_enable": _verb_ext_enable,
    "ext_disable": _verb_ext_disable,
    "ext_list": _verb_ext_list,
}

# Verbs requiring step-up re-auth: a FRESH password in the request, not just a
# live session token, so a stolen token alone cannot trigger them. `destroy` is
# the data-destroying verb; this is the cheap half of its gate (the recoverable
# soft-delete + rate-limit land before the webui is exposed beyond localhost).
STEP_UP_VERBS = frozenset({"destroy", "box_remove"})

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


def _err(kind: str, message: str) -> dict:
    return {"ok": False, "error": {"kind": kind, "message": message}}


def _auth_login(args: dict, tokens) -> dict:
    """Verify the operator password and issue a session token. Never calls
    rscore / docker. Absent/wrong password (or no password set, which fails
    closed) → kind 'auth'."""
    password = args.get("password")
    if not isinstance(password, str) or not broker_auth.verify_password(password):
        return _err("auth", "invalid credentials")
    token, expires_at = tokens.issue(broker_auth.DEFAULT_PRINCIPAL)
    return {"ok": True, "result": {
        "token": token, "expires_at": expires_at,
        "principal": broker_auth.DEFAULT_PRINCIPAL}}


def dispatch(verb, args, token=None, tokens=None, *, op_id=None,
             verbs: dict | None = None, audit=None, oplog=None) -> dict:
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
    # learns the step-up exists. The fresh password is verified here and not
    # forwarded to rscore.
    if verb in STEP_UP_VERBS:
        step_pw = args.get("password")
        if not isinstance(step_pw, str) or not broker_auth.verify_password(step_pw):
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
                            oplog=make_oplog))

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
    Hashing + storage live in broker_auth; this is the thin terminal wrapper."""
    import getpass
    try:
        pw = getpass.getpass("New broker password: ")
        pw2 = getpass.getpass("Confirm: ")
    except (EOFError, KeyboardInterrupt):
        print("\naborted")
        return
    if not pw:
        print("aborted: empty password")
        return
    if pw != pw2:
        print("aborted: passwords do not match")
        return
    broker_auth.set_password(pw)
    print(f"broker password set ({broker_auth.PASSWD_FILE})")
    # Tokens live only in the running daemon's memory and are not bound to the
    # password — rotating the secret does NOT drop sessions already issued.
    if _running():
        print("note: the broker is running — existing session tokens stay valid "
              "until they expire or the broker is restarted. To revoke live "
              "sessions now: research broker stop && research broker start")
