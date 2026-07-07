#!/opt/conda/bin/python
"""reader-server.py — read-only mobile artifact reader for the research workflow
(STAGE_READER).

Serves the supervisor project's *artifact surfaces* to a mobile browser: markdown
rendered via python-markdown, notebooks rendered via nbconvert, everything else
served raw (images, CSV, text). Ships in the reader dist (`tools/`) and is cp'd
into a reader-enabled supervisor at boot, run under the container conda python.

Design (settled with the PI):
- **Read-only by construction.** GET/HEAD only; any write method → 405. There is
  no write path in the code at all — the workspace bind-mount is the data plane,
  never mutated. Previews are cached compute, never materialized in the tree.
- **Rendered previews are cached, invalidated by mtime.** A `stat()` on each
  request IS the freshness check (no inotify watcher, no watch descriptors over
  thousands of worker files) — a changed file re-renders lazily on next read.
- **Cache is thread-safe.** ThreadingHTTPServer serves requests concurrently, so
  one lock guards every cache lookup / insert / evict (byte-accounting + eviction
  ordering race across threads even though CPython dicts won't corrupt). Rendering
  runs OUTSIDE the lock — a concurrent double-render is idempotent and cheap.
- **Path containment is the one security-critical surface.** Every request path is
  resolved to its realpath (symlinks followed), which must land under WORKSPACE
  AND classify into the artifact allowlist — a symlink pointing outside the
  workspace or at a denied subtree is rejected (404). `..` segments are rejected
  before resolution.
- **Isolation.** Notebook outputs can embed arbitrary HTML/JS; this server runs on
  its own per-project origin port (the webui origin-isolation lane), so embedded
  scripts are contained to that origin and can't reach the webui/vault origin.

nbconvert is imported lazily on first .ipynb render, so the idle footprint is a
bare stdlib process.
"""
from __future__ import annotations

import html
import http.server
import logging
import mimetypes
import os
import shutil
import socketserver
import threading
import urllib.parse
from collections import OrderedDict
from pathlib import Path

# ---- knobs (named, with reasoning — see CLAUDE.md "no magic numbers") -------

# The listen port on the supervisor netns; the webui reaches it via container DNS
# (rs-project-<proj>:<port>). Fixed at the reader service's default_port; the env
# override exists only for the harness. Mirrors CODE_SERVER_STUB_PORT's shape.
READER_PORT = int(os.environ.get("READER_PORT", "8445"))

# The project root, bind-mounted read-only in practice (this server never writes).
WORKSPACE = Path(os.environ.get("READER_WORKSPACE", "/workspace")).resolve()

# Rendered-preview cache budget (total bytes across all entries). Rendered
# notebooks run ~100 KB–5 MB once base64 figures are inlined, so 256 MiB holds
# dozens warm at once; halving it would thrash on a figure-heavy project (evicting
# a notebook the PI is still scrolling), while 10x would start contending with the
# project's own --memory limit. The cache is pure recomputable derived state — an
# eviction only costs one re-render on the next view.
READER_CACHE_MAX_BYTES = 256 * 1024 * 1024

# Top-level artifact subtrees browsable in full (whole subtree viewable).
#   logbook/ plan/ shared/ published/ — the general artifact surfaces.
#   results/  — the PI-visible ACCEPTED-deliverables bundles + manifest.json
#               (the project inventory / executive surface).
#   staging/  — the cycle awaiting the PI's review; its entries are symlinks into
#               workers/<name>/work/outputs/<slug>/, which classify "ok" on the
#               resolved path, so the staged cycle is readable from a phone too.
_ALLOW_DIRS = {"logbook", "plan", "shared", "published", "results", "staging"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s reader-server %(levelname)s %(message)s")
log = logging.getLogger("reader")


# ---------------------------------------------------------------------------
# Artifact allowlist — what of /workspace the reader may show.
# ---------------------------------------------------------------------------
#
# The research workspace holds far more than deliverables (creds, plan drafts,
# worker inboxes, the .orchestrator control plane). The reader surfaces ONLY the
# PI-facing artifact surfaces. Every access is classified on the RESOLVED real
# path (post-symlink), so a symlink can't dodge this:
#
#   "ok"  — content-viewable (render / raw) AND, if a dir, listable.
#   "nav" — a navigation ancestor: listable as a dir, but its own content is not
#           served (you can browse THROUGH it to reach allowed leaves).
#   None  — denied.
#
# Worker surfaces are deliberately narrow: a worker's outputs/ subtree plus its
# research_log.md + summary.md — never its inbox/, scratch/, task.md, .claude/,
# log.jsonl, or creds. The accepted (results/) and staged (staging/) surfaces are
# whole-subtree allowed above; staging/ entries symlink into a worker's outputs/,
# which classifies "ok" on the resolved path.
def _classify(rel: str) -> str | None:
    """Classify a workspace-relative POSIX path (already '..'-free, no leading
    '/'). Returns "ok" | "nav" | None."""
    if rel == "":
        return "nav"
    segs = rel.split("/")
    # Dotfiles/dot-dirs are ALWAYS hidden — on direct /view/ and /raw/ requests,
    # not just in listings. This is where the control plane / creds / .claude live;
    # denying any dot-leading segment on the RESOLVED path is the single carve that
    # keeps a direct-URL fetch from reaching them.
    if any(s.startswith(".") for s in segs):
        return None
    top = segs[0]
    if top in _ALLOW_DIRS:
        return "ok"
    if top == "workers":
        if len(segs) <= 2:
            return "nav"                      # workers/ , workers/<w>/
        if segs[2] != "work":
            return None
        if len(segs) == 3:
            return "nav"                      # workers/<w>/work/
        sub = segs[3]
        if sub == "outputs":
            return "ok"                       # workers/<w>/work/outputs[/...]
        if len(segs) == 4 and sub in ("research_log.md", "summary.md"):
            return "ok"                       # the two allowed loose files
        return None
    return None


def _resolve(req_path: str) -> tuple[Path, str] | None:
    """Map a URL sub-path to a contained, allowlisted (realpath, rel) pair, or
    None if it escapes the workspace, contains '..', or is denied by the
    allowlist. The classification runs on the RESOLVED path so a symlink can't
    smuggle access to a denied/outside target."""
    rel = urllib.parse.unquote(req_path or "").strip("/")
    if rel:
        parts = rel.split("/")
        if any(p in ("", ".", "..") for p in parts):
            return None
    # A percent-encoded NUL (%00) survives unquote as an embedded null byte, which
    # makes Path.resolve()/relative_to raise ValueError (not OSError) — catch it
    # here so it 404s rather than escaping the handler as a per-request traceback.
    try:
        real = (WORKSPACE / rel).resolve()
        # Containment: the resolved path must be WORKSPACE or below it.
        if real != WORKSPACE and WORKSPACE not in real.parents:
            return None
        rel_real = real.relative_to(WORKSPACE).as_posix()
    except (ValueError, OSError):
        return None
    rel_real = "" if rel_real == "." else rel_real
    if _classify(rel_real) is None:
        return None
    return real, rel_real


# ---------------------------------------------------------------------------
# Rendered-preview cache (thread-safe, mtime-invalidated, byte-budgeted LRU).
# ---------------------------------------------------------------------------
class _RenderCache:
    def __init__(self, max_bytes: int) -> None:
        self._max = max_bytes
        self._lock = threading.Lock()
        # key -> (mtime_ns, size, body: bytes, content_type: str)
        self._d: "OrderedDict[str, tuple[int, int, bytes, str]]" = OrderedDict()
        self._total = 0

    def get(self, key: str, mtime_ns: int, size: int):
        with self._lock:
            hit = self._d.get(key)
            if hit and hit[0] == mtime_ns and hit[1] == size:
                self._d.move_to_end(key)
                return hit[2], hit[3]
            return None

    def put(self, key: str, mtime_ns: int, size: int,
            body: bytes, content_type: str) -> None:
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self._total -= len(old[2])
            self._d[key] = (mtime_ns, size, body, content_type)
            self._total += len(body)
            while self._total > self._max and len(self._d) > 1:
                _, ev = self._d.popitem(last=False)
                self._total -= len(ev[2])


_CACHE = _RenderCache(READER_CACHE_MAX_BYTES)


# ---------------------------------------------------------------------------
# Rendering. python-markdown for .md; nbconvert (lazy import) for .ipynb.
# ---------------------------------------------------------------------------
_MOBILE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 1rem 1.1rem 4rem;
  font: 16px/1.6 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
  max-width: 46rem; margin-inline: auto;
  color: #1a1a1a; background: #fff; overflow-wrap: anywhere; }
@media (prefers-color-scheme: dark) {
  body { color: #e6e6e6; background: #161616; }
  a { color: #6ea8fe; } tr:nth-child(even) td { background: #1e1e1e; }
  pre, code { background: #222 !important; } }
a { color: #0b5ed7; text-decoration: none; } a:hover { text-decoration: underline; }
img, svg, table { max-width: 100%; } img { height: auto; }
pre { overflow-x: auto; padding: .75rem; background: #f4f4f4; border-radius: 6px; }
code { background: #f4f4f4; padding: .1em .35em; border-radius: 4px; }
pre code { padding: 0; background: none; }
table { border-collapse: collapse; display: block; overflow-x: auto; }
th, td { border: 1px solid #8884; padding: .35rem .6rem; text-align: left; }
.rd-crumb { font-size: .9rem; margin: 0 0 1rem; opacity: .8; }
.rd-list { list-style: none; padding: 0; }
.rd-list li { padding: .55rem 0; border-bottom: 1px solid #8883; }
.rd-list a { display: block; }
.rd-dir::before { content: "\\1F4C1  "; } .rd-file::before { content: "\\1F4C4  "; }
"""

_VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'


def _page(title: str, body_html: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset='utf-8'>{_VIEWPORT}"
            f"<title>{html.escape(title)}</title><style>{_MOBILE_CSS}</style>"
            f"</head><body>{body_html}</body></html>").encode("utf-8")


def _breadcrumb(rel: str) -> str:
    parts = [p for p in rel.split("/") if p]
    links = ['<a href="/">workspace</a>']
    acc = ""
    for p in parts:
        acc = f"{acc}/{p}" if acc else p
        links.append(f'<a href="/tree/{urllib.parse.quote(acc)}">{html.escape(p)}</a>')
    return f'<div class="rd-crumb">{" / ".join(links)}</div>'


def _render_markdown(text: str, rel: str) -> bytes:
    import markdown  # stdlib-cheap once installed; part of the dist
    body = markdown.markdown(
        text, extensions=["fenced_code", "tables", "sane_lists"])
    return _page(rel or "workspace", _breadcrumb(rel) + body)


def _render_notebook(path: Path, rel: str) -> bytes:
    from nbconvert import HTMLExporter  # lazy — heavy import only on first .ipynb
    import nbformat
    nb = nbformat.read(str(path), as_version=4)
    body, _ = HTMLExporter(template_name="classic").from_notebook_node(nb)
    # nbconvert emits a standalone doc with its own <head>; inject the viewport
    # meta so it's legible on a phone (its CSS is desktop-shaped otherwise).
    if "<head>" in body and "viewport" not in body:
        body = body.replace("<head>", "<head>" + _VIEWPORT, 1)
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# Directory listing.
# ---------------------------------------------------------------------------
def _listing(real: Path, rel: str) -> bytes:
    rows: list[tuple[bool, str, str]] = []   # (is_dir, name, href)
    try:
        entries = sorted(os.scandir(real), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        entries = []
    for e in entries:
        if e.name.startswith("."):
            continue
        child_rel = f"{rel}/{e.name}" if rel else e.name
        got = _resolve(child_rel)               # re-classify each child on realpath
        if got is None:
            continue
        is_dir = e.is_dir()
        if is_dir:
            href = "/tree/" + urllib.parse.quote(got[1])
        else:
            if _classify(got[1]) != "ok":       # nav-only leaf files aren't viewable
                continue
            href = "/view/" + urllib.parse.quote(got[1])
        rows.append((is_dir, e.name, href))
    items = "".join(
        f'<li><a class="{"rd-dir" if d else "rd-file"}" href="{h}">{html.escape(n)}</a></li>'
        for d, n, h in rows)
    body = _breadcrumb(rel) + (f'<ul class="rd-list">{items}</ul>' if items
                               else "<p><em>(nothing to show here)</em></p>")
    return _page(rel or "workspace", body)


# ---------------------------------------------------------------------------
# HTTP handler.
# ---------------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "rs-reader/1"

    def log_message(self, fmt, *args):  # route through logging, not stderr spew
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This server serves ONLY the workspace; deny being framed by anyone but
        # our own origin is unnecessary (the webui frames it cross-origin by
        # design), but block content-type sniffing of raw artifacts.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _fail(self, code: int, msg: str) -> None:
        self._send(code, _page(f"{code}", f"<h1>{code}</h1><p>{html.escape(msg)}</p>"),
                   "text/html; charset=utf-8")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "":
            return self._route_tree("")
        for prefix, handler in (("/tree/", self._route_tree),
                                ("/view/", self._route_view),
                                ("/raw/", self._route_raw)):
            if path.startswith(prefix):
                return handler(path[len(prefix):])
        # Bare "/tree" etc. → treat as root of that verb.
        if path in ("/tree", "/view", "/raw"):
            return self._route_tree("")
        self._fail(404, "not found")

    # Any non-GET/HEAD method is refused — structural read-only guarantee.
    def _reject_write(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = _reject_write

    def _route_tree(self, sub: str) -> None:
        got = _resolve(sub)
        if got is None:
            return self._fail(404, "not found")
        real, rel = got
        if real.is_dir():
            return self._send(200, _listing(real, rel), "text/html; charset=utf-8")
        # A file hit on /tree → redirect intent: just view it.
        return self._route_view(sub)

    def _route_view(self, sub: str) -> None:
        got = _resolve(sub)
        if got is None:
            return self._fail(404, "not found")
        real, rel = got
        if _classify(rel) != "ok" or not real.is_file():
            return self._fail(404, "not found")
        try:
            st = real.stat()
        except OSError:
            return self._fail(404, "not found")
        cached = _CACHE.get(rel, st.st_mtime_ns, st.st_size)
        if cached is not None:
            return self._send(200, cached[0], cached[1])
        suffix = real.suffix.lower()
        try:
            if suffix == ".md":
                body = _render_markdown(real.read_text(errors="replace"), rel)
                ctype = "text/html; charset=utf-8"
            elif suffix == ".ipynb":
                body = _render_notebook(real, rel)
                ctype = "text/html; charset=utf-8"
            else:
                # Non-rendered types: hand off to raw so images/CSV/etc. display.
                return self._route_raw(sub)
        except Exception as exc:  # a corrupt notebook / bad markdown must not 500 opaque
            log.warning("render failed for %s: %s", rel, exc)
            return self._fail(422, "could not render this file")
        _CACHE.put(rel, st.st_mtime_ns, st.st_size, body, ctype)
        self._send(200, body, ctype)

    def _route_raw(self, sub: str) -> None:
        got = _resolve(sub)
        if got is None:
            return self._fail(404, "not found")
        real, rel = got
        if _classify(rel) != "ok" or not real.is_file():
            return self._fail(404, "not found")
        # STREAM raw bytes rather than read the whole file into memory: shared/data/
        # routinely holds multi-GB research files, and a few concurrent large GETs
        # would OOM the memory-capped supervisor. stat FIRST (a failure there 404s
        # cleanly, before any header is sent); then stream the body in kernel-sized
        # chunks (HEAD sends headers only). A failure mid-stream can't be recovered
        # into a status — headers are already on the wire — so it just aborts.
        try:
            size = real.stat().st_size
        except OSError:
            return self._fail(404, "not found")
        ctype = mimetypes.guess_type(real.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            try:
                with open(real, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
            except OSError:
                pass   # headers already sent; nothing to do but drop the connection


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    srv = _Server(("0.0.0.0", READER_PORT), _Handler)
    log.info("reader-server listening on 0.0.0.0:%d, workspace=%s",
             READER_PORT, WORKSPACE)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
