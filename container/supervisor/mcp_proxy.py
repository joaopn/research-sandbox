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
  - SSE pass-through (line-flushed) when upstream emits text/event-stream
  - Transparent 3xx redirect-following with method+body preserved, so a
    worker hitting ``/<name>/mcp`` against an upstream that 307s to
    ``/mcp/`` (Starlette ``redirect_slashes=True`` default) sees only
    the final response. Worker-side following can't work — relative
    ``Location`` headers resolve into the proxy's URL space, not the
    upstream's.
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

import json
import os
import signal
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


_OPENER = urllib.request.build_opener(_TransparentRedirect())

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
        try:
            with _OPENER.open(req, timeout=UPSTREAM_TIMEOUT) as resp:
                self._stream_response(resp, name, method, upstream_path,
                                      client_ip, body_len)
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read()
            except Exception:
                err_body = b""
            self.send_response(e.code)
            for h, v in e.headers.items():
                if h.lower() in HOP_BY_HOP:
                    continue
                self.send_header(h, v)
            if "content-length" not in {h.lower() for h in e.headers.keys()}:
                self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
            audit({
                "src": client_ip, "mcp": name, "method": method,
                "path": upstream_path, "status": e.code,
                "bytes_in": body_len, "bytes_out": len(err_body),
            })
        except urllib.error.URLError as e:
            self._error(502, name=name,
                        message=f"upstream unreachable: {e.reason}",
                        method=method, src=client_ip,
                        path=upstream_path, bytes_in=body_len)
        except Exception as e:  # pragma: no cover
            self._error(500, name=name, message=f"proxy error: {e}",
                        method=method, src=client_ip,
                        path=upstream_path, bytes_in=body_len)

    def _stream_response(self, resp, name, method, upstream_path,
                          client_ip, body_len):
        ctype = resp.headers.get("Content-Type", "")
        is_sse = "text/event-stream" in ctype.lower()
        self.send_response(resp.status)
        for h, v in resp.headers.items():
            if h.lower() in HOP_BY_HOP:
                continue
            self.send_header(h, v)
        self.end_headers()
        chunk_size = 1024 if is_sse else 8192
        bytes_out = 0
        try:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                self.wfile.write(chunk)
                if is_sse:
                    self.wfile.flush()
                bytes_out += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        audit({
            "src": client_ip, "mcp": name, "method": method,
            "path": upstream_path, "status": resp.status,
            "bytes_in": body_len, "bytes_out": bytes_out,
            **({"sse": True} if is_sse else {}),
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
