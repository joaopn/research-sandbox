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

# Management sessions: opaque webui cookie → {broker_token, expires}. Distinct
# from the per-project SSH SESSIONS map. Process-memory only; a webui restart
# drops them (re-login), mirroring the broker's own in-daemon token store.
BROKER_SESSIONS: dict[str, dict] = {}
BROKER_COOKIE = "rs_broker"

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
                      token: str | None = None,
                      timeout: float = BROKER_CALL_TIMEOUT_S) -> dict:
    """Send one framed request to the broker and return the parsed reply.
    Async mirror of cli/broker.py::client_call. Raises BrokerUnavailable /
    BrokerForbidden; otherwise returns the reply dict (which may itself be an
    {ok:false,...} application error such as unauthorized/validation/failed)."""
    payload = {"verb": verb, "args": args or {}}
    if token is not None:
        payload["token"] = token
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


async def broker_create_handler(request: web.Request) -> web.Response:
    """POST /broker/project {name,type,egress,enable[],disable[],memory,cpus} —
    create a project (gated, origin-checked). The broker's CREATE_WEBUI_FIELDS
    allow-list is the real input boundary (it drops `data`/`ssh_port`/any
    path-shaped field), so the body is forwarded as-is. Uses the longer create
    timeout — a cold create stages inner images synchronously."""
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
    status, reply = await _relay(request, "create", body,
                                 timeout=BROKER_CREATE_TIMEOUT_S)
    return web.json_response(reply, status=status)


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
    origin-checked). `destroy` carries a step-up `password` in the body that the
    broker re-verifies; the others ignore the body."""
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
    status, reply = await _relay(request, action, args)
    return web.json_response(reply, status=status)


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

    kind=ssh non-always-on services whose id starts with `pi-` are
    gated on the project's per-supervisor sandbox.json: a `pi-<short>`
    tab shows iff the baked sandbox `<short>` is enabled there (plus one
    synthesized tab per BYO sandbox). Read directly off the existing
    `/projects:ro` bind-mount — same data plane that powers the rail's
    status sub-line; no cache, no SSH, no docker socket. Lifecycle
    changes (`research project sandbox enable / disable`) reflect on the
    next page load. RO mount surface is wider than this filter
    (covers `.creds/` etc.), so adding new file reads here doesn't
    expand the webui's trust posture."""
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

    # Project flavor (STAGE_SANDBOX_PROJECT.md). "sandbox" projects swap the
    # agentic Supervisor + Editor tabs for the non-agent Management tab; their
    # box tabs come from kind="sandbox" sandbox.json entries. Read off the same
    # /projects:ro bind-mount as everything else here.
    is_sandbox = _read_project_type(project) == "sandbox"

    out: dict[str, dict] = {}
    sandbox_map: dict[str, str] | None = None
    for sid, svc in services.SERVICES.items():
        # Flavor gate: Management only in sandbox projects; the Supervisor
        # (claude agent) tab only in research projects. The Editor (code-server)
        # IS kept for sandbox projects — it's the artifact-management surface.
        if sid == "management" and not is_sandbox:
            continue
        if sid == "supervisor" and is_sandbox:
            continue
        if svc.get("always_on"):
            out[sid] = svc
        elif svc.get("kind") == "http":
            if probe_up.get(sid):
                out[sid] = svc
        elif svc.get("kind") == "ssh" and sid.startswith("pi-"):
            if sandbox_map is None:
                sandbox_map = _read_project_sandbox(project)
            # A baked-sandbox SERVICES tab `pi-<short>` shows iff the project
            # enables baked sandbox `<short>` (sandbox.json key, no prefix).
            if sandbox_map.get(sid[len("pi-"):]) == "baked":
                out[sid] = svc

    # Synthesized per-box tabs: BYO sandboxes (STAGE_CLI_TAXONOMY) and
    # sandbox-flavor boxes (kind="sandbox", STAGE_SANDBOX_PROJECT.md). Both
    # ride the rs-pi-iso-<name> container/tab conventions, so one synth path
    # serves both. Same data-plane discipline — read off the /projects:ro
    # bind-mount, no SSH/docker socket, lifecycle reflects on next page load.
    if sandbox_map is None:
        sandbox_map = _read_project_sandbox(project)
    for name, kind in sorted(sandbox_map.items()):
        if kind not in ("byo", "sandbox"):
            continue
        spec = services.pi_isolated_service(name)
        if spec is not None:
            out[f"{services.PI_ISOLATED_ID_PREFIX}{name}"] = spec
    return web.json_response(out)


def _read_project_sandbox(project: str) -> dict[str, str]:
    """Return ``{name: kind}`` for the sandboxes enabled for ``project``,
    read from its `.orchestrator/sandbox.json` off the `/projects:ro`
    bind-mount (kind is "baked" or "byo"). Tolerates: missing workspace,
    missing sandbox.json (no sandboxes enabled), invalid JSON — all return
    an empty dict so the tab strip silently omits the sandbox tabs. No
    cache: the read is cheap and lifecycle changes propagate on next load."""
    workspace = _project_workspace(project)
    if workspace is None:
        return {}
    f = workspace / ".orchestrator" / "sandbox.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {n: e.get("kind") for n, e in data.items()
            if isinstance(e, dict)}


def _read_project_type(project: str) -> str:
    """Project flavor from `.orchestrator/project.json` off the `/projects:ro`
    bind-mount ("research" or "sandbox"). Same no-cache, no-socket discipline
    as _read_project_sandbox. Defaults to "research" when the marker is
    missing (legacy projects) or unreadable."""
    workspace = _project_workspace(project)
    if workspace is None:
        return "research"
    f = workspace / ".orchestrator" / "project.json"
    if not f.is_file():
        return "research"
    try:
        data = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return "research"
    if isinstance(data, dict) and data.get("type") == "sandbox":
        return "sandbox"
    return "research"


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
    app.router.add_post("/broker/project", broker_create_handler)
    # attach is a fixed segment registered before the {action} variable so it
    # routes to the keyring handler, not the start|stop|update|destroy dispatcher
    # (which also rejects "attach" defensively).
    app.router.add_post(
        "/broker/project/{name}/attach", broker_attach_handler)
    app.router.add_post(
        "/broker/project/{name}/{action}", broker_project_action_handler)
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
