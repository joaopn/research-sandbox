#!/usr/bin/env python3
"""rs-publish — publish a dev repo's built image to a public registry.

Staged into rs-fetch surfaces beside rs-fetch and rs-sync. Run by hand:

    rs-publish <repo>                          # dry run: what would happen
    rs-publish <repo> --apply                  # upload, tag, move :latest
    rs-publish <repo> --no-latest --apply      # upload and tag, leave :latest
    rs-publish <repo> --latest <tag> --apply   # move :latest to a published tag

RS builds images from the Services page's Images tab and keeps one per repo
on its local shelf (the shared Gitea's registry). This tool reads that image
with the read-only image token staged at ~/.dev-tokens/image-reader.token and
uploads it, byte for byte, under every git tag recorded on it (or, when its
commit had no tag, under the full commit id), then moves `latest`. A version
or commit tag that already exists on the target with other content is never
moved: the run stops before any upload.

The registry account is read from /workspace/.rs-publish.env (or --env):
    RS_PUBLISH_REGISTRY=ghcr.io/<owner>     where images go: host[:port]/path
    RS_PUBLISH_USER=<owner>                 the account to sign in as
    RS_PUBLISH_TOKEN=ghp_...                for GitHub: a classic token with
                                            write:packages
The file must belong to you and be readable by you only (chmod 600).

Stdlib only: this file is staged into containers and imports no host module.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO, Callable

# --- wiring (lockstep with cli/gitea.py GITEA_CONTAINER / GITEA_INNER_PORT /
# OPERATOR_USER / IMAGES_ORG and rscore's staging; pinned by a test) ----------
GITEA_HOST = "rs-gitea"
GITEA_PORT = "3000"
SHELF_BASE = f"http://{GITEA_HOST}:{GITEA_PORT}"
SHELF_USER = "operator"
IMAGES_ORG = "rs-images"
# The read-only image token the host stages beside the operator token.
SHELF_TOKEN_PATH = Path.home() / ".dev-tokens" / "image-reader.token"
# /workspace: the only folder that survives a container recreate.
DEFAULT_ENV = Path("/workspace/.rs-publish.env")

# Test-only overrides (the harness points rs-publish at a throwaway shelf and
# a plain-http target). Unset in every real container.
SHELF_BASE = os.environ.get("RS_PUBLISH_TEST_SHELF_URL") or SHELF_BASE
if os.environ.get("RS_PUBLISH_TEST_SHELF_TOKEN_FILE"):
    SHELF_TOKEN_PATH = Path(os.environ["RS_PUBLISH_TEST_SHELF_TOKEN_FILE"])
_TEST_PLAIN_HTTP = os.environ.get("RS_PUBLISH_TEST_PLAIN_HTTP") == "1"

# --- the image rules (MIRRORS of cli/image_registry.py; pinned by a test) ---
IMAGE_NAME_RE = re.compile(r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*\Z")
REGISTRY_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHELF_TAG = "built"
JSON_MAX_BYTES = 4 * 1024 * 1024
CALL_TIMEOUT_S = 15
MANIFEST_DEPTH_MAX = 4
MEDIA_INDEX = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")
MEDIA_MANIFEST = ("application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json")
ACCEPT = ", ".join(MEDIA_INDEX + MEDIA_MANIFEST)
LABEL_COMMIT = "org.opencontainers.image.revision"
LABEL_SOURCE = "org.opencontainers.image.source"
LABEL_GIT_TAGS = "io.research-sandbox.git-tags"

# Bound for each socket operation of a call that crosses the internet (every
# call to the target registry, and the shelf's blob streams that feed it). It
# bounds a stalled peer, not a long upload: each send and receive completes as
# data moves. The slowest legitimate wait is the registry verifying a large
# layer after its last byte, seconds per gigabyte. At half this a
# multi-gigabyte layer on a slow registry could be cut off; at ten times a
# stalled connection would hold the terminal for most of an hour.
TARGET_IO_TIMEOUT_S = 300

# The shortest gap between two redraws of the live progress line on a
# terminal. Without a limit the line would be redrawn after every 8 KB block
# http.client sends (about 250,000 times for 2 GB), costing terminal writes
# for no information. At 0.5 s a person sees steady movement; ten times longer
# reads as frozen (the complaint this line answers), ten times shorter only
# flickers. Off a terminal no line is redrawn: one line per finished layer.
PROGRESS_REDRAW_S = 0.5

LATEST = "latest"
ENV_KEYS = ("RS_PUBLISH_REGISTRY", "RS_PUBLISH_USER", "RS_PUBLISH_TOKEN")
# host[:port]: a DNS name or an IPv4 address, then an optional port.
HOST_RE = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
                     r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?::[0-9]{1,5})?\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


class PublishError(Exception):
    pass


class Replaced(PublishError):
    pass


def die(msg: str) -> None:
    raise PublishError(msg)


# ============================================================================
# Settings
# ============================================================================

def _close(fd: int, what: str) -> None:
    """Close a file this tool read; a failed close names the file (a raw
    OSError would read as a failed write to the output)."""
    try:
        os.close(fd)
    except OSError as e:
        die(f"cannot read {what}: {e.strerror}")


def read_env(path: Path) -> dict:
    """Parse the settings file, refusing an unsafe or malformed one. Opened
    without following links and without blocking (a planted FIFO cannot hang
    it); must be a regular file owned by this user with no group or other
    bits."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        die(f"the settings file {path} does not exist. Create it with these "
            f"lines (and chmod 600 it):\n  RS_PUBLISH_REGISTRY=ghcr.io/<owner>\n"
            f"  RS_PUBLISH_USER=<owner>\n  RS_PUBLISH_TOKEN=<for GitHub: a "
            f"classic token with write:packages>")
    except OSError as e:
        die(f"cannot open the settings file {path}: {e.strerror} (a link is "
            f"refused)")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            die(f"the settings file {path} is not a regular file")
        if st.st_uid != os.getuid():
            die(f"the settings file {path} is not owned by you")
        if st.st_mode & 0o077:
            die(f"the settings file {path} is readable by others; run "
                f"chmod 600 {path}")
        raw = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw += chunk
    except OSError as e:
        die(f"cannot read the settings file {path}: {e.strerror}")
    finally:
        _close(fd, f"the settings file {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        die(f"the settings file {path} is not UTF-8 text")
    out: dict = {}
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            die(f"{path} line {n}: expected KEY=value")
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip()
        if k not in ENV_KEYS:
            die(f"{path} line {n}: unknown key {k!r} (known: "
                f"{', '.join(ENV_KEYS)})")
        if k in out:
            die(f"{path} line {n}: {k} is set twice")
        out[k] = v
    for k in ENV_KEYS:
        if not out.get(k):
            die(f"{path}: {k} is missing")
    host, ns = parse_registry(out["RS_PUBLISH_REGISTRY"], path)
    return {"host": host, "namespace": ns, "user": out["RS_PUBLISH_USER"],
            "token": out["RS_PUBLISH_TOKEN"]}


def parse_registry(value: str, path: "Path | str" = "RS_PUBLISH_REGISTRY") -> tuple:
    """(host[:port], namespace) of `host[:port]/part[/part…]`; every path
    part a valid lower-case image-name part."""
    head, sep, rest = value.partition("/")
    if not sep or not rest:
        die(f"{path}: RS_PUBLISH_REGISTRY must be host/namespace, e.g. "
            f"ghcr.io/<owner>, not {value!r}")
    if not HOST_RE.match(head):
        die(f"{path}: RS_PUBLISH_REGISTRY names no valid registry host: {head!r}")
    parts = rest.split("/")
    for p in parts:
        if not IMAGE_NAME_RE.match(p):
            die(f"{path}: RS_PUBLISH_REGISTRY part {p!r} is not a valid image "
                f"name part (lower-case letters, digits, '.', '_', '-')")
    return head, "/".join(parts)


def image_name(repo: str) -> str:
    name = repo.lower() if isinstance(repo, str) else ""
    if not IMAGE_NAME_RE.match(name):
        die(f"repo name {repo!r} cannot be an image name")
    return name


def read_shelf_token() -> str:
    try:
        fd = os.open(SHELF_TOKEN_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        die(f"the image-reading token {SHELF_TOKEN_PATH} is missing: RS stages "
            f"it into rs-fetch projects and boxes. Stop and start the project "
            f"(in a box: then restart the box)")
    except OSError as e:
        die(f"cannot open {SHELF_TOKEN_PATH}: {e.strerror}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            die(f"{SHELF_TOKEN_PATH} is not a regular file")
        raw = os.read(fd, 4096).strip()
    except OSError as e:
        die(f"cannot read {SHELF_TOKEN_PATH}: {e.strerror}")
    finally:
        _close(fd, str(SHELF_TOKEN_PATH))
    if not re.fullmatch(rb"[0-9a-f]{40}", raw):
        die(f"{SHELF_TOKEN_PATH} does not hold a token; stop and start the "
            f"project (in a box: then restart the box)")
    return raw.decode("ascii")


# ============================================================================
# The registry client
# ============================================================================

class _Http(Exception):
    """An HTTP answer outside the expected statuses (internal)."""

    def __init__(self, status: int, headers) -> None:
        super().__init__(status)
        self.status = status
        self.headers = headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                 # the 3xx then surfaces as an HTTPError


class _Hashing:
    """A read-only stream that hashes and counts what passes through it, and
    never yields more than `size` bytes. `progress(count, sent)`, when given,
    is called with the running count after each read, and once with
    sent=True on the first empty read after the last byte: http.client reads
    the body block by block until an empty read, so by then every block has
    been handed to the socket."""

    def __init__(self, fh: BinaryIO, size: int,
                 progress: "Callable[[int, bool], None] | None" = None) -> None:
        self._fh = fh
        self._left = size
        self.sha = hashlib.sha256()
        self.count = 0
        self._progress = progress
        self._sent = False

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0 or n == 0:
            if self._left <= 0 and self._progress and not self._sent:
                self._sent = True
                self._progress(self.count, True)
            return b""
        want = self._left if n is None or n < 0 else min(n, self._left)
        data = self._fh.read(want)
        if not data:
            raise PublishError("a blob from the shelf ended before its size")
        self._left -= len(data)
        self.count += len(data)
        self.sha.update(data)
        if self._progress:
            self._progress(self.count, False)
        return data


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


AUTH_PARAM_RE = re.compile(r'([A-Za-z_]+)\s*=\s*(?:"([^"]*)"|([^,\s]*))')


def parse_challenge(values) -> "tuple[str, dict]":
    """The scheme ("bearer" or "basic") and parameters of a registry's
    WWW-Authenticate answer; Bearer wins when both are offered."""
    found: dict = {}
    for v in values or ():
        scheme, _, rest = (v or "").strip().partition(" ")
        scheme = scheme.lower()
        if scheme in ("bearer", "basic") and scheme not in found:
            found[scheme] = {m.group(1).lower(): (m.group(2) if m.group(2) is not None
                                                  else m.group(3))
                             for m in AUTH_PARAM_RE.finditer(rest)}
    if "bearer" in found:
        return "bearer", found["bearer"]
    if "basic" in found:
        return "basic", found["basic"]
    die("the registry asked for a sign-in method rs-publish does not know")
    raise AssertionError


class Registry:
    """Calls against one registry as one account. `pin` (the shelf): every
    address the registry names (sign-in, upload) is taken as a path on this
    client's own base, and proxy settings are ignored. Otherwise (the target):
    the sign-in address must be https (or, under the test override, on the
    target's own host), and an upload address must be on the target's own
    scheme, host and port. Errors never carry a credential or a body."""

    def __init__(self, base: str, label: str, user: "str | None", secret: "str | None",
                 *, pin: bool, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.label = label
        parts = urllib.parse.urlsplit(self.base)
        self._origin = (parts.scheme, parts.netloc.lower())
        self._basic = (None if user is None
                       else "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode())
        self._pin = pin
        self._timeout = timeout
        handlers: list = [_NoRedirect()]
        if pin:
            handlers.insert(0, urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)
        self._scheme: "str | None" = None
        self._params: dict = {}
        self._bearers: dict = {}

    # -- transport ---------------------------------------------------------

    def _raw(self, method: str, url: str, label: str, auth: "str | None", *,
             headers: "dict | None" = None, data: Any = None, ok=(200,),
             timeout: "float | None" = None):
        hdrs = dict(headers or {})
        if auth:
            hdrs["Authorization"] = auth
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            resp = self._opener.open(req, timeout=timeout or self._timeout)
        except urllib.error.HTTPError as e:
            hdrs_in = e.headers
            e.close()
            raise _Http(e.code, hdrs_in) from None
        except urllib.error.URLError as e:
            die(f"{self.label}: {method} {label} unreachable: {e.reason}")
        except (OSError, http.client.HTTPException) as e:
            die(f"{self.label}: {method} {label} failed: {type(e).__name__}")
        except ValueError:
            # An address the registry named with a non-ASCII character in it
            # (UnicodeEncodeError when the request line is encoded), or —
            # defensively, none reaches here today — a header value with a
            # line break. A fixed text, never the value. An address with a
            # space is an HTTPException (InvalidURL), handled above.
            die(f"{self.label}: {method} {label} could not be sent")
        if resp.status not in ok:
            status, hdrs_in = resp.status, resp.headers
            resp.close()
            raise _Http(status, hdrs_in)
        return resp

    def _read(self, resp, limit: int, label: str) -> bytes:
        declared = resp.length
        try:
            with resp:
                body = resp.read(limit)
        except (OSError, http.client.HTTPException) as e:
            die(f"{self.label}: reading {label} failed: {type(e).__name__}")
        if isinstance(declared, int) and len(body) < min(declared, limit):
            die(f"{self.label}: reading {label} was cut off")
        return body

    # -- sign-in -----------------------------------------------------------

    def ping(self) -> None:
        """Learn how the registry wants to be signed in to."""
        try:
            self._raw("GET", self.base + "/v2/", "/v2/", None).close()
        except _Http as e:
            if e.status != 401:
                die(f"{self.label} answered HTTP {e.status} at /v2/; is it a "
                    f"container registry?")
            self._scheme, self._params = parse_challenge(
                e.headers.get_all("WWW-Authenticate") if e.headers else None)
            return
        self._scheme = "none"

    def _realm(self) -> str:
        realm = self._params.get("realm") or ""
        p = urllib.parse.urlsplit(realm)
        if self._pin:
            if not p.path:
                die(f"{self.label} named no sign-in address")
            return self.base + p.path + (f"?{p.query}" if p.query else "")
        if p.scheme == "https" and p.netloc:
            return realm
        if (_TEST_PLAIN_HTTP and p.scheme == "http"
                and (p.scheme, p.netloc.lower()) == self._origin):
            return realm
        die(f"{self.label} named a sign-in address that is not https; refused")
        raise AssertionError

    def _bearer(self, name: str, actions: str) -> str:
        scope = f"repository:{name}:{actions}"
        tok = self._bearers.get(scope)
        if tok:
            return tok
        realm = self._realm()
        q = {"scope": scope}
        if self._params.get("service"):
            q["service"] = self._params["service"]
        url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(q, safe=":/,")
        try:
            resp = self._raw("GET", url, "sign-in", self._basic)
        except _Http as e:
            if e.status in (401, 403):
                die(self.refused_text(e.status))
            die(f"{self.label}: sign-in answered HTTP {e.status}")
        try:
            doc = json.loads(self._read(resp, JSON_MAX_BYTES, "sign-in"))
        except (ValueError, RecursionError):
            die(f"{self.label}: the sign-in answer is not JSON")
        tok = doc.get("token") or doc.get("access_token") if isinstance(doc, dict) else None
        if not isinstance(tok, str) or not tok:
            die(f"{self.label}: the sign-in answer carries no token")
        if not re.fullmatch(r"[\x21-\x7e]+", tok):
            die(f"{self.label}: the sign-in answer carries a token that cannot be sent")
        self._bearers[scope] = "Bearer " + tok
        return self._bearers[scope]

    def refused_text(self, status: int) -> str:
        if self._basic is None:
            return f"{self.label} refused an anonymous read (HTTP {status})"
        if self._pin:
            return (f"{self.label} refused the image-reading token (HTTP "
                    f"{status}); stop and start the project (in a box: then "
                    f"restart the box) to stage a fresh one")
        return f"{self.label} refused the account's credential (HTTP {status})"

    def auth(self, name: str, actions: str) -> "str | None":
        if self._scheme is None:
            self.ping()
        if self._scheme == "bearer":
            return self._bearer(name, actions)
        if self._scheme == "basic":
            return self._basic
        return None

    def forget(self, name: str, actions: str) -> bool:
        """Drop a cached bearer so the next call signs in again; False when
        there is nothing to renew (not a bearer registry)."""
        if self._scheme != "bearer":
            return False
        self._bearers.pop(f"repository:{name}:{actions}", None)
        return True

    def call(self, method: str, path: str, name: str, actions: str, *,
             headers: "dict | None" = None, data: "bytes | None" = None,
             ok=(200,), timeout: "float | None" = None):
        """One call, signed in; a 401 after a successful sign-in signs in
        again once (registry tokens expire in minutes) and retries."""
        for attempt in (1, 2):
            auth = self.auth(name, actions)
            try:
                return self._raw(method, self.base + path, path, auth,
                                 headers=headers, data=data, ok=ok, timeout=timeout)
            except _Http as e:
                if e.status == 401 and attempt == 1 and self.forget(name, actions):
                    continue
                raise
        raise AssertionError

    # -- reads -------------------------------------------------------------

    def get_manifest(self, name: str, ref: str) -> "tuple[bytes, str, str] | None":
        path = f"/v2/{name}/manifests/{urllib.parse.quote(ref, safe=':')}"
        try:
            resp = self.call("GET", path, name, "pull", headers={"Accept": ACCEPT})
        except _Http as e:
            if e.status == 404:
                return None
            if e.status in (401, 403):
                die(f"{self.label}: reading {name}:{ref} was refused (HTTP "
                    f"{e.status})")
            die(f"{self.label}: reading {name}:{ref} answered HTTP {e.status}")
        media = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        body = self._read(resp, JSON_MAX_BYTES + 1, path)
        if len(body) > JSON_MAX_BYTES:
            die(f"{self.label}: the manifest {name}:{ref} is too large")
        return body, media, _sha256(body)

    def blob_exists(self, name: str, digest: str) -> bool:
        path = f"/v2/{name}/blobs/{digest}"
        try:
            self.call("HEAD", path, name, "pull").close()
        except _Http as e:
            if e.status == 404:
                return False
            # A registry redirects only to content it holds.
            if e.status in (301, 302, 303, 307, 308):
                return True
            die(f"{self.label}: checking a layer of {name} answered HTTP {e.status}")
        return True

    def open_blob(self, name: str, digest: str):
        path = f"/v2/{name}/blobs/{digest}"
        try:
            return self.call("GET", path, name, "pull", timeout=TARGET_IO_TIMEOUT_S)
        except _Http as e:
            if e.status == 404:
                raise Replaced(REPLACED) from None
            die(f"{self.label}: reading a layer of {name} answered HTTP {e.status}")

    # -- writes ------------------------------------------------------------

    def _location(self, location: str) -> str:
        if not location:
            die(f"{self.label} gave no upload address")
        if self._pin:
            p = urllib.parse.urlsplit(location)
            if not p.path.startswith("/v2/"):
                die(f"{self.label} gave an upload address outside /v2/")
            return self.base + p.path + (f"?{p.query}" if p.query else "")
        full = urllib.parse.urljoin(self.base + "/", location)
        p = urllib.parse.urlsplit(full)
        if (p.scheme, p.netloc.lower()) != self._origin:
            die(f"{self.label} gave an upload address on another host; refused")
        if not p.path.startswith("/v2/"):
            die(f"{self.label} gave an upload address outside /v2/")
        return full

    def write_refused(self, e: _Http, name: str) -> None:
        if e.status in (401, 403):
            die(f"{self.label} refused the account permission to upload to "
                f"{name} (HTTP {e.status}): check the token's write access to "
                f"packages")
        die(f"{self.label}: uploading to {name} answered HTTP {e.status}")

    def upload_blob(self, name: str, digest: str, size: int,
                    open_src: "Callable[[], Any]",
                    progress: "Callable[[int, bool], None] | None" = None) -> None:
        """Upload blob `digest` of `size` bytes read from `open_src()`; the
        stream is hashed as it goes and `progress` (see _Hashing) is told of
        each read. A 401 on the upload itself signs in again and re-reads the
        source once, its count starting again from 0."""
        for attempt in (1, 2):
            start_path = f"/v2/{name}/blobs/uploads/"
            try:
                resp = self.call("POST", start_path, name, "pull,push",
                                 headers={"Content-Length": "0"}, ok=(202,))
            except _Http as e:
                self.write_refused(e, name)
            with resp:
                location = resp.headers.get("Location") or ""
            url = self._location(location)
            url += ("&" if "?" in url else "?") + "digest=" + urllib.parse.quote(digest)
            src = open_src()
            body = _Hashing(src, size, progress)
            try:
                self._raw("PUT", url, start_path + "<upload>",
                          self.auth(name, "pull,push"), data=body,
                          headers={"Content-Length": str(size),
                                   "Content-Type": "application/octet-stream"},
                          ok=(201,), timeout=TARGET_IO_TIMEOUT_S).close()
            except _Http as e:
                if body.count == size and "sha256:" + body.sha.hexdigest() != digest:
                    # The registry checks the digest too and refuses (400):
                    # the cause is the shelf's bytes, say so.
                    die(f"a layer of {name} read from the shelf does not match "
                        f"its checksum; nothing was tagged")
                if e.status == 401 and attempt == 1 and self.forget(name, "pull,push"):
                    continue
                self.write_refused(e, name)
            finally:
                src.close()
            if body.count != size or "sha256:" + body.sha.hexdigest() != digest:
                die(f"a layer of {name} read from the shelf does not match its "
                    f"checksum; nothing was tagged")
            return

    def put_manifest(self, name: str, ref: str, body: bytes, media: str) -> None:
        path = f"/v2/{name}/manifests/{urllib.parse.quote(ref, safe=':')}"
        try:
            self.call("PUT", path, name, "pull,push", data=body,
                      headers={"Content-Type": media}, ok=(201,)).close()
        except _Http as e:
            self.write_refused(e, name)


REPLACED = ("the image was replaced on the shelf while publishing (a new build "
            "finished); run again")


# ============================================================================
# The shelf image
# ============================================================================

def _descriptor(d: Any, what: str) -> "tuple[str, int]":
    if not isinstance(d, dict):
        die(f"{what} is not an object")
    digest, size = d.get("digest"), d.get("size")
    if not (isinstance(digest, str) and DIGEST_RE.match(digest)):
        die(f"{what} names no sha256 checksum")
    if not (isinstance(size, int) and not isinstance(size, bool) and size >= 0):
        die(f"{what} names no size")
    return digest, size


def _json(data: bytes, what: str) -> dict:
    try:
        doc = json.loads(data)
    except (ValueError, RecursionError):
        die(f"{what} is not valid JSON")
    if not isinstance(doc, dict):
        die(f"{what} is not a JSON object")
    return doc


def _parts(doc: dict, what: str) -> list:
    layers = doc.get("layers")
    if layers is None:
        layers = []
    if not isinstance(layers, list):
        die(f"{what} lists its layers in an unknown form")
    return [doc.get("config")] + layers


class ShelfImage:
    """The built image of one repo, read by checksum: its top manifest, every
    child manifest (in push order: children before parents), every blob, and
    its labels."""

    def __init__(self, shelf: Registry, name: str, top: "tuple[bytes, str, str]") -> None:
        self.name = name
        self.digest = top[2]
        self.manifests: list = []          # (digest, body, media), push order
        self.blobs: dict = {}              # digest -> size
        self._shelf = shelf
        self._seen: set = set()
        self._walk(top[0], top[2], 1, top[1])
        self.labels = self._labels(top[0])

    def _media(self, doc: dict, header: str, digest: str) -> str:
        media = doc.get("mediaType") or header or ""
        if isinstance(doc.get("manifests"), list):
            media = media or MEDIA_INDEX[0]
            ok = MEDIA_INDEX
        else:
            media = media or MEDIA_MANIFEST[0]
            ok = MEDIA_MANIFEST
        if media not in ok:
            die(f"manifest {digest} has an unknown media type")
        return media

    def _get(self, digest: str) -> "tuple[bytes, str]":
        got = self._shelf.get_manifest(self.name, digest)
        if got is None:
            raise Replaced(REPLACED)
        if got[2] != digest:
            die(f"manifest {digest} read from the shelf does not match its checksum")
        return got[0], got[1]

    def _walk(self, body: bytes, digest: str, depth: int, header: str = "") -> None:
        if digest in self._seen:
            return
        if depth > MANIFEST_DEPTH_MAX:
            die("the image nests its manifests too deeply")
        doc = _json(body, f"manifest {digest}")
        media = self._media(doc, header, digest)
        children = doc.get("manifests")
        if isinstance(children, list):
            for i, child in enumerate(children):
                c_digest, c_size = _descriptor(child, f"{digest} entry {i}")
                if c_size > JSON_MAX_BYTES:
                    die(f"manifest {c_digest} is too large")
                c_body, c_media = self._get(c_digest)
                if len(c_body) != c_size:
                    die(f"manifest {c_digest} does not match its size")
                self._walk(c_body, c_digest, depth + 1, c_media)
        else:
            for i, part in enumerate(_parts(doc, f"manifest {digest}")):
                b_digest, b_size = _descriptor(part, f"{digest} part {i}")
                self.blobs[b_digest] = b_size
        self._seen.add(digest)
        self.manifests.append((digest, body, media))

    def _labels(self, top_body: bytes) -> dict:
        doc = _json(top_body, "the image's manifest")
        children = doc.get("manifests")
        if isinstance(children, list):
            pick = None
            for child in children:
                plat = child.get("platform") if isinstance(child, dict) else None
                if isinstance(plat, dict) and plat.get("os") not in (None, "unknown"):
                    pick = child
                    break
            if pick is None:
                die("the image's index lists no platform image")
            body = next(b for d, b, _ in self.manifests
                        if d == _descriptor(pick, "the platform entry")[0])
            doc = _json(body, "the image's platform manifest")
        cfg_digest, cfg_size = _descriptor(_parts(doc, "the image's manifest")[0],
                                           "the image's config")
        if cfg_size > JSON_MAX_BYTES:
            die("the image's config is too large")
        resp = self._shelf.open_blob(self.name, cfg_digest)
        raw = self._shelf._read(resp, JSON_MAX_BYTES + 1, "the image's config")
        if _sha256(raw) != cfg_digest:
            die("the image's config does not match its checksum")
        cfg = _json(raw, "the image's config")
        inner = cfg.get("config")
        labels = inner.get("Labels") if isinstance(inner, dict) else None
        return labels if isinstance(labels, dict) else {}

    def commit(self) -> str:
        c = self.labels.get(LABEL_COMMIT)
        return c if isinstance(c, str) else ""

    def git_tags(self) -> list:
        raw = self.labels.get(LABEL_GIT_TAGS)
        if raw is None:
            return []
        try:
            tags = json.loads(raw) if isinstance(raw, str) else None
        except (ValueError, RecursionError):
            tags = None
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            die("the image's list of git tags is not readable; build it again")
        return tags

    def size(self) -> int:
        return sum(self.blobs.values())


def version_tags(img: ShelfImage) -> list:
    """The tags the image is published under: its git tags, or its full
    commit id when its commit had none. Refuses a tag that is not a registry
    tag, or is literally `latest` (the build refuses those too; checked again
    because the label is data)."""
    tags = img.git_tags()
    if not tags:
        commit = img.commit()
        if not COMMIT_RE.match(commit):
            die("the image has no git tag and no commit id to publish it under; "
                "build it again")
        return [commit]
    out = []
    for t in tags:
        if t == LATEST:
            die("the image carries the git tag 'latest', which rs-publish never "
                "uses as a version; retag the commit and build again")
        if not REGISTRY_TAG_RE.match(t):
            die(f"the git tag {t!r} cannot be a registry tag; retag the commit "
                f"and build again")
        if t not in out:
            out.append(t)
    return out


def _bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError


def _duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s} s"
    h, m = divmod(m, 60)
    return f"{h} h {m} min"


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


class _Progress:
    """What the upload loop shows. On a terminal: one status line, redrawn in
    place at most every PROGRESS_REDRAW_S (and at each layer's start and when
    its last byte is sent), cut to the terminal's width. Elsewhere: one line
    per finished layer. Every write is flushed. Nothing here raises but
    PublishError: it runs inside the upload, where any other exception would
    read as a network failure."""

    def __init__(self, layers: int, total: int, out=None,
                 clock: "Callable[[], float] | None" = None) -> None:
        self.out = out if out is not None else sys.stdout
        self.clock = clock or time.monotonic
        self.tty = _isatty(self.out)
        self.layers, self.total = layers, total
        self.started = self.clock()
        self.finished = 0           # bytes of the layers already uploaded
        self.index = self.size = self.count = 0
        self.last: "float | None" = None
        self.open = False           # a status line is on the terminal
        self.broken = False         # a write failed; write nothing more

    # -- what the upload reports ---------------------------------------------

    def layer(self, index: int, size: int) -> None:
        self.index, self.size, self.count = index, size, 0
        self._draw(force=True)

    def read(self, count: int, sent: bool) -> None:
        """The _Hashing callback: an absolute count, so a retried layer that
        starts again from 0 is never counted twice."""
        self.count = count
        self._draw(force=sent, sent=sent)

    def done_layer(self) -> None:
        self.finished += self.size
        self.count = 0
        if not self.tty:
            self._write(f"  layer {self.index} of {self.layers} uploaded: "
                        f"{_bytes(self.size)}, {self._total_text()}\n")

    def finish(self) -> None:
        """The total line, once every layer went up."""
        line = (f"  uploaded {self.layers} layer{'s' if self.layers != 1 else ''}, "
                f"{_bytes(self.total)} in {_duration(self.clock() - self.started)}")
        if self.tty:
            self._write("\r" + self._fit(line) + "\n")
            self.open = False
        else:
            self._write(line + "\n")

    def end(self) -> None:
        """End an open status line, so what follows starts on its own line."""
        if self.open:
            self.open = False
            self._write("\n")

    # -- drawing -------------------------------------------------------------

    def _total_text(self) -> str:
        moved = self.finished + self.count
        pct = moved * 100 // self.total if self.total > 0 else 100
        return f"total {_bytes(moved)} of {_bytes(self.total)} ({pct}%)"

    def _speed_text(self, now: float) -> str:
        elapsed = now - self.started
        moved = self.finished + self.count
        if elapsed <= 0 or moved <= 0:
            return ""
        rate = moved / elapsed
        left = max(0, self.total - moved) / rate
        return f", {_bytes(rate)}/s, about {_duration(left)} left"

    def _fit(self, line: str) -> str:
        width = max(1, shutil.get_terminal_size().columns - 1)
        return line[:width].ljust(width)

    def _draw(self, force: bool = False, sent: bool = False) -> None:
        if not self.tty or self.broken:
            return
        now = self.clock()
        if not force and self.last is not None and now - self.last < PROGRESS_REDRAW_S:
            return
        self.last = now
        head = f"  layer {self.index} of {self.layers}: "
        if sent:
            line = (head + f"all {_bytes(self.size)} sent, waiting for the "
                    f"registry to check it, {self._total_text()}")
        else:
            line = (head + f"{_bytes(self.count)} of {_bytes(self.size)}, "
                    f"{self._total_text()}{self._speed_text(now)}")
        self.open = True            # first: an interrupted write still gets its line end
        self._write("\r" + self._fit(line))

    def _write(self, text: str) -> None:
        if self.broken or self.out is None:     # no stdout: print() writes nothing too
            return
        try:
            self.out.write(text)
            self.out.flush()
        except (OSError, ValueError) as e:
            self.broken = True
            self.open = False
            where = "the terminal" if self.tty else "the output"
            die(f"cannot write the upload progress to {where} "
                f"({getattr(e, 'strerror', None) or type(e).__name__}); "
                f"nothing was tagged")


# ============================================================================
# Publishing
# ============================================================================

def target_registry(env: dict, *, anonymous: bool = False) -> Registry:
    scheme = "http" if _TEST_PLAIN_HTTP else "https"
    label = f"the registry {env['host']}"
    if anonymous:
        return Registry(f"{scheme}://{env['host']}", label, None, None,
                        pin=False, timeout=TARGET_IO_TIMEOUT_S)
    return Registry(f"{scheme}://{env['host']}", label, env["user"], env["token"],
                    pin=False, timeout=TARGET_IO_TIMEOUT_S)


def move_latest(args, env: dict, image: str) -> int:
    target = target_registry(env)
    name = f"{env['namespace']}/{image}"
    tag = args.latest
    if tag == LATEST or not REGISTRY_TAG_RE.match(tag):
        die(f"{tag!r} is not a version tag")
    got = target.get_manifest(name, tag)
    if got is None:
        die(f"{env['host']}/{name}:{tag} is not published; publish it first")
    cur = target.get_manifest(name, LATEST)
    where = f"{env['host']}/{name}"
    if cur is not None and cur[2] == got[2]:
        print(f"{where}:latest already is {tag}; nothing to do")
        return 0
    if not args.apply:
        print(f"would move {where}:latest to {tag} ({got[2][:19]}); nothing "
              f"uploaded. Run again with --apply to do it")
        return 0
    media = got[1] if got[1] in MEDIA_INDEX + MEDIA_MANIFEST else _media_of(got[0])
    target.put_manifest(name, LATEST, got[0], media)
    print(f"moved {where}:latest to {tag}")
    return 0


def _media_of(body: bytes) -> str:
    doc = _json(body, "the manifest")
    if doc.get("mediaType") in MEDIA_INDEX + MEDIA_MANIFEST:
        return doc["mediaType"]
    return MEDIA_INDEX[0] if isinstance(doc.get("manifests"), list) else MEDIA_MANIFEST[0]


def publish(args, env: dict, image: str) -> int:
    shelf = Registry(SHELF_BASE, "the shelf (RS's Gitea)", SHELF_USER,
                     read_shelf_token(), pin=True, timeout=CALL_TIMEOUT_S)
    shelf_name = f"{IMAGES_ORG}/{image}"
    top = shelf.get_manifest(shelf_name, SHELF_TAG)
    if top is None:
        die(f"{args.repo} has no built image; build one on the Services "
            f"page's Images tab")
    img = ShelfImage(shelf, shelf_name, top)
    tags = version_tags(img)
    commit = img.commit()
    print(f"built image of {args.repo}: commit {commit[:12] or 'unknown'}, "
          f"{_bytes(img.size())}, checksum {img.digest[:19]}")

    target = target_registry(env)
    name = f"{env['namespace']}/{image}"
    where = f"{env['host']}/{name}"
    # Every check against the target before the first write.
    todo, done = [], []
    for t in tags:
        got = target.get_manifest(name, t)
        if got is None:
            todo.append(t)
        elif got[2] == img.digest:
            done.append(t)
        else:
            die(f"{where}:{t} already exists with other content ({got[2][:19]}); "
                f"a published version never moves. Nothing was uploaded")
    move = False
    if not args.no_latest:
        cur = target.get_manifest(name, LATEST)
        move = cur is None or cur[2] != img.digest
    missing = [(d, s) for d, s in img.blobs.items() if not target.blob_exists(name, d)]
    for t in done:
        print(f"  {where}:{t} is already published")
    for t in todo:
        print(f"  {'will create' if not args.apply else 'creating'} {where}:{t}")
    if move:
        print(f"  {'will move' if not args.apply else 'moving'} {where}:latest")
    if not todo and not move:
        print("everything is already published; nothing to do")
        return 0
    print(f"  {len(missing)} of {len(img.blobs)} layers to upload, "
          f"{_bytes(sum(s for _, s in missing))}")
    if not args.apply:
        print("dry run: nothing was uploaded. Run again with --apply to publish")
        return 0

    if missing:
        progress = _Progress(len(missing), sum(s for _, s in missing))
        try:
            for i, (d, s) in enumerate(missing, 1):
                progress.layer(i, s)
                target.upload_blob(name, d, s,
                                   lambda d=d: shelf.open_blob(shelf_name, d),
                                   progress=progress.read)
                progress.done_layer()
            progress.finish()
        finally:
            progress.end()
    for d, body, media in img.manifests:
        if d != img.digest:
            target.put_manifest(name, d, body, media)
    top_media = img.manifests[-1][2]
    written = []
    try:
        for t in todo:
            target.put_manifest(name, t, img.manifests[-1][1], top_media)
            written.append(t)
        if move:
            target.put_manifest(name, LATEST, img.manifests[-1][1], top_media)
            written.append(LATEST)
    except PublishError as e:
        if written:
            raise PublishError(f"{e}. Published before the failure: "
                               f"{', '.join(written)}; run again to finish") from None
        raise
    print(f"published {where}: {', '.join(written)}")
    try:
        now = shelf.get_manifest(shelf_name, SHELF_TAG)
    except PublishError:
        now = None          # advisory only: the publish itself is done
    if now is not None and now[2] != img.digest:
        print("note: a newer build reached the shelf while publishing; this run "
              "published the one it started with")
    anonymous_note(env, name, (todo or tags)[0])
    return 0


def anonymous_note(env: dict, name: str, tag: str) -> None:
    """Say whether the image can be pulled without signing in: a registry may
    keep a newly published package private (GitHub's does)."""
    try:
        got = target_registry(env, anonymous=True).get_manifest(name, tag)
    except PublishError:
        got = None
    if got is not None:
        print(f"anyone can pull it: docker pull {env['host']}/{name}:{tag}")
        return
    print(f"note: {env['host']}/{name} cannot be pulled without signing in. "
          f"A registry may keep a new package private (GitHub does): to let "
          f"anyone pull it, make the package public in its settings on the "
          f"registry, once")


def _quiet(stream) -> None:
    """After a failed run, send what `stream` still holds to /dev/null when
    its output is gone (a reader that stopped, a hung-up terminal, a full
    disk): otherwise the flush at exit fails again and Python adds its own
    error and exit status to ours."""
    if stream is None:
        return
    try:
        stream.flush()
    except (OSError, ValueError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, stream.fileno())
        finally:
            os.close(devnull)


def _fail(msg: str, code: int) -> int:
    """End a failed run: quiet stdout, say `msg` on stderr (quieting it too
    when it is the same dead output, `2>&1 | head`), return `code`."""
    _quiet(sys.stdout)
    try:
        print(f"rs-publish: {msg}", file=sys.stderr)
    except (OSError, ValueError):
        _quiet(sys.stderr)
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="rs-publish",
        description="Publish a dev repo's built image (from the Services "
                    "page's Images tab) to a registry. Dry run unless --apply.")
    ap.add_argument("repo", help="the mirrored repo whose built image to publish")
    ap.add_argument("--apply", action="store_true", help="do it (default: show only)")
    ap.add_argument("--no-latest", action="store_true", help="leave :latest where it is")
    ap.add_argument("--latest", metavar="TAG",
                    help="only move :latest to an already published TAG")
    ap.add_argument("--env", type=Path, default=DEFAULT_ENV,
                    help=f"the settings file (default {DEFAULT_ENV})")
    args = ap.parse_args(argv)
    # Each line as it is printed, also into a pipe or a log being tailed (a
    # pipe is block-buffered by default: the whole run would show at the end).
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)
    try:
        if args.latest is not None and args.no_latest:
            die("--latest and --no-latest cannot be used together")
        image = image_name(args.repo)
        env = read_env(args.env)
        if args.latest is not None:
            return move_latest(args, env, image)
        return publish(args, env, image)
    except PublishError as e:
        return _fail(str(e), 1)
    except KeyboardInterrupt:
        return _fail("interrupted; tags already written stay; run again to finish", 130)
    except OSError as e:
        # Every network, settings-file and token error is a PublishError by
        # now: an OSError here is a write to the output (a print) that failed.
        what = ("the output was closed before rs-publish finished"
                if isinstance(e, BrokenPipeError)
                else f"cannot write the output ({e.strerror or type(e).__name__})")
        if args.apply:
            what += "; tags already written stay; run again to finish"
        return _fail(what, 1)


if __name__ == "__main__":
    sys.exit(main())
