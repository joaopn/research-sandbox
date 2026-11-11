"""mcp_proxy — stdlib reverse proxy in front of MCP servers.

Runs as a long-lived container (`mcp-proxy`) in each supervisor's inner
dockerd, on the `rs-inner` user-defined bridge. Workers reach it as
``http://mcp-proxy:8888/<name>/<rest>``; the proxy forwards to the upstream
``ip:port`` recorded in its config (rendered by the supervisor entrypoint
from the host-side registry + per-project allowlist).

The proxy is intentionally dumb — it doesn't enforce ACLs (the per-project
allowlist + .mcp.json already gates which MCPs the worker even knows about,
and rs-router's iptables gates network reachability). It only does:

  - URL rewrite ``/<name>/<rest>`` → ``http://<ip>:<port>/<rest>``
  - Header injection (e.g. Authorization) from config
  - Streamed bodies (SSE included) are relayed piece by piece as the
    upstream sends them. A body whose length the upstream did not state
    (chunked or close-delimited) is relayed with ``Connection: close``, so
    its end is the connection's close; an error reply states the length
    of the bytes relayed (HEAD and 304 keep the upstream's)
  - Transparent 3xx redirect-following with method+body preserved, so a
    worker hitting ``/<name>/mcp`` against an upstream that 307s to
    ``/mcp/`` (Starlette ``redirect_slashes=True`` default) sees only
    the final response. Worker-side following can't work — relative
    ``Location`` headers resolve into the proxy's URL space, not the
    upstream's.
  - Client disconnects propagate: when the worker hangs up before its
    response was delivered, the upstream connection is shut down (the
    upstream sees the caller leave instead of a connection held open until
    it answers) and the request is logged with status 499
  - Audit log to /var/log/mcp-proxy/mcp-proxy.jsonl, one JSON line per request
  - SIGHUP reload of /etc/mcp-proxy/config.json

Config schema (rendered by the supervisor):

    {
      "<name>": {
        "ip": "172.17.0.1",
        "port": 9999,
        "headers": {"Authorization": "Bearer ..."}   // optional
      },
      ...
    }
"""

from __future__ import annotations

import http.client
import json
import os
import select
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get("MCP_PROXY_CONFIG", "/etc/mcp-proxy/config.json")
AUDIT_LOG = os.environ.get("MCP_PROXY_AUDIT", "/var/log/mcp-proxy/mcp-proxy.jsonl")
LISTEN_HOST = os.environ.get("MCP_PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MCP_PROXY_PORT", "8888"))
UPSTREAM_TIMEOUT = float(os.environ.get("MCP_PROXY_TIMEOUT", "86400"))  # 1 day
# Host header pinned on outgoing upstream requests. Stable, predictable,
# can't be spoofed by a worker — gives MCP servers a value they can put in
# their DNS-rebinding-protection allowlist. The default matches the proxy's
# rs-inner DNS name + listen port.
PROXY_HOSTNAME = os.environ.get("MCP_PROXY_HOSTNAME", "mcp-proxy:8888")

# Upper bound of one forwarded response piece. ``read1`` returns what one
# upstream read holds and never waits to fill it, so this value only bounds
# the memory held per piece, not when bytes are forwarded. Same size as the
# MCP gateway's PIECE (container/mcp-gateway/gateway.py).
RELAY_PIECE = 65536

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


class _TransparentRedirect(urllib.request.HTTPRedirectHandler):
    """Follow 3xx redirects transparently, preserving method and body,
    and rewrite proxy-pointing absolute Locations back to the upstream.

    urllib's default handler refuses 307/308 + non-GET (raises HTTPError)
    and silently downgrades 301/302/303 + POST to GET. Both are wrong
    for a forwarding proxy: the worker can't see redirects (a relative
    ``Location`` resolves into the proxy's URL space and 404s under the
    name-prefix router), and 307/308 are *defined* to preserve method.

    There's a second failure mode caused by our Host pinning. We send
    every upstream request with ``Host: mcp-proxy:8888`` so the upstream
    has a stable value to whitelist for DNS-rebinding-protection. But
    any framework that builds absolute ``Location`` URLs from the
    request's ``Host`` (Starlette's ``redirect_slashes`` is the one we
    hit in practice — sdp-jobs/FastMCP) emits ``Location:
    http://mcp-proxy:8888/<rest>``, which points back at us. Naively
    following that loops into the proxy and 404s as MCP name ``mcp``.
    We detect this by netloc-matching the proposed ``newurl`` against
    ``PROXY_HOSTNAME`` and rewriting the authority to the original
    upstream's authority, so the redirect actually lands on the real
    server.

    Loop detection still lives in ``HTTPRedirectHandler.http_error_302``
    (capped at 4 repeats); we only override the per-step request-rewrite
    policy. Cross-host redirects to *other* upstreams are safe-by-
    default: the rs-router only opens firewall holes for the registered
    ``(ip, port)``, so any redirect to an unregistered destination fails
    at the network layer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_parsed = urllib.parse.urlsplit(newurl)
        if new_parsed.netloc and new_parsed.netloc == PROXY_HOSTNAME:
            orig_parsed = urllib.parse.urlsplit(req.full_url)
            new_parsed = new_parsed._replace(
                scheme=orig_parsed.scheme, netloc=orig_parsed.netloc,
            )
            newurl = urllib.parse.urlunsplit(new_parsed)
        return urllib.request.Request(
            newurl.replace(" ", "%20"),
            data=req.data,
            headers=dict(req.headers),
            origin_req_host=req.origin_req_host,
            unverifiable=True,
            method=req.get_method(),
        )


class _ClientWatch:
    """Watch one forwarded request's client socket while the request is in
    flight, and shut the upstream connection(s) down when the client leaves.

    Without it the forwarding thread sits in a blocking upstream read (a
    sync role call, an idle SSE stream) with nothing looking at the client:
    a worker that hangs up leaves the thread and the upstream connection
    pinned until the upstream answers, and the upstream never learns its
    caller is gone.

    One watcher thread per IN-FLIGHT request (not per connection): it
    blocks in poll() on the client socket and a wake pipe, without a
    timeout. A FIN, an RST or an error on the client socket aborts: every
    upstream socket registered for the request is shut down, which wakes
    the forwarding thread's blocked read and sends FIN upstream. Data from
    the client while its request is outstanding is not a disconnect (see
    ``_client_sent_data``). ``finish()`` ends the window in which an abort
    can happen; ``stop()`` joins the thread and releases its fds.

    Upstream sockets are registered as ``dup()`` copies, shut down and
    closed only under the lock: a shutdown therefore always acts on the
    request's own connection, never on a descriptor number another thread
    has since reused.

    Documented boundaries:
      - A client that HALF-closes (``shutdown(SHUT_WR)``) after sending its
        request is treated as gone: a FIN cannot be told apart from a killed
        process at the socket level. This is nginx's default
        (``proxy_ignore_client_abort off``); no MCP client half-closes.
      - A client that leaves while the upstream TCP connect is still in
        progress releases nothing until that connect returns (bounded by the
        kernel's connect timeout); the connection is then shut down at once
        and the request fails as a disconnect.
      - Benign race, close-delimited response bodies only: a client that
        closes in the instant between the last byte of such a body and the
        proxy's final empty read is logged 499 although everything was
        delivered; nothing is cut short. Content-Length and chunked bodies
        cannot hit it (see ``Handler._stream_response``).
    """

    def __init__(self, client_sock):
        self._client = client_sock
        self._lock = threading.Lock()
        self.aborted = False
        self._done = False
        self._upstreams = []
        self._thread = None
        self._closed = False
        # Created last: nothing after it can raise, so a failed constructor
        # never leaks the pipe.
        self._wake_r, self._wake_w = os.pipe()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._watch, name="mcp-proxy-watch", daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        # Every exit path RETURNS: once the client is at EOF poll() reports
        # it readable forever, so looping on after an abort would spin.
        poller = select.poll()
        poller.register(self._client.fileno(), select.POLLIN)
        poller.register(self._wake_r, select.POLLIN)
        while True:
            events = poller.poll()
            if any(fd == self._wake_r for fd, _ in events):
                return
            if any(ev & (select.POLLHUP | select.POLLERR | select.POLLNVAL)
                   for _, ev in events):
                self.abort()
                return
            try:
                data = self._client.recv(
                    1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
            except BlockingIOError:
                continue                # spurious readiness
            except OSError:
                self.abort()            # ECONNRESET: the client reset
                return
            if not data:
                self.abort()            # FIN: the client closed
                return
            self._client_sent_data()
            return

    def _client_sent_data(self) -> None:
        """The client sent bytes while its request is outstanding
        (pipelining, or the unread rest of a chunked request body). That is
        not a disconnect: stop watching and let the request proceed exactly
        as an unwatched one would. The peek consumed nothing, so the next
        request on the connection is intact."""

    def abort(self) -> None:
        with self._lock:
            if self._done or self.aborted:
                return
            self.aborted = True
            for sock in self._upstreams:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def register(self, sock) -> None:
        """Called from the forwarding thread right after an upstream
        connection connects (every redirect hop included)."""
        with self._lock:
            if self.aborted:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                raise ConnectionAbortedError("client disconnected")
            if self._done:
                return
            try:
                self._upstreams.append(sock.dup())
            except OSError:
                pass    # out of fds: this upstream stays unwatched

    def finish(self) -> bool:
        """Close the abort window; return whether the request was aborted."""
        with self._lock:
            self._done = True
            return self.aborted

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self._done = True
            upstreams, self._upstreams = self._upstreams, []
            for sock in upstreams:
                sock.close()
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        # join() on a never-started thread raises RuntimeError.
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join()
        os.close(self._wake_r)
        os.close(self._wake_w)


class _NoWatch:
    """Stand-in when a watch cannot be set up (out of fds or threads): the
    request is forwarded unwatched, exactly as before watches existed."""
    aborted = False

    def register(self, sock) -> None:
        pass

    def finish(self) -> bool:
        return False

    def stop(self) -> None:
        pass


_NO_WATCH = _NoWatch()

# The watch of the request the current thread is forwarding. Thread-local
# rather than carried on the Request because _TransparentRedirect mints a
# new Request per hop; every hop connects on the forwarding thread.
_current = threading.local()


class _WatchedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        watch = getattr(_current, "watch", None)
        if watch is not None:
            watch.register(self.sock)


class _WatchedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_WatchedHTTPConnection, req)


# build_opener drops its default HTTPHandler when given an instance of a
# subclass, so every upstream connection goes through the watched class.
_OPENER = urllib.request.build_opener(
    _WatchedHTTPHandler(), _TransparentRedirect())

config_lock = threading.RLock()
config: dict = {}


def load_config() -> None:
    global config
    try:
        with open(CONFIG_PATH) as f:
            new = json.load(f)
    except FileNotFoundError:
        with config_lock:
            config = {}
        sys.stderr.write(f"config {CONFIG_PATH} missing; serving 404 for all routes\n")
        return
    except json.JSONDecodeError as e:
        sys.stderr.write(f"config {CONFIG_PATH} invalid JSON ({e}); keeping previous\n")
        return
    if not isinstance(new, dict):
        sys.stderr.write(f"config {CONFIG_PATH} root must be an object; keeping previous\n")
        return
    with config_lock:
        config = new
    sys.stderr.write(f"loaded {len(new)} MCP route(s) from {CONFIG_PATH}\n")


def audit(record: dict) -> None:
    record["ts"] = round(time.time(), 3)
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    except OSError as e:
        sys.stderr.write(f"audit write failed: {e}\n")


def split_route(raw_path: str):
    """Split ``/<name>/<rest>`` (rest may include query-string). Returns
    (name, upstream_path) or None on malformed input.

    Rejects path traversal (``..`` segments, including URL-encoded variants):
    a worker could otherwise reach adjacent endpoints on the upstream MCP
    that weren't intended. Defense-in-depth — workers are sandboxed but the
    proxy shouldn't blindly forward."""
    if not raw_path.startswith("/"):
        return None
    rest = raw_path[1:]
    if not rest:
        return None
    sep_idx = -1
    for i, c in enumerate(rest):
        if c in "/?":
            sep_idx = i
            break
    if sep_idx == -1:
        name = rest
        upstream_path = "/"
    else:
        name = rest[:sep_idx]
        tail = rest[sep_idx:]
        upstream_path = tail if tail.startswith("/") else "/" + tail
    if not name:
        return None
    # Strip query-string before traversal check; decode %xx to catch
    # %2e%2e and similar encodings.
    path_only = upstream_path.split("?", 1)[0]
    decoded = urllib.parse.unquote(path_only)
    if any(seg == ".." for seg in decoded.split("/")):
        return None
    return name, upstream_path


class Handler(BaseHTTPRequestHandler):
    server_version = "rs-mcp-proxy/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence default access log
        pass

    def _proxy(self):
        route = split_route(self.path)
        if route is None:
            self._error(400, name=None, message="malformed path")
            return
        name, upstream_path = route
        with config_lock:
            entry = config.get(name)
        if entry is None:
            self._error(404, name=name, message=f"unknown MCP {name!r}")
            return

        ip = entry["ip"]
        port = entry["port"]
        extra_headers = dict(entry.get("headers", {}))
        upstream_url = f"http://{ip}:{port}{upstream_path}"

        method = self.command
        body_len = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(body_len) if body_len > 0 else None

        out_headers = {}
        for h, v in self.headers.items():
            if h.lower() in HOP_BY_HOP or h.lower() == "host":
                continue
            out_headers[h] = v
        # Pin Host so upstream MCPs see a stable value (matches the proxy's
        # canonical rs-inner DNS name) regardless of what the client sent.
        out_headers["Host"] = PROXY_HOSTNAME
        for h, v in extra_headers.items():
            out_headers[h] = v

        req = urllib.request.Request(
            upstream_url, data=body, headers=out_headers, method=method,
        )
        client_ip = self.client_address[0]
        watch = _NO_WATCH
        try:
            try:
                watch = _ClientWatch(self.connection)
                watch.start()
            except (OSError, RuntimeError) as e:
                sys.stderr.write(
                    f"client watch unavailable ({e}); forwarding unwatched\n")
                watch.stop()
                watch = _NO_WATCH
            _current.watch = watch
            self._forward(req, watch, name, method, upstream_path,
                          client_ip, body_len)
        finally:
            _current.watch = None
            watch.stop()

    def _forward(self, req, watch, name, method, upstream_path,
                 client_ip, body_len):
        try:
            with _OPENER.open(req, timeout=UPSTREAM_TIMEOUT) as resp:
                self._stream_response(resp, watch, name, method,
                                      upstream_path, client_ip, body_len)
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read()
            except Exception:
                err_body = b""
            if watch.finish():
                self._client_gone(name, method, upstream_path, client_ip,
                                  body_len, upstream_status=e.code)
                return
            # The whole error body has been read, so its length is known:
            # state it, never the upstream's header (chunked + a stray
            # Content-Length, an unparsable one, or a torn body whose read
            # failed would otherwise leave the client waiting for bytes that
            # never come). A bodiless reply (HEAD, 304) keeps the upstream's
            # Content-Length, which describes the representation, not bytes
            # on this wire.
            bodiless = self._error_reply_bodiless(method, e.code)
            self.send_response(e.code)
            for h, v in e.headers.items():
                if h.lower() in HOP_BY_HOP:
                    continue
                if not bodiless and h.lower() == "content-length":
                    continue
                self.send_header(h, v)
            if not bodiless or "content-length" not in {
                    h.lower() for h in e.headers.keys()}:
                self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
            audit({
                "src": client_ip, "mcp": name, "method": method,
                "path": upstream_path, "status": e.code,
                "bytes_in": body_len, "bytes_out": len(err_body),
            })
        except urllib.error.URLError as e:
            if watch.finish():
                self._client_gone(name, method, upstream_path, client_ip,
                                  body_len)
                return
            self._error(502, name=name,
                        message=f"upstream unreachable: {e.reason}",
                        method=method, src=client_ip,
                        path=upstream_path, bytes_in=body_len)
        except Exception as e:  # pragma: no cover
            # An aborted getresponse() (RemoteDisconnected) lands here.
            if watch.finish():
                self._client_gone(name, method, upstream_path, client_ip,
                                  body_len)
                return
            self._error(500, name=name, message=f"proxy error: {e}",
                        method=method, src=client_ip,
                        path=upstream_path, bytes_in=body_len)

    def _stream_response(self, resp, watch, name, method, upstream_path,
                          client_ip, body_len):
        ctype = resp.headers.get("Content-Type", "")
        is_sse = "text/event-stream" in ctype.lower()
        # Decided before any read: http.client's length changes as the body
        # is consumed.
        framed = self._body_is_framed(resp)
        self.send_response(resp.status)
        for h, v in resp.headers.items():
            if h.lower() in HOP_BY_HOP:
                continue
            # An unframed body's upstream Content-Length (sent beside a
            # chunked encoding, or unparsable) does not describe what is
            # relayed.
            if not framed and h.lower() == "content-length":
                continue
            self.send_header(h, v)
        if not framed:
            # One request per connection: the body's end is the close, so
            # a stream of unknown length needs no re-framing.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        bytes_out = 0
        read_error = None
        client_failed = False
        while True:
            try:
                # read1 forwards what has arrived; read(n) would hold a
                # short event until n bytes or the end of the body came.
                chunk = resp.read1(RELAY_PIECE)
            except (OSError, http.client.HTTPException) as e:
                read_error = e
                break
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                client_failed = True
                break
            bytes_out += len(chunk)
        aborted = watch.finish()
        # 499 only when the client left BEFORE the response was delivered
        # (nginx's rule). A client that reads a complete response and then
        # hangs up may trip the watch before finish(); that is a normal end.
        gone = client_failed or (aborted and (
            read_error is not None or not self._delivered_in_full(resp)))
        # An upstream reset with the client still there keeps its old record
        # (the upstream's status); any other read failure goes to _proxy's
        # error branches as before.
        if read_error is not None and not gone and not isinstance(
                read_error, (BrokenPipeError, ConnectionResetError)):
            raise read_error
        record = {
            "src": client_ip, "mcp": name, "method": method,
            "path": upstream_path, "status": resp.status,
            "bytes_in": body_len, "bytes_out": bytes_out,
            **({"sse": True} if is_sse else {}),
        }
        if gone:
            self.close_connection = True
            record.update(status=499, upstream_status=resp.status,
                          error="client disconnected")
        audit(record)

    @staticmethod
    def _body_is_framed(resp) -> bool:
        """Whether the relayed reply carries its own end: only when the
        upstream stated a usable Content-Length. http.client sets ``length``
        to None for a chunked reply that has a body (its framing is
        hop-by-hop and dropped here) and for a missing, unparsable or
        negative Content-Length (close-delimited), and to 0 for a bodiless
        reply (HEAD, 204, 304, 1xx). Must be read before the body is."""
        return resp.length is not None

    @staticmethod
    def _error_reply_bodiless(method, status) -> bool:
        """Whether an upstream error reply carries no body: http.client's
        own rule (HEAD, 204, 304, 1xx) restricted to the cases that reach
        the error branch, HEAD and 304."""
        return method == "HEAD" or status == 304

    @staticmethod
    def _delivered_in_full(resp) -> bool:
        """Whether a read loop that ended without an error read the whole
        body. A chunked body ends cleanly only at its terminating chunk (a
        cut one raises IncompleteRead). A Content-Length body is complete
        when no length remains (HEAD/204/304 have length 0); a cut one
        returns b"" with length left. A close-delimited body cannot be told
        from a cut one."""
        if resp.chunked:
            return True
        return resp.length == 0

    def _client_gone(self, name, method, path, client_ip, body_len, **extra):
        """The client left before its response was sent: write nothing,
        drop the connection, and audit status 499 (nginx's "client closed
        request", a status no upstream sends)."""
        self.close_connection = True
        audit({
            "src": client_ip, "mcp": name, "method": method, "path": path,
            "status": 499, "error": "client disconnected",
            "bytes_in": body_len, **extra,
        })

    def _error(self, code, name, message, **kw):
        body = json.dumps({"error": message, "mcp": name}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        audit({
            "mcp": name, "status": code, "error": message,
            "bytes_out": len(body), **kw,
        })

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_PATCH = _proxy
    do_HEAD = _proxy
    do_OPTIONS = _proxy


def main():
    load_config()

    def _sighup(signum, frame):
        sys.stderr.write("SIGHUP received; reloading config\n")
        load_config()

    signal.signal(signal.SIGHUP, _sighup)

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    sys.stderr.write(f"mcp-proxy listening on {LISTEN_HOST}:{LISTEN_PORT}\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
