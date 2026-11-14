#!/usr/bin/env python3
"""rs-sync — mirror a Gitea fork's issues and merged pull requests to GitHub.

Staged into rs-fetch surfaces beside rs-fetch. Run by hand:

    rs-sync <gitea owner>/<repo> <github owner>/<repo>            # dry run
    rs-sync <gitea owner>/<repo> <github owner>/<repo> --apply    # do it
    rs-sync <gitea owner>/<repo> <github owner>/<repo> --remove [--apply]

What goes over, one way (Gitea -> GitHub):
  * every closed issue, and every open issue that carries none of the
    labels in RS_SYNC_HOLD_LABELS, with its comments, as ONE dated import
    through GitHub's Issue Import API (the only GitHub route that keeps the
    original creation, close and comment dates);
  * every merged pull request, as a closed, locked issue titled
    "[PR] <title>", labelled "merged-pr", listing the commits it landed as;
  * the Gitea labels the mirrored items carry, each tagged " (rs-sync)".
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
import json
import os
import re
import stat
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
# The product's home, linked from the optional header line.
RS_HOME = "https://github.com/joaopn/research-sandbox"

# Test-only overrides (the harness points rs-sync at a throwaway Gitea and a
# fake GitHub). Unset in every real container.
GITEA_BASE = os.environ.get("RS_SYNC_TEST_GITEA_URL") or GITEA_BASE
GITHUB_API = os.environ.get("RS_SYNC_TEST_GITHUB_API") or GITHUB_API
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

    def __init__(self, owner, repo, target, table):
        self.owner, self.repo, self.target = owner, repo, target
        self.table = table            # Gitea number -> GitHub number

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


def _shield(t: str) -> str:
    return SHIELD_OPEN + t + SHIELD_CLOSE


def _finish(s: str) -> str:
    """The final pass over a whole converted text: neutralise every join the
    piecewise conversion made (an "@" from one piece before a name from the
    next, say), leaving shielded references alone, then drop the shields."""
    return _neutralise(s, shields=True).replace(SHIELD_OPEN, "").replace(SHIELD_CLOSE, "")
_GITEA_HOST_RE = re.compile(r"^https?://%s(?::\d+)?(/.*)?$" % re.escape(GITEA_HOST), re.I)


def _ref(ctx: Ctx, n: int) -> str:
    m = ctx.table.get(n)
    return _shield(f"#{m}") if m else f"gitea#{ZW}{n}"


def _gitea_path(url: str, ctx: "Ctx | None" = None):
    """The path of a URL on the Gitea address, or None. A relative link
    (/attachments/…, /<owner>/<repo>/issues/N) is Gitea's too: Gitea renders
    it against its own address. So is the fork's path on any address that is
    not GitHub (a link copied from the webui's Gitea tab)."""
    if url.startswith("/") and not url.startswith("//"):
        return url
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
    text = text.replace(SHIELD_OPEN, "").replace(SHIELD_CLOSE, "")
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

    def call(self, method, path, body=None, accept=None, full_url=None, ok404=False):
        url = full_url or f"{self.base}{path}"
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
        _, out, _ = self.call("POST", "/graphql",
                              {"query": query, "variables": variables})
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
        subj = _finish(_convert_inline(ctx, s.replace(SHIELD_OPEN, "").replace(SHIELD_CLOSE, "")))
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
    t = _finish(_convert_inline(ctx, (title or "").replace(SHIELD_OPEN, "").replace(SHIELD_CLOSE, "")))
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


def preflight_github(gh: GitHub, target: str):
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
    me, repo_info, _ = preflight_github(gh, target)
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
# The remove
# ============================================================================

def remove(args, env) -> None:
    """Delete everything rs-sync mirrored for the named fork, from GitHub
    alone: the marked issues by the token's account (whatever the fork's repo
    id), and the tagged labels no remaining issue carries."""
    owner, repo = parse_repo_arg(args.gitea, "the Gitea repo")
    target = "/".join(parse_repo_arg(args.github, "the GitHub repo"))
    gh = GitHub(GITHUB_API, env["RS_SYNC_GITHUB_TOKEN"])
    me, _, _ = preflight_github(gh, target)
    have = read_github(gh, target, me["id"], owner, repo, for_remove=True)
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
    if not args.apply:
        REPORT["dry_run"] = True
        return
    deleted = []
    for v in doomed:
        delete_issue(gh, v["node_id"])
        deleted.append(v["number"])
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
        description="mirror a Gitea fork's issues and merged pull requests to "
                    "a GitHub repo (a dry run unless --apply)")
    ap.add_argument("gitea", help="the Gitea fork, <owner>/<repo>")
    ap.add_argument("github", help="the GitHub repo, <owner>/<repo>")
    ap.add_argument("--apply", action="store_true", help="write to GitHub")
    ap.add_argument("--remove", action="store_true",
                    help="delete everything rs-sync mirrored for this fork")
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
