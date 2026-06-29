"""Research Sandbox webui — service-aware browser front for project supervisors.

Two service kinds: `ssh` (WS-wrapped, browser xterm.js terminal) and `http`
(reverse-proxied with a per-project session cookie issued by /session/<proj>).
The supervisor's container DNS name (`rs-project-<proj>`) is the only handle
the webui has on each project — no docker socket, no host mounts; connectivity
is via `docker network connect` of this container to every `rs-net-<project>`.
"""
import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets
import ssl
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import asyncssh
from aiohttp import web, ClientSession, ClientTimeout, WSMsgType
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import services


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webui")

STATIC_DIR = Path(__file__).parent / "static"
TLS_DIR = Path(os.environ.get("WEBUI_TLS_DIR", "/app/tls"))
LISTEN_HOST = os.environ.get("WEBUI_HOST", "0.0.0.0")
# Listen port is fixed to match the Dockerfile's EXPOSE; the host-side
# WEBUI_PORT only changes the host:container mapping in docker-compose,
# never the in-container listen port.
LISTEN_PORT = 7777
HOST_BIND = os.environ.get("WEBUI_BIND", "127.0.0.1")

# Container DNS prefix — the webui reaches each supervisor at
# `rs-project-<name>` over the per-project bridge it's been network-connected
# to. Mirrors `container_name_for(project)` in research.py.
PROJECT_CONTAINER_PREFIX = "rs-project-"

# Session TTL for the /session/<proj>-issued cookie. Eight hours = one full
# work day; cookies expire silently and the SPA re-POSTs /session on the
# next service-tab open. Not user-visible until expiry; no re-prompt for
# the master password (the SPA still has the SSH credential in the vault).
SESSION_TTL_SECONDS = 8 * 60 * 60

# How long a TCP probe waits before declaring a per-project service down.
# Used both by the legacy /probe endpoint and by project_services_handler's
# enumeration. 3s is the number /probe shipped with in W1; kept consistent.
TCP_PROBE_TIMEOUT_SECONDS = 3.0

# Read-side mount of the host PROJECTS_DIR. The rail's per-project status
# sub-line is computed from this tree — workers/<n>/work/, logbook/, file
# mtimes, total size. Compose mounts it `:ro`; the server further enforces
# project names match a strict regex and resolve inside this root.
PROJECTS_ROOT = Path(os.environ.get("RS_PROJECTS_ROOT", "/projects"))

# Project-name regex mirrors the host-side validator's character class.
# Passed straight from a query string, so the regex is the only barrier
# between client input and a Path join.
PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# HTTP hop-by-hop headers that must NOT be forwarded across a proxy boundary
# per RFC 7230 §6.1. Stripped both inbound (request → upstream) and outbound
# (upstream → response). aiohttp ClientSession adds its own connection
# management; copying these would confuse it.
HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",
})

# In-memory session map: cookie token → {"project": str, "expires": float}.
# Single-process webui; no cross-process sharing needed. Stale entries get
# garbage-collected lazily on lookup.
SESSIONS: dict[str, dict] = {}


class HostKeyValidator(asyncssh.SSHClient):
    """Capture host key during handshake; reject if it doesn't match expected."""

    def __init__(self, expected_fp: str | None):
        super().__init__()
        self.expected_fp = expected_fp
        self.actual_fp: str | None = None

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        self.actual_fp = key.get_fingerprint("sha256")
        if self.expected_fp is None:
            return True  # TOFU: caller will record what we got
        return self.actual_fp == self.expected_fp


def origin_ok(request: web.Request) -> bool:
    """Reject WS handshakes whose Origin isn't this server's own."""
    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    parsed = urlparse(origin)
    return parsed.netloc == request.host


# ===========================================================================
# Broker relay — management lifecycle via the host-side broker daemon.
#
# The webui holds NO docker socket and NO standing authority: it relays the
# operator's password to the broker (which authenticates), then holds the
# broker-issued session token in *process memory*, keyed by an opaque webui
# cookie the browser gets. Every relay (reads included) requires that session,
# so a logged-out / network-reached webui can't enumerate or mutate anything.
#
# Wire protocol mirrors cli/broker.py::client_call (length-prefixed JSON over
# the broker's AF_UNIX socket) — cli/broker.py is the authoritative spec, and
# the bash acceptance test (this client → real broker) is the conformance check
# that catches drift between the two implementations.
# ===========================================================================

# The broker's parent dir (~/.research-sandbox) is bind-mounted into the webui;
# point this at the socket inside it. Parent-dir mount (not the socket file):
# the daemon recreates the socket on restart, and a single-file bind-mount pins
# the original inode.
RS_BROKER_SOCKET = os.environ.get("RS_BROKER_SOCKET", "/run/rs-broker/broker.sock")

_BROKER_LEN = struct.Struct(">I")
# A read verb is instant; `start` recreates the supervisor (slow). 30s covers
# the slowest verb the webui relays while still failing fast on a hung daemon.
BROKER_CALL_TIMEOUT_S = 30

# `create` is the slow outlier: workspace + per-project network + sysbox
# supervisor run + inner-image staging (docker save|load into the inner dockerd)
# routinely takes 10–30s cold, and the daemon handles it synchronously. Give the
# create relay a margin well past the worst observed cold create so a legit one
# isn't cut off mid-flight, while still bounding a truly hung daemon: 120s is
# ~4× the worst case (headroom for a loaded host / slow staging). Half (60s)
# brushes a slow cold create with several inner images; 10× (1200s) is too loose
# to detect a hang. The serial broker means this also caps how long one create
# blocks other verbs — a job/poll upgrade is the documented path if creates ever
# exceed this bound.
BROKER_CREATE_TIMEOUT_S = 120

# Read-timeout for a webui-fired BACKGROUND op (create/start/stop/update/destroy).
# Unlike the synchronous relays, the HTTP response already returned the op_id, so
# this timeout no longer bounds a request — its ONLY job is to terminalize an op
# whose daemon accepted the connection but never replied (a wedged daemon). A
# genuinely *down* broker still fails instantly at connect (FileNotFoundError /
# ConnectionRefused), independent of this value, so a large bound never delays
# real failure detection — it only stops cutting off a legitimately slow op.
# It must exceed the worst legitimate wall time: a cold create (<=120s, per
# BROKER_CREATE_TIMEOUT_S) possibly queued behind another slow op on the serial
# broker. 600s = 5x the single-op cold ceiling — covers a ~2-deep queue of cold
# creates with loaded-host headroom; a daemon silent for 10 min is wedged. (Half,
# 300s, still brushes a queued pair on a slow host; 10x, 3600s, would delay
# wedged-daemon detection to an hour.) The earlier 120s alias was the bug: a cold
# create occasionally ran past it and the webui reported "broker unreachable"
# while the serial daemon finished the create — visible on the next refresh.
BROKER_OP_TIMEOUT_S = 600

# Management sessions: opaque webui cookie → {broker_token, expires}. Distinct
# from the per-project SSH SESSIONS map. Process-memory only; a webui restart
# drops them (re-login), mirroring the broker's own in-daemon token store.
BROKER_SESSIONS: dict[str, dict] = {}
BROKER_COOKIE = "rs_broker"

# Per-operation progress (WEBUI_OPLOG). The broker writes a per-op view log
# under run/oplogs/; run/ is RO-mounted at the socket's parent dir, so the
# webui reads the view file directly — out-of-band from the one-shot socket, so
# it can poll while the op is still in flight.
RS_BROKER_OPLOG_DIR = str(Path(RS_BROKER_SOCKET).parent / "oplogs")

# In-process op handles: op_id → {"state","result","task","broker_token"}.
# state ∈ {"running","ok","failed"}. The browser gets the op_id immediately and
# polls the log file (durable) + this status (the structured verb result on
# completion). Add-only for now (mirrors the broker's add-only file retention);
# a webui restart drops it, after which the browser falls back to the log's
# terminal milestone. NOTE for the deferred retention work: a completed `create`
# result carries the project ssh_password, so it lingers here keyed by op_id for
# the webui's lifetime — when retention lands, evict OP_RUNS entries (and
# consider dropping entry["result"] once the browser has fetched it).
OP_RUNS: dict[str, dict] = {}

# op_id names a file on both sides; validate it as a safe basename before it
# does. Mirror of cli/broker.py::_OP_ID_RE — first char alnum, rest
# alnum/dot/dash/underscore: no separator, no leading dot, so traversal is out.
_OP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Global login rate-limit. NOT per-IP: behind tailscale / a reverse proxy the
# source IP collapses to one address (per-IP would lock everyone out) or is
# spoofable; a global cap is unspoofable and simple. The broker's scrypt verify
# (~tens of ms each) is the real brute-force throttle — this is a bounded
# backstop. 10 failures within 60s trips a 60s auto-clearing lockout: a human
# fat-fingering never trips it; a script is throttled to ~10 tries/min atop
# scrypt's cost. Half (5) risks false-tripping a fumbling human; 10x (100) is
# too loose to matter.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 60
LOGIN_LOCKOUT_SECONDS = 60


class LoginLimiter:
    """Global failed-login limiter with a bounded, auto-clearing lockout. `now`
    is injectable so the cooldown is testable without sleeping."""

    def __init__(self, max_failures=LOGIN_MAX_FAILURES,
                 window_s=LOGIN_WINDOW_SECONDS, lockout_s=LOGIN_LOCKOUT_SECONDS,
                 now=time.time):
        self._max = max_failures
        self._window = window_s
        self._lockout = lockout_s
        self._now = now
        self._failures: list[float] = []
        self._locked_until = 0.0

    def retry_after(self) -> int:
        """Seconds remaining on the lockout, or 0 if not currently locked."""
        return max(0, int(self._locked_until - self._now()))

    def record_failure(self) -> None:
        now = self._now()
        self._failures = [t for t in self._failures if now - t < self._window]
        self._failures.append(now)
        if len(self._failures) >= self._max:
            self._locked_until = now + self._lockout
            self._failures.clear()

    def record_success(self) -> None:
        self._failures.clear()
        self._locked_until = 0.0


LOGIN_LIMITER = LoginLimiter()


class BrokerUnavailable(Exception):
    """Broker socket missing / unreachable / mid-frame close."""


class BrokerForbidden(Exception):
    """Broker peer-uid reject — the webui's uid != the broker's (the
    uid-equality contract). Distinct from unreachable so the SPA can show the
    'uid match?' message rather than a generic outage."""


async def broker_call(verb: str, args: dict | None = None, *,
                      token: str | None = None, op_id: str | None = None,
                      timeout: float = BROKER_CALL_TIMEOUT_S) -> dict:
    """Send one framed request to the broker and return the parsed reply.
    Async mirror of cli/broker.py::client_call. Raises BrokerUnavailable /
    BrokerForbidden; otherwise returns the reply dict (which may itself be an
    {ok:false,...} application error such as unauthorized/validation/failed).
    `op_id`, optional + backward-compatible like `token`, keys the broker's
    per-op progress log for a write verb."""
    payload = {"verb": verb, "args": args or {}}
    if token is not None:
        payload["token"] = token
    if op_id is not None:
        payload["op_id"] = op_id
    data = json.dumps(payload).encode()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(RS_BROKER_SOCKET), timeout=timeout)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        raise BrokerUnavailable(str(e))
    try:
        writer.write(_BROKER_LEN.pack(len(data)) + data)
        await writer.drain()
        hdr = await asyncio.wait_for(
            reader.readexactly(_BROKER_LEN.size), timeout=timeout)
        (n,) = _BROKER_LEN.unpack(hdr)
        body = await asyncio.wait_for(reader.readexactly(n), timeout=timeout)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError) as e:
        raise BrokerUnavailable(str(e))
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    reply = json.loads(body)
    if (not reply.get("ok")
            and reply.get("error", {}).get("kind") == "forbidden"):
        raise BrokerForbidden(reply["error"].get("message", "forbidden"))
    return reply


def _broker_session(request: web.Request) -> dict | None:
    """The live management session for this request, or None. GC's an expired
    entry on lookup."""
    tok = request.cookies.get(BROKER_COOKIE)
    if not tok:
        return None
    s = BROKER_SESSIONS.get(tok)
    if not s:
        return None
    if time.time() > s["expires"]:
        BROKER_SESSIONS.pop(tok, None)
        return None
    return s


async def _relay(request: web.Request, verb: str,
                 args: dict | None = None, *,
                 timeout: float = BROKER_CALL_TIMEOUT_S) -> tuple[int, dict]:
    """Gated relay: require a management session, call the broker with its
    token, map every failure to an (http_status, body) pair."""
    s = _broker_session(request)
    if s is None:
        return 401, {"ok": False, "error": {"kind": "unauthorized"}}
    try:
        reply = await broker_call(verb, args, token=s["broker_token"], timeout=timeout)
    except BrokerUnavailable:
        return 503, {"ok": False, "error": {"kind": "broker_unavailable"}}
    except BrokerForbidden:
        return 403, {"ok": False, "error": {"kind": "forbidden"}}
    if (not reply.get("ok")
            and reply.get("error", {}).get("kind") == "unauthorized"):
        # Broker token expired/invalid → drop the webui session too.
        BROKER_SESSIONS.pop(request.cookies.get(BROKER_COOKIE), None)
        return 401, {"ok": False, "error": {"kind": "unauthorized"}}
    return 200, reply


async def broker_login_handler(request: web.Request) -> web.Response:
    """POST /broker/login {password} — relay to the broker; on success mint a
    management session cookie holding the broker token server-side."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    wait = LOGIN_LIMITER.retry_after()
    if wait > 0:
        return web.json_response(
            {"ok": False, "error": {"kind": "rate_limited", "retry_after": wait}},
            status=429, headers={"Retry-After": str(wait)})
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "error": {"kind": "bad_request"}}, status=400)
    password = body.get("password")
    if not isinstance(password, str):
        return web.json_response(
            {"ok": False, "error": {"kind": "bad_request"}}, status=400)
    try:
        reply = await broker_call("login", {"password": password})
    except BrokerUnavailable:
        return web.json_response(
            {"ok": False, "error": {"kind": "broker_unavailable"}}, status=503)
    except BrokerForbidden:
        return web.json_response(
            {"ok": False, "error": {"kind": "forbidden"}}, status=403)
    if not reply.get("ok"):
        LOGIN_LIMITER.record_failure()
        return web.json_response(
            {"ok": False, "error": {"kind": "auth"}}, status=401)
    LOGIN_LIMITER.record_success()
    result = reply["result"]
    cookie = secrets.token_urlsafe(32)
    expires_at = float(result.get("expires_at", time.time()))
    BROKER_SESSIONS[cookie] = {
        "broker_token": result["token"],
        "expires": expires_at,
    }
    max_age = max(0, int(expires_at - time.time()))
    response = web.json_response({"ok": True})
    response.set_cookie(
        BROKER_COOKIE, cookie, path="/broker",
        httponly=True, secure=True, samesite="Strict", max_age=max_age)
    return response


async def broker_logout_handler(request: web.Request) -> web.Response:
    """POST /broker/logout — revoke the broker token, drop the session, clear
    the cookie. Always 200 (idempotent)."""
    s = _broker_session(request)
    if s is not None:
        try:
            await broker_call("logout", token=s["broker_token"])
        except (BrokerUnavailable, BrokerForbidden):
            pass
    tok = request.cookies.get(BROKER_COOKIE)
    if tok:
        BROKER_SESSIONS.pop(tok, None)
    response = web.json_response({"ok": True})
    response.del_cookie(BROKER_COOKIE, path="/broker")
    return response


async def broker_projects_handler(request: web.Request) -> web.Response:
    """GET /broker/projects — the host's authoritative project list (gated).
    The SameSite=Strict session cookie is the CSRF defense for this read."""
    status, body = await _relay(request, "list")
    return web.json_response(body, status=status)


async def broker_workflows_handler(request: web.Request) -> web.Response:
    """GET /broker/workflows — the store catalog (built-ins + BYO), the agent
    enum, and the default workflow, for the create form's pickers (gated). The
    webui image has no `cli/`, so this relay to the broker's in-process
    `workflow.load_catalog()` is the only way the browser learns the catalog.
    SameSite=Strict cookie is the CSRF defense for this read, mirroring
    /broker/projects."""
    status, body = await _relay(request, "workflows")
    return web.json_response(body, status=status)


def _mint_op_id(name: str, action: str) -> str:
    """A safe-basename op_id embedding project/action/ts for a browsable handle,
    plus a random suffix for uniqueness. The broker re-validates it against the
    same charset before it names a file; a project name with valid chars (the
    broker's name regex is a subset of the op_id charset) always yields a valid
    op_id. `time.time()` is fine here — the webui is not a resumable workflow."""
    return f"{name}-{action}-{int(time.time())}-{secrets.token_hex(4)}"


async def _run_op(op_id: str, verb: str, args: dict,
                  broker_token: str, timeout: float) -> None:
    """Background task: drive one write verb at the (serial) broker, keyed by
    op_id so the broker writes its progress log. The HTTP handler already
    returned the op_id; the browser tails the log + polls status. While the
    serial daemon is busy with an earlier op this call blocks at the socket —
    no view file yet — which the browser renders as "doing" until milestones
    start landing."""
    entry = OP_RUNS[op_id]
    try:
        reply = await broker_call(verb, args, token=broker_token,
                                  op_id=op_id, timeout=timeout)
    except BrokerUnavailable:
        entry.update(state="failed",
                     result={"ok": False, "error": {"kind": "broker_unavailable"}})
        return
    except BrokerForbidden:
        entry.update(state="failed",
                     result={"ok": False, "error": {"kind": "forbidden"}})
        return
    except Exception:
        # Catch-all: an unexpected error must move the op to a TERMINAL state, or
        # the background task dies with the entry stuck "running" forever and the
        # browser polls /status with no terminal milestone in the log either.
        entry.update(state="failed",
                     result={"ok": False, "error": {"kind": "internal"}})
        return
    if (not reply.get("ok")
            and reply.get("error", {}).get("kind") == "unauthorized"):
        # Broker token expired mid-op → drop the webui session too (mirrors
        # _relay), so the next browser action re-logins instead of silently 401'ing.
        for cookie, sess in list(BROKER_SESSIONS.items()):
            if sess.get("broker_token") == broker_token:
                BROKER_SESSIONS.pop(cookie, None)
    entry["result"] = reply
    entry["state"] = "ok" if reply.get("ok") else "failed"


async def _start_op(request: web.Request, verb: str, args: dict,
                    timeout: float, *, op_seed: str | None = None) -> web.Response:
    """Gated + already origin-checked by the caller: mint an op_id, kick the
    broker verb as a background task, and return the op_id immediately so the
    browser can start tailing the log before the op completes. `op_seed` is the
    string the op_id is built from — defaults to args["name"] (the project, for
    project verbs); the box verbs pass the PROJECT explicitly because their
    args["name"] is the BOX name (or None for an auto-named add), which would
    yield an unscoped "None-…" op_id."""
    s = _broker_session(request)
    if s is None:
        return web.json_response(
            {"ok": False, "error": {"kind": "unauthorized"}}, status=401)
    seed = op_seed if op_seed is not None else str(args.get("name", "op"))
    op_id = _mint_op_id(seed, verb)
    # An invalid project name yields an op_id the GET endpoints (and the broker)
    # would reject as a non-basename, leaving the browser with an unpollable
    # handle. Reject it up front as a normal validation error — a valid name (the
    # broker's name regex ⊂ the op_id charset) always mints a valid op_id.
    if not _OP_ID_RE.match(op_id):
        return web.json_response(
            {"ok": False, "error": {"kind": "validation",
             "message": "project name has characters not allowed in a project name"}},
            status=200)
    OP_RUNS[op_id] = {"state": "running", "result": None,
                      "broker_token": s["broker_token"]}
    task = asyncio.create_task(
        _run_op(op_id, verb, args, s["broker_token"], timeout))
    OP_RUNS[op_id]["task"] = task
    return web.json_response({"ok": True, "op_id": op_id})


async def broker_create_handler(request: web.Request) -> web.Response:
    """POST /broker/project {name,type,egress,enable[],disable[],memory,cpus} —
    create a project (gated, origin-checked). Returns {op_id} immediately and
    runs the broker `create` as a background task; the browser tails the op log.
    The broker's CREATE_WEBUI_FIELDS allow-list is the real input boundary (it
    drops `data`/`ssh_port`/any path-shaped field), so the body is forwarded
    as-is. The longer create timeout bounds the background broker call (cold
    create stages inner images synchronously)."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "error": {"kind": "bad_request"}}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"ok": False, "error": {"kind": "bad_request"}}, status=400)
    return await _start_op(request, "create", body, BROKER_OP_TIMEOUT_S)


async def broker_attach_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/attach — JIT keyring: return the project's
    SSH coordinates incl. password (gated, origin-checked). POST + origin-check
    because it returns a credential, not a cacheable read. The browser holds the
    result transiently in memory and never persists it to the vault."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    name = request.match_info.get("name", "")
    status, reply = await _relay(request, "attach", {"name": name})
    return web.json_response(reply, status=status)


async def broker_project_action_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/{action} — start|stop|update|destroy (gated,
    origin-checked). Returns {op_id} immediately and runs the verb as a
    background task; the browser tails the op log. `destroy` carries a step-up
    `password` in the body that rides the background request and the broker
    re-verifies; the others ignore the body. The longer timeout bounds the
    background call with headroom for a recreate queued behind another op on the
    serial daemon."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    name = request.match_info.get("name", "")
    action = request.match_info.get("action", "")
    if action not in ("start", "stop", "update", "destroy"):
        return web.json_response(
            {"ok": False, "error": {"kind": "bad_request"}}, status=400)
    args = {"name": name}
    if action == "destroy":
        try:
            req_body = await request.json()
        except Exception:
            req_body = {}
        pw = req_body.get("password") if isinstance(req_body, dict) else None
        if isinstance(pw, str):
            args["password"] = pw
    elif action == "update":
        # The editor extension toggles code-server via `update` enable/disable
        # (STAGE_BOX_EXT_UX C). The broker's UPDATE_WEBUI_FIELDS ({name, enable,
        # disable, role_mcp_upstream}) is the real boundary — only those reach the
        # verb; nothing host-shaped. Forward for `update` only (start/stop ignore them).
        try:
            req_body = await request.json()
        except Exception:
            req_body = {}
        if isinstance(req_body, dict):
            for k in ("enable", "disable"):
                if req_body.get(k) is not None:
                    args[k] = req_body[k]
    return await _start_op(request, action, args, BROKER_OP_TIMEOUT_S)


async def broker_box_add_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/box {name?,preset?,agent?,editor?,mcps?,repo?,
    ref?,setup?} — add a box to a running dind project (gated, origin-checked).
    Returns {op_id} immediately and tails like create/destroy. The broker's
    BOX_ADD_WEBUI_FIELDS allow-list is the real input boundary; the body is
    forwarded as box fields under the project name from the URL. `browser` is GONE
    (folded into the websearcher preset); `agent` defaults to None so the box's
    preset default applies (BoxAddRequest.from_kwargs)."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    project = request.match_info.get("name", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    args = {"project": project, "name": body.get("name"),
            "preset": body.get("preset"), "agent": body.get("agent"),
            "editor": bool(body.get("editor")), "mcps": body.get("mcps"),
            "repo": body.get("repo"), "ref": body.get("ref"),
            "setup": body.get("setup")}
    return await _start_op(request, "box_add", args, BROKER_OP_TIMEOUT_S,
                           op_seed=project)


async def broker_box_remove_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/box/{box}/remove {password} — discard a
    sandbox box (gated, origin-checked, STEP-UP). The step-up `password` rides
    the background request and the broker re-verifies it; rscore never sees it."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    project = request.match_info.get("name", "")
    box = request.match_info.get("box", "")
    args = {"project": project, "name": box}
    try:
        req_body = await request.json()
    except Exception:
        req_body = {}
    pw = req_body.get("password") if isinstance(req_body, dict) else None
    if isinstance(pw, str):
        args["password"] = pw
    if isinstance(req_body, dict):
        args["keep_workspace"] = bool(req_body.get("keep_workspace"))
    return await _start_op(request, "box_remove", args, BROKER_OP_TIMEOUT_S,
                           op_seed=project)


async def broker_boxes_handler(request: web.Request) -> web.Response:
    """GET /broker/project/{name}/boxes — the project's sandbox boxes with live
    container state (gated). A fast read (no op log); relayed synchronously."""
    project = request.match_info.get("name", "")
    status, reply = await _relay(request, "box_list", {"project": project})
    return web.json_response(reply, status=status)


async def broker_box_presets_handler(request: web.Request) -> web.Response:
    """GET /broker/project/{name}/box-presets — the box-preset catalog (built-ins
    + the operator box-registry) + the project's allowed MCPs (gated). Fast read;
    relayed synchronously. Drives the box window's preset cards + MCP picker.
    die()s (→ non-ok) for a non-dind / stopped project, so the box window's
    fetch-then-open path surfaces the error instead of opening."""
    project = request.match_info.get("name", "")
    status, reply = await _relay(request, "box_presets", {"project": project})
    return web.json_response(reply, status=status)


async def broker_op_log_handler(request: web.Request) -> web.Response:
    """GET /broker/op/{op_id}/log?from=<n> — the view-log byte-slice since `n`
    plus the new EOF. Reads the RO-mounted view file directly (out-of-band from
    the one-shot socket), gated by the management session cookie — no broker
    token round-trip, like /broker/projects. The SameSite=Strict cookie is the
    CSRF defense for this read; origin-check belongs on the firing POST, not
    here. A not-yet-created file is "0 bytes, not started" (the op is queued at
    the serial daemon) — never a 404."""
    if _broker_session(request) is None:
        return web.json_response(
            {"ok": False, "error": {"kind": "unauthorized"}}, status=401)
    op_id = request.match_info.get("op_id", "")
    if not _OP_ID_RE.match(op_id):
        return web.json_response(
            {"ok": False, "error": {"kind": "bad_request"}}, status=400)
    try:
        frm = max(0, int(request.query.get("from", "0")))
    except ValueError:
        frm = 0
    path = Path(RS_BROKER_OPLOG_DIR) / f"{op_id}.view.log"
    if not path.exists():
        # Op not started yet (queued behind another op on the serial broker), or
        # never existed. Either way: no bytes, not an error — the browser shows
        # "doing" and keeps polling from 0.
        return web.json_response(
            {"ok": True, "from": frm, "next": 0, "data": "", "started": False})
    try:
        with open(path, "rb") as f:
            f.seek(frm)
            chunk = f.read()
            nxt = f.tell()
    except OSError as e:
        return web.json_response(
            {"ok": False, "error": {"kind": "io_error", "message": str(e)}},
            status=500)
    # View log is JSONL flushed per whole milestone line, so every EOF is a line
    # boundary → slicing at a prior `next` never splits a record.
    return web.json_response({
        "ok": True, "from": frm, "next": nxt,
        "data": chunk.decode("utf-8", "replace"), "started": True})


async def broker_op_status_handler(request: web.Request) -> web.Response:
    """GET /broker/op/{op_id} — {state: running|ok|failed, result?}. The
    in-process handle for an op this webui started; the browser calls it once
    the log shows a terminal milestone, to render the structured verb result.
    Unknown op_id (e.g. after a webui restart that dropped OP_RUNS) → state
    'unknown'; the browser falls back to the log's terminal milestone."""
    if _broker_session(request) is None:
        return web.json_response(
            {"ok": False, "error": {"kind": "unauthorized"}}, status=401)
    op_id = request.match_info.get("op_id", "")
    if not _OP_ID_RE.match(op_id):
        return web.json_response(
            {"ok": False, "error": {"kind": "bad_request"}}, status=400)
    entry = OP_RUNS.get(op_id)
    if entry is None:
        return web.json_response({"ok": True, "state": "unknown", "result": None})
    return web.json_response(
        {"ok": True, "state": entry["state"], "result": entry["result"]})


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """SSH-kind service WS handler. Path: /ws/<project>/<service>.

    The browser supplies host/port/credentials in the first JSON frame
    (vault-decrypted); the registry supplies the post-login command and the
    default port if the browser didn't set one. Project name is taken from
    the URL but isn't used for routing here — the browser already has the
    SSH endpoint in its vault."""
    if not origin_ok(request):
        return web.Response(status=403, text="Origin rejected")

    service_id = request.match_info.get("service", "")
    # resolve() (not get()) so per-project PI-isolated tabs (`pi-iso-<name>`,
    # not in the static registry) resolve to a synthesized spec. The
    # synthesizer validates the name before building the docker-exec command,
    # so a malformed id returns None → 404 rather than executing.
    svc = services.resolve(service_id)
    if svc is None or svc.get("kind") != "ssh":
        return web.Response(status=404, text=f"unknown ssh service {service_id!r}")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    try:
        first = await asyncio.wait_for(ws.receive(), timeout=10)
    except asyncio.TimeoutError:
        await ws.close()
        return ws

    if first.type != WSMsgType.TEXT:
        await ws.send_json({"type": "error", "msg": "expected JSON connect message"})
        await ws.close()
        return ws

    try:
        connect = json.loads(first.data)
    except json.JSONDecodeError:
        await ws.send_json({"type": "error", "msg": "invalid JSON"})
        await ws.close()
        return ws

    if connect.get("type") != "connect":
        await ws.send_json({"type": "error", "msg": "first message must be type=connect"})
        await ws.close()
        return ws

    host = connect.get("host")
    port = int(connect.get("port", svc.get("default_port", 22)))
    username = connect.get("username") or "research"
    password = connect.get("password")
    expected_fp = connect.get("fingerprint")
    rows = int(connect.get("rows", 24))
    cols = int(connect.get("cols", 80))

    if not host or not password:
        await ws.send_json({"type": "error", "msg": "host and password required"})
        await ws.close()
        return ws

    validator = HostKeyValidator(expected_fp)

    try:
        conn = await asyncssh.connect(
            host=host, port=port,
            username=username, password=password,
            client_factory=lambda: validator,
            known_hosts=None,
            client_keys=None,
            connect_timeout=10,
        )
    except asyncssh.HostKeyNotVerifiable:
        await ws.send_json({"type": "fingerprint_mismatch",
                            "actual": validator.actual_fp})
        await ws.close()
        return ws
    except asyncssh.PermissionDenied:
        await ws.send_json({"type": "auth_failed"})
        await ws.close()
        return ws
    except Exception as e:
        log.warning(f"SSH connect to {host}:{port} failed: {e}")
        await ws.send_json({"type": "error", "msg": f"connect failed: {e}"})
        await ws.close()
        return ws

    await ws.send_json({"type": "connected", "fingerprint": validator.actual_fp})

    try:
        async with conn:
            proc = await conn.create_process(
                term_type="xterm-256color",
                term_size=(cols, rows),
                command=svc["command"],
                encoding=None,
            )

            async def from_browser():
                async for msg in ws:
                    if msg.type == WSMsgType.BINARY:
                        proc.stdin.write(msg.data)
                    elif msg.type == WSMsgType.TEXT:
                        try:
                            ctrl = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        if ctrl.get("type") == "resize":
                            proc.change_terminal_size(
                                width=int(ctrl.get("cols", cols)),
                                height=int(ctrl.get("rows", rows)),
                            )

            async def to_browser():
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode()
                    await ws.send_bytes(chunk)

            done, pending = await asyncio.wait(
                [asyncio.create_task(from_browser()),
                 asyncio.create_task(to_browser())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            try:
                proc.terminate()
            except OSError:
                pass
    finally:
        if not ws.closed:
            await ws.close()

    return ws


async def tcp_probe(host: str, port: int,
                    timeout: float = TCP_PROBE_TIMEOUT_SECONDS) -> bool:
    """Single TCP-connect probe: True iff the kernel completed handshake."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def probe_handler(request: web.Request) -> web.Response:
    """TCP-connect probe used to color tabs as up/down."""
    host = request.query.get("host", "")
    try:
        port = int(request.query.get("port", "0"))
    except ValueError:
        return web.json_response({"up": False, "error": "invalid port"})
    if not host or port < 1 or port > 65535:
        return web.json_response({"up": False, "error": "host/port required"})
    return web.json_response({"up": await tcp_probe(host, port)})


async def services_handler(request: web.Request) -> web.Response:
    """Static service registry. SPA intersects this with the per-project
    enabled-set to build the tab strip."""
    return web.json_response(services.SERVICES)


async def project_services_handler(request: web.Request) -> web.Response:
    """Per-project enabled-set. always_on services are always included;
    kind=http services are included iff their default port is currently
    listening on `rs-project-<proj>`. Probe-driven rather than label-
    driven: this avoids granting the webui a docker socket while
    preserving the property that disabled services don't surface a tab.
    A crashed (enabled-but-not-listening) service also drops off, which
    is the correct UX — a tab that 502s on click is worse than no tab.

    One tab is synthesized per box (kind="sandbox" in the project's
    per-supervisor extensions.json), read directly off the existing
    `/projects:ro` bind-mount — same data plane that powers the rail's
    status sub-line; no cache, no SSH, no docker socket. Box lifecycle
    changes (add / remove a box) reflect on the next page load. RO mount
    surface is wider than this filter (covers `.creds/` etc.), so adding
    new file reads here doesn't expand the webui's trust posture."""
    project = request.match_info.get("project", "")
    upstream = f"{PROJECT_CONTAINER_PREFIX}{project}"

    # Probe every kind=http service in parallel up-front, then iterate
    # SERVICES once in insertion order to assemble the response. The
    # insertion order is the SPA's tab order — Editor (code-server) must
    # land before Supervisor, and the prior "always_on first, http after
    # probes" pass inverted that.
    probe_jobs: list[tuple[str, dict]] = [
        (sid, svc) for sid, svc in services.SERVICES.items()
        if svc.get("kind") == "http" and not svc.get("always_on")
    ]
    probe_up: dict[str, bool] = {}
    if probe_jobs:
        results = await asyncio.gather(*[
            tcp_probe(upstream, int(svc.get("default_port", 0)))
            for _, svc in probe_jobs
        ])
        probe_up = {sid: up for (sid, _), up in zip(probe_jobs, results)}

    # A bare docker box (substrate=docker) has no claude-supervisor — its
    # "Supervisor" tab is really a plain login shell (ssh+byobu bash on the
    # rs-minimal image, which still ships ssh+byobu+code-server). Relabel it
    # "Shell" so the tab is honest, but KEEP it — it's the box's only terminal.
    # Substrate read off the same marker; it stays hidden from the user (Q7).
    is_docker = _read_project_substrate(project) == "docker"

    out: dict[str, dict] = {}
    for sid, svc in services.SERVICES.items():
        # Flavor gate (STAGE_SANDBOX_DIND_AGENT): sandbox-dind now runs an agent, so
        # it shows the Supervisor tab like research — the agent-less Management tab is
        # retired. The Editor (code-server) tab is port-probed below; sandbox-dind
        # defaults the editor OFF (lean), so its probe fails and the Editor tab
        # auto-omits unless --enable code-server.
        if sid == "supervisor" and is_docker:
            out[sid] = {**svc, "label": "Shell (CLI)"}   # honest label for a bare box
            continue
        if sid == "supervisor":
            # Box harness is a standing dind utility (STAGE_DIND_UNIFY): ANY non-docker
            # project (research + sandbox-dind) can spawn rs-sandbox boxes, so stamp
            # box_harness so the SPA shows the box "+" control. (Reached only for
            # non-docker — the docker case continued above.) Copy the spec — never
            # mutate the shared SERVICES dict.
            out[sid] = {**svc, "box_harness": not is_docker}
            continue
        if svc.get("always_on"):
            out[sid] = svc
        elif svc.get("kind") == "http":
            if probe_up.get(sid):
                out[sid] = svc

    # Synthesized per-box tabs: boxes (kind="sandbox", STAGE_SANDBOX_PROJECT.md)
    # ride the rs-pi-iso-<name> container/tab conventions. Same data-plane
    # discipline — read off the /projects:ro bind-mount, no SSH/docker socket,
    # lifecycle reflects on next page load. A box that opted into the editor ALSO
    # gets an http editor tab — its code-server stub is published onto the
    # supervisor netns at editor_port, so it's probed like any http tab (a stopped
    # box → port unbound → probe fails → no dead-iframe tab).
    sandbox_map = _read_project_extensions(project)
    box_editor_jobs = [
        (name, int(e["editor_port"]))
        for name, e in sorted(sandbox_map.items())
        if e.get("kind") == "sandbox" and e.get("editor") and e.get("editor_port")
    ]
    box_editor_up: dict[str, bool] = {}
    if box_editor_jobs:
        results = await asyncio.gather(*[
            tcp_probe(upstream, port) for _, port in box_editor_jobs])
        box_editor_up = {name: up for (name, _), up in zip(box_editor_jobs, results)}

    for name, entry in sorted(sandbox_map.items()):
        if entry.get("kind") != "sandbox":
            continue
        spec = services.pi_isolated_service(name)
        if spec is not None:
            # Stamp box_kind so the SPA shows the per-tab box "✕" (terminal tab only).
            out[f"{services.PI_ISOLATED_ID_PREFIX}{name}"] = {**spec, "box_kind": "sandbox"}
        if box_editor_up.get(name):
            espec = services.pi_isolated_editor_service(name, int(entry["editor_port"]))
            if espec is not None:
                out[f"{services.PI_ISOLATED_EDITOR_ID_PREFIX}{name}"] = espec
    return web.json_response(out)


def _read_project_extensions(project: str) -> dict[str, dict]:
    """Return ``{name: entry}`` for the boxes recorded for ``project``, read from
    its `.orchestrator/extensions.json` off the `/projects:ro` bind-mount (each
    entry carries `kind` ("sandbox" for a box), and for an editor box `editor` +
    `editor_port`). Tolerates: missing workspace, missing extensions.json (no
    boxes), invalid JSON — all return an empty dict so the tab strip silently omits
    the box tabs. No cache: the read is cheap and lifecycle changes propagate on
    next load."""
    workspace = _project_workspace(project)
    if workspace is None:
        return {}
    f = workspace / ".orchestrator" / "extensions.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {n: e for n, e in data.items() if isinstance(e, dict)}


def _read_project_marker(project: str) -> dict:
    """Parse a project's `.orchestrator/project.json` off the `/projects:ro`
    bind-mount, or {} when missing/unreadable (legacy projects, bad JSON). The
    single read behind _read_project_type / _read_project_substrate and the
    rail's workflow label — same no-cache, no-socket discipline as
    _read_project_extensions. The marker carries {type, substrate, workflow, agent}."""
    workspace = _project_workspace(project)
    if workspace is None:
        return {}
    f = workspace / ".orchestrator" / "project.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_project_type(project: str) -> str:
    """Legacy project flavor ("research" | "sandbox-dind"), defaulting to "research"
    for missing/unreadable markers. NOTE: a bare docker box derives to
    "research" too — for a user-facing label prefer the marker's `workflow`
    (what the user actually picked), which the rail badge now shows."""
    return ("sandbox-dind" if _read_project_marker(project).get("type") == "sandbox-dind"
            else "research")


def _read_project_substrate(project: str) -> str:
    """Containment substrate ("docker" | "dind-sysbox"), defaulting to
    "dind-sysbox" for legacy markers that predate the docker substrate. Used to
    render a bare docker box's tabs correctly (no claude-supervisor)."""
    return ("docker" if _read_project_marker(project).get("substrate") == "docker"
            else "dind-sysbox")


# ---- /projects/status — per-project rail sub-line data ----------------

def _project_workspace(name: str) -> Path | None:
    """Resolve a vault-supplied project name to its `<root>/<name>/workspace`
    path, returning None if the name fails validation, escapes the root,
    or doesn't exist on disk."""
    if not PROJECT_NAME_RE.match(name):
        return None
    try:
        root_real = PROJECTS_ROOT.resolve()
    except OSError:
        return None
    candidate = (PROJECTS_ROOT / name).resolve()
    try:
        candidate.relative_to(root_real)
    except ValueError:
        return None
    workspace = candidate / "workspace"
    if not workspace.is_dir():
        return None
    return workspace


def _compute_status(name: str) -> dict:
    """Walk a project's workspace and produce {workers_running, workers_done,
    disk_bytes, latest}. `latest` carries the freshest mtime across event-
    bearing paths (log.jsonl → active, DONE → done, outputs/* → output,
    research_log.md → notes, logbook/* → logbook, plus the worker dir's
    own mtime → spawn) AND the workspace-relative path that produced it,
    so the rail can name the actual file the user might want to open
    rather than a generic kind label.

    Walks the tree once: disk_bytes accumulates st_size for every file
    encountered and the path discriminator picks event-kinds off the same
    pass. Uncached on purpose — start simple, add a TTL cache only when
    profiling shows a real cost."""
    workspace = _project_workspace(name)
    if workspace is None:
        return {"error": "not_found"}

    workers_running = 0
    workers_done = 0
    latest_ts: float = 0.0
    latest_kind: str | None = None
    latest_path: str | None = None

    def bump(kind: str, path: str, ts: float) -> None:
        nonlocal latest_ts, latest_kind, latest_path
        if ts > latest_ts:
            latest_ts = ts
            latest_kind = kind
            latest_path = path

    workers_dir = workspace / "workers"
    if workers_dir.is_dir():
        try:
            for entry in os.scandir(workers_dir):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                done_marker = Path(entry.path) / "work" / "DONE"
                if done_marker.is_file():
                    workers_done += 1
                else:
                    workers_running += 1
                try:
                    bump("spawn",
                         f"workers/{entry.name}/",
                         entry.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    pass
        except OSError:
            pass

    disk_bytes = 0
    for dirpath, _dirnames, filenames in os.walk(workspace, followlinks=False):
        try:
            rel_parts = Path(dirpath).relative_to(workspace).parts
        except ValueError:
            rel_parts = ()
        in_worker_work = (
            len(rel_parts) >= 3
            and rel_parts[0] == "workers"
            and rel_parts[2] == "work"
        )
        worker_tail = rel_parts[3:] if in_worker_work else ()
        in_logbook = rel_parts == ("logbook",)
        for fname in filenames:
            try:
                st = os.stat(os.path.join(dirpath, fname), follow_symlinks=False)
            except OSError:
                continue
            disk_bytes += st.st_size
            rel_path = "/".join((*rel_parts, fname)) if rel_parts else fname
            if in_worker_work:
                if not worker_tail:
                    if fname == "log.jsonl":
                        bump("active", rel_path, st.st_mtime)
                    elif fname == "DONE":
                        bump("done", rel_path, st.st_mtime)
                    elif fname == "research_log.md":
                        bump("notes", rel_path, st.st_mtime)
                elif worker_tail[0] == "outputs":
                    bump("output", rel_path, st.st_mtime)
            elif in_logbook:
                bump("logbook", rel_path, st.st_mtime)

    out: dict = {
        "workers_running": workers_running,
        "workers_done": workers_done,
        "disk_bytes": disk_bytes,
        "flavor": _read_project_type(name),     # "research" | "sandbox-dind" (legacy)
        # The user-facing label: the WORKFLOW the user picked (empty/research/
        # sandbox-dind/BYO), not the derived flavor — so a docker `empty` box stops
        # mislabelling as "research". Falls back to the flavor for legacy markers
        # that predate the workflow field. Substrate stays hidden (Q7).
        "workflow": _read_project_marker(name).get("workflow") or _read_project_type(name),
        "latest": None,
    }
    if latest_kind is not None:
        out["latest"] = {
            "kind": latest_kind,
            "path": latest_path,
            "ts_ms": int(latest_ts * 1000),
        }
    return out


async def projects_status_handler(request: web.Request) -> web.Response:
    """GET /projects/status?names=foo,bar — batched per-project status lookup.

    Names are client-supplied (the SPA's vault drives the rail) and
    validated against PROJECT_NAME_RE before any filesystem access. Each
    name's compute runs in a worker thread so a deep walk on one project
    can't stall the event loop for the others."""
    raw = request.query.get("names", "")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        return web.json_response({})
    results = await asyncio.gather(*[
        asyncio.to_thread(_compute_status, n) for n in names
    ])
    return web.json_response(dict(zip(names, results)))


# ---- /session/<project> — issue a per-project session cookie -----------

def _session_valid(token: str, project: str) -> bool:
    """Look up `token` in SESSIONS, validate project + TTL. Garbage-collects
    expired entries on lookup."""
    s = SESSIONS.get(token)
    if not s:
        return False
    if s["project"] != project:
        return False
    if time.time() > s["expires"]:
        SESSIONS.pop(token, None)
        return False
    return True


async def session_handler(request: web.Request) -> web.Response:
    """POST /session/<project> — validate the project's SSH credentials
    by attempting an SSH connect, then issue an HttpOnly session cookie
    scoped to /proxy/<project>/. The credential is the same one the
    vault holds for the xterm tab; this hands it through to gate the
    iframe-rendered http-kind services without inventing a second auth.

    Body: JSON `{host, port?, username?, password, fingerprint?}` (the
    same shape the SSH `connect` message uses on /ws). On success the
    response sets `Set-Cookie: rs_session_<proj>=<token>; Path=/proxy/
    <proj>/; Secure; HttpOnly; SameSite=Strict`."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")

    project = request.match_info.get("project", "")
    if not project:
        return web.Response(status=400, text="project required")

    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid JSON")

    host = body.get("host")
    port = int(body.get("port", 22))
    username = body.get("username") or "research"
    password = body.get("password")
    expected_fp = body.get("fingerprint")

    if not host or not password:
        return web.Response(status=400, text="host and password required")

    validator = HostKeyValidator(expected_fp)
    try:
        conn = await asyncssh.connect(
            host=host, port=port,
            username=username, password=password,
            client_factory=lambda: validator,
            known_hosts=None, client_keys=None,
            connect_timeout=10,
        )
        conn.close()
    except asyncssh.HostKeyNotVerifiable:
        return web.json_response(
            {"type": "fingerprint_mismatch", "actual": validator.actual_fp},
            status=401)
    except asyncssh.PermissionDenied:
        return web.json_response({"type": "auth_failed"}, status=401)
    except Exception as e:
        log.warning(f"/session/{project}: SSH connect failed: {e}")
        return web.json_response({"type": "error", "msg": str(e)}, status=502)

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {
        "project": project,
        "expires": time.time() + SESSION_TTL_SECONDS,
    }
    response = web.json_response(
        {"ok": True, "fingerprint": validator.actual_fp})
    response.set_cookie(
        f"rs_session_{project}", token,
        path=f"/proxy/{project}/",
        httponly=True, secure=True, samesite="Strict",
        max_age=SESSION_TTL_SECONDS,
    )
    return response


# ---- /proxy/<project>/<service>/<path> — kind=http reverse proxy --------

def _filter_headers(headers, drop: frozenset[str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in drop}


async def _proxy_ws(request: web.Request,
                    upstream_url: str) -> web.StreamResponse:
    """WS pass-through. Opens upstream first so a failure returns a clean
    502 to the browser instead of a bare close frame after handshake."""
    client_subprotocols: list[str] = []
    sec_proto = request.headers.get("Sec-WebSocket-Protocol")
    if sec_proto:
        client_subprotocols = [
            p.strip() for p in sec_proto.split(",") if p.strip()
        ]

    timeout = ClientTimeout(total=None, sock_read=None)
    sess = ClientSession(timeout=timeout)
    try:
        try:
            upstream_ws = await sess.ws_connect(
                upstream_url,
                protocols=client_subprotocols,
                heartbeat=30,
                max_msg_size=0,
            )
        except Exception as e:
            await sess.close()
            log.info(f"WS upstream connect failed: {e}")
            return web.Response(status=502, text=f"upstream WS failed: {e}")

        chosen = upstream_ws.protocol
        client_ws = web.WebSocketResponse(
            protocols=[chosen] if chosen else (),
            heartbeat=30,
            max_msg_size=0,
        )
        await client_ws.prepare(request)

        async def c2u():
            async for msg in client_ws:
                if msg.type == WSMsgType.TEXT:
                    await upstream_ws.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await upstream_ws.send_bytes(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING,
                                  WSMsgType.CLOSED, WSMsgType.ERROR):
                    break

        async def u2c():
            async for msg in upstream_ws:
                if msg.type == WSMsgType.TEXT:
                    await client_ws.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await client_ws.send_bytes(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING,
                                  WSMsgType.CLOSED, WSMsgType.ERROR):
                    break

        try:
            _, pending = await asyncio.wait(
                [asyncio.create_task(c2u()), asyncio.create_task(u2c())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        finally:
            if not upstream_ws.closed:
                await upstream_ws.close()
            if not client_ws.closed:
                await client_ws.close()
        return client_ws
    finally:
        await sess.close()


async def _proxy_http(request: web.Request,
                      upstream_url: str) -> web.StreamResponse:
    """HTTP pass-through. Streams body both directions; preserves status,
    headers (minus hop-by-hop), and any Set-Cookie from upstream."""
    headers_in = _filter_headers(request.headers, HOP_BY_HOP_HEADERS)

    # Stream the body upstream rather than buffering — code-server's
    # editor saves (an .ipynb, a large prose file) need to flow through
    # without an intermediate read into memory. GET/HEAD have no body.
    data = None
    if request.method not in ("GET", "HEAD"):
        data = request.content

    # No request timeout — code-server's WS workbench traffic and long polls
    # need to live as long as the user holds the tab. Connection close is
    # the source of truth for "done", not a wall clock.
    timeout = ClientTimeout(total=None, sock_read=None)
    async with ClientSession(timeout=timeout, auto_decompress=False) as sess:
        async with sess.request(
            request.method,
            upstream_url,
            headers=headers_in,
            data=data,
            allow_redirects=False,
        ) as upstream_resp:
            headers_out = _filter_headers(
                upstream_resp.headers, HOP_BY_HOP_HEADERS)
            response = web.StreamResponse(
                status=upstream_resp.status,
                reason=upstream_resp.reason,
                headers=headers_out,
            )
            await response.prepare(request)
            async for chunk in upstream_resp.content.iter_any():
                if not chunk:
                    break
                await response.write(chunk)
            await response.write_eof()
            return response


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    """`/proxy/<project>/<service>/<tail>` — reverse-proxy kind=http
    services. Validates the project session cookie issued by /session,
    then forwards HTTP or WS upgrades to `rs-project-<proj>:<port>/<tail>`,
    stripping the proxy prefix so the upstream sees its own root.

    Trailing-slash discipline: if hit at `/proxy/<proj>/<svc>` (no slash),
    redirect to the slash form. code-server's relative-URL resolution
    requires the trailing slash for asset paths to come out correct."""
    project = request.match_info.get("project", "")
    service_id = request.match_info.get("service", "")
    tail = request.match_info.get("tail", "")

    cookie = request.cookies.get(f"rs_session_{project}")
    if not cookie or not _session_valid(cookie, project):
        return web.Response(status=401, text="session required")

    svc = services.get(service_id)
    if svc is None or svc.get("kind") != "http":
        return web.Response(
            status=404, text=f"unknown http service {service_id!r}")

    # No-trailing-slash edge: /proxy/<proj>/<svc> → 301 to /proxy/<proj>/<svc>/
    if request.path == f"/proxy/{project}/{service_id}":
        new = request.path + "/"
        if request.query_string:
            new += "?" + request.query_string
        raise web.HTTPMovedPermanently(location=new)

    upstream_host = f"{PROJECT_CONTAINER_PREFIX}{project}"
    upstream_port = int(svc.get("default_port", 0))

    is_ws = (request.headers.get("Upgrade", "").lower() == "websocket")
    scheme = "ws" if is_ws else "http"
    upstream_url = f"{scheme}://{upstream_host}:{upstream_port}/{tail}"
    if request.query_string:
        upstream_url += "?" + request.query_string

    if is_ws:
        return await _proxy_ws(request, upstream_url)
    return await _proxy_http(request, upstream_url)


async def index_handler(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


def cert_covers_bind(cert_path: Path, bind: str) -> bool:
    """Check whether the existing cert's SAN already includes `bind`."""
    if not cert_path.exists():
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
    except Exception:
        return False
    try:
        bind_ip = ipaddress.ip_address(bind)
        return any(
            isinstance(e, x509.IPAddress) and e.value == bind_ip for e in san
        )
    except ValueError:
        return any(isinstance(e, x509.DNSName) and e.value == bind for e in san)


def generate_self_signed(cert_path: Path, key_path: Path, bind: str) -> None:
    """Write a fresh self-signed cert+key covering localhost and `bind`."""
    log.info(f"Generating self-signed TLS cert at {cert_path} (bind={bind})")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rs-webui")])

    san_entries = [
        x509.DNSName("localhost"),
        x509.DNSName("rs-webui"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv6Address("::1")),
    ]
    try:
        bind_ip = ipaddress.ip_address(bind)
        if not any(isinstance(e, x509.IPAddress) and e.value == bind_ip
                   for e in san_entries):
            san_entries.append(x509.IPAddress(bind_ip))
    except ValueError:
        if bind not in ("localhost", "rs-webui"):
            san_entries.append(x509.DNSName(bind))

    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256()))

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert_path.chmod(0o644)
    key_path.chmod(0o600)


def ensure_tls(cert_path: Path, key_path: Path, bind: str) -> ssl.SSLContext:
    # `.custom` marker — written by `research webui cert-tailscale` (and any
    # future cert helpers) — opts out of the auto-regenerate path. The
    # user-provided cert's SAN may not cover `bind` (e.g. cert covers an FQDN,
    # WEBUI_BIND is the IP the FQDN resolves to), and that's a valid
    # configuration: the browser sees the trusted cert when accessing via
    # the FQDN, which is the URL the user actually navigates to.
    custom = cert_path.parent / ".custom"
    if custom.exists():
        provider = custom.read_text().strip() or "user-supplied"
        log.info(f"using {provider} cert (skipping self-signed regen)")
        if not (cert_path.exists() and key_path.exists()):
            log.error(
                f".custom marker present but cert/key missing at "
                f"{cert_path.parent} — falling back to self-signed")
            generate_self_signed(cert_path, key_path, bind)
    elif not (cert_path.exists() and key_path.exists()) or \
            not cert_covers_bind(cert_path, bind):
        generate_self_signed(cert_path, key_path, bind)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx


def main() -> None:
    cert_path = TLS_DIR / "cert.pem"
    key_path = TLS_DIR / "key.pem"
    ssl_ctx = ensure_tls(cert_path, key_path, HOST_BIND)

    # client_max_size=0 disables aiohttp's default 1 MiB request-body cap.
    # The default would silently reject realistic uploads (saving an .ipynb,
    # writing a large file from code-server's editor). This webui is single-
    # user behind self-signed TLS on a user-chosen bind, so the DoS posture
    # the cap was protecting against doesn't apply. Upstream services
    # (code-server, future jupyter) impose their own bounds.
    app = web.Application(client_max_size=0)
    app.router.add_get("/", index_handler)
    app.router.add_get("/probe", probe_handler)
    app.router.add_get("/services", services_handler)
    app.router.add_get("/services/{project}", project_services_handler)
    app.router.add_get("/projects/status", projects_status_handler)
    app.router.add_post("/session/{project}", session_handler)
    # Broker relay (management lifecycle) — login-gated; no docker socket.
    app.router.add_post("/broker/login", broker_login_handler)
    app.router.add_post("/broker/logout", broker_logout_handler)
    app.router.add_get("/broker/projects", broker_projects_handler)
    app.router.add_get("/broker/workflows", broker_workflows_handler)
    app.router.add_post("/broker/project", broker_create_handler)
    # attach is a fixed segment registered before the {action} variable so it
    # routes to the keyring handler, not the start|stop|update|destroy dispatcher
    # (which also rejects "attach" defensively).
    app.router.add_post(
        "/broker/project/{name}/attach", broker_attach_handler)
    # Sandbox-box add/list — the POST .../box fixed segment MUST precede the
    # {action} variable (same shadowing reason as attach: {action} would match
    # the literal "box" first and 400 it). The 6-segment remove + the boxes GET
    # don't collide, but stay grouped here.
    app.router.add_post("/broker/project/{name}/box", broker_box_add_handler)
    app.router.add_post(
        "/broker/project/{name}/box/{box}/remove", broker_box_remove_handler)
    app.router.add_get("/broker/project/{name}/boxes", broker_boxes_handler)
    app.router.add_get(
        "/broker/project/{name}/box-presets", broker_box_presets_handler)
    app.router.add_post(
        "/broker/project/{name}/{action}", broker_project_action_handler)
    # op-log tail + status (GETs, session-gated; the more specific /log first).
    app.router.add_get("/broker/op/{op_id}/log", broker_op_log_handler)
    app.router.add_get("/broker/op/{op_id}", broker_op_status_handler)
    app.router.add_get("/ws/{project}/{service}", ws_handler)
    # Proxy: trailing-slash form catches /proxy/<proj>/<svc>/ + everything
    # below. The no-slash form is also routed (matched by proxy_handler's
    # 301 path) so we can redirect inbound /proxy/<proj>/<svc> requests
    # rather than 404'ing them.
    app.router.add_route(
        "*", "/proxy/{project}/{service}/{tail:.*}", proxy_handler)
    app.router.add_route(
        "*", "/proxy/{project}/{service}", proxy_handler)
    app.router.add_static("/static", STATIC_DIR)

    log.info(f"Research Sandbox webui listening on https://{LISTEN_HOST}:{LISTEN_PORT}")
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT,
                ssl_context=ssl_ctx, access_log=log)


if __name__ == "__main__":
    main()
