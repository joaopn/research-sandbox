"""broker — host-side daemon that exposes a closed lifecycle verb vocabulary
over a unix-domain socket, so a future browser surface can drive project
lifecycle without ever holding a docker socket.

Runs as the user, with the user's existing docker access. Opt-in
(`research broker start`); with it stopped the CLI-first system is unchanged.

Wire protocol — length-prefixed JSON over a unix socket (no HTTP, no network
listener). One request per connection:

    →  [4-byte big-endian length] [UTF-8 JSON: {"verb": <str>, "args": {…}}]
    ←  [4-byte big-endian length] [UTF-8 JSON reply]
        success:  {"ok": true,  "result": <verb result>}
        failure:  {"ok": false, "error": {"kind": <str>, "message": <str>}}

Containment, by construction:
  * **Closed verb allowlist** (`VERBS`) — a request can only name a verb in
    this dict; never a docker passthrough. This skeleton serves read-only
    `list` / `status` only. Write verbs are a deliberate future edit here,
    behind a login gate (not in this step).
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Same host-side tree as the MCP registry (~/.research-sandbox/). User-owned.
BROKER_DIR = Path.home() / ".research-sandbox"
BROKER_SOCKET = BROKER_DIR / "broker.sock"
BROKER_PIDFILE = BROKER_DIR / "broker.pid"
BROKER_LOG = BROKER_DIR / "broker.log"

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
# Verb dispatch (the closed vocabulary)
# ---------------------------------------------------------------------------


def _verb_list(_args: dict) -> list[dict]:
    return [dataclasses.asdict(s) for s in rscore.list_projects()]


def _verb_status(args: dict) -> dict:
    req = rscore.StatusRequest.from_kwargs(**args)   # may raise ValidationError
    return dataclasses.asdict(rscore.status(req))    # may die() → SystemExit


# The allowlist. Read-only this step. Adding a write verb is a deliberate edit
# here (behind a login gate, a later step) — never an accident, never a
# passthrough.
VERBS = {
    "list": _verb_list,
    "status": _verb_status,
}


def _err(kind: str, message: str) -> dict:
    return {"ok": False, "error": {"kind": kind, "message": message}}


def dispatch(verb, args, verbs: dict | None = None) -> dict:
    """Resolve and run one verb, mapping every failure mode to a reply dict.
    Pure (no socket) so it is unit-testable on its own."""
    table = VERBS if verbs is None else verbs
    fn = table.get(verb)
    if fn is None:
        return _err("unknown_verb",
                    f"unknown verb {verb!r}; allowed: {sorted(table)}")
    if not isinstance(args, dict):
        return _err("bad_request", "args must be a JSON object")
    # A verb that calls die() raises SystemExit after printing to stderr; capture
    # that text so the failure message reaches the caller instead of the daemon
    # log. Serial request handling makes the global stderr swap race-free.
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            result = fn(args)
        return {"ok": True, "result": result}
    except rscore.ValidationError as e:
        return _err("validation", str(e))
    except SystemExit:
        msg = buf.getvalue().strip() or "operation failed"
        # die() prints "error: <msg>"; trim the prefix for a clean envelope.
        return _err("failed", msg.split("error: ", 1)[-1])


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
    return uid == os.getuid()


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
        self._send(dispatch(msg.get("verb"), msg.get("args") or {}))

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
    server = _Server(str(BROKER_SOCKET), _Handler)

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
                socket_path=None, timeout: float = CLIENT_TIMEOUT_S) -> dict:
    """Send one framed request and return the parsed reply dict."""
    path = str(socket_path or BROKER_SOCKET)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        req = json.dumps({"verb": verb, "args": args or {}}).encode()
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
