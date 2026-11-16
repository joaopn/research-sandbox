"""image_registry — a small client for the shared Gitea's container registry.

Built images wait in that registry (the "shelf") until the maintainer publishes
them. This module is the host side: it pushes an image that is laid out in the
standard image layout (`index.json` + `blobs/sha256/<hex>`, what `docker save`
writes) and reads one back. Stdlib only.

Three rules every caller relies on:

  * The client talks to ONE address, the one it was given. The registry's token
    realm and its upload `Location` headers are built from Gitea's own root URL
    (`http://rs-gitea:3000/`), a name the host cannot resolve, so only the path
    and query of such a URL are used, joined onto the client's own base.
  * Proxy settings are ignored: the registry is on the loopback address.
  * No error carries a credential or a response body.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, BinaryIO, Callable, Protocol

# One image name component as registries define it (the distribution spec's
# name grammar): lower-case alphanumerics joined by `.`, `_`, `__` or dashes.
IMAGE_NAME_RE = re.compile(r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*\Z")
# A tag as registries define it: at most 128 characters, not led by `.` or `-`.
REGISTRY_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

# The one tag an image carries on the shelf: each repo has at most one image.
SHELF_TAG = "built"

# Bound for every JSON document read whole into memory (the layout's index, a
# manifest, an image config). 4 MiB is the size the distribution spec says a
# registry should accept for a manifest, so a document any registry would take
# fits, and a hostile archive cannot make the host buffer more than that per
# document. At half this a legal manifest with many layers could be refused;
# nothing legitimate is larger.
JSON_MAX_BYTES = 4 * 1024 * 1024

# Bound for each registry call that carries no blob. The same value as the
# Gitea API client's quick calls (gitea.API_TIMEOUT_S); kept as a literal so
# this module imports nothing from the host modules.
CALL_TIMEOUT_S = 15

# How deep manifests may nest in one image. A built image is an index over
# manifests (depth 2); an index of indexes is legal and rare (depth 3). One
# level beyond that is allowed for, and anything deeper is refused: the archive
# comes out of a builder that ran a repo's own build steps.
MANIFEST_DEPTH_MAX = 4

MEDIA_INDEX = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")
MEDIA_MANIFEST = ("application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json")
ACCEPT = ", ".join(MEDIA_INDEX + MEDIA_MANIFEST)

LABEL_COMMIT = "org.opencontainers.image.revision"
LABEL_SOURCE = "org.opencontainers.image.source"
# The git tags that pointed at the built commit, as a JSON list of strings.
LABEL_GIT_TAGS = "io.research-sandbox.git-tags"


class RegistryError(Exception):
    """A registry call failed. `status` is the HTTP status when the registry
    answered, None when it did not."""

    def __init__(self, msg: str, status: "int | None" = None) -> None:
        super().__init__(msg)
        self.status = status


def image_name(repo: str) -> str:
    """The image name a repo's built image is stored under: the repo name
    lower-cased. Raises ValueError when that is not a legal image name."""
    name = repo.lower() if isinstance(repo, str) else ""
    if not IMAGE_NAME_RE.match(name):
        raise ValueError(f"repo name {repo!r} cannot be used as an image name")
    return name


def valid_registry_tag(tag: str) -> bool:
    return isinstance(tag, str) and bool(REGISTRY_TAG_RE.match(tag))


class LayoutSource(Protocol):
    """Where an image layout is read from. `read_small` returns a member whole
    (refusing one larger than `limit`); `open_blob` returns a binary stream of
    exactly `size` bytes and a `finish` callable that raises RegistryError when
    the producer failed. `finish` runs after the stream is consumed, and also
    when the upload failed with the stream only partly read and still open:
    there it must not block, and must not report an abort the consumer caused
    as the producer's failure."""

    def read_small(self, path: str, limit: int) -> bytes: ...

    def open_blob(self, path: str, size: int) -> "tuple[BinaryIO, Callable[[], None]]": ...


class _Hashing:
    """A read-only stream that hashes and counts what passes through it, and
    never yields more than `size` bytes."""

    def __init__(self, fh: BinaryIO, size: int) -> None:
        self._fh = fh
        self._left = size
        self.sha = hashlib.sha256()
        self.count = 0

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0 or n == 0:
            return b""
        want = self._left if n is None or n < 0 else min(n, self._left)
        # The caller (http.client) asks for a block at a time, so memory per
        # blob stays flat whatever its size.
        data = self._fh.read(want)
        if not data:
            # The producer ended early. Returning b"" here would leave the
            # request short of its declared length and the registry waiting
            # for the rest, with nothing to end the wait.
            raise RegistryError("a blob ended before its declared size")
        self._left -= len(data)
        self.count += len(data)
        self.sha.update(data)
        return data


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _blob_path(digest: str) -> str:
    if not isinstance(digest, str) or not DIGEST_RE.match(digest):
        raise RegistryError("the image names a checksum that is not sha256")
    return "blobs/sha256/" + digest.split(":", 1)[1]


def _json(data: bytes, what: str) -> dict:
    try:
        doc = json.loads(data)
    except (ValueError, RecursionError):        # UnicodeDecodeError is a ValueError
        raise RegistryError(f"{what} is not valid JSON") from None
    if not isinstance(doc, dict):
        raise RegistryError(f"{what} is not a JSON object")
    return doc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                 # the 3xx then surfaces as an HTTPError


def _parts(doc: dict, what: str) -> list:
    """The config and layer descriptors of an image manifest, as a list."""
    layers = doc.get("layers")
    if layers is None:
        layers = []
    if not isinstance(layers, list):
        raise RegistryError(f"{what} lists its layers in an unknown form")
    return [doc.get("config")] + layers


class RegistryClient:
    """Calls against one registry address as one user. Every failure is a
    RegistryError naming the method, the path and the status only."""

    def __init__(self, base: str, user: str, token: str) -> None:
        self._base = base.rstrip("/")
        self._basic = "Basic " + base64.b64encode(
            f"{user}:{token}".encode()).decode()
        # No proxy handler: the registry is on the loopback address. No
        # redirects either: urllib would carry the Authorization header to
        # whatever address a redirect names.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())
        self._bearers: dict[str, str] = {}

    # -- transport ---------------------------------------------------------

    def _pin(self, location: str) -> str:
        """An upload location as an address on THIS client's base: only its
        path and query are taken."""
        parts = urllib.parse.urlsplit(location)
        if not parts.path.startswith("/v2/"):
            raise RegistryError("the registry gave an upload location outside /v2/")
        return self._base + parts.path + (f"?{parts.query}" if parts.query else "")

    def _open(self, method: str, url: str, label: str, *, auth: str,
              headers: "dict | None" = None, data: Any = None,
              ok: tuple = (200,), timeout: "float | None" = CALL_TIMEOUT_S):
        hdrs = {"Authorization": auth}
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            resp = self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            e.close()
            raise RegistryError(f"registry {method} {label} -> HTTP {e.code}",
                                status=e.code) from None
        except urllib.error.URLError as e:
            raise RegistryError(
                f"registry {method} {label} unreachable: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise RegistryError(
                f"registry {method} {label} failed: {type(e).__name__}") from None
        if resp.status not in ok:
            status = resp.status
            resp.close()
            raise RegistryError(f"registry {method} {label} -> HTTP {status}",
                                status=status)
        return resp

    @staticmethod
    def _read(resp, limit: int, label: str) -> bytes:
        """Up to `limit` bytes of a response body; a connection that fails
        part way is a RegistryError like any other failed call."""
        # The length http.client parsed from the reply: None for a chunked
        # reply or a length it could not parse.
        declared = resp.length
        try:
            with resp:
                body = resp.read(limit)
        except (OSError, http.client.HTTPException) as e:
            raise RegistryError(
                f"registry read of {label} failed: {type(e).__name__}") from None
        # A bounded read returns what arrived, without complaint, when the
        # connection ends early: compare with the length the reply announced.
        if isinstance(declared, int) and len(body) < min(declared, limit):
            raise RegistryError(f"registry read of {label} was cut off")
        return body

    def _bearer(self, name: str, actions: str) -> str:
        scope = f"repository:{name}:{actions}"
        tok = self._bearers.get(scope)
        if tok:
            return tok
        url = (f"{self._base}/v2/token?service=container_registry&scope="
               + urllib.parse.quote(scope, safe=":/,"))
        resp = self._open("GET", url, "/v2/token", auth=self._basic)
        doc = _json(self._read(resp, JSON_MAX_BYTES, "/v2/token"),
                    "the registry's token reply")
        tok = doc.get("token")
        if not isinstance(tok, str) or not tok:
            raise RegistryError("the registry's token reply carries no token")
        self._bearers[scope] = "Bearer " + tok
        return self._bearers[scope]

    # -- reads -------------------------------------------------------------

    def get_manifest(self, name: str, ref: str) -> "tuple[bytes, str, str] | None":
        """(body, media type, digest) of `name:ref`, or None when the registry
        says there is none (404). The digest is computed from the body."""
        path = f"/v2/{name}/manifests/{urllib.parse.quote(ref, safe=':')}"
        try:
            resp = self._open("GET", self._base + path, path,
                              auth=self._bearer(name, "pull"),
                              headers={"Accept": ACCEPT})
        except RegistryError as e:
            if e.status == 404:
                return None
            raise
        media = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        body = self._read(resp, JSON_MAX_BYTES + 1, path)
        if len(body) > JSON_MAX_BYTES:
            raise RegistryError(f"registry GET {path}: the manifest is too large")
        return body, media, _sha256(body)

    def get_small_blob(self, name: str, digest: str) -> bytes:
        """A blob read whole, bounded like a manifest (an image config)."""
        path = f"/v2/{name}/blobs/{digest}"
        resp = self._open("GET", self._base + path, path,
                          auth=self._bearer(name, "pull"))
        body = self._read(resp, JSON_MAX_BYTES + 1, path)
        if len(body) > JSON_MAX_BYTES:
            raise RegistryError(f"registry GET {path}: the blob is too large")
        if _sha256(body) != digest:
            raise RegistryError(f"registry GET {path}: checksum mismatch")
        return body

    def blob_exists(self, name: str, digest: str) -> bool:
        path = f"/v2/{name}/blobs/{digest}"
        try:
            self._open("HEAD", self._base + path, path,
                       auth=self._bearer(name, "pull")).close()
        except RegistryError as e:
            if e.status == 404:
                return False
            raise
        return True

    # -- writes ------------------------------------------------------------

    def upload_blob(self, name: str, digest: str, size: int, fh: BinaryIO) -> None:
        """Upload `size` bytes from `fh` as blob `digest`. The stream is hashed
        as it goes; a mismatch is an error (the registry refuses it too)."""
        auth = self._bearer(name, "pull,push")
        path = f"/v2/{name}/blobs/uploads/"
        with self._open("POST", self._base + path, path, auth=auth,
                        headers={"Content-Length": "0"}, ok=(202,)) as resp:
            location = resp.headers.get("Location") or ""
        url = self._pin(location)
        url += ("&" if "?" in url else "?") + "digest=" + urllib.parse.quote(digest)
        body = _Hashing(fh, size)
        # No time bound on the upload itself: its length is the blob's, and the
        # connection fails on its own if the registry goes away.
        self._open("PUT", url, path + "<upload>", auth=auth, data=body,
                   headers={"Content-Length": str(size),
                            "Content-Type": "application/octet-stream"},
                   ok=(201,), timeout=None).close()
        if body.count != size or "sha256:" + body.sha.hexdigest() != digest:
            raise RegistryError(f"blob {digest}: what was read does not match "
                                f"its checksum or size")

    def put_manifest(self, name: str, ref: str, body: bytes, media: str) -> None:
        path = f"/v2/{name}/manifests/{urllib.parse.quote(ref, safe=':')}"
        self._open("PUT", self._base + path, path,
                   auth=self._bearer(name, "pull,push"), data=body,
                   headers={"Content-Type": media}, ok=(201,)).close()


def _descriptor(d: Any, what: str) -> "tuple[str, int, str]":
    if not isinstance(d, dict):
        raise RegistryError(f"{what} is not an object")
    digest, size, media = d.get("digest"), d.get("size"), d.get("mediaType") or ""
    if not (isinstance(digest, str) and DIGEST_RE.match(digest)):
        raise RegistryError(f"{what} names no sha256 checksum")
    if not (isinstance(size, int) and not isinstance(size, bool) and size >= 0):
        raise RegistryError(f"{what} names no size")
    return digest, size, media if isinstance(media, str) else ""


def push_image(client: RegistryClient, name: str, source: LayoutSource,
               tag: str = SHELF_TAG) -> "tuple[str, set[str]]":
    """Push the single image in `source` to `name:tag`.

    Order: blobs, then child manifests by checksum, then the image's own
    manifest under `tag` last — so a push that fails part way leaves `tag`
    pointing at whatever it pointed at before. Every part is checked against
    its checksum. Returns the image's digest and the digests of every manifest
    it consists of (itself included): what a caller must keep when it removes
    the repo's older versions.
    """
    index = _json(source.read_small("index.json", JSON_MAX_BYTES),
                  "the image's index")
    entries = index.get("manifests")
    if not isinstance(entries, list) or len(entries) != 1:
        raise RegistryError("the image archive must hold exactly one image")
    top_digest, top_size, _ = _descriptor(entries[0], "the image's index entry")
    closure: set[str] = set()

    def manifest(digest: str, size: int, ref: str, depth: int = 1) -> None:
        if digest in closure:           # already pushed: a repeated child
            return
        if depth > MANIFEST_DEPTH_MAX:
            raise RegistryError("the image nests its manifests too deeply")
        if size > JSON_MAX_BYTES:
            raise RegistryError(f"manifest {digest} is too large")
        body = source.read_small(_blob_path(digest), JSON_MAX_BYTES)
        if len(body) != size or _sha256(body) != digest:
            raise RegistryError(f"manifest {digest} does not match its checksum")
        doc = _json(body, f"manifest {digest}")
        media = doc.get("mediaType")
        if media is None:
            media = ""
        # The media type becomes a request header, and it comes out of the
        # archive: only the known ones are sent.
        if media and media not in MEDIA_INDEX + MEDIA_MANIFEST:
            raise RegistryError(f"manifest {digest} has an unknown media type")
        children = doc.get("manifests")
        if isinstance(children, list):
            media = media or MEDIA_INDEX[0]
            for i, child in enumerate(children):
                c_digest, c_size, _ = _descriptor(child, f"{digest} entry {i}")
                manifest(c_digest, c_size, c_digest, depth + 1)
        else:
            media = media or MEDIA_MANIFEST[0]
            for i, part in enumerate(_parts(doc, f"manifest {digest}")):
                b_digest, b_size, _ = _descriptor(part, f"{digest} part {i}")
                if client.blob_exists(name, b_digest):
                    continue
                fh, finish = source.open_blob(_blob_path(b_digest), b_size)
                try:
                    try:
                        client.upload_blob(name, b_digest, b_size, fh)
                    except RegistryError:
                        # A producer that failed explains a short or damaged
                        # stream better than the upload's own error does.
                        finish()
                        raise
                    finish()
                finally:
                    fh.close()
        closure.add(digest)
        client.put_manifest(name, ref, body, media)

    manifest(top_digest, top_size, tag)
    return top_digest, closure


def _image_labels(doc: dict, read) -> dict:
    """The labels of the image whose manifest (or index) is `doc`; `read`
    returns a blob's bytes by digest. An index is followed to its first real
    platform (build attestations are listed with the platform "unknown")."""
    children = doc.get("manifests")
    if isinstance(children, list):
        pick = None
        for child in children:
            plat = child.get("platform") if isinstance(child, dict) else None
            if isinstance(plat, dict) and plat.get("os") not in (None, "unknown"):
                pick = child
                break
        if pick is None:
            raise RegistryError("the image's index lists no platform image")
        doc = _json(read(_descriptor(pick, "the image's platform entry")[0]),
                    "the image's platform manifest")
    cfg_digest = _descriptor(_parts(doc, "the image's manifest")[0],
                             "the image's config")[0]
    cfg = _json(read(cfg_digest), "the image's config")
    inner = cfg.get("config")
    labels = inner.get("Labels") if isinstance(inner, dict) else None
    return labels if isinstance(labels, dict) else {}


def layout_labels(source: LayoutSource) -> dict:
    """The labels of the single image in `source`, read before anything is
    pushed, so a caller can check the image is the one it asked for."""
    index = _json(source.read_small("index.json", JSON_MAX_BYTES),
                  "the image's index")
    entries = index.get("manifests")
    if not isinstance(entries, list) or len(entries) != 1:
        raise RegistryError("the image archive must hold exactly one image")

    def read(digest: str) -> bytes:
        body = source.read_small(_blob_path(digest), JSON_MAX_BYTES)
        if _sha256(body) != digest:
            raise RegistryError(f"{digest} does not match its checksum")
        return body
    top = _descriptor(entries[0], "the image's index entry")[0]
    return _image_labels(_json(read(top), "the image's manifest"), read)


def image_info(client: RegistryClient, name: str,
               tag: str = SHELF_TAG) -> "dict | None":
    """What is known about `name:tag`: digest, commit, git tags, source,
    created and size (config plus layers of the image for the first real
    platform). None when there is no such image."""
    got = client.get_manifest(name, tag)
    if got is None:
        return None
    body, _, digest = got
    doc = _json(body, "the image's manifest")
    children = doc.get("manifests")
    if isinstance(children, list):
        # An image index: the first entry that is a real platform (build
        # attestations are listed with the platform "unknown").
        pick = None
        for child in children:
            plat = child.get("platform") if isinstance(child, dict) else None
            if isinstance(plat, dict) and plat.get("os") not in (None, "unknown"):
                pick = child
                break
        if pick is None:
            raise RegistryError("the image's index lists no platform image")
        c_digest, _, _ = _descriptor(pick, "the image's platform entry")
        sub = client.get_manifest(name, c_digest)
        if sub is None:
            raise RegistryError("the image's platform manifest is missing")
        doc = _json(sub[0], "the image's platform manifest")
    parts = _parts(doc, "the image's manifest")
    cfg_digest, cfg_size, _ = _descriptor(parts[0], "the image's config")
    size = cfg_size
    for i, layer in enumerate(parts[1:]):
        size += _descriptor(layer, f"layer {i}")[1]
    cfg = _json(client.get_small_blob(name, cfg_digest), "the image's config")
    inner = cfg.get("config")
    labels = inner.get("Labels") if isinstance(inner, dict) else None
    if not isinstance(labels, dict):
        labels = {}
    tags: list[str] = []
    raw_tags = labels.get(LABEL_GIT_TAGS)
    if isinstance(raw_tags, str):
        try:
            raw = json.loads(raw_tags)
        except (ValueError, RecursionError):
            raw = None
        if isinstance(raw, list):
            tags = [t for t in raw if isinstance(t, str)]
    text = lambda v: v if isinstance(v, str) else ""        # noqa: E731
    return {"digest": digest, "commit": text(labels.get(LABEL_COMMIT)),
            "git_tags": tags, "source": text(labels.get(LABEL_SOURCE)),
            "created": text(cfg.get("created")), "size": size}
