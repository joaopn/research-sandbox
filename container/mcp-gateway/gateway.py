"""rs-mcpgw — the shared MCP gateway.

One container beside the router and Gitea. It holds the addresses and secret
headers of the registered MCP servers and the wiring table (both rendered by
the host into /etc/mcp-gateway, read-only), and is the only place a project
ever reaches those servers through.

  - A request `/<server>[/<rest>]` with `Authorization: Bearer <token>` is
    relayed to that server when the token is known, the call arrives from the
    token's own project network, and the server is on that consumer's list.
    The caller's token never goes upstream; the server's own secret headers
    do. Anything else is refused with a fixed one-line answer.
  - SIGHUP re-reads both files (a failed re-read keeps the last good pair;
    every attempt prints one line).
  - `--probe NAME --timeout S`: ask one server to introduce itself as an MCP
    server and print ONE JSON line {outcome, http_status, detail}. The whole
    probe ends within S seconds whatever the server (or its DNS) does.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import select
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVERS_PATH = os.environ.get("MCP_GATEWAY_SERVERS", "/etc/mcp-gateway/servers.json")
GRANTS_PATH = os.environ.get(
    "MCP_GATEWAY_GRANTS", os.path.join(os.path.dirname(SERVERS_PATH), "grants.json"))
LISTEN_HOST = os.environ.get("MCP_GATEWAY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MCP_GATEWAY_PORT", "8888"))

# An `initialize` reply is a few hundred bytes; the cap only stops a server
# from feeding the probe without end.
PROBE_MAX_BYTES = 65536
# A trailing-slash redirect is one hop; three leaves room for a canonical-path
# chain, and a loop ends after three requests instead of urllib's ten.
PROBE_MAX_REDIRECTS = 3
# How long a caller may take to send one whole request head, and how long a
# server may take to accept the connection. Both are local or one network hop: a
# healthy peer needs milliseconds, a slow hosted server a few seconds. Half of
# it would cut cold TLS handshakes; ten times it would let a silent connection
# hold a thread for minutes. The same value is the IDLE bound on a request
# body. The server's reply is NOT bounded: a tool call may legitimately run for
# a long time.
HEAD_TIMEOUT_S = float(os.environ.get("MCP_GATEWAY_HEAD_TIMEOUT_S", "30"))
CONNECT_TIMEOUT_S = float(os.environ.get("MCP_GATEWAY_CONNECT_TIMEOUT_S", "30"))
# One read from either side; only a buffer size.
PIECE = 65536
# A chunked request body has no declared length, so the relay must hold it to
# send it on with one. MCP requests are JSON-RPC messages, normally a few KiB;
# 8 MiB is three orders of magnitude above that and still small next to the
# gateway's memory, so a caller cannot make the shared gateway hold an
# unbounded body. A larger upload sends a Content-Length and is streamed
# through without being held at all.
CHUNKED_BODY_MAX = 8 * 1024 * 1024

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "rs-mcp-gateway", "version": "1"}},
}

SERVER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# Never forwarded in either direction (RFC 9110 hop-by-hop), plus the framing
# headers this relay sets itself.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailer", "trailers", "transfer-encoding", "upgrade",
              "content-length"}
# Never sent to a server: the caller's token, its Host, and RS's ownership mark.
CALLER_ONLY = {"authorization", "host", "x-rs-mcp"}

# Rebound whole, never mutated: one atomic store each, so the SIGHUP handler
# needs no lock (a lock here could be re-entered by a second HUP and never
# released). `state` is ONE object so a request never sees a new server list
# with an old wiring table.
state: dict = {"servers": {}, "consumers": {}, "subnets": {}}


def say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def read_servers() -> dict:
    with open(SERVERS_PATH, "rb") as f:
        data = json.loads(f.read())
    found = data["servers"]
    if not isinstance(found, dict):
        raise ValueError("servers is not an object")
    for name, entry in found.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            raise ValueError("a server has no address")
    return found


def read_grants() -> tuple:
    """(consumers, subnets). No file yet = nobody is wired."""
    try:
        with open(GRANTS_PATH, "rb") as f:
            data = json.loads(f.read())
    except FileNotFoundError:
        return {}, {}
    consumers, subnets = data["consumers"], data["subnets"]
    if not isinstance(consumers, dict) or not isinstance(subnets, dict):
        raise ValueError("the wiring table is not two objects")
    nets = {}
    for project, subnet in subnets.items():
        nets[project] = ipaddress.ip_network(subnet)
    for digest, c in consumers.items():
        if not isinstance(c, dict) or c.get("project") not in nets \
                or not isinstance(c.get("consumer"), str) \
                or not isinstance(c.get("servers"), list):
            raise ValueError("a consumer is not valid")
    return consumers, nets


def reload_all(*_ignored) -> None:
    """Every attempt prints one line; a failure keeps the last good pair."""
    global state
    try:
        found = read_servers()
        consumers, subnets = read_grants()
    except Exception as e:  # noqa: BLE001 — any failure keeps the last pair
        say(f"reload failed: {type(e).__name__}; keeping {len(state['servers'])}")
        return
    state = {"servers": found, "consumers": consumers, "subnets": subnets}
    say(f"reload: {len(found)} servers, {len(consumers)} wired")


def audit(**fields) -> None:
    fields["ts"] = round(time.time(), 3)
    say(json.dumps(fields, sort_keys=True))


class Relay(BaseHTTPRequestHandler):
    server_version = "rs-mcpgw/2"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        # The head must arrive within HEAD_TIMEOUT_S. The same value stays on
        # the socket as an idle bound while the request body is read; _forward
        # lifts it once the body has been sent on.
        try:
            self.connection.settimeout(HEAD_TIMEOUT_S)
        except OSError:
            self.close_connection = True
            return
        # The socket timeout bounds each read; this bounds the head as a whole
        # (a caller sending one byte at a time would otherwise hold a thread
        # for as long as it liked). _serve cancels it once the head is in.
        self._head_timer = threading.Timer(HEAD_TIMEOUT_S, self._drop_slow_head)
        self._head_timer.daemon = True
        self._head_timer.start()
        try:
            super().handle_one_request()
        finally:
            self._head_timer.cancel()

    def _drop_slow_head(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _refuse(self, status: int, message: str, **who) -> None:
        body = (json.dumps({"error": message}) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True
        audit(method=self.command, status=status, refused=message, **who)

    def _request_body(self):
        """(pieces, length) of the caller's body, or (None, None) without one.
        A body with a length is streamed through unbuffered. A chunked body
        is decoded here (http.server does not) and sent on with a length, so
        it is held in memory and therefore capped."""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            body = b""
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    raise ConnectionError("caller left mid-body")
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
                if size < 0:
                    raise ValueError("negative chunk size")
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass                           # trailers
                    break
                if len(body) + size > CHUNKED_BODY_MAX:
                    raise OverflowError("chunked body too large")
                piece = self.rfile.read(size)
                if len(piece) != size:
                    raise ConnectionError("caller left mid-body")
                body += piece
                self.rfile.readline(3)                 # the chunk's CRLF
            return iter([body]) if body else iter(()), len(body)
        length = self.headers.get("Content-Length")
        if length is None:
            return None, None
        left = int(length)
        if left < 0:
            raise ValueError("negative length")

        def pieces(left=left):
            # Runs while the body is being sent on (inside conn.request): a
            # caller that stalls or leaves is told apart from a server failure.
            while left:
                try:
                    piece = self.rfile.read(min(PIECE, left))
                except OSError:
                    piece = b""
                if not piece:
                    self._body_failed = True
                    raise ConnectionError("caller stalled or left mid-body")
                left -= len(piece)
                yield piece
        return pieces(), left

    def _serve(self):
        # The head is in. The socket keeps HEAD_TIMEOUT_S as an IDLE bound
        # while the request body is read (a caller that declares a body and
        # then goes silent must not hold a thread); _forward lifts it once the
        # body has been sent on.
        self._head_timer.cancel()
        current = state                                # one consistent pair
        path, _, query = self.path.partition("?")
        name, _, rest = path.lstrip("/").partition("/")
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        consumer = current["consumers"].get(
            hashlib.sha256(token.encode()).hexdigest()) if token else None
        if consumer is None:
            return self._refuse(401, "this caller is not wired to any MCP server")
        who = {"project": consumer["project"], "consumer": consumer["consumer"],
               "server": name}
        try:
            inside = ipaddress.ip_address(self.client_address[0]) \
                in current["subnets"][consumer["project"]]
        except (ValueError, KeyError):
            inside = False
        if not inside:
            return self._refuse(403, "this call did not arrive from its project", **who)
        if not SERVER_NAME_RE.match(name) or name not in consumer["servers"]:
            return self._refuse(403, "this caller is not wired to that MCP server", **who)
        # A wired server is its registered address and what lies beneath it:
        # a path that climbs out of it (also percent-encoded) is refused.
        if any(seg in (".", "..") for seg in urllib.parse.unquote(rest).split("/")) \
                or "\\" in rest:
            return self._refuse(400, "the path leaves the MCP server's address", **who)
        entry = current["servers"].get(name)
        if entry is None:
            return self._refuse(404, "that MCP server is not on the list any more", **who)
        try:
            body, length = self._request_body()
        except OverflowError:
            return self._refuse(413, "the request is too large to send without "
                                     "a declared length", **who)
        except (ValueError, OSError):
            return self._refuse(400, "the request could not be read", **who)
        self._body_failed = False
        self._forward(entry, rest, query, body, length, who)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _serve

    def _forward(self, entry, rest, query, body, length, who) -> None:
        url = urllib.parse.urlsplit(entry["url"])
        scheme, host, port = _origin(url)
        target = url.path or "/"
        if rest:
            target = target.rstrip("/") + "/" + rest
        if query:
            target += "?" + query
        headers = {}
        for h, v in self.headers.items():
            if h.lower() not in HOP_BY_HOP and h.lower() not in CALLER_ONLY:
                headers[h] = v
        secret = {h.lower() for h in (entry.get("headers") or {})}
        headers = {h: v for h, v in headers.items() if h.lower() not in secret}
        headers.update(entry.get("headers") or {})
        if length is not None:
            headers["Content-Length"] = str(length)
        conn = None
        try:
            if scheme == "https":
                conn = http.client.HTTPSConnection(
                    host, port, timeout=CONNECT_TIMEOUT_S,
                    context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT_S)
            conn.connect()
            conn.sock.settimeout(None)                 # connected: reads are unbounded
            up_sock = conn.sock
            conn.request(self.command, target, body=body, headers=headers)
            self.connection.settimeout(None)           # the body is sent: the reply may take long
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as e:
            # ValueError: http.client refuses a request line or header it
            # cannot encode (a non-ASCII byte in the path).
            if conn is not None:
                conn.close()
            if self._body_failed:
                return self._refuse(408, "the request body stopped arriving", **who)
            if isinstance(e, ValueError) and not isinstance(e, OSError):
                return self._refuse(400, "the request could not be sent on", **who)
            return self._refuse(502, "the MCP server could not be reached "
                                     f"({type(e).__name__})", **who)
        sent = 0
        try:
            self.send_response(resp.status)
            for h, v in resp.getheaders():
                if h.lower() not in HOP_BY_HOP:
                    self.send_header(h, v)
            # One request per connection: the body's end is then the close, so
            # a stream of unknown length needs no re-framing.
            self.send_header("Connection", "close")
            if resp.length is not None:
                self.send_header("Content-Length", str(resp.length))
            self.end_headers()
            self.close_connection = True
            self.wfile.flush()
            if self.command != "HEAD":
                sent = self._pump(resp, up_sock)
        except OSError:
            pass                                        # the caller left
        finally:
            conn.close()
        audit(method=self.command, status=resp.status, bytes=sent, **who)

    def _pump(self, resp, up_sock) -> int:
        """Server → caller, piece by piece as it arrives, until the server
        ends the body. A caller that hangs up while the server is silent (an
        open event stream) is noticed by a watcher, which shuts the server's
        socket down so the blocked read below returns."""
        sent = 0
        wake_r, wake_w = os.pipe()

        def watch() -> None:
            while True:
                ready, _, _ = select.select([self.connection, wake_r], [], [])
                if wake_r in ready:
                    return                              # the body ended first
                try:
                    # Readable with nothing to read = the caller closed. Stray
                    # bytes (one request per connection, so nothing more is
                    # expected) are discarded and the watch goes on.
                    gone = self.connection.recv(PIECE) == b""
                except OSError:
                    gone = True
                if gone:
                    try:
                        up_sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            while True:
                try:
                    piece = resp.read1(PIECE)
                except (OSError, http.client.HTTPException):
                    return sent                         # shut down by the watcher, or torn
                if not piece:
                    return sent
                self.wfile.write(piece)
                self.wfile.flush()
                sent += len(piece)
        finally:
            os.write(wake_w, b"x")
            watcher.join()
            os.close(wake_r)
            os.close(wake_w)


def serve() -> None:
    # Handlers BEFORE the first load: as PID 1 a HUP or TERM with the default
    # disposition is dropped, and docker stop would wait out its grace period.
    signal.signal(signal.SIGHUP, reload_all)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    reload_all()
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Relay)
    httpd.daemon_threads = True
    say(f"listening on {LISTEN_HOST}:{LISTEN_PORT}")
    httpd.serve_forever()


# --- probe -------------------------------------------------------------------

def _origin(parts: urllib.parse.SplitResult) -> tuple:
    return (parts.scheme, (parts.hostname or "").lower(),
            parts.port or (443 if parts.scheme == "https" else 80))


def _is_initialize_reply(obj) -> bool:
    return (isinstance(obj, dict) and isinstance(obj.get("result"), dict)
            and "protocolVersion" in obj["result"])


def _speaks_mcp(buf: bytes) -> bool:
    text = buf.decode("utf-8", "replace")
    try:
        if _is_initialize_reply(json.loads(text)):
            return True
    except ValueError:
        pass
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                if _is_initialize_reply(json.loads(line[5:].strip())):
                    return True
            except ValueError:
                continue
    return False


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout("deadline")
    return left


def _exchange(entry: dict, deadline: float) -> dict:
    url = urllib.parse.urlsplit(entry["url"])
    origin = _origin(url)
    body = json.dumps(INITIALIZE).encode()
    for _hop in range(PROBE_MAX_REDIRECTS + 1):
        scheme, host, port = _origin(url)
        if scheme == "https":
            conn = http.client.HTTPSConnection(
                host, port, timeout=_left(deadline),
                context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=_left(deadline))
        try:
            headers = {"Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream"}
            headers.update(entry.get("headers") or {})
            conn.request("POST", url.path or "/", body=body, headers=headers)
            # Kept by name: http.client drops conn.sock once a reply says
            # Connection: close, while the response still reads from it.
            sock = conn.sock
            sock.settimeout(_left(deadline))
            resp = conn.getresponse()
            status = resp.status
            if status in (301, 302, 303, 307, 308):
                target = urllib.parse.urlsplit(urllib.parse.urljoin(
                    urllib.parse.urlunsplit(url), resp.getheader("Location") or ""))
                if _origin(target) != origin:
                    return {"outcome": "not_mcp", "http_status": status,
                            "detail": "the server redirects to another address"}
                url = target
                continue
            if status in (401, 403):
                return {"outcome": "refused", "http_status": status, "detail": ""}
            buf = b""
            while len(buf) < PROBE_MAX_BYTES:
                sock.settimeout(_left(deadline))
                chunk = resp.read1(min(4096, PROBE_MAX_BYTES - len(buf)))
                if not chunk:
                    break
                buf += chunk
                if _speaks_mcp(buf):
                    return {"outcome": "mcp", "http_status": status, "detail": ""}
            if _speaks_mcp(buf):
                return {"outcome": "mcp", "http_status": status, "detail": ""}
            return {"outcome": "not_mcp", "http_status": status,
                    "detail": "the answer is not an MCP introduction"}
        finally:
            conn.close()
    return {"outcome": "not_mcp", "http_status": None,
            "detail": "the server keeps redirecting"}


def _unreachable(e: BaseException) -> dict:
    if isinstance(e, ssl.SSLCertVerificationError):
        why = "its certificate is not trusted"
    elif isinstance(e, ssl.SSLError):
        why = "the secure connection failed"
    elif isinstance(e, socket.gaierror):
        why = "its name does not resolve"
    elif isinstance(e, (socket.timeout, TimeoutError)):
        why = "no answer in time"
    elif isinstance(e, ConnectionRefusedError):
        why = "the connection was refused"
    elif isinstance(e, http.client.HTTPException):
        why = "it did not answer as a web server"
    else:
        why = "the connection failed"
    return {"outcome": "unreachable", "http_status": None,
            "detail": f"{why} ({type(e).__name__})"}


def probe(name: str, timeout_s: float) -> None:
    """Prints exactly one JSON line, from the MAIN thread only, and ends the
    process: a worker stuck in a name lookup or a slow read cannot outlive the
    deadline (os._exit does not wait for it)."""
    deadline = time.monotonic() + timeout_s
    result: list = []

    def work() -> None:
        try:
            entry = read_servers().get(name)
            if entry is None:
                result.append({"outcome": "unknown", "http_status": None,
                               "detail": "no such server"})
                return
            result.append(_exchange(entry, deadline))
        except BaseException as e:  # noqa: BLE001 — every failure is an outcome
            result.append(_unreachable(e))

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout_s)
    out = result[0] if result else {"outcome": "unreachable", "http_status": None,
                                    "detail": "no answer in time (deadline)"}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    os._exit(0)


def main(argv: list) -> None:
    if len(argv) == 4 and argv[0] == "--probe" and argv[2] == "--timeout":
        probe(argv[1], float(argv[3]))
    if argv:
        sys.stderr.write("usage: gateway.py [--probe NAME --timeout SECONDS]\n")
        sys.exit(2)
    serve()


if __name__ == "__main__":
    main(sys.argv[1:])
