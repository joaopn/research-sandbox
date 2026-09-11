#!/opt/conda/bin/python
"""reader-server.py — read-only mobile file reader for a project workspace.

Serves a project's workspace to a mobile browser: markdown rendered via
python-markdown, notebooks rendered via nbconvert, any other text file rendered as
a plain page, and everything else streamed raw: images, the documents a browser
renders natively, binaries, and text above the render ceiling (markdown and
notebooks have no such ceiling). Ships in the reader dist (`tools/`) and is cp'd
into a reader-enabled supervisor at boot, run under the container conda python.

Every workflow gets the same view — the workspace minus a short deny-list — rather
than a curated per-workflow surface. A curated allowlist of research folder names
was the original design and it made the reader useless on every other workflow: a
dev project's files are its repo clone and its worktrees, none of which a research
allowlist can name, so the whole tab rendered as one empty folder.

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
  AND survive the deny-list — a symlink pointing outside the workspace or at a
  denied path is rejected (404). `..` segments are rejected before resolution.
  Because the deny test runs on the RESOLVED path, an innocently-named symlink
  cannot smuggle a denied target.
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

# Types the BROWSER renders better than an escaped <pre>, checked BEFORE the
# content sniff and handed to the raw stream (which is what they got before this
# server rendered text at all). The sniff alone cannot decide this: SVG and
# PostScript are NUL-free text, and an uncompressed PDF often is for its first
# page of bytes, so a content-only rule would turn a figure into its own source
# — and `results/` is where a worker's accepted .pdf/.svg figures land, i.e.
# exactly what the reader exists to show. HTML is here for the same reason and
# on the same terms as a rendered notebook: this server already serves notebook
# output that can embed arbitrary markup, and origin isolation (not escaping) is
# what contains it, so escaping a worker's plotly export would cost the figure
# and buy nothing. Everything else — including the application/* configs
# (.json, .toml, .xml) — falls through to the sniff and renders as text.
_RAW_MIME_PREFIXES = ("image/", "video/", "audio/")
_RAW_MIME_TYPES = frozenset({
    "application/pdf", "application/postscript",
    "text/html", "application/xhtml+xml",
})

# Text render (see _render_text). A file the reader can't render specially is
# still usually TEXT — source, config, a Dockerfile, a LICENSE — and handing it to
# the browser raw means a download on a phone rather than a read, because a
# guessed mimetype for those is either an odd text/* subtype or nothing at all.
# So the choice is made on CONTENT: read the first chunk and look for a NUL byte,
# the same test git uses to call a blob binary. One page-sized window is the
# whole heuristic (git reads a longer one; this is our own number) — a
# binary format carrying no NUL in its first 4 KiB is not one this reader could
# usefully display anyway, and a larger window only costs latency on every file.
READER_TEXT_SNIFF_BYTES = 4096

# Ceiling above which a TEXT file streams raw instead of rendering. It bounds this
# branch only — markdown and notebook rendering are unbounded, as they were before
# this branch existed, so a huge .ipynb can still buffer whole. The render path
# holds the whole body in memory and caches it, while a research project's
# shared/data/ routinely holds multi-GB files. 4 MiB clears any real source file
# by several times (the largest tracked file in this repo is 585 KB) and is about
# as much monospace as a phone browser will lay out as one <pre>; ten times higher
# would start evicting a notebook the operator is still scrolling out of the cache
# budget above — and the cached entry is the ESCAPED body, which a quote-dense
# file inflates several-fold, so the budget is spent faster than the ceiling
# suggests. Nothing becomes unreachable — over the ceiling is exactly today's
# raw stream.
READER_TEXT_RENDER_MAX_BYTES = 4 * 1024 * 1024

# ---- the deny-list (see _classify) -----------------------------------------

# Build residue: machine-generated, never authored, and pure noise in a listing —
# a clone of this repo grows one per package dir the moment anything runs in it,
# each landing right where the source files are. Matched as a path SEGMENT anywhere,
# because both nest arbitrarily deep in a repo.
_DENY_DIRS = {"__pycache__", "node_modules"}

# Worker process residue, denied ONLY inside a worker's own work dir (below) —
# never as bare names anywhere, because a repo may legitimately carry a folder
# called `scratch`, and hiding something the operator authored is a worse failure
# than showing some noise. `scratch/` is created per worker and defined by the
# worker role doc as exploration and dead ends; `log.jsonl` (the stream-json
# transcript) and `terminal.log` (the terminal capture) are megabytes of
# one-event-per-line text that no one opens a phone to read.
_DENY_WORKER_RESIDUE = {"scratch", "log.jsonl", "terminal.log"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s reader-server %(levelname)s %(message)s")
log = logging.getLogger("reader")


# ---------------------------------------------------------------------------
# The deny-list — what of the workspace the reader refuses to show.
# ---------------------------------------------------------------------------
#
# Everything in the workspace is viewable EXCEPT what this function denies. That
# is the whole model: a workspace's shape differs per workflow (a research project
# has logbook/plan/results/workers, a dev project has a repo clone and worktrees,
# a management project has box workspaces), and any allowlist of names is wrong
# for every workflow it was not written for.
#
# What carries the containment is the dot rule, and it carries it alone: every
# credential-bearing path any flavor writes into a workspace is dot-leading —
# .claude/.credentials.json (supervisor + each worker), the rebuild stashes
# (.creds-stash, .creds-stash-home.json, .ssh-stash, .gitconfig-stash),
# .role-mcps/<role>/.creds/, the .orchestrator control plane, a box's staged
# .claude/, and a clone's .git. Box secrets and the dev consumer's git credentials
# live in $HOME, outside the mount entirely. Since the test below runs on the
# RESOLVED path, a non-dot symlink aimed at any of them is denied too.
#
#   "ok"  — viewable, and listable if it is a directory.
#   None  — denied, both in listings and on a direct /view/ or /raw/ fetch.
def _classify(rel: str) -> str | None:
    """Classify a workspace-relative POSIX path (already '..'-free, no leading
    '/'). Returns "ok" | None."""
    if rel == "":
        return "ok"
    segs = rel.split("/")
    # Dotfiles/dot-dirs are ALWAYS hidden — on direct /view/ and /raw/ requests,
    # not just in listings. This is where the control plane / creds / .claude live;
    # denying any dot-leading segment on the RESOLVED path is the single carve that
    # keeps a direct-URL fetch from reaching them.
    if any(s.startswith(".") for s in segs):
        return None
    if any(s in _DENY_DIRS for s in segs):
        return None
    # Worker process residue, scoped to workers/<name>/work/ so the names mean
    # what the research workflow says they mean and nowhere else.
    if len(segs) > 3 and segs[0] == "workers" and segs[2] == "work" \
            and segs[3] in _DENY_WORKER_RESIDUE:
        return None
    return "ok"


def _resolve(req_path: str) -> tuple[Path, str] | None:
    """Map a URL sub-path to a contained, permitted (realpath, rel) pair, or
    None if it escapes the workspace, contains '..', or is denied by the
    deny-list. The classification runs on the RESOLVED path so a symlink can't
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
# Rendering — the theme, then the renderers that use it: python-markdown for
# .md, nbconvert (lazy import) for .ipynb, an escaped <pre> for anything else
# that is text.
#
# Theme: two real palettes, and a button that picks one.
# ---------------------------------------------------------------------------
#
# Every colour is a token, defined three times: light on bare :root, dark under
# the system preference GUARDED so an explicit light choice still wins, and dark
# again under an explicit choice. Before the button is ever pressed the page
# follows the system, which is what it always did.
#
# The two dark blocks carry IDENTICAL values, which is the only reason their equal
# specificity (0-2-0 each) is harmless. Give explicit-dark a palette of its own and
# source order becomes load-bearing — put it last if you do.
#
# The button cannot be labelled by the server: one page is built for every viewer
# and the theme lives in the viewer's browser, so a server-written label is stale
# for anyone whose stored choice differs from the default. Both labels ship in the
# markup and the same three blocks choose between them, which also means there is
# no flash of the wrong word. The accessible name is deliberately state-neutral —
# an aria-label cannot be swapped by CSS, and a stale one is worse than a general
# one.
_MOBILE_CSS = """
:root {
  color-scheme: light;
  --rd-bg: #ffffff; --rd-fg: #1a1a1a; --rd-link: #0b5ed7;
  --rd-soft: #f4f4f4; --rd-rule: #d8d8d8; --rd-row: #fafafa;
  --rd-btn-bg: #f4f4f4; --rd-btn-fg: #1a1a1a; --rd-btn-rule: #b0b0b0;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --rd-bg: #121212; --rd-fg: #e6e6e6; --rd-link: #6ea8fe;
    --rd-soft: #1e1e1e; --rd-rule: #333333; --rd-row: #181818;
    --rd-btn-bg: #242424; --rd-btn-fg: #e6e6e6; --rd-btn-rule: #5a5a5a;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --rd-bg: #121212; --rd-fg: #e6e6e6; --rd-link: #6ea8fe;
  --rd-soft: #1e1e1e; --rd-rule: #333333; --rd-row: #181818;
  --rd-btn-bg: #242424; --rd-btn-fg: #e6e6e6; --rd-btn-rule: #5a5a5a;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 1rem 1.1rem 4rem;
  font: 16px/1.6 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
  max-width: 46rem; margin-inline: auto;
  color: var(--rd-fg); background: var(--rd-bg); overflow-wrap: anywhere; }
a { color: var(--rd-link); text-decoration: none; }
a:hover { text-decoration: underline; }
img, svg, table { max-width: 100%; } img { height: auto; }
pre { overflow-x: auto; padding: .75rem; background: var(--rd-soft);
  border-radius: 6px; }
code { background: var(--rd-soft); padding: .1em .35em; border-radius: 4px; }
pre code { padding: 0; background: none; }
table { border-collapse: collapse; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--rd-rule); padding: .35rem .6rem; text-align: left; }
tr:nth-child(even) td { background: var(--rd-row); }
.rd-crumb { font-size: .9rem; margin: 0 0 1rem; opacity: .8; }
.rd-list { list-style: none; padding: 0; }
.rd-list li { padding: .55rem 0; border-bottom: 1px solid var(--rd-rule); }
.rd-list a { display: block; }
.rd-dir::before { content: "\\1F4C1  "; } .rd-file::before { content: "\\1F4C4  "; }
/* The theme button rides in a fixed bar that mirrors the COLUMN's geometry —
   same max-width, same auto margins — so its right edge tracks the text column
   instead of the window's corner (on a wide screen a window-anchored button
   strands itself in the corner, far from anything it belongs to). The bar takes
   no pointer events, so only the button itself is clickable over the text. */
.rd-theme-bar { position: fixed; top: 0; left: 0; right: 0; z-index: 9;
  max-width: 46rem; margin-inline: auto; padding: .5rem .6rem 0 0;
  display: flex; justify-content: flex-end; pointer-events: none; }
.rd-theme { pointer-events: auto;
  min-width: 2.75rem; min-height: 2.75rem; padding: .35rem .7rem;
  font: inherit; font-size: .85rem; line-height: 1.2; cursor: pointer;
  color: var(--rd-btn-fg); background: var(--rd-btn-bg);
  border: 1px solid var(--rd-btn-rule); border-radius: 8px; }
/* Which word shows is the same three-way question as the palette. */
.rd-theme .rd-to-dark { display: inline; }
.rd-theme .rd-to-light { display: none; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .rd-theme .rd-to-dark { display: none; }
  :root:not([data-theme="light"]) .rd-theme .rd-to-light { display: inline; }
}
:root[data-theme="dark"] .rd-theme .rd-to-dark { display: none; }
:root[data-theme="dark"] .rd-theme .rd-to-light { display: inline; }
/* The column reserves the corner the button sits in, at EVERY width — the bar
   above tracks the column, so the button is inside the column box and would
   otherwise cover the first line of each scrolled screenful. The reserve is
   wider where it is free (a roomy window) and tight where it is not (a phone),
   which is also why the word drops on a narrow screen: the glyph alone fits the
   smaller gutter, and a button that outgrew its gutter would be back on top of
   the text. */
body { padding-right: 5.5rem; }
@media (max-width: 52rem) {
  body { padding-right: 3.5rem; }
  .rd-theme .rd-word { display: none; }
}
"""

# The stored choice, and the two scripts that read and write it. Kept OUT of the
# f-string in _page: a literal brace inside that literal is a replacement field.
#
# THE HEAD SCRIPT MUST STAY SYNCHRONOUS AND IN THE HEAD, and it carries the click
# handler for the same reason. Every page here is a fresh document, so a choice
# applied from the body would paint the wrong theme first on every navigation —
# and the button is the first node in the body, tappable the moment it paints,
# while a 4 MiB text render is still arriving over a phone connection. A handler
# defined at the END of the body does not exist yet for all of that time, so every
# early tap is silently ignored (measured: throttled to 250 kbps, a real click
# changed nothing and logged a ReferenceError to a console no phone user opens).
#
# It touches documentElement (body does not exist yet) and validates the stored
# value, which is why junk in that key never reaches the attribute at all: the
# page then behaves exactly as it does with nothing stored, following the system,
# and the first tap flips it normally. WITHOUT the validation the junk would land
# on the attribute, and the handler — which asks whether it reads "dark", not
# whether it is valid — would take it for light and write "dark", i.e. no visible
# change at all on a system that was already dark.
#
# Every storage access is wrapped. It is first-party here (the webui frames this
# on the same host, and partitioning ignores the port), but a private window, a
# policy, or an opaque origin can still make even a READ throw — in which case the
# button works for this page and the next page starts from the system preference.
_THEME_KEY = "rs-reader-theme"

_THEME_HEAD_JS = """
(function () {
  try {
    var t = window.localStorage.getItem('%(key)s');
    if (t === 'dark' || t === 'light') {
      document.documentElement.setAttribute('data-theme', t);
    }
  } catch (e) { /* storage unavailable: follow the system, as before */ }
})();
function rdToggleTheme() {
  var root = document.documentElement;
  var dark = root.getAttribute('data-theme') === 'dark'
    || (!root.hasAttribute('data-theme')
        && window.matchMedia('(prefers-color-scheme: dark)').matches);
  var next = dark ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  try { window.localStorage.setItem('%(key)s', next); } catch (e) { /* this page only */ }
}
""" % {"key": _THEME_KEY}

_THEME_BUTTON_HTML = (
    '<div class="rd-theme-bar">'
    '<button class="rd-theme" type="button" onclick="rdToggleTheme()"'
    ' aria-label="Switch between light and dark theme">'
    '<span class="rd-to-dark">\u263e<span class="rd-word"> Dark</span></span>'
    '<span class="rd-to-light">\u2600<span class="rd-word"> Light</span></span>'
    '</button></div>')

_VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'


def _page(title: str, body_html: str) -> bytes:
    """Every document this server renders itself — listings, markdown, text and
    the error pages. (A notebook is nbconvert's own document and does not pass
    through here, so it carries neither the button nor the palette.)"""
    return (f"<!doctype html><html><head><meta charset='utf-8'>{_VIEWPORT}"
            f"<title>{html.escape(title)}</title><style>{_MOBILE_CSS}</style>"
            f"<script>{_THEME_HEAD_JS}</script>"
            f"</head><body>{_THEME_BUTTON_HTML}{body_html}"
            f"</body></html>").encode("utf-8")


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


def _browser_renders(name: str) -> bool:
    """True for a file whose type the browser displays better than escaped
    source (see _RAW_MIME_TYPES) — checked before the content sniff."""
    mime = mimetypes.guess_type(name)[0] or ""
    return mime in _RAW_MIME_TYPES or mime.startswith(_RAW_MIME_PREFIXES)


def _looks_like_text(path: Path) -> bool:
    """Content sniff: a file with no NUL byte in its first chunk is text."""
    try:
        with open(path, "rb") as f:
            return b"\0" not in f.read(READER_TEXT_SNIFF_BYTES)
    except OSError:
        return False


def _render_text(path: Path, rel: str) -> bytes:
    """Render a text file as a plain page: escaped into a <pre>, wrapped in the
    same mobile shell as markdown. Unlike markdown this content is NEVER trusted
    markup — a source file full of angle brackets must read as source, and a
    workspace can hold files a worker or a cloned repo wrote."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return _page(rel or "workspace",
                 _breadcrumb(rel)
                 + f"<pre><code>{html.escape(text)}</code></pre>")


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
        # _resolve unquotes (its callers hand it URL sub-paths), so a real filename
        # containing '%' must be quoted on the way in or it is decoded as an escape
        # — which yields a dead link, or points the row at an unrelated file, or
        # drops the row. Rare in a curated research tree; ordinary in a repo clone.
        try:
            quoted = urllib.parse.quote(child_rel)
        except UnicodeEncodeError:
            # scandir decodes names with surrogateescape, so a name carrying
            # non-UTF-8 bytes has no URL form at all. Skip the row: before the
            # deny-list, only the handful of agent-written folders could hold such
            # a name, and now one foreign file anywhere (a clone, a share dropped
            # in shared/data/) would otherwise take the whole listing down with an
            # unsent response rather than lose one row.
            continue
        got = _resolve(quoted)                  # re-classify on realpath
        if got is None:
            continue
        is_dir = e.is_dir()
        href = ("/tree/" if is_dir else "/view/") + urllib.parse.quote(got[1])
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
        # _resolve already applied the deny-list; this guard is the directory
        # check. It must stay: without it a /view/ or /raw/ of a DIRECTORY reaches
        # the raw path, which sends Content-Length + a content type and only then
        # hits IsADirectoryError — headers already on the wire, so the OSError
        # swallow below would turn a clean 404 into a truncated body.
        if not real.is_file():
            return self._fail(404, "not found")
        try:
            st = real.stat()
        except OSError:
            return self._fail(404, "not found")
        cached = _CACHE.get(rel, st.st_mtime_ns, st.st_size)
        if cached is not None:
            return self._send(200, cached[0], cached[1])
        suffix = real.suffix.lower()
        body = None
        ctype = "text/html; charset=utf-8"
        # Render INSIDE the try; hand off to raw OUTSIDE it. The hand-off writes
        # headers itself, so leaving it in here would let a later failure append a
        # 422 to a response already on the wire — the same "headers are already
        # sent" hazard the is_file() guard above exists for.
        try:
            if suffix == ".md":
                body = _render_markdown(real.read_text(errors="replace"), rel)
            elif suffix == ".ipynb":
                body = _render_notebook(real, rel)
            elif (not _browser_renders(real.name)
                    and st.st_size <= READER_TEXT_RENDER_MAX_BYTES
                    and _looks_like_text(real)):
                body = _render_text(real, rel)          # text, small enough
        except Exception as exc:  # a corrupt notebook / bad markdown must not 500 opaque
            log.warning("render failed for %s: %s", rel, exc)
            return self._fail(422, "could not render this file")
        if body is None:
            # Images, documents the browser renders, binaries, oversized text:
            # hand off to raw, which streams rather than buffering.
            return self._route_raw(sub)
        _CACHE.put(rel, st.st_mtime_ns, st.st_size, body, ctype)
        self._send(200, body, ctype)

    def _route_raw(self, sub: str) -> None:
        got = _resolve(sub)
        if got is None:
            return self._fail(404, "not found")
        real, rel = got
        if not real.is_file():          # see the same guard in _route_view
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
