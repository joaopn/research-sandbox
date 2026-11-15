#!/usr/bin/env python3
"""rs-sync — mirror a Gitea fork's issues and merged pull requests to GitHub.

Staged into rs-fetch surfaces beside rs-fetch. Run by hand:

    rs-sync <gitea owner>/<repo> <github owner>/<repo>            # dry run
    rs-sync <gitea owner>/<repo> <github owner>/<repo> --apply    # do it
    rs-sync <gitea owner>/<repo> <github owner>/<repo> --remove [--apply]

--skip wiki and --skip board leave a part out; --wiki-overwrite lets a run
commit (or a remove force-push) past edits made on the GitHub wiki.

What goes over, one way (Gitea -> GitHub):
  * every closed issue, and every open issue that carries none of the
    labels in RS_SYNC_HOLD_LABELS, with its comments, as ONE dated import
    through GitHub's Issue Import API (the only GitHub route that keeps the
    original creation, close and comment dates);
  * every merged pull request, as a closed, locked issue titled
    "[PR] <title>", labelled "merged-pr", listing the commits it landed as;
  * the Gitea labels the mirrored items carry, each tagged " (rs-sync)";
  * the fork's wiki: the GitHub wiki's tree is replaced by the converted Gitea
    tree, one commit per run whose message ends with a marker line;
  * the fork's boards: one GitHub project with a Status and an Area field,
    every open mirrored issue that is a card an item in the same column.
Every mirrored item is locked. Later runs bring GitHub level with Gitea:
new comments, edits, closes and label changes go over dated at the run.

Numbers are never assumed to match. Every mirrored body and comment ends with
a hidden marker (an HTML comment naming the fork, its repo id, the Gitea
number and, for a comment, its kind and id). Each run rebuilds the table
Gitea number -> GitHub number from those markers, on items authored by the
token's own GitHub account, and rewrites every reference through it. A
reference to something not mirrored becomes a code span GitHub does not
link. Nothing is stored locally: the markers are the record, so --remove
needs only GitHub.

The GitHub token is read from ~/.rs-sync.env (or --env):
    RS_SYNC_GITHUB_TOKEN=ghp_...        a classic token, repo/public_repo
    RS_SYNC_HOLD_LABELS=severity:*      required; comma-separated label names
                                        or prefixes ending in "*"; "*" alone
                                        holds every open issue; an empty
                                        value holds nothing
    RS_SYNC_HEADER=on|off               optional, default off
The Gitea side is read with the read-only issue token staged at
~/.dev-tokens/issue-reader.token.

Stdlib only: this file is staged into containers and imports no host module.
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import fcntl
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

# --- wiring (lockstep with cli/gitea.py GITEA_CONTAINER / GITEA_INNER_PORT,
# as rs_fetch.py; this file cannot import host modules) -----------------------
GITEA_HOST = "rs-gitea"
GITEA_PORT = "3000"
GITEA_BASE = f"http://{GITEA_HOST}:{GITEA_PORT}"
# The read-only issue token the host stages beside the operator token.
GITEA_TOKEN_PATH = Path.home() / ".dev-tokens" / "issue-reader.token"
TOKENS_DIR = Path.home() / ".dev-tokens"
DEFAULT_ENV = Path.home() / ".rs-sync.env"
GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"
# The product's home, linked from the optional header line.
RS_HOME = "https://github.com/joaopn/research-sandbox"

# Test-only overrides (the harness points rs-sync at a throwaway Gitea and a
# fake GitHub). Unset in every real container.
GITEA_BASE = os.environ.get("RS_SYNC_TEST_GITEA_URL") or GITEA_BASE
GITHUB_API = os.environ.get("RS_SYNC_TEST_GITHUB_API") or GITHUB_API
# The GitHub wiki is <this>/<owner>/<repo>.wiki.git (a local folder of bare
# repos in the harness).
GITHUB_WIKI_BASE = os.environ.get("RS_SYNC_TEST_GITHUB_WIKI_BASE") or GITHUB_WEB
if os.environ.get("RS_SYNC_TEST_GITEA_TOKEN_FILE"):
    GITEA_TOKEN_PATH = Path(os.environ["RS_SYNC_TEST_GITEA_TOKEN_FILE"])
_TEST_FAST = os.environ.get("RS_SYNC_TEST_FAST") == "1"

# Bounds. Gitea answers at most 50 rows a page whatever `limit` asks
# (measured on 28.2.0); GitHub at most 100.
API_TIMEOUT_S = 15            # lockstep with rs_fetch / gitea.py quick calls
GITEA_PAGE = 50
GITHUB_PAGE = 100
# GitHub's body limit: "body is too long (maximum is 65536 characters)".
# Counted in UTF-8 bytes, never below the character count, so conservative.
GITHUB_BODY_MAX = 65536
# A GitHub label description holds at most 100 characters.
GITHUB_LABEL_DESC_MAX = 100
# GitHub's documented secondary limits for content-creating requests
# ("Rate limits for the REST API": no more than 80 per minute and 500 per
# hour), and its documented wait when a limit answer carries no retry-after
# header ("wait for at least one minute before retrying").
CONTENT_PER_MINUTE = 80
CONTENT_PER_HOUR = 500
# GitHub's REST best practices: "wait at least one second between each
# request" that creates or changes content, for one user.
MIN_WRITE_GAP_S = 1.0
# GitHub refuses an issue title over 256 characters ("title is too long").
GITHUB_TITLE_MAX = 256
NO_HEADER_WAIT_S = 60
# The longest total wait one request may spend on limit answers before the
# run stops: GitHub's hourly window. A longer wait means the limit is not
# clearing, and the run is resumed later instead.
RATE_WAIT_MAX_S = 3600
# Import polling. GitHub processes an import in the background; the probe
# saw imports finish in seconds. Poll every 2 s for up to 5 minutes: at half
# the deadline a slow-but-healthy import of an issue with many comments could
# be abandoned (it would then block the next run until it lands, which the
# start-of-run check handles); at ten times it, a stuck import would hold the
# terminal for an hour.
IMPORT_POLL_S = 2
IMPORT_DEADLINE_S = 300

MARKER_TAG = "rs-sync v1"
LABEL_TAG = " (rs-sync)"
PR_LABEL = "merged-pr"
PR_LABEL_COLOR = "6f42c1"
PR_LABEL_DESC = "a merged pull request from the private dev repo"
HEADER_LINE = f"*mirrored from a private dev repo with [rs-sync]({RS_HOME})*"
CONTINUED = "(continued in the next comment)"
NAME_RE = r"[A-Za-z0-9._-]+"
IMPORT_ACCEPT = "application/vnd.github.golden-comet-preview+json"

REPORT: dict = {}


class SyncError(Exception):
    """A refusal or failure that stops the run, with what to do."""


def die(msg: str) -> None:
    raise SyncError(msg)


# ============================================================================
# The env file
# ============================================================================

ENV_KEYS = {"RS_SYNC_GITHUB_TOKEN", "RS_SYNC_HOLD_LABELS", "RS_SYNC_HEADER"}


def read_env(path: Path) -> dict:
    """Parse the env file, refusing an unsafe or malformed one. Opened without
    following links and without blocking (a planted FIFO cannot hang it); must
    be a regular file owned by this user with no group or other bits."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        die(f"the settings file {path} does not exist. Create it with these "
            f"lines (and chmod 600 it):\n  RS_SYNC_GITHUB_TOKEN=<a classic "
            f"GitHub token with repo or public_repo>\n  RS_SYNC_HOLD_LABELS="
            f"<labels whose open issues stay off GitHub, e.g. severity:*>")
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
    finally:
        os.close(fd)
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
                f"{', '.join(sorted(ENV_KEYS))})")
        if k in out:
            die(f"{path} line {n}: {k} is set twice")
        out[k] = v
    if not out.get("RS_SYNC_GITHUB_TOKEN"):
        die(f"{path}: RS_SYNC_GITHUB_TOKEN is missing")
    if "RS_SYNC_HOLD_LABELS" not in out:
        die(f"{path}: RS_SYNC_HOLD_LABELS is required: it names the labels "
            f"whose open issues stay off GitHub (e.g. severity:*); an empty "
            f"value publishes every open issue")
    out["hold"] = parse_hold(out["RS_SYNC_HOLD_LABELS"], path)
    header = out.get("RS_SYNC_HEADER", "off")
    if header not in ("on", "off"):
        die(f"{path}: RS_SYNC_HEADER must be on or off, not {header!r}")
    out["header"] = header == "on"
    return out


def parse_hold(value: str, path="RS_SYNC_HOLD_LABELS") -> list:
    """Hold entries: exact names or prefixes ending in '*', compared ignoring
    case. '' holds nothing; an empty entry ('a,,b', a trailing comma) is
    refused."""
    if value.strip() == "":
        return []
    out = []
    for raw in value.split(","):
        e = raw.strip()
        if not e:
            die(f"{path}: RS_SYNC_HOLD_LABELS has an empty entry")
        if "*" in e[:-1]:
            die(f"{path}: RS_SYNC_HOLD_LABELS entry {e!r}: '*' may only end "
                f"an entry")
        out.append(e.lower())
    return out


def held(labels, hold) -> bool:
    if "*" in hold:                  # "*" alone: every open issue, labelled or not
        return True
    for lab in labels:
        name = lab.lower()
        for h in hold:
            if h.endswith("*"):
                if name.startswith(h[:-1]):
                    return True
            elif name == h:
                return True
    return False


# ============================================================================
# Markers
# ============================================================================

_MARKER_RE = re.compile(
    r"^<!-- rs-sync v1 gitea:(?P<owner>%s)/(?P<repo>%s)@(?P<rid>\d+)#(?P<n>\d+)"
    r"(?: (?P<kind>comment|review|review-comment):(?P<cid>\d+))?"
    r"(?: part:(?P<part>\d+))? -->$" % (NAME_RE, NAME_RE))


def marker(owner, repo, rid, n, kind=None, cid=None, part=None) -> str:
    s = f"<!-- {MARKER_TAG} gitea:{owner}/{repo}@{rid}#{n}"
    if kind:
        s += f" {kind}:{cid}"
    if part:
        s += f" part:{part}"
    return s + " -->"


def parse_marker(body):
    """The marker on the LAST non-empty line, or None."""
    if not isinstance(body, str):
        return None
    lines = [ln for ln in body.replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return None
    m = _MARKER_RE.match(lines[-1].strip())
    if not m:
        return None
    d = m.groupdict()
    return {"owner": d["owner"], "repo": d["repo"], "rid": int(d["rid"]),
            "n": int(d["n"]), "kind": d["kind"],
            "cid": int(d["cid"]) if d["cid"] else None,
            "part": int(d["part"]) if d["part"] else None}


# ============================================================================
# The text conversion
# ============================================================================

class Ctx:
    """What a conversion needs: the fork, the target and the number table."""

    def __init__(self, owner, repo, target, table, wiki=False):
        self.owner, self.repo, self.target = owner, repo, target
        self.table = table            # Gitea number -> GitHub number
        # A wiki page: GitHub does not link #N there, so a mapped reference
        # is written as the mirrored issue's URL; relative links resolve
        # against the fork's wiki.
        self.wiki = wiki

    def gh_url(self, m):
        return f"https://github.com/{self.target}/issues/{m}"


# How a reference or a mention is kept from linking: a zero-width space after
# the "#" or the "@". Measured on GitHub (the maintainer's probe): "#​1"
# is not linked, while a code span, an escape or an entity around it is not
# reliably enough (they depend on where GitHub's markdown parser draws
# boundaries). Nothing here adds backticks, so no added text can pair with
# the author's. The cost is cosmetic: copying the text carries the invisible
# character.
ZW = "​"
NOT_MIRRORED_LINK = "(link to the private gitea, not mirrored)"
NOT_MIRRORED_IMAGE = "(image not mirrored)"

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LIST_RE = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:\s|$)")


def _fence_open(line: str):
    """The fence a line opens, or None. A backtick fence whose info string
    holds a backtick is ordinary text (CommonMark)."""
    m = _FENCE_RE.match(line)
    if not m:
        return None
    run = m.group(1)
    if run[0] == "`" and "`" in line[m.end():]:
        return None
    return run


def _fence_closes(line: str, fence: str) -> bool:
    m = _FENCE_RE.match(line)
    return bool(m) and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
        and line.strip() == m.group(1)


# Every region of the text is made safe, whatever GitHub's parser decides it
# is: no region passes a linkable "#digit", "GH-digit" or "@name" through.
#   * plain text: references to mirrored items map to their GitHub numbers;
#     every other linkable form gets a zero-width space (_neutralise);
#   * fenced code: only the zero-width space (code looks the same);
#   * URLs and link targets: "@" -> %40 and "#digit" -> %23digit, so the link
#     still works (a purely numeric anchor excepted) but cannot link or ping;
#   * image alt text and link titles: converted like plain text;
#   * where something is dropped or unwrapped, a zero-width space keeps the
#     text on either side from joining into a new reference.

# An entity that renders as one of the characters a reference is made of.
# A hex reference may lack its ";" (an HTML block decodes "&#x40o" as "@o");
# a decimal one must have it ("&#5 " is the text "&#5", whose "#5" the plain
# rule below catches).
_ENT = r"&#[0-9]+;|&#[xX][0-9a-fA-F]+;?|&num;|&commat;"
_ENT_RE = re.compile(_ENT)
# The longest entity _ENT can match that still names a real code point.
_ENT_MAX = len("&#x10FFFF;")


def _ent_char(e: str) -> str:
    named = {"&num;": "#", "&commat;": "@"}
    if e in named:
        return named[e]
    body = e[2:].rstrip(";")
    try:
        n = int(body[1:], 16) if body[:1] in "xX" else int(body)
        return chr(n)
    except (ValueError, OverflowError):
        return ""


def _is_digit(c: str) -> bool:
    return len(c) == 1 and "0" <= c <= "9"


def _is_alnum(c: str) -> bool:
    return len(c) == 1 and c.isascii() and c.isalnum()


_G = r"(?:[Gg]|&#0*(?:71|103);|&#[xX]0*(?:47|67);?)"
_H = r"(?:[Hh]|&#0*(?:72|104);|&#[xX]0*(?:48|68);?)"
_DASH = r"(?:\\?-|&#0*45;|&#[xX]0*2[dD];?)"
_NEUTRAL_RE = re.compile(r"(?P<gh>" + _G + _H + _DASH + r")|(?P<ent>" + _ENT + r")|(?P<hash>\\?#)|(?P<at>@)")


def _neutralise(s: str, shields: bool = False) -> str:
    """Insert a zero-width space after every "#", "GH-" or "@" (in any
    spelling, entities included) that is followed by what would make GitHub
    link or ping: a digit after "#"/"GH-", a letter or digit after "@" with
    no letter or digit before it. Nothing else changes."""
    starts, ends = [], []
    if shields:
        for m in re.finditer(SHIELD_OPEN + "[^" + SHIELD_CLOSE + "]*" + SHIELD_CLOSE, s):
            starts.append(m.start())
            ends.append(m.end())
    spans = bool(starts)

    def shielded(i):
        k = bisect.bisect_right(starts, i) - 1     # spans are sorted and disjoint
        return k >= 0 and i < ends[k]

    def nxt(i):
        while i < len(s) and s[i] in (SHIELD_OPEN, SHIELD_CLOSE):
            i += 1
        if s[i:i + 1] == WREF_OPEN:
            return "h"          # a wiki reference may become a URL: treat it as one
        m = _ENT_RE.match(s, i)
        return _ent_char(m.group(0)) if m else s[i:i + 1]

    def before_ok(i):
        # the character before position i, through an entity if one ends there
        j = s.rfind("&", max(0, i - _ENT_MAX), i)
        if j >= 0 and _ENT_RE.fullmatch(s, j, i):
            c = _ent_char(s[j:i])
        else:
            c = s[i - 1:i]
        return not _is_alnum(c)

    def sub(m):
        t, end = m.group(0), m.end()
        if spans and shielded(m.start()):
            return t
        if m.group("ent"):
            c = _ent_char(t)
            if c == "#" and _is_digit(nxt(end)):
                return t + ZW
            if c == "@" and _is_alnum(nxt(end)) and before_ok(m.start()):
                return t + ZW
            return t
        if m.group("gh"):
            return t + ZW if _is_digit(nxt(end)) and before_ok(m.start()) else t
        if m.group("hash"):
            return t + ZW if _is_digit(nxt(end)) else t
        return t + ZW if _is_alnum(nxt(end)) and before_ok(m.start()) else t
    return _NEUTRAL_RE.sub(sub, s)


def _url_safe(u: str) -> str:
    """A URL that keeps working but cannot link a reference or ping."""
    u = re.sub(r"(?<![A-Za-z0-9])@(?=[A-Za-z0-9])", "%40", u)
    u = re.sub(r"#(?=[0-9])", "%23", u)
    u = re.sub(r"(?<![A-Za-z0-9])([Gg][Hh])-(?=[0-9])", r"\1%2D", u)
    return _neutralise(u)


_TITLE = r"(?:[ \t]+(?:\n[ \t]*)?(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?"
_INLINE_RE = re.compile(
    r"(?P<htmlc><!--[\s\S]*?-->)"
    r"|(?P<img>!\[(?P<alt>[^\]\n]*)\]\((?P<isrc>[^)\s]+)(?P<ititle>" + _TITLE + r")\))"
    r"|(?P<link>\[(?P<ltext>[^\]\n]*)\]\((?P<lhref>[^)\s]+)(?P<ltitle>" + _TITLE + r")\))"
    r"|(?P<refdef>(?m:^)[ ]{0,3}\[(?P<rdlabel>[^\]\n]+)\]:[ \t]*"
    r"(?P<rdurl><[^>\n]*>|(?:https?://|www\.|\.{0,2}/)\S*))"
    r"|(?P<attr>\b(?P<aname>href|src)[ \t]*=[ \t]*(?P<q>[\"'])(?P<aurl>[^\"'\n]*)(?P=q))"
    r"|(?P<auto><(?:https?://|www\.)[^>\s]+>)"
    r"|(?P<url>(?<![^\s*_~(])(?:https?://|www\.)[^\s<>()\[\]]+)",
    re.I)
# Lookarounds use [A-Za-z0-9], never \w: "_" is a word character, and GitHub
# renders _#5_ as italics around a linked #5.
_AN = "A-Za-z0-9"
_REF_RE = re.compile(
    r"(?P<slug>(?<![%(an)s._/-])(?P<su>_*)(?P<so>[A-Za-z0-9][A-Za-z0-9._-]*)/(?P<sr>%(n)s)#(?P<sn>[0-9]+)(?![%(an)s]|&#))"
    r"|(?P<hash>(?<![%(an)s&])\\?#(?P<hn>[0-9]+)(?![%(an)s]|&#))"
    r"|(?P<gh>(?<![%(an)s-])GH-(?P<gn>[0-9]+)(?![%(an)s]|&#))"
    r"|(?P<bang>(?<![%(an)s!\]])!(?P<bn>[0-9]+)(?![%(an)s]|&#))"
    % {"an": _AN, "n": NAME_RE})
# Text that must stay a live reference (a mapped "#N", another repo's
# "owner/repo#N") is held between SHIELD_OPEN and SHIELD_CLOSE until the final
# pass (_finish), which neutralises everything outside the shields and then
# drops them. Private-use characters, stripped from the input.
SHIELD_OPEN, SHIELD_CLOSE = "\ue000", "\ue001"
# A wiki page's mapped reference waits as WREF_OPEN url WREF_SEP number
# WREF_CLOSE until the final pass can see what follows it (private-use too).
WREF_OPEN, WREF_SEP, WREF_CLOSE = "\ue002", "\ue003", "\ue004"
_PRIVATE = (SHIELD_OPEN, SHIELD_CLOSE, WREF_OPEN, WREF_SEP, WREF_CLOSE)
_WREF_RE = re.compile(re.escape(WREF_OPEN) + r"([^\ue002-\ue004]*)" + re.escape(WREF_SEP)
                      + r"([0-9]+)" + re.escape(WREF_CLOSE))
# GitHub's autolink ends a bare URL at whitespace or "<" and then drops
# trailing ? ! . , : * _ ~ and an unmatched ")": only those may follow the
# URL, or the link would swallow them and lead nowhere (#5/#6, #5's, #5.1).
_URL_TAIL_OK = re.compile(r"[?!.,:*_~)]*")
# cmark-gfm ends an autolink at ASCII whitespace only (a no-break or
# ideographic space is part of the link), or at "<".
_URL_END = " \t\n\r\f\v<"


def _strip_private(t: str) -> str:
    for c in _PRIVATE:
        t = t.replace(c, "")
    return t


def _shield(t: str) -> str:
    return SHIELD_OPEN + t + SHIELD_CLOSE


def _wiki_ref(m) -> str:
    # Scan only the trimmed punctuation: the scan stops at the first other
    # character (the next reference's marker included), so each one is
    # bounded by the gap to the next and the pass stays linear.
    s, t = m.string, _URL_TAIL_OK.match(m.string, m.end()).end()
    clean = t == len(s) or s[t] in _URL_END
    return m.group(1) if clean else "#" + m.group(2)


def _finish(s: str) -> str:
    """The final pass over a whole converted text: neutralise every join the
    piecewise conversion made (an "@" from one piece before a name from the
    next, say), leaving shielded references alone, then drop the shields. A
    wiki page's mapped reference becomes the mirror's URL where GitHub's
    autolink ends it cleanly, else the plain #M (right number, unlinked)."""
    s = _neutralise(s, shields=True)
    # An "&" (or "&#") right before a shielded reference would read as an
    # entity with it ("&" + "#64" + ";" is "@", "&#" + "x40rg/…" is "@rg/…"):
    # a zero-width space keeps them apart.
    s = re.sub("(&#?)" + SHIELD_OPEN, "\\1" + ZW + SHIELD_OPEN, s)
    s = s.replace(SHIELD_OPEN, "").replace(SHIELD_CLOSE, "")
    return _WREF_RE.sub(_wiki_ref, s) if WREF_OPEN in s else s
_GITEA_HOST_RE = re.compile(r"^https?://%s(?::\d+)?(/.*)?$" % re.escape(GITEA_HOST), re.I)


def _ref(ctx: Ctx, n: int) -> str:
    """A reference to Gitea #n: its GitHub number, or neutralised when
    unmapped. In a wiki page (where GitHub does not link #N) the mapped form
    is the mirrored issue's URL, which the wiki links by itself: no link
    syntax is added, so nothing can nest inside a link's text or show as
    markup in code."""
    m = ctx.table.get(n)
    if not m:
        return f"gitea#{ZW}{n}"
    if ctx.wiki:
        return _shield(WREF_OPEN + ctx.gh_url(m) + WREF_SEP + str(m) + WREF_CLOSE)
    return _shield(f"#{m}")


def _gitea_path(url: str, ctx: "Ctx | None" = None):
    """The path of a URL on the Gitea address, or None. A relative link
    (/attachments/…, /<owner>/<repo>/issues/N) is Gitea's too: Gitea renders
    it against its own address. So is the fork's path on any address that is
    not GitHub (a link copied from the webui's Gitea tab)."""
    if url.startswith("/") and not url.startswith("//"):
        return url
    if ctx is not None and ctx.wiki and url and not url.startswith(("#", "//")) \
            and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url) \
            and not url.lower().startswith("www."):
        # A wiki page's relative link: Gitea resolves it against the fork's
        # wiki. Another page or a file of the wiki stays as it is (GitHub's
        # wiki resolves it the same way); anything above the wiki
        # (../issues/5) is a Gitea path.
        root = f"/{ctx.owner}/{ctx.repo}/wiki"
        path = posixpath.normpath(root + "/" + re.split(r"[?#]", url, maxsplit=1)[0])
        if path == root or path.startswith(root + "/"):
            return None
        return path
    m = _GITEA_HOST_RE.match(url)
    if m:
        return m.group(1) or "/"
    if ctx is not None:
        m = re.match(r"^(?:https?://)?([^/\s]+)(/.*)$", url, re.I)
        if m and m.group(1).lower() not in ("github.com", "www.github.com") \
                and m.group(2).lower().startswith(f"/{ctx.owner}/{ctx.repo}/".lower()):
            return m.group(2)
    return None


def _gitea_item_url(ctx: Ctx, path: str):
    """A link to an issue, PR or comment of the fork -> its GitHub URL (str
    starting with http) or the neutral reference; any other Gitea path ->
    None."""
    m = re.match(r"^/(%s)/(%s)/(?:issues|pulls)/(\d+)\b" % (NAME_RE, NAME_RE), path)
    if not m or (m.group(1).lower(), m.group(2).lower()) != (ctx.owner.lower(), ctx.repo.lower()):
        return None
    n = int(m.group(3))
    g = ctx.table.get(n)
    return _shield(ctx.gh_url(g)) if g else f"gitea#{ZW}{n}"


def _is_link(item) -> bool:
    """A mapped GitHub URL from _gitea_item_url (shielded)."""
    return bool(item) and item.startswith(SHIELD_OPEN + "http")


def _target_issue_url(ctx: Ctx, url: str) -> bool:
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/(%s)/(%s)/(?:issues|pull)/\d+"
                 % (NAME_RE, NAME_RE), url, re.I)
    return bool(m) and f"{m.group(1)}/{m.group(2)}".lower() == ctx.target.lower()


def _neutral_url(url: str) -> str:
    """A GitHub URL that must create no cross-reference: the host is broken
    with a zero-width space (GFM may still autolink the text, to an address
    that leads nowhere)."""
    return re.sub(r"(github)(\.com)", r"\1" + ZW + r"\2", url, count=1, flags=re.I)


def _convert_url(ctx: Ctx, url: str) -> str:
    path = _gitea_path(url, ctx)
    if path is not None:
        item = _gitea_item_url(ctx, path)
        return item if item else NOT_MIRRORED_LINK
    if _target_issue_url(ctx, url):
        return _neutral_url(url)
    return _url_safe(url)


def _convert_refs(ctx: Ctx, text: str) -> str:
    """Plain text: references to the fork map (or are neutralised); every
    other linkable form in the text between them is neutralised."""
    out, pos = [], 0
    for m in _REF_RE.finditer(text):
        out.append(_neutralise(text[pos:m.start()]))
        pos = m.end()
        if m.group("slug"):
            # A match starts only where a run of name characters starts (so
            # each character is read a constant number of times); leading
            # underscores (emphasis) are written back as they are, outside the
            # shield.
            su, so, sr, n = m.group("su"), m.group("so"), m.group("sr"), int(m.group("sn"))
            out.append(su)
            if (so.lower(), sr.lower()) == (ctx.owner.lower(), ctx.repo.lower()):
                out.append(_ref(ctx, n))
            elif sr.lower() == ctx.repo.lower() or f"{so}/{sr}".lower() == ctx.target.lower():
                out.append(f"{so}/{sr}#{ZW}{n}")
            elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", so):
                out.append(_shield(f"{so}/{sr}#{m.group('sn')}"))   # another repo: kept exactly
            else:
                out.append(f"{so}/{sr}#{ZW}{n}")        # an owner GitHub cannot have
        elif m.group("hash"):
            out.append(_ref(ctx, int(m.group("hn"))))
        elif m.group("gh"):
            out.append(_ref(ctx, int(m.group("gn"))))
        else:
            out.append(_ref(ctx, int(m.group("bn"))))
    out.append(_neutralise(text[pos:]))
    return "".join(out)


def _title(ctx: Ctx, t: str) -> str:
    return _convert_inline(ctx, t) if t else ""


def _convert_inline(ctx: Ctx, text: str) -> str:
    out, pos = [], 0
    for m in _INLINE_RE.finditer(text):
        out.append(_convert_refs(ctx, text[pos:m.start()]))
        pos = m.end()
        if m.group("htmlc"):
            # The inside is converted too: GitHub reads some "comments" (with
            # "--" inside, say) as plain text. A dropped forged marker leaves a
            # zero-width space, so the text around it cannot join.
            if m.group("htmlc").startswith("<!-- rs-sync"):
                out.append(ZW)
            else:
                out.append("<!--" + _convert_inline(ctx, m.group("htmlc")[4:-3]) + "-->")
        elif m.group("img"):
            if _gitea_path(m.group("isrc"), ctx) is not None:
                out.append(ZW + NOT_MIRRORED_IMAGE)
            else:
                out.append(f"![{_convert_inline(ctx, m.group('alt'))}]"
                           f"({_url_safe(m.group('isrc'))}{_title(ctx, m.group('ititle'))})")
        elif m.group("link"):
            href = m.group("lhref")
            ltext = _convert_inline(ctx, m.group("ltext"))
            path = _gitea_path(href, ctx)
            if path is not None:
                item = _gitea_item_url(ctx, path)
                if _is_link(item):
                    out.append(f"[{ltext}]({item})")
                else:
                    out.append(f"{ZW}{ltext} {item or NOT_MIRRORED_LINK}")
            elif _target_issue_url(ctx, href):
                out.append(f"{ZW}{ltext} {_neutral_url(href)}")
            else:
                out.append(f"[{ltext}]({_url_safe(href)}{_title(ctx, m.group('ltitle'))})")
        elif m.group("refdef"):
            raw = m.group("rdurl")
            angled = raw.startswith("<") and raw.endswith(">")
            u = raw[1:-1] if angled else raw
            path = _gitea_path(u, ctx)
            if path is not None:
                item = _gitea_item_url(ctx, path)
                u = item if _is_link(item) else "#"
            elif _target_issue_url(ctx, u):
                u = "#"
            else:
                u = _url_safe(u)
            label = _convert_inline(ctx, m.group("rdlabel"))
            lead = m.group(0)[:m.start("rdlabel") - m.start() - 1]
            out.append(f"{lead}[{label}]: " + (f"<{u}>" if angled else u))
        elif m.group("attr"):
            u = m.group("aurl")
            path = _gitea_path(u, ctx)
            if path is not None:
                item = _gitea_item_url(ctx, path)
                u = item if _is_link(item) else ""
            elif _target_issue_url(ctx, u):
                u = "#"
            else:
                u = _url_safe(u)
            q = m.group("q")
            out.append(f"{m.group('aname')}={q}{u}{q}")
        elif m.group("auto"):
            u = _convert_url(ctx, m.group("auto")[1:-1])
            live = (u.lower().startswith(("http://", "https://")) or _is_link(u)) and ZW not in u
            out.append(f"<{u}>" if live else u)
        else:
            out.append(_convert_url(ctx, m.group("url")))
    out.append(_convert_refs(ctx, text[pos:]))
    return "".join(out)


def convert(ctx: Ctx, text) -> str:
    """Convert Gitea markdown for GitHub. Fenced code blocks outside lists
    get only the zero-width space (_neutralise), so code looks the same and,
    should GitHub read the lines as text after all, nothing links. Everything
    else is converted (see the region list above)."""
    if not text:
        return ""
    text = _strip_private(text)
    lines = text.replace("\r\n", "\n").split("\n")
    out, buf = [], []
    fence = None
    prev_blank = True
    in_list = False

    def flush():
        if buf:
            out.append(_convert_inline(ctx, "\n".join(buf)))
            buf.clear()

    for line in lines:
        if fence is not None:
            out.append(_neutralise(line))
            if _fence_closes(line, fence):
                fence = None
            prev_blank = False
            continue
        if line.strip() and prev_blank and not line[:1].isspace() and not _LIST_RE.match(line):
            in_list = False
        f = None if in_list else _fence_open(line)
        if f:
            flush()
            fence = f
            out.append(_neutralise(line))
            prev_blank = False
            continue
        if not line.strip():
            buf.append(line)
            prev_blank = True
            continue
        if _LIST_RE.match(line):
            in_list = True
        buf.append(line)
        prev_blank = False
    flush()
    return _finish("\n".join(out))


# ============================================================================
# Bodies and splitting
# ============================================================================

def _blocks(text: str) -> list:
    """Top-level blocks: blank-line separated, never splitting a fenced code
    block or a run of consecutive non-blank lines (a list, a table)."""
    blocks, cur, fence = [], [], None
    for line in text.split("\n"):
        if fence is not None:
            cur.append(line)
            if _fence_closes(line, fence):
                fence = None
            continue
        f = _fence_open(line)
        if f:
            fence = f
            cur.append(line)
            continue
        if not line.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _size(s: str) -> int:
    return len(s.encode("utf-8"))


def assemble(prefix: str, text: str, mark_for) -> list:
    """The body parts of one item. `prefix` (header and author line) goes
    first, then the text; every part ends with its marker as the last line
    (`mark_for(k)`: k=None for the first part, 1.. for continuations). Only
    the first part carries the prefix. Split at top-level blocks when over
    GITHUB_BODY_MAX; a single block over the cap stops the run."""
    def build(parts_text, k, more):
        body = parts_text
        if more:
            body = (body + "\n\n" if body else "") + CONTINUED
        return (body + "\n\n" if body else "") + mark_for(k)

    whole = prefix + text
    first = build(whole, None, False)
    if _size(first) <= GITHUB_BODY_MAX:
        return [first]
    blocks = _blocks(text)
    parts, cur, k = [], prefix.rstrip("\n"), None
    for b in blocks:
        trial = (cur + "\n\n" + b) if cur else b
        if _size(build(trial, k, True)) <= GITHUB_BODY_MAX:
            cur = trial
            continue
        if not cur or cur == prefix.rstrip("\n"):
            die(f"a single block of {_size(b)} bytes is over GitHub's "
                f"{GITHUB_BODY_MAX}-byte limit and cannot be split")
        parts.append(build(cur, k, True))
        k = (k or 0) + 1
        cur = b
        if _size(build(cur, k, True)) > GITHUB_BODY_MAX:
            die(f"a single block of {_size(b)} bytes is over GitHub's "
                f"{GITHUB_BODY_MAX}-byte limit and cannot be split")
    parts.append(build(cur, k, False))
    return parts


def prefix_for(header_on: bool, author: str, owner: str, is_item: bool) -> str:
    p = ""
    if header_on and is_item:
        p += HEADER_LINE + "\n\n"
    if author and author.lower() != owner.lower():
        p += f"*by {_neutralise(author)} on gitea*\n\n"
    return p


# ============================================================================
# HTTP
# ============================================================================

class Gitea:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token

    def get(self, path, params=None, raw_status=False):
        url = f"{self.base}/api/v1{path}"
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        req = Request(url, headers={"Authorization": f"token {self.token}",
                                    "Accept": "application/json"})
        try:
            with urlopen(req, timeout=API_TIMEOUT_S) as r:
                data = r.read()
                total = r.headers.get("X-Total-Count")
                status = r.status
        except HTTPError as e:
            if raw_status:
                return e.code, None, None
            if e.code in (401, 403):
                die(f"Gitea refused the issue-reading token (GET {path} -> "
                    f"HTTP {e.code}). This project has no working Gitea read "
                    f"access for issues; stop and start this project")
            die(f"gitea GET {path} -> HTTP {e.code}")
        except (URLError, OSError) as e:
            die(f"Gitea did not answer at {self.base} "
                f"({getattr(e, 'reason', e)}); check that it is running "
                f"(Sandboxes -> Infrastructure), then run again")
        try:
            body = json.loads(data) if data else None
        except ValueError:
            die(f"gitea GET {path}: the answer is not JSON")
        if raw_status:
            return status, body, total
        return body, total

    def all(self, path, params=None):
        """Every row of a list route, page by page to the end. A failed or
        non-list page stops the run: a short list must never read as
        deletions."""
        rows, page, seen = [], 1, set()
        while True:
            p = dict(params or {})
            p.update({"limit": GITEA_PAGE, "page": page})
            body, total = self.get(path, p)
            if not isinstance(body, list):
                die(f"gitea GET {path} page {page}: not a list")
            new = 0
            for r in body:
                key = json.dumps(r.get("id"), sort_keys=True) if isinstance(r, dict) and "id" in r \
                    else json.dumps(r, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(r)
                new += 1
            if total is not None and total.isascii() and total.isdigit():
                if len(rows) >= int(total):
                    return rows
            if not body or new == 0 or len(body) < GITEA_PAGE:
                if total is not None and total.isascii() and total.isdigit() \
                        and len(rows) < int(total):
                    die(f"gitea GET {path}: listed {len(rows)} of {total} rows")
                return rows
            page += 1


class GitHub:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token
        self.writes = []            # timestamps of content-creating requests
        self.scopes = None

    def _pace(self, clock=time.time, sleep=time.sleep):
        """Wait until the next content write fits GitHub's secondary limits:
        the hourly and per-minute counts over the writes made so far, and at
        least MIN_WRITE_GAP_S since the last one."""
        if _TEST_FAST:
            return
        now = clock()
        self.writes = [t for t in self.writes if now - t < 3600]
        if len(self.writes) >= CONTENT_PER_HOUR:
            sleep(max(0.0, self.writes[-CONTENT_PER_HOUR] + 3600 - now))
            now = clock()
        recent = [t for t in self.writes if now - t < 60]
        if len(recent) >= CONTENT_PER_MINUTE:
            sleep(max(0.0, recent[-CONTENT_PER_MINUTE] + 60 - now))
            now = clock()
        if self.writes:
            gap = max(MIN_WRITE_GAP_S, 60.0 / CONTENT_PER_MINUTE) - (now - self.writes[-1])
            if gap > 0:
                sleep(gap)

    def call(self, method, path, body=None, accept=None, full_url=None, ok404=False,
             write=None):
        url = full_url or f"{self.base}{path}"
        if write is None:
            write = method in ("POST", "PATCH", "PUT", "DELETE")
        waited = 0.0
        while True:
            if write:
                self._pace()
            hdr = {"Authorization": f"token {self.token}",
                   "Accept": accept or "application/vnd.github+json",
                   "User-Agent": "rs-sync"}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                hdr["Content-Type"] = "application/json"
            req = Request(url, data=data, method=method, headers=hdr)
            try:
                with urlopen(req, timeout=API_TIMEOUT_S) as r:
                    raw = r.read()
                    headers = r.headers
                    status = r.status
            except HTTPError as e:
                headers = e.headers
                text = e.read().decode("utf-8", "replace")[:300]
                limited = e.code == 429 or (e.code == 403 and (
                    "rate limit" in text.lower()
                    or headers.get("x-ratelimit-remaining") == "0"
                    or headers.get("retry-after")))
                if limited:
                    wait = _limit_wait(headers)
                    if waited + wait > RATE_WAIT_MAX_S:
                        die(f"GitHub's rate limit did not clear within "
                            f"{RATE_WAIT_MAX_S} s ({method} {path}); run "
                            f"again later: the markers say what is done")
                    print(f"  rate limited; waiting {int(wait)} s", file=sys.stderr)
                    time.sleep(1 if _TEST_FAST else wait)
                    waited += wait
                    continue
                if e.code in (404, 410) and ok404:
                    return e.code, None, headers
                if write:
                    self.writes.append(time.time())
                die(f"GitHub {method} {path} -> HTTP {e.code}: {_safe(text, self.token)}")
            except (URLError, OSError) as e:
                die(f"GitHub did not answer ({method} {path}: "
                    f"{getattr(e, 'reason', e)})")
            if write:
                self.writes.append(time.time())
            if self.scopes is None and headers.get("X-OAuth-Scopes") is not None:
                self.scopes = headers.get("X-OAuth-Scopes")
            try:
                out = json.loads(raw) if raw else None
            except ValueError:
                out = None
            return status, out, headers

    def all(self, path, params=None, accept=None):
        """Every row, following the Link header to the last page. A failed
        page stops the run: a short list must never read as unmirrored
        items, which would be imported a second time."""
        p = dict(params or {})
        p["per_page"] = GITHUB_PAGE
        url = f"{self.base}{path}?{urlencode(p)}"
        rows = []
        while url:
            _, body, headers = self.call("GET", path, accept=accept, full_url=url)
            if not isinstance(body, list):
                die(f"GitHub GET {path}: not a list")
            rows.extend(body)
            url = _next_link(headers.get("Link") or "")
        return rows

    def graphql(self, query, variables):
        # A query is a read (no pacing); a mutation creates or changes content.
        _, out, _ = self.call("POST", "/graphql",
                              {"query": query, "variables": variables},
                              write=query.lstrip().startswith("mutation"))
        if not isinstance(out, dict) or out.get("errors"):
            die(f"GitHub GraphQL failed: {_safe(json.dumps((out or {}).get('errors'))[:300], self.token)}")
        return out.get("data")


def _limit_wait(headers) -> float:
    ra = headers.get("retry-after")
    if ra and ra.strip().isdigit():
        return float(ra.strip())
    if headers.get("x-ratelimit-remaining") == "0":
        reset = headers.get("x-ratelimit-reset")
        if reset and reset.strip().isdigit():
            return max(1.0, float(reset) - time.time())
    return float(NO_HEADER_WAIT_S)


def _next_link(link: str):
    for part in link.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


def _safe(text: str, *secrets) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


# ============================================================================
# Reading Gitea
# ============================================================================

def gitea_answers() -> None:
    """Gitea's public health route answers, or the run stops with that, before
    any message about the token."""
    try:
        with urlopen(Request(f"{GITEA_BASE}/api/healthz"), timeout=API_TIMEOUT_S):
            return
    except HTTPError:
        return                      # it answered
    except (URLError, OSError) as e:
        die(f"Gitea did not answer at {GITEA_BASE} "
            f"({getattr(e, 'reason', e)}); check that it is running "
            f"(Sandboxes -> Infrastructure), then run again")


def read_gitea(g: Gitea, owner: str, repo: str) -> dict:
    base = f"/repos/{quote(owner)}/{quote(repo)}"
    info, _ = g.get(base)
    if not isinstance(info, dict) or "id" not in info:
        die(f"gitea: {owner}/{repo} not readable")
    issues = [i for i in g.all(f"{base}/issues", {"state": "all", "type": "issues"})
              if not i.get("pull_request")]
    pulls = g.all(f"{base}/pulls", {"state": "all"})
    comments = g.all(f"{base}/issues/comments")
    labels = g.all(f"{base}/labels")
    by_issue: dict = {}
    for c in comments:
        n = _number_from_url(c.get("issue_url") or c.get("pull_request_url") or "")
        if n is not None:
            by_issue.setdefault(n, []).append(
                {"kind": "comment", "id": c["id"], "body": c.get("body") or "",
                 "user": (c.get("user") or {}).get("login") or "",
                 "created_at": c.get("created_at")})
    merged = []
    for p in pulls:
        if not p.get("merged"):
            continue
        n = p["number"]
        reviews = g.all(f"{base}/pulls/{n}/reviews")
        extra = []
        for rv in reviews:
            if (rv.get("state") or "").upper() in ("PENDING", "REQUEST_REVIEW"):
                continue
            if (rv.get("body") or "").strip():
                extra.append({"kind": "review", "id": rv["id"],
                              "body": f"**Review ({(rv.get('state') or '').lower()})**\n\n{rv['body']}",
                              "user": (rv.get("user") or {}).get("login") or "",
                              "created_at": rv.get("submitted_at") or rv.get("created_at")})
            if rv.get("comments_count"):
                for rc in g.all(f"{base}/pulls/{n}/reviews/{rv['id']}/comments"):
                    line = rc.get("position") or rc.get("original_position") or ""
                    extra.append({"kind": "review-comment", "id": rc["id"], "review": rv["id"],
                                  "body": f"`{rc.get('path', '')}:{line}`\n\n{rc.get('body') or ''}",
                                  "user": (rc.get("user") or {}).get("login") or "",
                                  "created_at": rc.get("created_at")})
        commits = g.all(f"{base}/pulls/{n}/commits")
        merged.append({"number": n, "title": p.get("title") or "",
                       "body": p.get("body") or "",
                       "user": (p.get("user") or {}).get("login") or "",
                       "created_at": p.get("created_at"),
                       "updated_at": p.get("updated_at"),
                       "closed_at": p.get("merged_at") or p.get("closed_at"),
                       "labels": [l["name"] for l in p.get("labels") or []],
                       "base": (p.get("base") or {}).get("ref") or "",
                       "subjects": [((c.get("commit") or {}).get("message") or "").split("\n", 1)[0]
                                    for c in commits],
                       "extra": extra})
    return {"id": info["id"], "info": info, "issues": issues, "merged": merged,
            "comments": by_issue, "labels": labels,
            "pull_numbers": {p["number"] for p in pulls}}


def _number_from_url(u: str):
    m = re.search(r"/(?:issues|pulls)/(\d+)$", u)
    return int(m.group(1)) if m else None


# ============================================================================
# The desired state
# ============================================================================

def desired_items(gt: dict, owner, repo, hold, existing_sources) -> list:
    """Every Gitea item that should be on GitHub, in number order. An open
    issue carrying a hold label is left out unless it is already mirrored
    (gaining a hold label after it went public changes nothing)."""
    items = []
    for i in gt["issues"]:
        labels = [l["name"] for l in i.get("labels") or []]
        is_open = i.get("state") == "open"
        if is_open and held(labels, hold) and i["number"] not in existing_sources:
            continue
        items.append({"n": i["number"], "pr": False, "title": i.get("title") or "",
                      "body": i.get("body") or "",
                      "user": (i.get("user") or {}).get("login") or "",
                      "created_at": i.get("created_at"),
                      "updated_at": i.get("updated_at"),
                      "closed_at": i.get("closed_at"),
                      "closed": not is_open, "labels": labels,
                      "comments": sorted(gt["comments"].get(i["number"], []),
                                         key=lambda c: (c["created_at"] or "", c["id"]))})
    for p in gt["merged"]:
        comments = gt["comments"].get(p["number"], []) + p["extra"]
        items.append({"n": p["number"], "pr": True, "title": "[PR] " + p["title"],
                      "body": p["body"], "user": p["user"],
                      "created_at": p["created_at"], "updated_at": p.get("updated_at"),
                      "closed_at": p["closed_at"],
                      "closed": True, "labels": p["labels"] + [PR_LABEL],
                      "base": p["base"], "subjects": p["subjects"],
                      "comments": sorted(comments, key=lambda c: (c["created_at"] or "",
                                                                  {"comment": 0, "review": 1,
                                                                   "review-comment": 2}[c["kind"]],
                                                                  c["id"]))})
    items.sort(key=lambda x: x["n"])
    return items


def pr_commit_lines(ctx, item, subjects_index) -> str:
    """The commits a PR record lists: linked when exactly one commit on the
    GitHub branch named like the PR's base carries the subject."""
    lines = []
    idx = subjects_index.get(item.get("base") or "")
    for s in item.get("subjects") or []:
        shas = (idx or {}).get(s) or []
        subj = _finish(_convert_inline(ctx, _strip_private(s)))
        if len(shas) == 1:
            lines.append(f"- {shas[0][:12]} {subj}")
        else:
            lines.append(f"- {subj}")
    if not lines:
        return ""
    return "**Commits:**\n\n" + "\n".join(lines)


def item_text(ctx: Ctx, item, subjects_index) -> str:
    text = convert(ctx, item["body"])
    if item["pr"]:
        commits = pr_commit_lines(ctx, item, subjects_index)
        if commits:
            text = (text + "\n\n" if text else "") + commits
    return text


def item_parts(ctx, item, rid, header_on, subjects_index) -> list:
    pre = prefix_for(header_on, item["user"], ctx.owner, True)
    return assemble(pre, item_text(ctx, item, subjects_index),
                    lambda k: marker(ctx.owner, ctx.repo, rid, item["n"], part=k))


def comment_parts(ctx, item, c, rid) -> list:
    pre = prefix_for(False, c["user"], ctx.owner, False)
    return assemble(pre, convert(ctx, c["body"]),
                    lambda k: marker(ctx.owner, ctx.repo, rid, item["n"], c["kind"], c["id"], k))


def convert_title(ctx, title) -> str:
    t = _finish(_convert_inline(ctx, _strip_private(title or "")))
    if len(t) > GITHUB_TITLE_MAX:
        # never leave part of a number after a "#" or "GH-": it would link
        # a different issue
        t = re.sub(r"(?:#|[Gg][Hh]-)[0-9]*$", "", t[:GITHUB_TITLE_MAX - 1]) + "…"
    return t


# ============================================================================
# Reading GitHub
# ============================================================================

def read_github(gh: GitHub, target: str, me_id: int, owner: str, repo: str,
                for_remove=False) -> dict:
    """The mirrored items and comments on GitHub, by their markers. Only items
    by the token's own user id are trusted; a duplicate claim, or a marker
    naming another fork, stops the run."""
    o, r = target.split("/", 1)
    base = f"/repos/{quote(o)}/{quote(r)}"
    issues = _dedupe(gh.all(f"{base}/issues", {"state": "all", "sort": "created",
                                                "direction": "asc"}))
    comments = _dedupe(gh.all(f"{base}/issues/comments", {"sort": "created",
                                                          "direction": "asc"}))
    items, foreign, others, marked = {}, [], [], []
    rid_seen = set()
    for i in issues:
        if i.get("pull_request"):
            continue
        if (i.get("user") or {}).get("id") != me_id:
            others.append(i["number"])
            continue
        m = parse_marker(i.get("body"))
        if not m or m["kind"]:
            others.append(i["number"])
            continue
        if (m["owner"].lower(), m["repo"].lower()) != (owner.lower(), repo.lower()):
            foreign.append((i["number"], f"{m['owner']}/{m['repo']}"))
            continue
        rid_seen.add(m["rid"])
        marked.append(i)
        if m["n"] in items and for_remove:
            continue
        if m["n"] in items:
            die(f"GitHub issues #{items[m['n']]['number']} and #{i['number']} "
                f"both claim Gitea #{m['n']}; delete one of them, then run again")
        items[m["n"]] = {"number": i["number"], "node_id": i.get("node_id"),
                         "title": i.get("title") or "", "body": i.get("body") or "",
                         "state": i.get("state"), "locked": bool(i.get("locked")),
                         "labels": sorted(l["name"] for l in i.get("labels") or []),
                         "rid": m["rid"], "comments": {}, "parts": {}}
    by_number = {v["number"]: k for k, v in items.items()}
    for c in comments:
        if (c.get("user") or {}).get("id") != me_id:
            continue
        m = parse_marker(c.get("body"))
        if not m:
            continue
        n = _number_from_url(c.get("issue_url") or "")
        src = by_number.get(n)
        if src is None:
            continue
        if (m["owner"].lower(), m["repo"].lower()) != (owner.lower(), repo.lower()) \
                or m["n"] != src:
            if not for_remove:
                die(f"comment {c['id']} on GitHub #{n} carries a marker for "
                    f"another item ({m['owner']}/{m['repo']} #{m['n']}); delete "
                    f"it, then run again")
            continue
        rid_seen.add(m["rid"])
        rec = {"id": c["id"], "body": c.get("body") or ""}
        if m["kind"] is None and m["part"]:
            if m["part"] in items[src]["parts"]:
                die(f"two GitHub comments on #{n} claim part {m['part']} of its "
                    f"body; delete one of them, then run again")
            items[src]["parts"][m["part"]] = rec
            continue
        if m["kind"] is None:
            continue
        key = (m["kind"], m["cid"], m["part"] or 0)
        if key in items[src]["comments"]:
            die(f"two GitHub comments on #{n} claim the same Gitea {m['kind']} "
                f"{m['cid']}; delete one of them, then run again")
        items[src]["comments"][key] = rec
    if foreign and not for_remove:
        die(f"this GitHub repo holds items mirrored from another Gitea repo "
            f"({', '.join(f'#{n} from {s}' for n, s in foreign[:5])}); rs-sync "
            f"mirrors one Gitea repo per GitHub repo")
    return {"items": items, "rids": rid_seen, "others": others, "issues": issues,
            "marked": marked}


# ============================================================================
# The sync
# ============================================================================

def _dedupe(rows):
    """Rows listed twice (an item created while the list was paging shifts
    the pages) count once: by id, else by number."""
    seen, out = set(), []
    for r in rows:
        k = ("id", r["id"]) if r.get("id") is not None else ("number", r.get("number"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def all_dev_tokens() -> list:
    out = []
    try:
        names = sorted(os.listdir(TOKENS_DIR))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".token"):
            continue
        try:
            fd = os.open(TOKENS_DIR / n, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as e:
            die(f"cannot read {TOKENS_DIR / n} ({e.strerror}); rs-sync checks "
                f"every token file so that no token's text is ever sent")
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                die(f"{TOKENS_DIR / n} is not a regular file")
            v = os.read(fd, 4096).decode("utf-8", "replace").strip()
        finally:
            os.close(fd)
        if v:
            out.append(v)
    return out


def check_secrets(texts, secrets, what):
    for t in texts:
        for s in secrets:
            if s and s in t:
                die(f"{what} contains the text of a token; nothing was sent "
                    f"for it. Remove the token from the Gitea text, then run "
                    f"again")


def parse_repo_arg(v: str, what: str):
    m = re.fullmatch(r"(%s)/(%s)" % (NAME_RE, NAME_RE), v or "")
    if not m or m.group(1).startswith("-") or m.group(2).startswith("-"):
        die(f"{what} must be <owner>/<repo>, got {v!r}")
    return m.group(1), m.group(2)


def preflight_github(gh: GitHub, target: str, board: bool = False):
    _, me, _ = gh.call("GET", "/user")
    if not isinstance(me, dict) or "id" not in me:
        die("GitHub: could not read the token's account")
    scopes = {s.strip() for s in (gh.scopes or "").split(",") if s.strip()}
    o, r = target.split("/", 1)
    status, repo, _ = gh.call("GET", f"/repos/{quote(o)}/{quote(r)}", ok404=True)
    if status == 404 or not isinstance(repo, dict):
        die(f"GitHub repo {target} not found, or the token cannot see it")
    need = "public_repo" if not repo.get("private") else "repo"
    if "repo" not in scopes and need not in scopes:
        die(f"the GitHub token lacks the {need} scope (it has: "
            f"{', '.join(sorted(scopes)) or 'none'})")
    if board and "project" not in scopes:
        die(f"the GitHub token lacks the project scope, which the board needs "
            f"(it has: {', '.join(sorted(scopes)) or 'none'}); add it to the "
            f"token, or run with --skip board")
    full = repo.get("full_name") or target
    if full.lower() != target.lower():
        die(f"GitHub repo {target} was renamed or moved to {full}; run with that name")
    if not (repo.get("permissions") or {}).get("admin"):
        die(f"the token's account is not an admin of {target}; GitHub's issue "
            f"import needs admin")
    if not repo.get("has_issues"):
        die(f"issues are switched off on {target}; turn them on in the "
            f"repository settings")
    return me, repo, scopes


def check_imports(gh: GitHub, target: str, repo: dict, gh_numbers: set):
    """Refuse while an earlier import is still landing: its issue would not be
    listed yet, and importing the item again would duplicate it."""
    o, r = target.split("/", 1)
    since = repo.get("created_at") or "2008-01-01T00:00:00Z"
    rows = gh.all(f"/repos/{quote(o)}/{quote(r)}/import/issues", {"since": since},
                  accept=IMPORT_ACCEPT)
    for row in rows:
        st = (row or {}).get("status")
        if st == "pending":
            die("an earlier import is still being processed by GitHub; try "
                "again in a few minutes")
        if st == "imported":
            # A finished import whose issue the list does not show: a direct
            # read decides. 200 means the list has not caught up (importing the
            # item again would duplicate it); anything else means the issue
            # was deleted after it landed (a remove, or by hand).
            n = _number_from_url((row or {}).get("issue_url") or "")
            if n is not None and n not in gh_numbers:
                REPORT["import_rows_read_directly"] = REPORT.get("import_rows_read_directly", 0) + 1
                status, got, _ = gh.call("GET", f"/repos/{quote(o)}/{quote(r)}/issues/{n}",
                                         ok404=True)
                # A transferred issue answers through a redirect with ANOTHER
                # repo's issue: for this repo it is gone, like a deleted one,
                # and the next run mirrors the Gitea item again.
                canon = (repo.get("full_name") or f"{o}/{r}").lower()
                here = isinstance(got, dict) and got.get("number") == n and \
                    (got.get("repository_url") or "").lower().endswith(f"/repos/{canon}")
                if status == 200 and here:
                    die(f"GitHub reports an import as done (#{n}) that its issue "
                        f"list does not show yet; try again in a few minutes")


def do_import(gh: GitHub, target, payload, n_gitea) -> int:
    o, r = target.split("/", 1)
    what = f"Gitea #{n_gitea}"
    try:
        _, res, _ = gh.call("POST", f"/repos/{quote(o)}/{quote(r)}/import/issues",
                            payload, accept=IMPORT_ACCEPT)
    except SyncError as e:
        die(f"{what}: {e}")
    url = (res or {}).get("url")
    if not url:
        die(f"{what}: GitHub's import answered without a status URL")
    deadline = time.time() + IMPORT_DEADLINE_S
    while True:
        _, st, _ = gh.call("GET", "", accept=IMPORT_ACCEPT, full_url=url)
        status = (st or {}).get("status")
        if status == "imported":
            n = _number_from_url(st.get("issue_url") or "")
            if n is None:
                die(f"{what}: GitHub's import finished without naming the issue")
            return n
        if status == "failed":
            die(f"{what}: GitHub's import failed: {json.dumps(st.get('errors'))[:300]}")
        if time.time() > deadline:
            die(f"{what}: GitHub's import did not finish within {IMPORT_DEADLINE_S} s; "
                f"run again later (the next run waits for it)")
        time.sleep(0.05 if _TEST_FAST else IMPORT_POLL_S)


def label_payloads(gt_labels, wanted) -> dict:
    """GitHub label payloads for the label names in use, from Gitea's labels
    (colour, description cut to fit with the tag)."""
    by = {l["name"]: l for l in gt_labels}
    out = {}
    for name in sorted(wanted):
        if name == PR_LABEL:
            desc, color = PR_LABEL_DESC, PR_LABEL_COLOR
        else:
            l = by.get(name) or {}
            desc = (l.get("description") or "").strip()
            color = (l.get("color") or "ededed").lstrip("#")[:6] or "ededed"
        room = GITHUB_LABEL_DESC_MAX - len(LABEL_TAG)
        if len(desc) > room:
            desc = desc[:room - 1] + "…"
            REPORT["label_descriptions_cut"] = REPORT.get("label_descriptions_cut", 0) + 1
        out[name] = {"name": name, "color": color, "description": desc + LABEL_TAG}
    return out


def estimate_minutes(writes: int) -> float:
    """Minutes the pacing needs for `writes` content writes: every full
    hourly allowance costs an hour, the rest goes at the per-minute pace."""
    per_min = min(CONTENT_PER_MINUTE, 60.0 / MIN_WRITE_GAP_S)
    full, rest = divmod(writes, CONTENT_PER_HOUR)
    return round(full * 60 + rest / per_min, 1)


def build_subjects_index(gh: GitHub, target, bases) -> dict:
    o, r = target.split("/", 1)
    idx = {}
    for b in sorted(bases):
        if not b:
            continue
        status, _, _ = gh.call("GET", f"/repos/{quote(o)}/{quote(r)}/branches/{quote(b, safe='')}",
                               ok404=True)
        if status == 404:
            idx[b] = {}
            REPORT.setdefault("pr_bases_missing_on_github", []).append(b)
            continue
        subj: dict = {}
        for c in gh.all(f"/repos/{quote(o)}/{quote(r)}/commits", {"sha": b}):
            s = ((c.get("commit") or {}).get("message") or "").split("\n", 1)[0]
            subj.setdefault(s, []).append(c.get("sha") or "")
        idx[b] = subj
    return idx


def sync(args, env) -> None:
    owner, repo = parse_repo_arg(args.gitea, "the Gitea repo")
    target = "/".join(parse_repo_arg(args.github, "the GitHub repo"))
    apply = args.apply
    secrets = [env["RS_SYNC_GITHUB_TOKEN"]] + all_dev_tokens()
    gh = GitHub(GITHUB_API, env["RS_SYNC_GITHUB_TOKEN"])
    skip = set(args.skip or [])
    me, repo_info, _ = preflight_github(gh, target, board="board" not in skip)
    REPORT["github"] = {"repo": target, "account": me.get("login")}
    gitea_answers()
    try:
        gtok = GITEA_TOKEN_PATH.read_text().strip()
    except OSError:
        gtok = ""
    if not gtok:
        die("this project has no working Gitea read access for issues "
            f"({GITEA_TOKEN_PATH} is missing); stop and start this project")
    g = Gitea(GITEA_BASE, gtok)
    gt = read_gitea(g, owner, repo)
    rid = gt["id"]
    REPORT["gitea"] = {"repo": f"{owner}/{repo}", "id": rid,
                       "issues": len(gt["issues"]), "merged_prs": len(gt["merged"])}
    have = read_github(gh, target, me["id"], owner, repo)
    REPORT["trusted"] = sorted([n, v["number"]] for n, v in have["items"].items())
    stale = sorted(r for r in have["rids"] if r != rid)
    if stale:
        die(f"this GitHub repo mirrors an earlier fork of the same name "
            f"(repo id {stale[0]}, now {rid}); run --remove --apply first, "
            f"then sync again (GitHub numbers are not given back)")
    items = desired_items(gt, owner, repo, env["hold"], set(have["items"]))
    REPORT["held_open_issues"] = sorted(
        i["number"] for i in gt["issues"]
        if i.get("state") == "open" and i["number"] not in {x["n"] for x in items})
    gh_numbers = {i["number"] for i in have["issues"]}
    if apply:
        check_imports(gh, target, repo_info, gh_numbers)

    table = {n: v["number"] for n, v in have["items"].items()}
    ctx = Ctx(owner, repo, target, table)
    bases = {it.get("base") for it in items if it["pr"]}
    subjects_index = build_subjects_index(gh, target, bases)
    stats = {"linked": 0, "unlinked": 0, "ambiguous": 0}
    for it in items:
        if it["pr"]:
            idx = subjects_index.get(it.get("base") or "") or {}
            for subj in it.get("subjects") or []:
                k = len(idx.get(subj) or [])
                stats["linked" if k == 1 else "unlinked" if k == 0 else "ambiguous"] += 1
    REPORT["pr_commits"] = stats

    # Labels: every name in use must exist on GitHub, tagged; a same-named
    # label without the tag (compared ignoring case) is someone else's.
    o, r = target.split("/", 1)
    base = f"/repos/{quote(o)}/{quote(r)}"
    gh_labels = {l["name"].lower(): l for l in gh.all(f"{base}/labels")}
    wanted = set()
    for it in items:
        wanted.update(it["labels"])
    payloads = label_payloads(gt["labels"], wanted)
    to_create = []
    for name, pl in payloads.items():
        cur = gh_labels.get(name.lower())
        if cur is None:
            to_create.append(pl)
        elif not (cur.get("description") or "").endswith(LABEL_TAG):
            die(f"a GitHub label named {cur['name']!r} already exists and was "
                f"not made by rs-sync; rename or delete it on GitHub, then "
                f"run again")

    # The wiki and the board: every read and refusal before any write.
    wanted = {it["n"] for it in items if not it["closed"] and not it["pr"]}
    wiki_state = board_state = None
    if "wiki" in skip:
        REPORT["wiki"] = {"skipped": "--skip wiki"}
    else:
        wiki_state = wiki_prepare(args, owner, repo, target, repo_info, gt["info"], gtok,
                                  env["RS_SYNC_GITHUB_TOKEN"], secrets, "sync")
    if "board" in skip:
        REPORT["board"] = {"skipped": "--skip board"}
    else:
        board_state = board_prepare(g, gh, owner, repo, target, secrets,
                                    public=not repo_info.get("private"))
        board_desired(board_state["boards"], wanted)      # refuses an issue on two boards

    plan = {"create_labels": [p["name"] for p in to_create], "import": [],
            "edit": [], "comment_new": [], "comment_edit": [], "comment_delete": [],
            "state": [], "labels": [], "lock": [], "delete": []}

    new_items = [it for it in items if it["n"] not in have["items"]]
    for it in new_items:
        plan["import"].append(it["n"])
    writes_est = len(to_create) + 2 * len(new_items)

    if not apply:
        _fill_existing_plan(plan, items, have, ctx, rid, env, subjects_index, gt, g, owner, repo, dry=True)
        writes_est += sum(len(plan[k]) for k in ("edit", "comment_new", "comment_edit",
                                                 "comment_delete", "state", "labels", "lock", "delete"))
        if wiki_state:
            wiki_apply(wiki_state, Ctx(owner, repo, target, table, wiki=True), me,
                       env["RS_SYNC_GITHUB_TOKEN"], secrets, dry=True)
        if board_state:
            node_of = {n: v["node_id"] for n, v in have["items"].items()}
            board_apply(gh, board_state, owner, repo, target, wanted, node_of, dry=True)
            bp = REPORT["board"]
            # Creating a project is three writes; an item still to be
            # imported is an add and two values.
            writes_est += (3 if bp.get("create_project") else 0) + 3 * bp.get("after_import", 0) \
                + sum(bp.get(k, 0) for k in ("project_settings", "fields", "add", "set",
                                             "remove", "move"))
        REPORT["plan"] = {k: (v if len(v) <= 40 else v[:40] + [f"… {len(v) - 40} more"])
                          for k, v in plan.items()}
        REPORT["plan_counts"] = {k: len(v) for k, v in plan.items()}
        REPORT["estimated_writes"] = writes_est
        REPORT["estimated_minutes"] = estimate_minutes(writes_est)
        REPORT["dry_run"] = True
        return

    done = {"labels_created": [], "imported": {}, "edited": [], "comments_posted": 0,
            "comments_edited": 0, "comments_deleted": 0, "closed_or_reopened": [],
            "relabelled": [], "locked": [], "deleted": []}
    REPORT["done"] = done
    check_secrets([p["name"] + " " + p["description"] for p in to_create], secrets,
                  "a Gitea label")
    for pl in to_create:
        gh.call("POST", f"{base}/labels", pl)
        done["labels_created"].append(pl["name"])

    # Imports, in Gitea number order, each awaited before the next.
    for it in new_items:
        parts = item_parts(ctx, it, rid, env["header"], subjects_index)
        payload_comments = [{"body": p, "created_at": it["created_at"]} for p in parts[1:]]
        for c in it["comments"]:
            for p in comment_parts(ctx, it, c, rid):
                payload_comments.append({"body": p, "created_at": c["created_at"]})
        title = convert_title(ctx, it["title"])
        check_secrets([title, parts[0]] + [c["body"] for c in payload_comments], secrets,
                      f"Gitea #{it['n']}")
        issue = {"title": title, "body": parts[0], "created_at": it["created_at"],
                 "closed": it["closed"], "labels": sorted(it["labels"])}
        if it.get("updated_at"):
            issue["updated_at"] = it["updated_at"]
        if it["closed"] and it["closed_at"]:
            issue["closed_at"] = it["closed_at"]
        n = do_import(gh, target, {"issue": issue, "comments": payload_comments}, it["n"])
        done["imported"][it["n"]] = n
        table[it["n"]] = n
        print(f"  imported Gitea #{it['n']} as GitHub #{n}", file=sys.stderr)

    # Re-read GitHub, then bring every mirrored item level (forward
    # references, edits, new comments, state, labels, lock, deletions).
    have = read_github(gh, target, me["id"], owner, repo)
    for n, v in have["items"].items():
        table[n] = v["number"]
    _fill_existing_plan(plan, items, have, ctx, rid, env, subjects_index, gt, g, owner, repo,
                        dry=False, gh=gh, base=base, done=done, secrets=secrets)
    if wiki_state:
        wiki_apply(wiki_state, Ctx(owner, repo, target, table, wiki=True), me,
                   env["RS_SYNC_GITHUB_TOKEN"], secrets, dry=False)
    if board_state:
        node_of = {n: v["node_id"] for n, v in have["items"].items()}
        board_apply(gh, board_state, owner, repo, target, wanted, node_of, dry=False)
    REPORT["dry_run"] = False


def _fill_existing_plan(plan, items, have, ctx, rid, env, subjects_index, gt, g, owner, repo,
                        dry, gh=None, base=None, done=None, secrets=()):
    """Compare each mirrored item with what it should be; in a dry run record
    the plan, otherwise apply it."""
    by_n = {it["n"]: it for it in items}
    # One item of each comment kind seen in this run, for the sanity read that
    # must answer 200 before any 404 of that kind is trusted.
    known = {}
    for it2 in items:
        for c in it2["comments"]:
            known.setdefault(c["kind"], (it2["n"], c))
    for n, cur in sorted(have["items"].items()):
        it = by_n.get(n)
        if it is None:
            continue
        num = cur["number"]
        parts = item_parts(ctx, it, rid, env["header"], subjects_index)
        title = convert_title(ctx, it["title"])
        if not dry:
            check_secrets([title] + parts, secrets, f"Gitea #{n}")
        if cur["body"] != parts[0] or cur["title"] != title:
            plan["edit"].append(num)
            if not dry:
                gh.call("PATCH", f"{base}/issues/{num}", {"title": title, "body": parts[0]})
                done["edited"].append(num)
        want_state = "closed" if it["closed"] else "open"
        if cur["state"] != want_state:
            plan["state"].append(num)
            if not dry:
                gh.call("PATCH", f"{base}/issues/{num}", {"state": want_state})
                done["closed_or_reopened"].append(num)
        if sorted(l.lower() for l in it["labels"]) != sorted(l.lower() for l in cur["labels"]):
            plan["labels"].append(num)
            if not dry:
                gh.call("PATCH", f"{base}/issues/{num}", {"labels": sorted(it["labels"])})
                done["relabelled"].append(num)
        # continuation parts of the body
        for k in range(1, len(parts)):
            rec = cur["parts"].get(k)
            if rec is None:
                plan["comment_new"].append(num)
                if not dry:
                    gh.call("POST", f"{base}/issues/{num}/comments", {"body": parts[k]})
                    done["comments_posted"] += 1
            elif rec["body"] != parts[k]:
                plan["comment_edit"].append(num)
                if not dry:
                    gh.call("PATCH", f"{base}/issues/comments/{rec['id']}", {"body": parts[k]})
                    done["comments_edited"] += 1
        for k, rec in sorted(cur["parts"].items()):
            if k >= len(parts):
                plan["comment_delete"].append(num)
                if not dry:
                    gh.call("DELETE", f"{base}/issues/comments/{rec['id']}")
                    done["comments_deleted"] += 1
        # comments
        want = {}
        for c in it["comments"]:
            for k, p in enumerate(comment_parts(ctx, it, c, rid)):
                want[(c["kind"], c["id"], k)] = p
        if not dry:
            check_secrets(want.values(), secrets, f"a comment of Gitea #{n}")
        for key, body in want.items():
            rec = cur["comments"].get(key)
            if rec is None:
                plan["comment_new"].append(num)
                if not dry:
                    gh.call("POST", f"{base}/issues/{num}/comments", {"body": body})
                    done["comments_posted"] += 1
            elif rec["body"] != body:
                plan["comment_edit"].append(num)
                if not dry:
                    gh.call("PATCH", f"{base}/issues/comments/{rec['id']}", {"body": body})
                    done["comments_edited"] += 1
        gone = [(key, rec) for key, rec in cur["comments"].items() if key not in want]
        if gone:
            verified = _gone_on_gitea(g, owner, repo, n, [k for k, _ in gone], known)
            for key, rec in gone:
                if key[2] > 0 and (key[0], key[1], 0) in want:
                    gone_ok = True          # a surplus continuation part
                else:
                    gone_ok = verified.get((key[0], key[1]), False)
                if not gone_ok:
                    continue
                plan["comment_delete"].append(num)
                if not dry:
                    gh.call("DELETE", f"{base}/issues/comments/{rec['id']}")
                    done["comments_deleted"] += 1
        if not cur["locked"]:
            plan["lock"].append(num)
            if not dry:
                gh.call("PUT", f"{base}/issues/{num}/lock", {})
                done["locked"].append(num)
    # Mirrored items whose Gitea source is gone (a direct read says 404).
    missing = [n for n in have["items"] if n not in by_n]
    if missing and not items:
        REPORT["not_deleted_unverifiable"] = REPORT.get("not_deleted_unverifiable", []) \
            + [f"Gitea #{n}" for n in missing]
        missing = []                # no known item to sanity-check the route with
    if missing:
        st = _gitea_status(g, f"/repos/{quote(owner)}/{quote(repo)}/issues/{items[0]['n']}")
        if st != 200:
            die(f"Gitea's issue read answered HTTP {st} for one that exists; "
                f"nothing was deleted")
    for n in sorted(missing):
        if _held_open(gt, n):
            continue
        status = _gitea_status(g, f"/repos/{quote(owner)}/{quote(repo)}/issues/{n}")
        if status != 404:
            if status == 200:
                continue
            die(f"cannot tell whether Gitea #{n} still exists (HTTP {status}); "
                f"nothing was deleted")
        num = have["items"][n]["number"]
        plan["delete"].append(num)
        if not dry:
            delete_issue(gh, have["items"][n]["node_id"])
            done["deleted"].append(num)


def _held_open(gt, n) -> bool:
    return any(i["number"] == n and i.get("state") == "open" for i in gt["issues"])


def _gitea_status(g: Gitea, path) -> int:
    status, _, _ = g.get(path, raw_status=True)
    return status


def _gone_on_gitea(g: Gitea, owner, repo, n, keys, known) -> dict:
    """Direct reads, per kind, deciding which Gitea comments are really gone
    (measured routes on 28.2.0). Before a 404 of a kind is trusted, one item
    of that kind seen in this run must read 200 through the same route; with
    none seen, nothing of that kind is deleted. An inline review comment has
    no read of its own (the plain-comment route answers it 204): it is gone
    only when no review of the PR lists it."""
    base = f"/repos/{quote(owner)}/{quote(repo)}"
    out = {}

    def route(kind, num, cid):
        if kind == "comment":
            return f"{base}/issues/comments/{cid}"
        return f"{base}/pulls/{num}/reviews/{cid}"

    for kind, cid in sorted({(k[0], k[1]) for k in keys}):
        if kind == "review-comment":
            out[(kind, cid)] = _review_comment_gone(g, base, n, cid)
            continue
        k = known.get(kind)
        if k is None:
            REPORT.setdefault("not_deleted_unverifiable", []).append(f"Gitea #{n} {kind} {cid}")
            out[(kind, cid)] = False
            continue
        st = _gitea_status(g, route(kind, k[0], k[1]["id"]))
        if st != 200:
            die(f"Gitea's read for a {kind} answered HTTP {st} for one that "
                f"exists; nothing was deleted")
        st = _gitea_status(g, route(kind, n, cid))
        if st == 404:
            out[(kind, cid)] = True
        elif st == 200:
            out[(kind, cid)] = False
        else:
            die(f"cannot tell whether Gitea {kind} {cid} still exists (HTTP "
                f"{st}); nothing was deleted")
    return out


def _review_comment_gone(g, base, n, cid) -> bool:
    for rv in g.all(f"{base}/pulls/{n}/reviews"):
        for rc in g.all(f"{base}/pulls/{n}/reviews/{rv['id']}/comments"):
            if rc.get("id") == cid:
                return False
    return True


def delete_issue(gh: GitHub, node_id):
    if not node_id:
        die("a mirrored issue has no node id; nothing was deleted")
    gh.graphql("mutation($id:ID!){deleteIssue(input:{issueId:$id}){clientMutationId}}",
               {"id": node_id})


# ============================================================================
# The wiki
# ============================================================================

# Where rs-sync keeps one bare git repository for the wiki work, reused by
# every run and never deleted: both wikis are fetched into it and the new tree
# is built from git objects, so no working tree (and nothing to clean up) is
# ever needed. It holds copies of public wiki content only, never a token.
CACHE_DIR = Path.home() / ".cache" / "rs-sync"
# The bound on one git network call (ls-remote, fetch, push of a wiki). A wiki
# with images is a few MB; at half this a slow link could cut a push off (the
# next run retries it), at ten times it a hung connection would hold the
# terminal for most of an hour. The same bound as one import's deadline.
GIT_TIMEOUT_S = 300
WIKI_SUBJECT = "Mirror the wiki of a private dev repo"
_GIT_HELPER = ('!f() { test "$1" = get || exit 0; echo username=x-access-token; '
               'echo "password=$RS_SYNC_GIT_TOKEN"; }; f')
_BRANCH_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*")
_MARK_LINE_RE = re.compile(r"rs-sync v1 gitea:(%s)/(%s)" % (NAME_RE, NAME_RE))


def wiki_marker(owner, repo) -> str:
    return f"{MARKER_TAG} gitea:{owner}/{repo}"


def is_wiki_commit(message: str, owner, repo) -> bool:
    """A commit rs-sync made for this fork: its message's last line is exactly
    the marker line."""
    lines = (message or "").rstrip().split("\n")
    m = _MARK_LINE_RE.fullmatch(lines[-1].strip()) if lines else None
    return bool(m) and (m.group(1).lower(), m.group(2).lower()) == (owner.lower(), repo.lower())


def _git_argv(args, token) -> list:
    """git with the user's own config shut out (no hooks, no signing, no
    stored credentials) and, when a token is needed, a one-shot helper that
    reads it from the environment: the token never enters argv."""
    argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
            "-c", "credential.helper="]
    if token:
        argv += ["-c", "credential.helper=" + _GIT_HELPER]
    return argv + list(args)


def _git_env(token, extra=None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k != "RS_SYNC_GIT_TOKEN"}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"})
    if token:
        env["RS_SYNC_GIT_TOKEN"] = token
    env.update(extra or {})
    return env


def _last_line(text: str) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1][:300] if lines else "no output"


def run_git(args, token=None, data=None, extra_env=None, what="git"):
    """(returncode, stdout bytes, stderr text with the token scrubbed)."""
    try:
        p = subprocess.run(_git_argv(args, token), input=data, capture_output=True,
                           stdin=None if data is not None else subprocess.DEVNULL,
                           env=_git_env(token, extra_env), timeout=GIT_TIMEOUT_S)
    except FileNotFoundError:
        die("git is not installed here; rs-sync needs it for the wiki (or run "
            "with --skip wiki)")
    except subprocess.TimeoutExpired:
        die(f"{what} did not finish within {GIT_TIMEOUT_S} s; run again later")
    return p.returncode, p.stdout, _safe(p.stderr.decode("utf-8", "replace"), token)


_CACHES: dict = {}


def wiki_cache() -> "WikiCache":
    """The process's one WikiCache (its lock is held until the process ends;
    a second open of the lock file would refuse against our own lock)."""
    key = str(CACHE_DIR)
    if key not in _CACHES:
        _CACHES[key] = WikiCache()
    return _CACHES[key]


class WikiCache:
    """The one bare repository the wiki work happens in, locked for the run."""

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.lock_fd = os.open(CACHE_DIR / "wiki.lock",
                                   os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as e:
            die(f"cannot open {CACHE_DIR / 'wiki.lock'} ({e.strerror})")
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            die("another rs-sync run is using the wiki cache; wait for it to "
                "finish, then run again")
        self.dir = CACHE_DIR / "wiki.git"
        if not (self.dir / "HEAD").is_file():
            rc, _, err = run_git(["init", "--bare", "-q", str(self.dir)], what="git init")
            if rc:
                die(f"cannot create the wiki cache {self.dir} ({_last_line(err)})")
        self.index = CACHE_DIR / "wiki.index"

    def git(self, args, **kw):
        return run_git(["--git-dir", str(self.dir)] + list(args), **kw)

    def must(self, args, what, **kw) -> bytes:
        rc, out, err = self.git(args, what=what, **kw)
        if rc:
            die(f"{what} failed ({_last_line(err)})")
        return out


def remote_head(cache: WikiCache, url, token, side):
    """(branch, sha) of a wiki's default branch, or None when that wiki does
    not exist (no first page yet, or switched off). Any other failure stops
    the run: an unreadable wiki must never read as an empty one."""
    rc, out, err = cache.git(["ls-remote", "--symref", url, "HEAD"], token=token,
                             what=f"reading the {side} wiki")
    if rc:
        low = err.lower()
        if "not found" in low or "does not appear to be a git repository" in low \
                or "wiki is disabled" in low:
            return None
        die(f"could not read the {side} wiki ({_last_line(err)}); run again later")
    branch = sha = None
    for line in out.decode("utf-8", "replace").splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            ref = line[5:-5]
            if ref.startswith("refs/heads/"):
                branch = ref[len("refs/heads/"):]
        elif line.endswith("\tHEAD"):
            sha = line.split("\t", 1)[0]
    if not sha:
        return None
    if not branch or not _BRANCH_RE.fullmatch(branch) or ".." in branch \
            or branch.endswith(".lock"):
        die(f"the {side} wiki's default branch {branch!r} is not one rs-sync can use")
    return branch, sha


def fetch_wiki(cache: WikiCache, url, token, branch, ref, side) -> str:
    cache.must(["fetch", "--no-tags", "-q", url, f"+refs/heads/{branch}:{ref}"],
               f"fetching the {side} wiki", token=token)
    return cache.must(["rev-parse", "--verify", ref + "^{commit}"],
                      f"reading the {side} wiki").decode().strip()


def wiki_commits(cache: WikiCache, revs) -> list:
    """(sha, message) of the commits `revs` selects, children first."""
    out = cache.must(["log", "-z", "--topo-order", "--format=%H%n%B"] + list(revs) + ["--"],
                     "reading the GitHub wiki's history")
    rows = []
    for rec in out.decode("utf-8", "replace").split("\0"):
        rec = rec.lstrip("\n")
        if rec:
            sha, _, msg = rec.partition("\n")
            rows.append((sha.strip(), msg))
    return rows


def _describe(rows) -> str:
    shown = [f"{sha[:10]} {(msg.strip().splitlines() or [''])[0][:60]!r}" for sha, msg in rows[:5]]
    more = f" and {len(rows) - 5} more" if len(rows) > 5 else ""
    return ", ".join(shown) + more


def wiki_files(cache: WikiCache, ref) -> list:
    """Every blob of a wiki tree as (mode, sha, path bytes, content bytes)."""
    out = cache.must(["ls-tree", "-r", "-z", "--full-tree", ref], "reading the Gitea wiki")
    files = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, kind, sha = meta.decode().split()
        if kind != "blob":
            REPORT.setdefault("wiki_entries_skipped", []).append(path.decode("utf-8", "replace"))
            continue
        data = cache.must(["cat-file", "blob", sha], "reading the Gitea wiki")
        files.append((mode, sha, path, data))
    return files


def build_wiki_tree(cache: WikiCache, files, ctx: Ctx, secrets) -> tuple:
    """The converted tree: markdown pages converted (a mapped reference becomes
    the mirrored issue's URL), every other file copied as it is. Returns
    (tree sha, pages converted). Each text is checked for a token first."""
    entries, pages = [], 0
    for mode, sha, path, data in files:
        name = path.decode("utf-8", "replace")
        if path.lower().endswith(b".md") and mode in ("100644", "100755"):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                REPORT["wiki_pages_not_text"] = REPORT.get("wiki_pages_not_text", 0) + 1
                entries.append((mode, sha, path))
                continue
            new = convert(ctx, text)
            check_secrets([new], secrets, f"the wiki page {name}")
            if new != text:
                sha = cache.must(["hash-object", "-w", "--stdin"], "writing the wiki tree",
                                 data=new.encode("utf-8")).decode().strip()
            pages += 1
        entries.append((mode, sha, path))
    with contextlib.suppress(FileNotFoundError):
        cache.index.unlink()
    env = {"GIT_INDEX_FILE": str(cache.index)}
    try:
        cache.must(["update-index", "-z", "--index-info"], "writing the wiki tree",
                   data=b"".join(f"{m} {s}\t".encode() + p + b"\0" for m, s, p in entries),
                   extra_env=env)
        tree = cache.must(["write-tree"], "writing the wiki tree", extra_env=env).decode().strip()
    finally:
        with contextlib.suppress(FileNotFoundError):
            cache.index.unlink()
    return tree, pages


def wiki_prepare(args, owner, repo, target, repo_info, gt_info, gtok, gh_token, secrets,
                 mode):
    """Everything the wiki needs before any write: both wikis fetched, the
    GitHub side checked for edits made there, the Gitea texts checked for
    tokens. Returns a state dict, or None when there is nothing to do."""
    gh_url = f"{GITHUB_WIKI_BASE.rstrip('/')}/{target}.wiki.git"
    if mode == "remove":
        if not repo_info.get("has_wiki"):
            REPORT["wiki"] = {"skipped": "the GitHub wiki is switched off"}
            return None
        cache = wiki_cache()
        head = remote_head(cache, gh_url, gh_token, "GitHub")
        if head is None:
            REPORT["wiki"] = {"skipped": "the GitHub wiki has no pages"}
            return None
        gh_sha = fetch_wiki(cache, gh_url, gh_token, head[0], "refs/rs-sync/github", "GitHub")
        ours = [r for r in wiki_commits(cache, [gh_sha]) if is_wiki_commit(r[1], owner, repo)]
        if not ours:
            REPORT["wiki"] = {"skipped": "no rs-sync commit on the GitHub wiki"}
            return None
        first = ours[-1][0]
        rc, out, _ = cache.git(["rev-parse", "--verify", "-q", first + "^1"], what="reading the GitHub wiki")
        if rc:
            die(f"the GitHub wiki's first commit is rs-sync's ({first[:10]}); there is no "
                f"earlier state to restore. Delete its pages in the Wiki tab, or run with "
                f"--skip wiki")
        parent = out.decode().strip()
        humans = [r for r in wiki_commits(cache, [gh_sha, "^" + parent])
                  if not is_wiki_commit(r[1], owner, repo)]
        if humans and not args.wiki_overwrite:
            die(f"the GitHub wiki has edits made on GitHub since rs-sync's first commit "
                f"({_describe(humans)}); a remove would drop them. Copy what should stay "
                f"elsewhere, then run again with --wiki-overwrite")
        REPORT["wiki"] = {"restore_to": parent[:10], "rs_sync_commits": len(ours),
                          "github_edits_overwritten": len(humans)}
        return {"cache": cache, "url": gh_url, "branch": head[0], "head": gh_sha,
                "parent": parent}

    if not gt_info.get("has_wiki", True):
        REPORT["wiki"] = {"skipped": "the Gitea wiki is switched off"}
        return None
    cache = wiki_cache()
    gitea_url = f"{GITEA_BASE.rstrip('/')}/{owner}/{repo}.wiki.git"
    ghead = remote_head(cache, gitea_url, gtok, "Gitea")
    if ghead is None:
        REPORT["wiki"] = {"skipped": "the Gitea wiki has no pages"}
        return None
    g_sha = fetch_wiki(cache, gitea_url, gtok, ghead[0], "refs/rs-sync/gitea", "Gitea")
    files = wiki_files(cache, g_sha)
    if not files:
        REPORT["wiki"] = {"skipped": "the Gitea wiki has no files"}
        return None
    for _, _, path, data in files:
        check_secrets([path.decode("utf-8", "replace"), data.decode("utf-8", "replace")], secrets,
                      f"the Gitea wiki file {_safe(path.decode('utf-8', 'replace'), *secrets)}")
    if not repo_info.get("has_wiki"):
        die(f"the wiki is switched off on {target}; turn it on in the repository "
            f"settings, or run with --skip wiki")
    head = remote_head(cache, gh_url, gh_token, "GitHub")
    if head is None:
        die(f"the GitHub wiki of {target} has no pages yet (GitHub creates it with "
            f"the first page): create any page in the repository's Wiki tab, then "
            f"run again")
    gh_sha = fetch_wiki(cache, gh_url, gh_token, head[0], "refs/rs-sync/github", "GitHub")
    rows = wiki_commits(cache, [gh_sha])
    ours = [r[0] for r in rows if is_wiki_commit(r[1], owner, repo)]
    humans = []
    if ours:
        humans = [r for r in wiki_commits(cache, [gh_sha, "^" + ours[0]])
                  if not is_wiki_commit(r[1], owner, repo)]
    if humans and not args.wiki_overwrite:
        die(f"the GitHub wiki has edits made on GitHub since rs-sync's last commit "
            f"({_describe(humans)}); a sync would replace them. Carry the edits into "
            f"the Gitea wiki if they should stay, then run again with --wiki-overwrite")
    gh_tree = cache.must(["rev-parse", "--verify", gh_sha + "^{tree}"],
                         "reading the GitHub wiki").decode().strip()
    return {"cache": cache, "url": gh_url, "branch": head[0], "head": gh_sha,
            "tree": gh_tree, "files": files, "overwritten": len(humans)}


def wiki_apply(state, ctx: Ctx, me, gh_token, secrets, dry):
    """Commit the converted Gitea tree on top of the GitHub wiki's head and
    push it; nothing when the tree is unchanged."""
    cache = state["cache"]
    tree, pages = build_wiki_tree(cache, state["files"], ctx, secrets)
    changed = tree != state["tree"]
    rep = {"pages": pages, "files": len(state["files"]) - pages, "changed": changed,
           "github_edits_overwritten": state["overwritten"]}
    REPORT["wiki"] = rep
    if dry or not changed:
        return
    login = me.get("login") or "rs-sync"
    email = f"{me.get('id')}+{login}@users.noreply.github.com"
    ident = {"GIT_AUTHOR_NAME": login, "GIT_AUTHOR_EMAIL": email,
             "GIT_COMMITTER_NAME": login, "GIT_COMMITTER_EMAIL": email}
    msg = f"{WIKI_SUBJECT}\n\n{wiki_marker(ctx.owner, ctx.repo)}\n"
    commit = cache.must(["commit-tree", tree, "-p", state["head"], "-F", "-"],
                        "writing the wiki commit", data=msg.encode(),
                        extra_env=ident).decode().strip()
    rc, _, err = cache.git(["push", "-q", state["url"], f"{commit}:refs/heads/{state['branch']}"],
                           token=gh_token, what="pushing the GitHub wiki")
    if rc:
        die(f"pushing the GitHub wiki failed ({_last_line(err)}); if it changed "
            f"during the run, run again")
    rep["commit"] = commit[:10]


def wiki_restore(state, gh_token):
    """Force the GitHub wiki back to the state before rs-sync's first commit,
    leased on the head that was checked."""
    cache = state["cache"]
    rc, _, err = cache.git(["push", "-q",
                            f"--force-with-lease=refs/heads/{state['branch']}:{state['head']}",
                            state["url"], f"{state['parent']}:refs/heads/{state['branch']}"],
                           token=gh_token, what="restoring the GitHub wiki")
    if rc:
        die(f"restoring the GitHub wiki failed ({_last_line(err)}); if it changed "
            f"during the run, run again")
    REPORT["wiki"]["restored"] = True


# ============================================================================
# The board
# ============================================================================

STATUS_FIELD = "Status"
AREA_FIELD = "Area"
# The colour of every option rs-sync writes: Gitea columns carry none.
OPTION_COLOR = "GRAY"
_BOARD_MARK_RE = re.compile(r"\(rs-sync v1 gitea:(%s)/(%s)\)\s*$" % (NAME_RE, NAME_RE))
# GitHub's GraphQL connections answer at most 100 nodes a page.
GRAPHQL_PAGE = 100


def board_description(owner, repo) -> str:
    return f"The boards of a private dev repo, mirrored with rs-sync ({wiki_marker(owner, repo)})"


def board_marked(desc, owner, repo) -> bool:
    m = _BOARD_MARK_RE.search(desc or "")
    return bool(m) and (m.group(1).lower(), m.group(2).lower()) == (owner.lower(), repo.lower())


def read_gitea_boards(g: Gitea, owner, repo) -> list:
    """The fork's open boards, in creation order, each with its columns in
    their order and each column's cards in their order (numbers)."""
    base = f"/repos/{quote(owner)}/{quote(repo)}"
    boards = []
    for b in sorted(g.all(f"{base}/projects"), key=lambda b: b["id"]):
        cols = []
        for c in sorted(g.all(f"{base}/projects/{b['id']}/columns"),
                        key=lambda c: (c.get("sorting") or 0, c["id"])):
            cards = g.all(f"{base}/projects/{b['id']}/columns/{c['id']}/issues")
            cols.append({"title": c.get("title") or "", "cards": [i["number"] for i in cards]})
        boards.append({"title": b.get("title") or "", "columns": cols})
    return boards


def board_desired(boards, wanted_numbers) -> dict:
    """What the GitHub project should hold: the Status options (the columns'
    union, in board order), the Area options (the board titles), and the items
    (an open mirrored issue that is a card), ordered by column, then board,
    then the card's place. A wanted issue on two boards stops the run."""
    statuses, areas, where = [], [], {}
    for b in boards:
        if b["title"] not in areas:
            areas.append(b["title"])
        for c in b["columns"]:
            if c["title"] not in statuses:
                statuses.append(c["title"])
            for k, n in enumerate(c["cards"]):
                if n not in wanted_numbers:
                    continue
                if n in where and where[n][1] != b["title"]:
                    die(f"Gitea #{n} is a card on two boards ({where[n][1]!r} and "
                        f"{b['title']!r}); the GitHub project gives an issue one area. "
                        f"Remove one of the cards, then run again")
                where.setdefault(n, (c["title"], b["title"], k))
    order = sorted(where, key=lambda n: (statuses.index(where[n][0]),
                                         areas.index(where[n][1]), where[n][2]))
    return {"statuses": statuses, "areas": areas,
            "items": [(n, where[n][0], where[n][1]) for n in order]}


def plan_moves(current, desired) -> list:
    """(item, after) moves that make the managed items' relative order equal
    `desired` (after None = to the top); none when it already is."""
    want = set(desired)
    cur = [i for i in current if i in want]
    moves = []
    for k, item in enumerate(desired):
        if k < len(cur) and cur[k] == item:
            continue
        moves.append((item, desired[k - 1] if k else None))
        cur.remove(item)
        cur.insert(k, item)
    return moves


Q_VIEWER = """query rsViewerProjects($after:String){viewer{id login projectsV2(first:%d,after:$after){
pageInfo{hasNextPage endCursor} nodes{id number title shortDescription}}}}""" % GRAPHQL_PAGE
Q_REPO = """query rsRepoId($o:String!,$n:String!){repository(owner:$o,name:$n){id}}"""
Q_PROJECT = """query rsProject($id:ID!,$after:String){node(id:$id){... on ProjectV2{id title public
shortDescription repositories(first:%d){nodes{id}} fields(first:%d){nodes{
... on ProjectV2FieldCommon{id name dataType} ... on ProjectV2SingleSelectField{options{id name}}}}
items(first:%d,after:$after,orderBy:{field:POSITION,direction:ASC}){pageInfo{hasNextPage endCursor}
nodes{id content{... on Issue{id}} fieldValues(first:%d){nodes{
... on ProjectV2ItemFieldSingleSelectValue{optionId field{... on ProjectV2FieldCommon{id}}}}}}}}}}""" % (
    GRAPHQL_PAGE, GRAPHQL_PAGE, GRAPHQL_PAGE, GRAPHQL_PAGE)
M_CREATE = """mutation rsCreateProject($o:ID!,$t:String!){createProjectV2(input:{ownerId:$o,title:$t}){projectV2{id}}}"""
M_UPDATE = """mutation rsUpdateProject($p:ID!,$d:String!,$pub:Boolean!){updateProjectV2(input:{projectId:$p,shortDescription:$d,public:$pub}){projectV2{id}}}"""
M_LINK = """mutation rsLinkProject($p:ID!,$r:ID!){linkProjectV2ToRepository(input:{projectId:$p,repositoryId:$r}){repository{id}}}"""
M_FIELD_CREATE = """mutation rsCreateField($p:ID!,$n:String!,$o:[ProjectV2SingleSelectFieldOptionInput!]){createProjectV2Field(input:{projectId:$p,dataType:SINGLE_SELECT,name:$n,singleSelectOptions:$o}){projectV2Field{... on ProjectV2FieldCommon{id}}}}"""
M_FIELD_UPDATE = """mutation rsUpdateField($f:ID!,$o:[ProjectV2SingleSelectFieldOptionInput!]){updateProjectV2Field(input:{fieldId:$f,singleSelectOptions:$o}){projectV2Field{... on ProjectV2FieldCommon{id}}}}"""
M_ADD = """mutation rsAddItem($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}"""
M_SET = """mutation rsSetValue($p:ID!,$i:ID!,$f:ID!,$v:String!){updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$v}}){projectV2Item{id}}}"""
M_DELETE_ITEM = """mutation rsDeleteItem($p:ID!,$i:ID!){deleteProjectV2Item(input:{projectId:$p,itemId:$i}){deletedItemId}}"""
M_MOVE = """mutation rsMoveItem($p:ID!,$i:ID!,$a:ID){updateProjectV2ItemPosition(input:{projectId:$p,itemId:$i,afterId:$a}){clientMutationId}}"""
M_DELETE_PROJECT = """mutation rsDeleteProject($p:ID!){deleteProjectV2(input:{projectId:$p}){projectV2{id}}}"""


def find_boards(gh: GitHub, owner, repo):
    """The token account's id and every project of it carrying this fork's
    marker."""
    after, viewer, marked = None, None, []
    while True:
        d = gh.graphql(Q_VIEWER, {"after": after})
        v = (d or {}).get("viewer") or {}
        viewer = v.get("id")
        conn = v.get("projectsV2") or {}
        for p in conn.get("nodes") or []:
            if p and board_marked(p.get("shortDescription"), owner, repo):
                marked.append(p)
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
        if not after:
            die("GitHub GraphQL: the projects list says it has more pages but gives no cursor")
    if not viewer:
        die("GitHub GraphQL did not name the token's account")
    return viewer, marked


def read_board(gh: GitHub, pid) -> dict:
    after, proj, items = None, None, []
    while True:
        d = gh.graphql(Q_PROJECT, {"id": pid, "after": after})
        node = (d or {}).get("node")
        if not isinstance(node, dict) or not node.get("id"):
            die("GitHub GraphQL: the mirrored project could not be read")
        proj = proj or node
        conn = node.get("items") or {}
        for it in conn.get("nodes") or []:
            if not it:
                continue
            vals = {}
            for fv in ((it.get("fieldValues") or {}).get("nodes") or []):
                fid = ((fv or {}).get("field") or {}).get("id")
                if fid and fv.get("optionId"):
                    vals[fid] = fv["optionId"]
            items.append({"id": it["id"], "content": (it.get("content") or {}).get("id"),
                          "values": vals})
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
        if not after:
            die("GitHub GraphQL: the project's item list says it has more pages but gives no cursor")
    fields = {}
    for f in ((proj.get("fields") or {}).get("nodes") or []):
        if f and f.get("name"):
            fields[f["name"]] = {"id": f.get("id"), "type": f.get("dataType"),
                                 "options": [(o["id"], o["name"]) for o in f.get("options") or []]}
    return {"id": proj["id"], "public": proj.get("public"),
            "desc": proj.get("shortDescription") or "",
            "repos": {r["id"] for r in ((proj.get("repositories") or {}).get("nodes") or []) if r},
            "fields": fields, "items": items}


def _check_field_types(cur):
    for name in (STATUS_FIELD, AREA_FIELD):
        f = cur["fields"].get(name)
        if f is not None and f["type"] != "SINGLE_SELECT":
            die(f"the GitHub project already has a field named {name!r} that is not a "
                f"single-select field; rename or delete it on GitHub, then run again")


def board_prepare(g: Gitea, gh: GitHub, owner, repo, target, secrets=(), public=True):
    """Reads and refusals before any write: the Gitea boards, the marked
    project and its fields. `public`: the project is public only when the
    GitHub repo is, so the board never shows more than the repo does."""
    boards = read_gitea_boards(g, owner, repo)
    check_secrets([b["title"] for b in boards] + [c["title"] for b in boards for c in b["columns"]],
                  secrets, "a Gitea board or column title")
    viewer, marked = find_boards(gh, owner, repo)
    if len(marked) > 1:
        die(f"{len(marked)} GitHub projects carry the marker of {owner}/{repo} "
            f"(numbers {', '.join(str(p.get('number')) for p in marked)}); delete all "
            f"but one, then run again")
    o, r = target.split("/", 1)
    d = gh.graphql(Q_REPO, {"o": o, "n": r})
    repo_id = ((d or {}).get("repository") or {}).get("id")
    if not repo_id:
        die(f"GitHub GraphQL could not read {target}'s id")
    cur = None
    if marked:
        cur = read_board(gh, marked[0]["id"])
        _check_field_types(cur)
    return {"boards": boards, "viewer": viewer, "project": marked[0] if marked else None,
            "repo_id": repo_id, "cur": cur, "public": public}


def _options(names):
    return [{"name": n, "color": OPTION_COLOR, "description": ""} for n in names]


def board_apply(gh: GitHub, state, owner, repo, target, wanted_numbers, node_of, dry):
    """Bring the GitHub project level with the Gitea boards. In a dry run,
    count what would be written."""
    want = board_desired(state["boards"], wanted_numbers)
    plan = {"create_project": False, "project_settings": 0, "fields": 0, "add": 0,
            "set": 0, "remove": 0, "move": 0}
    REPORT["board"] = plan
    proj = state["project"]
    no_columns = not want["statuses"]
    if no_columns and proj is None:
        plan["skipped"] = "the fork has no open Gitea boards with columns"
        return
    pending = [n for n, _, _ in want["items"] if not node_of.get(n)]
    if pending:
        plan["after_import"] = len(pending)
    desc, public = board_description(owner, repo), state["public"]
    managed_contents = {v for v in node_of.values() if v}
    if proj is None:
        plan["create_project"] = True
        plan["fields"] = 2
        plan["add"] = len(want["items"]) - len(pending)
        plan["set"] = 2 * plan["add"]
        if dry:
            return
        d = gh.graphql(M_CREATE, {"o": state["viewer"], "t": target.split("/", 1)[1]})
        pid = (((d or {}).get("createProjectV2") or {}).get("projectV2") or {}).get("id")
        if not pid:
            die("GitHub did not return the new project's id")
        # The marker first: a project without it would be found by no later
        # run and removed by no --remove.
        gh.graphql(M_UPDATE, {"p": pid, "d": desc, "pub": public})
        gh.graphql(M_LINK, {"p": pid, "r": state["repo_id"]})
        state["project"] = {"id": pid}
        # From here the counts are of the writes actually made.
        plan.update(fields=0, add=0, set=0)
        cur = read_board(gh, pid)
    elif dry:
        cur = state["cur"]
    else:
        # Read again: the issue pass may have deleted issues, and GitHub
        # drops their items with them.
        cur = read_board(gh, proj["id"])
        _check_field_types(cur)
    pid = state["project"]["id"]

    def remove_unwanted(keep):
        for it in [i for i in cur["items"] if i["content"] in managed_contents
                   and i["content"] not in keep]:
            plan["remove"] += 1
            if not dry:
                gh.graphql(M_DELETE_ITEM, {"p": pid, "i": it["id"]})
            cur["items"].remove(it)

    if no_columns:
        # The Gitea boards are gone: rs-sync's items go, as Gitea's cards did.
        plan["skipped"] = "the fork has no open Gitea boards with columns; rs-sync's items are removed"
        remove_unwanted(set())
        return
    if cur["desc"] != desc or bool(cur["public"]) != public:
        plan["project_settings"] += 1
        if not dry:
            gh.graphql(M_UPDATE, {"p": pid, "d": desc, "pub": public})
    if state["repo_id"] not in cur["repos"]:
        plan["project_settings"] += 1
        if not dry:
            gh.graphql(M_LINK, {"p": pid, "r": state["repo_id"]})

    # The two fields, with exactly the Gitea options in order.
    fields_want = ((STATUS_FIELD, want["statuses"]), (AREA_FIELD, want["areas"]))
    changed = False
    for name, opts in fields_want:
        f = cur["fields"].get(name)
        if f is None:
            plan["fields"] += 1
            if not dry:
                gh.graphql(M_FIELD_CREATE, {"p": pid, "n": name, "o": _options(opts)})
            changed = True
        elif [n for _, n in f["options"]] != opts:
            plan["fields"] += 1
            if not dry:
                gh.graphql(M_FIELD_UPDATE, {"f": f["id"], "o": _options(opts)})
            changed = True
    if changed and dry:
        # Option ids may change with the options: every wanted value would be set.
        on_board = {i["content"] for i in cur["items"]}
        plan["add"] = sum(1 for n, _, _ in want["items"]
                          if node_of.get(n) and node_of[n] not in on_board)
        plan["set"] = 2 * (len(want["items"]) - len(pending))
        return
    if changed:
        cur = read_board(gh, pid)
        for name, opts in fields_want:
            f = cur["fields"].get(name)
            if f is None or f["type"] != "SINGLE_SELECT" or [n for _, n in f["options"]] != opts:
                # Without this every run would rewrite the field and reset
                # every value, or fail on a missing option.
                die(f"GitHub did not keep the project's {name} field as rs-sync wrote it "
                    f"(options sent: {opts}); run again, and if this repeats, check the "
                    f"Gitea {'column' if name == STATUS_FIELD else 'board'} titles")
    sf, af = cur["fields"][STATUS_FIELD], cur["fields"][AREA_FIELD]
    sopt = {n: i for i, n in sf["options"]}
    aopt = {n: i for i, n in af["options"]}

    # Items: the mirrored issues of this repo are rs-sync's; anything else on
    # the project (a person's issue, a draft) is left alone.
    by_content = {i["content"]: i for i in cur["items"] if i["content"] in managed_contents}
    order = []
    for n, status, area in want["items"]:
        node = node_of.get(n)
        if not node:
            continue
        it = by_content.get(node)
        if it is None:
            plan["add"] += 1
            if dry:
                plan["set"] += 2
                continue
            d = gh.graphql(M_ADD, {"p": pid, "c": node})
            iid = (((d or {}).get("addProjectV2ItemById") or {}).get("item") or {}).get("id")
            if not iid:
                die(f"GitHub did not return the project item for Gitea #{n}")
            it = {"id": iid, "content": node, "values": {}}
            cur["items"].append(it)
            by_content[node] = it
        for fid, oid in ((sf["id"], sopt[status]), (af["id"], aopt[area])):
            if it["values"].get(fid) != oid:
                plan["set"] += 1
                if not dry:
                    gh.graphql(M_SET, {"p": pid, "i": it["id"], "f": fid, "v": oid})
        order.append(it["id"])
    remove_unwanted({node_of.get(n) for n, _, _ in want["items"]})
    if dry and plan["add"]:
        return
    if plan["add"] and not dry:
        # Where GitHub places a new item is not measured: read the order back.
        cur = read_board(gh, pid)
    listed = {i["id"] for i in cur["items"]}
    unlisted = [i for i in order if i not in listed]
    if unlisted:
        die(f"GitHub does not list {len(unlisted)} new project item(s) yet; run again "
            f"shortly to put them in order")
    moves = plan_moves([i["id"] for i in cur["items"]], order)
    plan["move"] = len(moves)
    if not dry:
        for item, after in moves:
            gh.graphql(M_MOVE, {"p": pid, "i": item, "a": after})


# ============================================================================
# The remove
# ============================================================================

def remove(args, env) -> None:
    """Delete everything rs-sync mirrored for the named fork, from GitHub
    alone: the marked issues by the token's account (whatever the fork's repo
    id), the marked project, the wiki as it was before rs-sync's first commit,
    and the tagged labels no remaining issue carries. Every check comes before
    the first deletion."""
    owner, repo = parse_repo_arg(args.gitea, "the Gitea repo")
    target = "/".join(parse_repo_arg(args.github, "the GitHub repo"))
    gh = GitHub(GITHUB_API, env["RS_SYNC_GITHUB_TOKEN"])
    skip = set(args.skip or [])
    me, repo_info, _ = preflight_github(gh, target, board="board" not in skip)
    have = read_github(gh, target, me["id"], owner, repo, for_remove=True)
    wiki_state = None
    if "wiki" in skip:
        REPORT["wiki"] = {"skipped": "--skip wiki"}
    else:
        wiki_state = wiki_prepare(args, owner, repo, target, repo_info, None, None,
                                  env["RS_SYNC_GITHUB_TOKEN"], (), "remove")
    projects = [] if "board" in skip else find_boards(gh, owner, repo)[1]
    o, r = target.split("/", 1)
    base = f"/repos/{quote(o)}/{quote(r)}"
    doomed = sorted(({"number": i["number"], "node_id": i.get("node_id")}
                     for i in have["marked"]), key=lambda v: v["number"])
    REPORT["remove"] = {"issues": [v["number"] for v in doomed],
                        "repo_ids": sorted(have["rids"])}
    remaining_labels = set()
    doomed_numbers = {v["number"] for v in doomed}
    for i in have["issues"]:
        if i["number"] not in doomed_numbers:
            for l in i.get("labels") or []:
                remaining_labels.add(l["name"].lower())
    labels = [l for l in gh.all(f"{base}/labels")
              if (l.get("description") or "").endswith(LABEL_TAG)
              and l["name"].lower() not in remaining_labels]
    REPORT["remove"]["labels"] = [l["name"] for l in labels]
    REPORT["remove"]["projects"] = [p.get("number") for p in projects]
    if "board" in skip:
        REPORT["board"] = {"skipped": "--skip board"}
    if not args.apply:
        REPORT["dry_run"] = True
        return
    deleted = []
    for v in doomed:
        delete_issue(gh, v["node_id"])
        deleted.append(v["number"])
    for p in projects:
        gh.graphql(M_DELETE_PROJECT, {"p": p["id"]})
    if wiki_state:
        wiki_restore(wiki_state, env["RS_SYNC_GITHUB_TOKEN"])
    for l in labels:
        gh.call("DELETE", f"{base}/labels/{quote(l['name'], safe='')}")
    REPORT["remove"]["deleted"] = deleted
    REPORT["dry_run"] = False


# ============================================================================
# main
# ============================================================================

def print_report():
    print("===== PASTE FROM HERE =====")
    print(json.dumps(REPORT, indent=1, sort_keys=True, default=str))
    print("===== TO HERE =====")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="rs-sync",
        description="mirror a Gitea fork's issues, merged pull requests, wiki and "
                    "boards to a GitHub repo (a dry run unless --apply)")
    ap.add_argument("gitea", help="the Gitea fork, <owner>/<repo>")
    ap.add_argument("github", help="the GitHub repo, <owner>/<repo>")
    ap.add_argument("--apply", action="store_true", help="write to GitHub")
    ap.add_argument("--remove", action="store_true",
                    help="delete everything rs-sync mirrored for this fork")
    ap.add_argument("--skip", action="append", choices=["wiki", "board"], default=[],
                    help="leave the wiki or the board out (repeatable)")
    ap.add_argument("--wiki-overwrite", action="store_true",
                    help="commit over (or, with --remove, force-push past) edits made "
                         "on the GitHub wiki")
    ap.add_argument("--env", default=str(DEFAULT_ENV),
                    help=f"the settings file (default {DEFAULT_ENV})")
    args = ap.parse_args(argv)
    started = time.time()
    rc = 0
    try:
        env = read_env(Path(args.env))
        if args.remove:
            remove(args, env)
        else:
            sync(args, env)
    except SyncError as e:
        REPORT["error"] = str(e)
        print(f"rs-sync: {e}", file=sys.stderr)
        rc = 1
    REPORT["seconds"] = round(time.time() - started, 1)
    print_report()
    return rc


if __name__ == "__main__":
    sys.exit(main())
