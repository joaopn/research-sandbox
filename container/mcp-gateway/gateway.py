"""rs-mcpgw — the shared MCP gateway.

One container beside the router and Gitea. It holds the addresses and secret
headers of the registered MCP servers (rendered by the host into
/etc/mcp-gateway/servers.json) and is the only place a project ever reaches
them through.

This version forwards NOTHING: no consumer is wired yet, so every request is
answered 403. What it does:

  - keeps the server list loaded, re-reading it on SIGHUP (a failed re-read
    keeps the last good list; every attempt prints one line);
  - `--probe NAME --timeout S`: ask one server to introduce itself as an MCP
    server and print ONE JSON line {outcome, http_status, detail}. The whole
    probe ends within S seconds whatever the server (or its DNS) does.

Stdlib only.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVERS_PATH = os.environ.get("MCP_GATEWAY_SERVERS", "/etc/mcp-gateway/servers.json")
LISTEN_HOST = os.environ.get("MCP_GATEWAY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MCP_GATEWAY_PORT", "8888"))

# An `initialize` reply is a few hundred bytes; the cap only stops a server
# from feeding the probe without end.
PROBE_MAX_BYTES = 65536
# A trailing-slash redirect is one hop; three leaves room for a canonical-path
# chain, and a loop ends after three requests instead of urllib's ten.
PROBE_MAX_REDIRECTS = 3

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "rs-mcp-gateway", "version": "1"}},
}

# Rebound whole, never mutated: one atomic store, so the SIGHUP handler needs
# no lock (a lock here could be re-entered by a second HUP and never released).
servers: dict = {}


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


def reload_servers(*_ignored) -> None:
    """Every attempt prints one line; a failure keeps the last good list."""
    global servers
    try:
        found = read_servers()
    except Exception as e:  # noqa: BLE001 — any failure keeps the last list
        say(f"reload failed: {type(e).__name__}; keeping {len(servers)}")
        return
    servers = found
    say(f"reload: {len(found)} servers")


class Refuse(BaseHTTPRequestHandler):
    server_version = "rs-mcpgw/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _refuse(self):
        body = b'{"error": "this caller is not wired to any MCP server"}\n'
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse


def serve() -> None:
    # Handlers BEFORE the first load: as PID 1 a HUP or TERM with the default
    # disposition is dropped, and docker stop would wait out its grace period.
    signal.signal(signal.SIGHUP, reload_servers)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    reload_servers()
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Refuse)
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
