"""mcp_servers — the MCP gateway's server list.

Stdlib only. The list is the set of MCP servers the shared gateway container
(`rs-mcpgw`) knows: an address and optional secret request headers per name.
It lives in ~/.research-sandbox/mcp-servers.json (0600) and is rendered,
unchanged, into the one folder the gateway container mounts read-only.

Every value is LITERAL: nothing is expanded from an environment, so the list
reads the same from the host command and from the broker daemon.

Refusal texts reach the browser verbatim: they name a field or a header NAME,
never a value, a host path or a command.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

STATE_DIR = Path.home() / ".research-sandbox"
LIST_PATH = STATE_DIR / "mcp-servers.json"
LOCK_PATH = STATE_DIR / "mcp-servers.lock"
# The only folder the gateway container sees (a directory bind, read-only: the
# rendered file is replaced by rename, which a single-file bind would not follow).
GATEWAY_DIR = STATE_DIR / "mcp-gateway"
RENDERED_NAME = "servers.json"

VERSION = 1
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# RFC 7230 token: what a header field name may be made of.
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
SCHEMES = ("http", "https")
ENTRY_KEYS = frozenset({"url", "description", "headers"})

OK, DAMAGED, NEWER = "ok", "damaged", "newer"

DAMAGED_TEXT = ("the MCP server list on the host cannot be used ({why}); "
                "use Start an empty list in Sandboxes → MCP to replace it "
                "(every server and secret on it is discarded)")
NEWER_TEXT = ("the MCP server list on the host was written by a newer version "
              "of RS than the one running; update RS to use it (nothing was "
              "changed)")
BUSY_TEXT = "the MCP server list is being changed, try again"


class ServerListError(Exception):
    """A refusal whose text is safe to show in the browser."""


class Busy(ServerListError):
    pass


def empty() -> dict[str, Any]:
    return {"version": VERSION, "servers": {}}


def normalize_url(url: Any) -> str:
    """The address as stored: scheme://host[:port]/path. Refuses anything else
    (a login, a query or a fragment in particular: a key belongs in a secret
    header, and the address is shown on the page). Never echoes the input."""
    if not isinstance(url, str) or not url.strip():
        raise ServerListError("an address is required")
    try:
        p = urllib.parse.urlsplit(url.strip())
        port = p.port
    except ValueError:
        raise ServerListError("the address is not a valid URL") from None
    if p.scheme not in SCHEMES:
        raise ServerListError("the address must start with http:// or https://")
    if p.username is not None or p.password is not None or "@" in p.netloc:
        raise ServerListError("the address must not carry a login; put "
                              "credentials in a secret header")
    if p.query or p.fragment or "?" in url or "#" in url:
        raise ServerListError("the address must not carry a query (?...) or a "
                              "fragment (#...); put a key in a secret header")
    host = p.hostname
    if not host or any(ord(c) < 0x21 or ord(c) >= 0x7f for c in host):
        raise ServerListError("the address needs a plain ASCII host name "
                              "(an international name in its xn-- form)")
    if port is not None and not 1 <= port <= 65535:
        raise ServerListError("the address's port must be from 1 to 65535")
    path = p.path or "/"
    if any(ord(c) < 0x21 or ord(c) >= 0x7f for c in path):
        raise ServerListError("the address's path may hold only plain ASCII "
                              "characters without spaces (write others "
                              "percent-encoded, as in %C3%A9)")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return f"{p.scheme}://{netloc}{path}"


def header_problem(name: Any, value: Any) -> str | None:
    if not isinstance(name, str) or not HEADER_NAME_RE.match(name):
        return "a header name may hold only letters, digits and - _ . ! # $ % & ' * + ^ ` | ~"
    if name.lower() in ("host", "content-length", "transfer-encoding", "connection"):
        return f"the header {name} is set by the gateway and cannot be a secret header"
    if not isinstance(value, str) or not value:
        return f"the header {name} needs a value"
    if any(c in value for c in "\r\n\0"):
        return f"the value of the header {name} must be a single line"
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        # http.client can send nothing else in a header.
        return (f"the value of the header {name} may hold only Latin-1 "
                "characters (a key or token normally does)")
    return None


def entry_problem(name: Any, entry: Any) -> str | None:
    """Why (name, entry) is not a valid server, or None. Used both for input
    and for judging a file read from disk."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return ("a server name starts with a lowercase letter and holds only "
                "lowercase letters, digits and dashes")
    if not isinstance(entry, dict):
        return "a server must be an object"
    if set(entry) - ENTRY_KEYS:
        return "a server holds an unknown field"
    try:
        if normalize_url(entry.get("url")) != entry.get("url"):
            return "a server's address is not in its stored form"
    except ServerListError as e:
        return str(e)
    desc = entry.get("description", "")
    if not isinstance(desc, str) or any(c in desc for c in "\r\n\0"):
        return "a description must be a single line of text"
    headers = entry.get("headers", {})
    if not isinstance(headers, dict):
        return "the secret headers must be an object"
    for hn, hv in headers.items():
        problem = header_problem(hn, hv)
        if problem:
            return problem
    return None


def judge(raw: bytes) -> tuple[str, dict[str, Any], str | None]:
    """(state, data, why) for the bytes of a list file."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return DAMAGED, empty(), "it is not valid JSON"
    if not isinstance(data, dict):
        return DAMAGED, empty(), "it does not hold a list object"
    version = data.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > VERSION:
        return NEWER, empty(), None
    # `True == 1`: a boolean is not a version.
    if isinstance(version, bool) or version != VERSION \
            or set(data) != {"version", "servers"} \
            or not isinstance(data["servers"], dict):
        return DAMAGED, empty(), "it does not have the expected shape"
    for name, entry in data["servers"].items():
        if entry_problem(name, entry):
            return DAMAGED, empty(), "it holds a server that is not valid"
    return OK, data, None


def read(path: Path | None = None) -> tuple[str, dict[str, Any], str | None]:
    """The ONE reader: (state, data, why). An absent file is an empty list."""
    path = LIST_PATH if path is None else path
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return OK, empty(), None
    except OSError as e:
        return DAMAGED, empty(), f"it cannot be read: {e.strerror or 'read failed'}"
    return judge(raw)


def load() -> dict[str, Any]:
    """The list, or a refusal naming what the operator can do."""
    state, data, why = read()
    if state == NEWER:
        raise ServerListError(NEWER_TEXT)
    if state == DAMAGED:
        raise ServerListError(DAMAGED_TEXT.format(why=why))
    return data


def _write_private(target: Path, text: str) -> None:
    """Write through a fresh 0600 temp + rename (never opens an existing name)."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def save(data: dict[str, Any]) -> None:
    try:
        _write_private(LIST_PATH, _dump(data))
    except OSError as e:
        raise ServerListError("cannot save the MCP server list on the host "
                              f"({e.strerror or 'write failed'})") from e


def render(data: dict[str, Any]) -> None:
    """Write the list where the gateway container reads it."""
    try:
        GATEWAY_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(GATEWAY_DIR, 0o700)
        _write_private(GATEWAY_DIR / RENDERED_NAME, _dump(data))
    except OSError as e:
        raise ServerListError("cannot write the MCP gateway's server list on "
                              f"the host ({e.strerror or 'write failed'})") from e


@contextlib.contextmanager
def lock(blocking: bool = True) -> Iterator[None]:
    """Serialize list edits across processes. Holders do FILE work only (never
    a docker call, never a read from a terminal). `blocking=False` is the
    broker's form: one attempt, then Busy — its accept thread must not wait."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        raise ServerListError("cannot lock the MCP server list on the host "
                              f"({e.strerror or 'open failed'})") from e
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Busy(BUSY_TEXT) from None
        except OSError as e:
            raise ServerListError("cannot lock the MCP server list on the host "
                                  f"({e.strerror or 'lock failed'})") from None
        yield
    finally:
        os.close(fd)


def public_view(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows a page or a listing may show: header NAMES only."""
    return [{"name": name,
             "url": e["url"],
             "description": e.get("description", ""),
             "headers": sorted(e.get("headers", {}))}
            for name, e in sorted(data["servers"].items())]
