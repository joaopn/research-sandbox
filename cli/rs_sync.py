#!/usr/bin/env python3
"""rs-sync — mirror a Gitea fork's issues and merged pull requests to GitHub.

Staged into rs-fetch surfaces beside rs-fetch. Run by hand:

    rs-sync <gitea owner>/<repo> <github owner>/<repo>            # dry run
    rs-sync <gitea owner>/<repo> <github owner>/<repo> --apply    # do it
    rs-sync <gitea owner>/<repo> <github owner>/<repo> --remove [--apply]

Every run ends with a plain summary; --report adds the full report as JSON,
for pasting back to the agent. The wiki goes over only with --wiki and the
board only with --board, for a sync and a remove alike; --wiki-overwrite (with
--wiki) lets a run commit (or a remove force-push) past edits made on the
GitHub wiki.

What goes over, one way (Gitea -> GitHub):
  * every closed issue, and every open issue that carries none of the
    labels in RS_SYNC_HOLD_LABELS, with its comments, as ONE dated import
    through GitHub's Issue Import API (the only GitHub route that keeps the
    original creation, close and comment dates);
  * every merged pull request, as a closed, locked issue titled
    "[PR] <title>", labelled "merged-pr", listing the commits it landed as;
  * except any issue or merged pull request carrying the Gitea label
    "sync-skip": it never goes over, open or closed, and a copy already on
    GitHub is deleted first, before anything else the run writes. Removing
    the label releases it: the next run imports it, under a new number;
  * every mirrored title starts with "[rs-sync] ", every mirrored item
    carries the label "rs-sync", and each issue and PR record opens with a
    line saying it was mirrored with rs-sync (RS_SYNC_HEADER=off drops it);
  * the Gitea labels the mirrored items carry, each tagged " (rs-sync)"
    (a label with no description gets "(rs-sync)");
    a label of the same name (any case) already on GitHub that rs-sync did
    not make is used as it is: never changed, never deleted;
  * with --wiki, the fork's wiki: the GitHub wiki's tree is replaced by the
    converted Gitea tree, one commit per run whose message ends with a marker
    line;
  * with --board, the fork's boards: one GitHub project with a Status and an
    Area field, every open mirrored issue that is a card an item in the same
    column.
Every mirrored item is locked. Later runs bring GitHub level with Gitea:
new comments, edits, closes and label changes go over dated at the run.

Numbers are never assumed to match. Every mirrored body and comment ends with
a hidden marker (an HTML comment naming the fork, its repo id, the Gitea
number and, for a comment, its kind and id). Each run rebuilds the table
Gitea number -> GitHub number from those markers, on items authored by the
token's own GitHub account, and rewrites every reference through it. A
reference to something not mirrored becomes text GitHub does not link
(gitea#N). Nothing is stored locally: the markers are the record, so --remove
needs only GitHub.

The GitHub token is read from /workspace/.rs-sync.env (or --env):
    RS_SYNC_GITHUB_TOKEN=ghp_...        a classic token, repo/public_repo,
                                        plus project, for --board
    RS_SYNC_HOLD_LABELS=severity:*      required; comma-separated label names
                                        or prefixes ending in "*"; "*" alone
                                        holds every open issue; an empty
                                        value holds nothing
    RS_SYNC_HEADER=on|off               optional, default on
The Gitea side is read with the read-only issue token staged at
~/.dev-tokens/issue-reader.token.

Stdlib only: this file is staged into containers and imports no host module.
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import fcntl
import http.client
import json
import math
import os
import posixpath
import re
import shutil
import ssl
import stat
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

# --- wiring (lockstep with cli/gitea.py GITEA_CONTAINER / GITEA_INNER_PORT,
# as rs_fetch.py; this file cannot import host modules) -----------------------
GITEA_HOST = "rs-gitea"
GITEA_PORT = "3000"
GITEA_BASE = f"http://{GITEA_HOST}:{GITEA_PORT}"
# The read-only issue token the host stages beside the operator token.
GITEA_TOKEN_PATH = Path.home() / ".dev-tokens" / "issue-reader.token"
TOKENS_DIR = Path.home() / ".dev-tokens"
# /workspace: the only folder that survives a container recreate.
DEFAULT_ENV = Path("/workspace/.rs-sync.env")
GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"
# The product's home, linked from the header line.
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
# A limit answer that names no usable wait (_limit_wait) waits
# NO_HEADER_WAIT_S at first and this many times longer at each further such
# answer to the same request, the last wait cut to what is left of
# RATE_WAIT_MAX_S: GitHub's "wait for an exponentially increasing amount of
# time between retries, and throw an error after a specific number of
# retries". At 2 that is 60, 120, 240, 480, 960 and 1740 s: six tries in the
# hour. At 1.5 it is about ten, still pressing on a limit GitHub says is not
# clearing; at 10 it is two (60 s, then 3540 s), so a limit that clears after
# two minutes idles the run for most of the hour.
LIMIT_BACKOFF_FACTOR = 2
# A read that gets no answer (a timeout, a dropped or cut-off connection) or
# GitHub's "not now" (HTTP 502/503/504), once GitHub has answered this run, is
# tried again; so is a write that is safe to send twice (PUT, PATCH, a REST
# DELETE, or a GraphQL mutation marked repeatable), since GitHub may have
# received the first one. A write that creates something never is. GitHub
# documents no retry interval for these: the minute is borrowed from its
# documented wait for a limit answer without retry-after. It weighs idle time
# after GitHub recovers (up to one interval) against how often rs-sync knocks
# on a struggling GitHub: at half, recovery is noticed up to 30 s sooner for
# about 1.7x the attempts per hour of outage (each attempt also costs up to
# API_TIMEOUT_S), a negligible gain in a run of an hour or more; at ten times,
# a single 20-second blip would idle the run ten minutes.
NO_ANSWER_WAIT_S = 60
# The longest total wait one request may spend on missing answers before the
# run stops (its own total, beside the rate-limit one). A first mirror is
# paced over an hour or more by CONTENT_PER_HOUR and left unattended, and a
# rate limit mid-run is already waited out for up to RATE_WAIT_MAX_S, so an
# outage of the same order costs nothing the operator does not already
# accept; at half, a forty-minute outage stops a run that has to be started
# again; at ten times, a dead run holds for half a day. Maintainer ruling
# 2026-11-21 ("1a": up to an hour).
NO_ANSWER_MAX_S = 3600
NO_ANSWER_STATUSES = (502, 503, 504)
# After an import, GitHub may report the issue imported before its listing
# shows it, or shows the comments it was imported with; a run that went on
# would post those comments again, and a run started then would import the
# issue again. The listing is read again every LIST_LAG_WAIT_S until it shows
# them all, for at most LIST_LAG_MAX_S. Each check is one full read of the
# mirrored issues and comments (a few pages of reads, 1 point each): a minute
# keeps that to about 60 reads of each page an hour; the hour matches the
# outage tolerance of NO_ANSWER_MAX_S (the maintainer's 2026-11-21 ruling). At
# a tenth of the interval, a two-minute lag costs twenty full reads; at ten
# times it, a lag of seconds idles the run ten minutes.
LIST_LAG_WAIT_S = 60
LIST_LAG_MAX_S = 3600
# Import polling. GitHub processes an import in the background; the probe
# saw imports finish in seconds. Every 2 s keeps the wait for a few-second
# import short at a cost of a handful of reads; at half it the reads double
# for no gain, at ten times it every import waits 20 s. There is no deadline:
# GitHub offers no way to find an abandoned import later (it has no listing of
# a repo's imports, B252), so giving up on one could import the item twice.
# The poll waits for GitHub's own answer, and Ctrl-C stops it with advice.
IMPORT_POLL_S = 2
IMPORT_STATUSES = ("pending", "imported", "failed")

MARKER_TAG = "rs-sync v1"
LABEL_TAG = " (rs-sync)"
PR_LABEL = "merged-pr"
PR_LABEL_COLOR = "6f42c1"
PR_LABEL_DESC = "a merged pull request from the private dev repo"
# Every mirrored item carries this label and its title starts with the tag.
# A Gitea label of the same name (any case) is replaced by this one: GitHub
# label names ignore case, so two spellings would collide at create.
RS_LABEL = "rs-sync"
RS_LABEL_COLOR = "0e8a16"
RS_LABEL_DESC = "mirrored from a private dev repo"
TITLE_TAG = "[rs-sync] "
HEADER_LINE = f"*mirrored from a private dev repo with [rs-sync]({RS_HOME})*"
CONTINUED = "(continued in the next comment)"
NAME_RE = r"[A-Za-z0-9._-]+"
IMPORT_ACCEPT = "application/vnd.github.golden-comet-preview+json"

REPORT: dict = {}


class SyncError(Exception):
    """A refusal or failure that stops the run, with what to do. `status` is
    the HTTP code when GitHub answered with an error, else None."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def die(msg: str) -> None:
    raise SyncError(msg)


class CountError(SyncError):
    """rs-sync's count of recent GitHub writes cannot be read or saved."""


class DuplicateClaim(SyncError):
    """Two GitHub issues or comments claim one Gitea item: someone deletes one."""


# ============================================================================
# What the run shows while it works
# ============================================================================

# The shortest gap between two redraws of the progress line on a terminal,
# as in rs-publish: at 0.5 s a person sees steady movement; ten times longer
# reads as frozen, ten times shorter only flickers. A wait's countdown
# redraws once a second, the unit it counts in.
PROGRESS_REDRAW_S = 0.5


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


class _Progress:
    """On a terminal (stderr, checked when the run starts): one status line
    redrawn in place, cut to the terminal's width, with the run's messages
    printed above it (say). Elsewhere the messages alone, as before. A failed
    terminal write turns the line off; it never stops the run."""

    def __init__(self):
        self.tty, self.open, self.total, self.status = False, False, None, ""
        self.started, self.last = 0.0, None

    def start(self):
        self.open, self.total, self.status, self.last = False, None, "", None
        self.tty = _isatty(sys.stderr)
        self.started = time.monotonic() if self.tty else 0.0

    def set_total(self, n: int):
        self.total = n

    def written(self) -> str:
        n = REPORT.get("writes_sent") or 0
        return f"{n} written" if self.total is None else f"{n} of about {max(self.total, n)} written"

    def writing(self, used: int = 0):
        """After a write is sent; `used`: the writes of the past hour."""
        if not self.tty:
            return
        n = REPORT.get("writes_sent") or 0
        so_far = _duration(time.monotonic() - self.started)
        if self.total is None:
            self.status = f"writing {n} · {so_far} so far"
        else:
            t = max(self.total, n)
            self.status = f"writing {n} of about {t} · {so_far} so far"
            if t > n:
                self.status += f" · about {_duration(estimate_minutes(t - n, used) * 60)} left"
        self.draw()

    def reading(self, text: str):
        """Before a read: drawn at once (each read is a network round trip,
        so this is at most a few redraws a second)."""
        if self.tty:
            self.status = text
            self.draw(force=True)

    def say(self, msg: str):
        if self.open:
            self._write("\r\033[K")
            self.open = False
        print(msg, file=sys.stderr)
        self.draw(force=True)

    def end(self):
        """End an open status line, so what follows starts on its own line."""
        if self.open:
            self.open = False
            self._write("\n")

    def draw(self, force: bool = False):
        if not self.tty or not self.status:
            return
        now = time.monotonic()
        if not force and self.last is not None and now - self.last < PROGRESS_REDRAW_S:
            return
        self.last = now
        try:
            cols = os.get_terminal_size(sys.stderr.fileno()).columns
        except (OSError, ValueError):
            cols = 0
        if cols <= 0:               # not known (a terminal with no size set): as shutil does
            cols = shutil.get_terminal_size().columns
        width = max(1, cols - 1)
        self.open = True
        self._write("\r" + ("  " + self.status)[:width].ljust(width))

    def _write(self, text: str):
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except (OSError, ValueError):
            self.tty = self.open = False


PROGRESS = _Progress()


def say(msg: str) -> None:
    """A line for the person running rs-sync (stderr), above the progress line."""
    PROGRESS.say(msg)


def _wait(seconds: float, why: str, fast: float = 1, verb: str = "waiting") -> None:
    """Sleep `seconds` (`fast` under RS_SYNC_TEST_FAST); on a terminal in
    one-second steps, the progress line counting down and saying why."""
    if _TEST_FAST:
        time.sleep(fast)
        return
    if not PROGRESS.tty:
        time.sleep(seconds)
        return
    end, before = time.monotonic() + seconds, PROGRESS.status
    while True:
        left = end - time.monotonic()
        if left <= 0:
            PROGRESS.status = before        # the line goes back to what it showed
            PROGRESS.draw(force=True)
            return
        PROGRESS.status = f"{verb} {_duration(left)}: {why} · {PROGRESS.written()}"
        PROGRESS.draw(force=True)
        time.sleep(min(1.0, left))


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
    header = out.get("RS_SYNC_HEADER", "on")
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
        if not cur:
            die(f"a single block of {_size(b)} bytes is over GitHub's "
                f"{GITHUB_BODY_MAX}-byte limit and cannot be split")
        # cur may be the prefix alone (a first block that fits only without
        # it): the prefix then goes out as its own first part.
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
        # The token's account id, once preflight_github has read it: the
        # writes are then shared with every rs-sync run of this container for
        # that account (writes_file); without it they are this process's only.
        self.account = None
        self.share_error = None     # a failed save of the shared count (_record_write)
        self.scopes = None
        # Whether GitHub itself has answered this run: a 2xx, or any HTTP
        # status but 502/503/504, which a proxy sends as readily as GitHub.
        # Until it has, a missing answer is a setup problem (blocked, a
        # proxy that cannot reach GitHub) and stops the run at once.
        self.answered = False

    def writes_file(self) -> "Path | None":
        return None if self.account is None else CACHE_DIR / f"writes-{self.account}.json"

    def share_writes(self, record=None, save=False, clock=time.time):
        """Merge the past hour's writes of every rs-sync run of this container
        (for this account) into self.writes; with `record`, add that write
        time and save the merged list. GitHub's limits are per account, and a
        run started right after another would otherwise count from zero. The
        file is held under its own lock (never during a wait), and written to
        a fixed temporary name renamed over it, so two runs going at once
        keep each other's writes. A missing file is an empty one (the state
        after a container recreate); one rs-sync cannot read, or a cache
        folder it cannot write, stops the run, naming it."""
        if record is not None:
            self.writes.append(record)
        path = self.writes_file()
        if path is None:
            return
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(CACHE_DIR / "writes.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as e:
            raise CountError(f"cannot open rs-sync's cache {CACHE_DIR / 'writes.lock'} "
                             f"({e.strerror}); make {CACHE_DIR} writable, then run again")
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as e:
                raise CountError(f"cannot lock rs-sync's cache {CACHE_DIR / 'writes.lock'} "
                                 f"({e.strerror}); run again")
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                shared = []
            except OSError as e:
                raise CountError(f"cannot read rs-sync's count of recent GitHub writes, {path} "
                                 f"({e.strerror}); delete it, then run again")
            else:
                try:
                    shared = json.loads(raw)
                    # Times rs-sync wrote: numbers, finite, none after now
                    # (each is taken before its own save).
                    if not isinstance(shared, list) or not all(
                            isinstance(t, (int, float)) and not isinstance(t, bool)
                            and math.isfinite(t) and t <= clock() for t in shared):
                        raise ValueError
                except (ValueError, OverflowError, RecursionError):
                    raise CountError(f"rs-sync's count of recent GitHub writes, {path}, is not a "
                                     f"list of times it can read, or holds a time after now (the "
                                     f"clock moved back, or the file was edited); delete it, then "
                                     f"run again")
            now = clock()
            self.writes = sorted({t for t in list(shared) + self.writes if now - t < 3600})
            if record is not None or save:
                tmp = path.with_name(path.name + ".tmp")
                try:
                    tmp.write_text(json.dumps(self.writes))
                    os.replace(tmp, path)
                except OSError as e:
                    raise CountError(f"cannot write rs-sync's count of recent GitHub writes, {path} "
                                     f"({e.strerror}); make {CACHE_DIR} writable, then run again")
        finally:
            os.close(fd)

    def check_count(self):
        """Stop on a failed save of the shared count (_record_write): before
        the next write, before an import, and at the end of a run."""
        if self.share_error is not None:
            raise self.share_error

    def _record_write(self):
        """Count a write. When the shared count cannot be saved, the write
        stays counted here and the run stops before its next write (call),
        so the caller still records the write that landed."""
        try:
            self.share_writes(record=time.time())
        except CountError as e:
            self.share_error = e

    def _pace(self, clock=time.time, sleep=None):
        """Wait until the next content write fits GitHub's secondary limits:
        the hourly and per-minute counts over the writes made so far (by every
        rs-sync run of this container, share_writes), and at least
        MIN_WRITE_GAP_S since the last one."""
        if _TEST_FAST:
            return

        def nap(seconds, why):
            if sleep is not None:
                sleep(seconds)
            elif why:
                _wait(seconds, why, verb="paused")
            else:
                time.sleep(seconds)
        self.share_writes(clock=clock)
        now = clock()
        self.writes = [t for t in self.writes if now - t < 3600]
        if len(self.writes) >= CONTENT_PER_HOUR:
            nap(max(0.0, self.writes[-CONTENT_PER_HOUR] + 3600 - now),
                f"GitHub allows {CONTENT_PER_HOUR} writes an hour")
            now = clock()
        recent = [t for t in self.writes if now - t < 60]
        if len(recent) >= CONTENT_PER_MINUTE:
            nap(max(0.0, recent[-CONTENT_PER_MINUTE] + 60 - now),
                f"GitHub allows {CONTENT_PER_MINUTE} writes a minute")
            now = clock()
        if self.writes:
            gap = max(MIN_WRITE_GAP_S, 60.0 / CONTENT_PER_MINUTE) - (now - self.writes[-1])
            if gap > 0:
                nap(gap, "")

    def _no_answer_wait(self, what, waited) -> int:
        """Wait out one missing answer of a read and return the request's new
        no-answer total; stop the run once NO_ANSWER_MAX_S would be passed."""
        if waited + NO_ANSWER_WAIT_S > NO_ANSWER_MAX_S:
            die(f"{what}; waited {waited} s; run again later")
        say(f"  {what}; trying again in {NO_ANSWER_WAIT_S} s")
        _wait(NO_ANSWER_WAIT_S, what)
        return waited + NO_ANSWER_WAIT_S

    def call(self, method, path, body=None, accept=None, full_url=None, ok404=False,
             write=None, repeatable=None, confirm=None):
        """One request. `repeatable`: a write that is safe to send twice
        (default: PUT, PATCH and DELETE; every PATCH rs-sync sends carries
        absolute values), tried again after a missing answer like a read.
        After such an attempt, a DELETE answered 404/410 counts as done (the
        first one landed), and a 4xx on a write with `confirm` counts as done
        when confirm() says the change is in place."""
        url = full_url or f"{self.base}{path}"
        # What every message names: the path, or the path part of the full
        # URL (the import-status poll passes an empty path).
        where = path or urlsplit(url).path
        if write is None:
            write = method in ("POST", "PATCH", "PUT", "DELETE")
        if repeatable is None:
            repeatable = method in ("PUT", "PATCH", "DELETE")
        waited = 0.0
        no_answer_waited = 0
        limits = 0                  # limit answers that named no usable wait
        unanswered = False          # an attempt of this request got no answer
        while True:
            # Reads and repeatable writes, and only once GitHub has answered
            # this run (a 502/503/504 never counts as that answer: see
            # self.answered).
            retry_ok = self.answered and (not write or repeatable)
            if write:
                self.check_count()
                self._pace()
                # Counted before the send: a count of requests that left
                # rs-sync, so 0 means nothing reached GitHub (the summary).
                REPORT["writes_sent"] = REPORT.get("writes_sent", 0) + 1
                PROGRESS.writing(len(self.writes))
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
                try:
                    text = e.read().decode("utf-8", "replace")
                except (OSError, http.client.HTTPException):
                    text = ""           # a cut-off error body reads as an empty one
                if e.code not in NO_ANSWER_STATUSES:
                    self.answered = True
                limited = e.code == 429 or (e.code == 403 and (
                    "rate limit" in text.lower()
                    or headers.get("x-ratelimit-remaining") == "0"
                    or headers.get("retry-after")))
                if limited:
                    wait = _limit_wait(headers)
                    over = (f"GitHub's rate limit did not clear within "
                            f"{RATE_WAIT_MAX_S} s ({method} {where}); run "
                            f"again later: the markers say what is done")
                    if wait is None:        # no usable wait named: doubling
                        # Never less than GitHub's minute: stop when less is left.
                        if RATE_WAIT_MAX_S - waited < NO_HEADER_WAIT_S:
                            die(over)
                        wait = min(NO_HEADER_WAIT_S * LIMIT_BACKOFF_FACTOR ** limits,
                                   RATE_WAIT_MAX_S - waited)
                        limits += 1
                    elif waited + wait > RATE_WAIT_MAX_S:
                        die(over)
                    say(f"  rate limited; waiting {int(wait)} s")
                    _wait(wait, "GitHub's rate limit")
                    waited += wait
                    continue
                if e.code in NO_ANSWER_STATUSES and retry_ok:
                    if write:               # GitHub may have counted it
                        self._record_write()
                    unanswered = True
                    no_answer_waited = self._no_answer_wait(
                        f"GitHub answered HTTP {e.code} (busy) ({method} {where})",
                        no_answer_waited)
                    continue
                if e.code in (404, 410) and ok404:
                    return e.code, None, headers
                if write:
                    self._record_write()
                if unanswered and method == "DELETE" and e.code in (404, 410):
                    return e.code, None, headers        # the first delete landed
                if unanswered and confirm is not None and 400 <= e.code < 500:
                    try:
                        if confirm():
                            return e.code, None, headers
                    except SyncError:
                        pass                # GitHub's error for the write stands
                raise SyncError(f"GitHub {method} {where} answered HTTP {e.code}: "
                                f"{_gh_error_text(text, self.token)}", status=e.code)
            except (URLError, OSError, http.client.HTTPException) as e:
                reason = getattr(e, "reason", None) or e
                if retry_ok and _no_answer(e):
                    if write:               # GitHub may have counted it
                        self._record_write()
                    unanswered = True
                    no_answer_waited = self._no_answer_wait(
                        f"GitHub did not answer ({method} {where}: {reason})",
                        no_answer_waited)
                    continue
                if write:                   # GitHub may have counted it
                    self._record_write()
                die(f"GitHub did not answer ({method} {where}: {reason})")
            self.answered = True
            if write:
                self._record_write()
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
        what = path.split("/", 4)[-1] if path.startswith("/repos/") else path.strip("/")
        page = 0
        while url:
            page += 1
            PROGRESS.reading(f"reading GitHub: {what}, page {page}")
            _, body, headers = self.call("GET", path, accept=accept, full_url=url)
            if not isinstance(body, list):
                die(f"GitHub GET {path}: not a list")
            rows.extend(body)
            url = _next_link(headers.get("Link") or "")
        return rows

    def graphql(self, query, variables, repeatable=False):
        # A query is a read (no pacing); a mutation creates or changes content,
        # and is tried again after a missing answer only when `repeatable`
        # (it sets an absolute value: sending it twice changes nothing).
        _, out, _ = self.call("POST", "/graphql",
                              {"query": query, "variables": variables},
                              write=query.lstrip().startswith("mutation"),
                              repeatable=repeatable)
        if not isinstance(out, dict) or out.get("errors"):
            errs = out.get("errors") if isinstance(out, dict) else "the answer was not a JSON object"
            die(f"GitHub GraphQL failed: {_gh_error_text(json.dumps({'errors': errs}), self.token)}")
        return out.get("data")


def _no_answer(e) -> bool:
    """Whether a network failure is a missing answer, worth trying again on a
    read: a timeout, or a connection dropped or cut off (mid-answer, or a TLS
    handshake the far end closed), directly or as a URLError's reason. GitHub
    being unreachable is not: a refused connection, a name that does not
    resolve, an unreachable network, a certificate that does not verify, a
    malformed URL; those stop the run at once."""
    if isinstance(e, URLError):
        e = e.reason
    if isinstance(e, ConnectionRefusedError):
        return False
    return isinstance(e, (TimeoutError, ConnectionError, ssl.SSLEOFError,
                          http.client.IncompleteRead, http.client.BadStatusLine))


def _limit_wait(headers) -> "float | None":
    # A retry-after of GitHub's documented minute ("wait for at least one
    # minute before retrying") or more is waited exactly. A shorter one is no
    # usable wait unless a reset time (with x-ratelimit-remaining 0) at least
    # that minute away comes with it: a "retry now" (0) would otherwise send
    # the request again at once, for as long as GitHub keeps answering that
    # way, while GitHub warns that requests sent while limited may get an
    # integration banned. A reset at least a second ahead, with no
    # retry-after, is waited exactly.
    # None: no usable wait was named, and the caller waits NO_HEADER_WAIT_S,
    # doubling at each further such answer (LIMIT_BACKOFF_FACTOR).
    ra = headers.get("retry-after")
    short = False
    if ra and ra.strip().isdigit():
        if float(ra.strip()) >= NO_HEADER_WAIT_S:
            return float(ra.strip())
        short = True                # under the minute: only a reset beyond it counts
    if headers.get("x-ratelimit-remaining") == "0":
        reset = headers.get("x-ratelimit-reset")
        if reset and reset.strip().isdigit():
            left = float(reset) - time.time()
            # A reset already past (a skewed clock) says "retry now": as above.
            if left >= (NO_HEADER_WAIT_S if short else 1.0):
                return left
    return None


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


# The longest GitHub error text an error line carries. GitHub's own JSON
# errors are a sentence or two; a proxy's HTML error page runs to kilobytes,
# and 300 characters keep its first line or two without flooding the terminal.
ERROR_TEXT_MAX = 300


def _gh_errors(errs) -> str:
    """GitHub's `errors` list (REST validation entries, GraphQL errors, an
    import's errors) as one line: each entry's field, code and message."""
    if not isinstance(errs, list):
        return "" if errs is None else str(errs)
    out = []
    for e in errs:
        if isinstance(e, dict):
            field = e.get("field") or e.get("resource") or ""
            words = [str(e[k]) for k in ("code", "message") if e.get(k)]
            out.append((f"{field}: " if field else "") + " ".join(words))
        else:
            out.append(str(e))
    return "; ".join(x for x in out if x)


def _gh_error_text(raw: str, *secrets) -> str:
    """A readable GitHub error: the JSON body's message plus its `errors`
    entries (a 422's message alone is only "Validation Failed"); any other
    body as it is. Parsed before it is cut, redacted, then cut."""
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if isinstance(body, dict) and (body.get("message") or body.get("errors")):
        msg = str(body.get("message") or "").strip()
        details = _gh_errors(body.get("errors"))
        text = f"{msg} ({details})" if msg and details else (msg or details)
    else:
        text = raw.strip()
    text = _safe(text, *secrets)
    return text if len(text) <= ERROR_TEXT_MAX else text[:ERROR_TEXT_MAX - 1] + "…"


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
    PROGRESS.reading("reading Gitea: issues, pull requests and comments")
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
    n_merged = sum(1 for p in pulls if p.get("merged"))
    for p in pulls:
        if not p.get("merged"):
            continue
        n = p["number"]
        PROGRESS.reading(f"reading Gitea: pull request {len(merged) + 1} of {n_merged}")
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

def with_rs_label(labels) -> list:
    """The item's labels with rs-sync's own label, replacing any spelling of
    it Gitea carries."""
    labels = [l for l in labels if l.lower() != RS_LABEL] + [RS_LABEL]
    return labels


# The Gitea label that keeps an issue or a merged PR off GitHub for good:
# never imported, and a copy already there is deleted at the next run. Its
# name is fixed, matched exactly but ignoring case, like every label
# comparison here.
SKIP_LABEL = "sync-skip"


def _has_skip_label(names) -> bool:
    return any((n or "").lower() == SKIP_LABEL for n in names)


def skipped_numbers(gt: dict) -> dict:
    """The Gitea numbers kept off GitHub by the skip label: issues (any
    state) and merged PRs. Issues and PRs share one numbering."""
    return {"issues": sorted(i["number"] for i in gt["issues"]
                             if _has_skip_label(l.get("name") for l in i.get("labels") or [])),
            "prs": sorted(p["number"] for p in gt["merged"] if _has_skip_label(p["labels"]))}


def _skip_set(gt: dict) -> set:
    k = skipped_numbers(gt)
    return set(k["issues"]) | set(k["prs"])


def desired_items(gt: dict, owner, repo, hold, existing_sources) -> list:
    """Every Gitea item that should be on GitHub, in number order. An open
    issue carrying a hold label is left out unless it is already mirrored
    (gaining a hold label after it went public changes nothing). An item
    carrying the skip label is always left out."""
    items = []
    kept_off = _skip_set(gt)
    for i in gt["issues"]:
        labels = [l["name"] for l in i.get("labels") or []]
        is_open = i.get("state") == "open"
        if i["number"] in kept_off:
            continue
        if is_open and held(labels, hold) and i["number"] not in existing_sources:
            continue
        items.append({"n": i["number"], "pr": False, "title": i.get("title") or "",
                      "body": i.get("body") or "",
                      "user": (i.get("user") or {}).get("login") or "",
                      "created_at": i.get("created_at"),
                      "updated_at": i.get("updated_at"),
                      "closed_at": i.get("closed_at"),
                      "closed": not is_open, "labels": with_rs_label(labels),
                      "comments": sorted(gt["comments"].get(i["number"], []),
                                         key=lambda c: (c["created_at"] or "", c["id"]))})
    for p in gt["merged"]:
        if p["number"] in kept_off:
            continue
        comments = gt["comments"].get(p["number"], []) + p["extra"]
        items.append({"n": p["number"], "pr": True, "title": "[PR] " + p["title"],
                      "body": p["body"], "user": p["user"],
                      "created_at": p["created_at"], "updated_at": p.get("updated_at"),
                      "closed_at": p["closed_at"],
                      "closed": True, "labels": with_rs_label(p["labels"] + [PR_LABEL]),
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
    # added after conversion: the tag is constant text the converter never sees
    t = (TITLE_TAG + t).rstrip()
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
            raise DuplicateClaim(f"GitHub issues #{items[m['n']]['number']} and #{i['number']} "
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
                raise DuplicateClaim(f"two GitHub comments on #{n} claim part {m['part']} of its "
                                     f"body; delete one of them, then run again")
            items[src]["parts"][m["part"]] = rec
            continue
        if m["kind"] is None:
            continue
        key = (m["kind"], m["cid"], m["part"] or 0)
        if key in items[src]["comments"]:
            raise DuplicateClaim(f"two GitHub comments on #{n} claim the same Gitea {m['kind']} "
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
    # From here every write is counted with the other runs' (a file rs-sync
    # cannot read or write stops the run now, before any write).
    gh.account = me["id"]
    gh.share_writes(save=True)
    REPORT["writes_this_hour"] = len(gh.writes)
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
            f"token, or run without --board")
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


def do_import(gh: GitHub, target, payload, n_gitea) -> int:
    """Send one import and wait for GitHub's answer. While it runs,
    REPORT["importing"] names the Gitea item, so a run that stops here ends
    with advice (main); it is cleared only on a definite answer: imported
    with its issue named, failed, a 4xx refusal of the POST, or a CountError
    (the shared write count could not be read, locked or saved) raised before
    the send of an attempt GitHub could have accepted."""
    o, r = target.split("/", 1)
    what = f"Gitea #{n_gitea}"
    REPORT["importing"] = n_gitea
    try:
        _, res, _ = gh.call("POST", f"/repos/{quote(o)}/{quote(r)}/import/issues",
                            payload, accept=IMPORT_ACCEPT)
    except SyncError as e:
        # A 4xx refusal, or a CountError raised before the send of the
        # attempt: this import is not on GitHub.
        if isinstance(e, CountError) or (e.status is not None and 400 <= e.status < 500):
            REPORT.pop("importing", None)
        die(f"{what}: {e}")
    url = (res if isinstance(res, dict) else {}).get("url")
    if not url:
        die(f"{what}: GitHub's import answered without a status URL")
    polls = 0
    while True:
        _, st, _ = gh.call("GET", "", accept=IMPORT_ACCEPT, full_url=url)
        polls += 1
        if not isinstance(st, dict):
            die(f"{what}: GitHub's import status answer was not a JSON object")
        status = st.get("status")
        if status not in IMPORT_STATUSES:
            die(f"{what}: GitHub's import status is {status!r}, not one of "
                f"{', '.join(IMPORT_STATUSES)}")
        if status == "imported":
            n = _number_from_url(st.get("issue_url") or "")
            if n is None:       # created, number unknown: keep the advice
                die(f"{what}: GitHub's import finished without naming the issue")
            REPORT.pop("importing", None)
            return n
        if status == "failed":
            REPORT.pop("importing", None)
            why = _gh_error_text(json.dumps({"errors": st.get("errors")}), gh.token) \
                if st.get("errors") else "GitHub gave no reason"
            die(f"{what}: GitHub's import failed: {why}")
        if polls == 2:
            say(f"  waiting for GitHub to import {what}…")
        _wait(IMPORT_POLL_S, f"GitHub is importing {what}", fast=0.05)


def label_payloads(gt_labels, wanted) -> dict:
    """GitHub label payloads for the label names in use, from Gitea's labels
    (colour, description cut to fit with the tag)."""
    by = {l["name"]: l for l in gt_labels}
    out = {}
    for name in sorted(wanted):
        if name == PR_LABEL:
            desc, color = PR_LABEL_DESC, PR_LABEL_COLOR
        elif name == RS_LABEL:
            desc, color = RS_LABEL_DESC, RS_LABEL_COLOR
        else:
            l = by.get(name) or {}
            desc = (l.get("description") or "").strip()
            color = (l.get("color") or "ededed").lstrip("#")[:6] or "ededed"
        room = GITHUB_LABEL_DESC_MAX - len(LABEL_TAG)
        if len(desc) > room:
            desc = desc[:room - 1] + "…"
            REPORT["label_descriptions_cut"] = REPORT.get("label_descriptions_cut", 0) + 1
        out[name] = {"name": name, "color": color, "description": tagged(desc)}
    return out


def tagged(desc: str) -> str:
    """A label description with rs-sync's tag. An empty description gets the
    tag without its leading space: GitHub trims a description, so " (rs-sync)"
    would be stored as "(rs-sync)" anyway (measured on a real mirror)."""
    return desc + LABEL_TAG if desc else LABEL_TAG.strip()


def made_by_rs_sync(label) -> bool:
    """Whether a GitHub label carries rs-sync's tag: a description of exactly
    "(rs-sync)" (an empty Gitea description, as GitHub stores it) or ending in
    " (rs-sync)". This one test decides both which labels the sync reuses as a
    person's and which ones --remove deletes."""
    d = label.get("description") or ""
    return d == LABEL_TAG.strip() or d.endswith(LABEL_TAG)


def label_plan(gt_labels, gh_labels, items) -> tuple:
    """Match the items' labels to GitHub's, ignoring case. A name already on
    GitHub takes GitHub's spelling, and a repeat on one item (two spellings of
    one name) is dropped, so the relabel check settles. Returns the payloads
    of the names missing from GitHub, to create tagged, and the labels in use
    that GitHub already has and rs-sync did not make (used as they are, never
    changed or deleted)."""
    on_gh = {l["name"].lower(): l for l in gh_labels}
    reused = set()
    for it in items:
        names, seen = [], set()
        for name in it["labels"]:
            cur = on_gh.get(name.lower())
            if cur is not None:
                name = cur["name"]
                if not made_by_rs_sync(cur):
                    reused.add(name)
            if name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
        it["labels"] = names
    missing = {n for it in items for n in it["labels"] if n.lower() not in on_gh}
    return list(label_payloads(gt_labels, missing).values()), sorted(reused, key=str.lower)


def plan_writes(plan, to_create, new_items, bp) -> int:
    """The GitHub writes a plan makes (the wiki's one push aside): labels,
    each import and its lock, the planned item changes, and the board's (bp:
    its dry pass's counts, or None). Creating a project is three writes; an
    item still to be imported is an add and two values."""
    n = len(to_create) + 2 * len(new_items)
    n += sum(len(plan[k]) for k in ("edit", "comment_new", "comment_edit", "comment_delete",
                                    "state", "labels", "lock", "delete", "skip_delete"))
    if bp:
        n += (3 if bp.get("create_project") else 0) + 3 * bp.get("after_import", 0) \
            + sum(bp.get(k, 0) for k in ("project_settings", "fields", "add", "set",
                                         "remove", "move"))
    return n


def estimate_minutes(writes: int, used: int = 0) -> float:
    """Minutes the pacing needs for `writes` content writes when `used` were
    already made in the past hour: every full hourly allowance costs an hour,
    the rest goes at the per-minute pace. The used writes are taken as just
    made (the most the wait for them can be)."""
    if writes <= 0:
        return 0.0
    per_min = min(CONTENT_PER_MINUTE, 60.0 / MIN_WRITE_GAP_S)
    # The 500th write of an hour still fits it (_pace waits only past it).
    full, rest = divmod(used + writes - 1, CONTENT_PER_HOUR)
    return round(max(0.0, full * 60 + (rest + 1) / per_min - used / per_min), 1)


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


def _item_kinds(items) -> dict:
    return {"issues_open": sum(1 for i in items if not i["pr"] and not i["closed"]),
            "issues_closed": sum(1 for i in items if not i["pr"] and i["closed"]),
            "prs": sum(1 for i in items if i["pr"])}


def sync(args, env) -> None:
    owner, repo = parse_repo_arg(args.gitea, "the Gitea repo")
    target = "/".join(parse_repo_arg(args.github, "the GitHub repo"))
    apply = args.apply
    secrets = [env["RS_SYNC_GITHUB_TOKEN"]] + all_dev_tokens()
    gh = GitHub(GITHUB_API, env["RS_SYNC_GITHUB_TOKEN"])
    skip = {p for p in ("wiki", "board") if not getattr(args, p)}
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
    REPORT["skipped"] = skipped_numbers(gt)
    kept_off = _skip_set(gt)
    # Mirrored copies of skipped items: deleted first in the apply. The dry
    # run lists the same set (both read the same `have`).
    skip_del = sorted(n for n in have["items"] if n in kept_off)
    refuse_missing_node_ids((have["items"][n]["number"], have["items"][n]["node_id"])
                            for n in skip_del)
    REPORT["held_open_issues"] = sorted(
        i["number"] for i in gt["issues"]
        if i.get("state") == "open" and i["number"] not in {x["n"] for x in items}
        and i["number"] not in kept_off)
    # For the summary: what is (or will be) on GitHub, and who can see it.
    REPORT["github"]["private"] = bool(repo_info.get("private"))
    REPORT["hold"] = env["RS_SYNC_HOLD_LABELS"]
    REPORT["items"] = _item_kinds(items)

    # A skipped item is never linked, even while its copy still exists.
    table = {n: v["number"] for n, v in have["items"].items() if n not in kept_off}
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
    missing_bases = set(REPORT.get("pr_bases_missing_on_github") or [])
    per_base = {}
    for it in items:
        if it["pr"] and it.get("base") in missing_bases:
            d = per_base.setdefault(it["base"], {"prs": 0, "unlinked": 0})
            d["prs"] += 1
            d["unlinked"] += len(it.get("subjects") or [])
    REPORT["pr_bases_missing"] = per_base

    # Labels: a name missing from GitHub is created, tagged; one already
    # there (compared ignoring case) is used in GitHub's spelling, and when
    # rs-sync did not make it, as it is.
    o, r = target.split("/", 1)
    base = f"/repos/{quote(o)}/{quote(r)}"
    to_create, reused = label_plan(gt["labels"], gh.all(f"{base}/labels"), items)
    REPORT["labels_reused"] = reused

    # The wiki and the board: every read and refusal before any write.
    wanted = {it["n"] for it in items if not it["closed"] and not it["pr"]}
    wiki_state = board_state = None
    if "wiki" in skip:
        REPORT["wiki"] = {"skipped": "add --wiki to include it"}
    else:
        wiki_state = wiki_prepare(args, owner, repo, target, repo_info, gt["info"], gtok,
                                  env["RS_SYNC_GITHUB_TOKEN"], secrets, "sync")
    if "board" in skip:
        REPORT["board"] = {"skipped": "add --board to include it"}
    else:
        board_state = board_prepare(g, gh, owner, repo, target, secrets,
                                    public=not repo_info.get("private"))
        board_desired(board_state["boards"], wanted)      # refuses an issue on two boards

    plan = {"create_labels": [p["name"] for p in to_create], "import": [],
            "edit": [], "comment_new": [], "comment_edit": [], "comment_delete": [],
            "state": [], "labels": [], "lock": [], "delete": [],
            "skip_delete": [have["items"][n]["number"] for n in skip_del]}

    new_items = [it for it in items if it["n"] not in have["items"]]
    for it in new_items:
        plan["import"].append(it["n"])
    REPORT["new_items"] = _item_kinds(new_items)
    if not apply:
        _fill_existing_plan(plan, items, have, ctx, rid, env, subjects_index, gt, g, owner, repo,
                            dry=True, kept_off=kept_off)
        if wiki_state:
            wiki_apply(wiki_state, Ctx(owner, repo, target, table, wiki=True), me,
                       env["RS_SYNC_GITHUB_TOKEN"], secrets, dry=True)
        bp = None
        if board_state:
            node_of = {n: v["node_id"] for n, v in have["items"].items()}
            board_apply(gh, board_state, owner, repo, target, wanted, node_of, dry=True)
            bp = REPORT["board"]
        writes_est = plan_writes(plan, to_create, new_items, bp)
        REPORT["plan"] = {k: (v if len(v) <= 40 else v[:40] + [f"… {len(v) - 40} more"])
                          for k, v in plan.items()}
        REPORT["plan_counts"] = {k: len(v) for k, v in plan.items()}
        # distinct issues per kind (a comment kind repeats its issue per comment)
        REPORT["plan_issues"] = {k: len(set(v)) for k, v in plan.items()}
        REPORT["estimated_writes"] = writes_est
        REPORT["estimated_minutes"] = estimate_minutes(writes_est,
                                                       REPORT.get("writes_this_hour") or 0)
        REPORT["dry_run"] = True
        return

    done = {"labels_created": [], "imported": {}, "edited": [], "comments_posted": 0,
            "comments_edited": 0, "comments_deleted": 0, "closed_or_reopened": [],
            "relabelled": [], "locked": [], "deleted": [], "skip_deleted": []}
    REPORT["done"] = done
    # Skipped items' copies go first, before any other write: an unrelated
    # failure later in the run must not keep them public.
    for n in skip_del:
        v = have["items"][n]
        delete_issue(gh, v["node_id"], v["number"])
        done["skip_deleted"].append(v["number"])
        say(f"  deleted GitHub #{v['number']} (Gitea #{n} carries {SKIP_LABEL})")
    # The total the progress line counts towards: the dry run's own plan,
    # worked out after the sync-skip deletions (they go first) and before any
    # other write. Its refusals stop the run here, before those writes: the
    # real pass would refuse the same Gitea state after the imports. REPORT
    # is restored from a shallow snapshot (the pass only adds keys), which
    # keeps `done` the same object; the wiki's dry pass is left out, as the
    # wiki push is outside the estimate.
    snapshot = dict(REPORT)
    try:
        pplan = {k: [] for k in plan}
        _fill_existing_plan(pplan, items, have, ctx, rid, env, subjects_index, gt, g, owner,
                            repo, dry=True, kept_off=kept_off)
        bp = None
        if board_state:
            board_apply(gh, board_state, owner, repo, target, wanted,
                        {n: v["node_id"] for n, v in have["items"].items()}, dry=True)
            bp = dict(REPORT["board"])
        total = plan_writes(pplan, to_create, new_items, bp)
    finally:
        REPORT.clear()
        REPORT.update(snapshot)
    PROGRESS.set_total((REPORT.get("writes_sent") or 0) + total)
    check_secrets([p["name"] + " " + p["description"] for p in to_create], secrets,
                  "a Gitea label")
    for pl in to_create:
        gh.call("POST", f"{base}/labels", pl)
        done["labels_created"].append(pl["name"])

    # Imports, in Gitea number order, each awaited before the next.
    sent = {}
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
        # What the listing must show before the run goes on: the body's
        # continuation parts and the comments, by their marker keys.
        sent[it["n"]] = (set(range(1, len(parts))),
                         {(c["kind"], c["id"], k) for c in it["comments"]
                          for k, _ in enumerate(comment_parts(ctx, it, c, rid))})
        table[it["n"]] = n
        say(f"  imported Gitea #{it['n']} as GitHub #{n}")

    # Re-read GitHub, then bring every mirrored item level (forward
    # references, edits, new comments, state, labels, lock, deletions).
    have = wait_for_imports(gh, target, me["id"], owner, repo, done["imported"], sent)
    for n, v in have["items"].items():
        if n not in kept_off:
            table[n] = v["number"]
    _fill_existing_plan(plan, items, have, ctx, rid, env, subjects_index, gt, g, owner, repo,
                        dry=False, gh=gh, base=base, done=done, secrets=secrets,
                        kept_off=kept_off)
    if wiki_state:
        wiki_apply(wiki_state, Ctx(owner, repo, target, table, wiki=True), me,
                   env["RS_SYNC_GITHUB_TOKEN"], secrets, dry=False)
    if board_state:
        node_of = {n: v["node_id"] for n, v in have["items"].items()}
        board_apply(gh, board_state, owner, repo, target, wanted, node_of, dry=False)
    gh.check_count()                # a save that failed after the last write
    REPORT["dry_run"] = False


def wait_for_imports(gh: GitHub, target, me_id, owner, repo, imported, sent) -> dict:
    """Read GitHub until every item this run imported is listed, as the issue
    this run made, with the body parts and comments its import sent. Missing:
    GitHub's listing lags; a run going on would post those comments again,
    and a run started now would import the item again, so the listing is
    read again every LIST_LAG_WAIT_S, for at most LIST_LAG_MAX_S, with
    REPORT["lagging"] naming the items (the advice of a run stopped then).
    Another number: an older copy (an import from an interrupted run that
    landed late) claims the item, and the run stops before any edit."""
    lag_waited = 0
    while True:
        try:
            have = read_github(gh, target, me_id, owner, repo)
        except DuplicateClaim:
            REPORT.pop("lagging", None)         # not a lag: the advice would mislead
            raise
        lagging = []
        for gn, num in sorted(imported.items()):
            listed = have["items"].get(gn)
            if listed is None:
                lagging.append((gn, num, "the issue"))
                continue
            if listed["number"] != num:
                REPORT.pop("lagging", None)
                raise DuplicateClaim(f"GitHub issues #{listed['number']} and #{num} both claim "
                                     f"Gitea #{gn}; delete one of them, then run again")
            parts, comments = sent.get(gn, (set(), set()))
            if not parts <= set(listed["parts"]) or not comments <= set(listed["comments"]):
                lagging.append((gn, num, "all of its comments"))
        if not lagging:
            REPORT.pop("lagging", None)
            return have
        REPORT["lagging"] = [gn for gn, _, _ in lagging]
        gn, num, what = lagging[0]
        more = f" (and {len(lagging) - 1} more)" if len(lagging) > 1 else ""
        if lag_waited + LIST_LAG_WAIT_S > LIST_LAG_MAX_S:
            die(f"GitHub has imported Gitea #{gn} as #{num} but does not list {what} "
                f"yet{more}; wait a few minutes, then run again")
        say(f"  GitHub has imported Gitea #{gn} as #{num} but does not list {what} "
            f"yet{more}; reading again in {LIST_LAG_WAIT_S} s")
        _wait(LIST_LAG_WAIT_S, "GitHub's listing lags behind the imports")
        lag_waited += LIST_LAG_WAIT_S


def _fill_existing_plan(plan, items, have, ctx, rid, env, subjects_index, gt, g, owner, repo,
                        dry, gh=None, base=None, done=None, secrets=(), kept_off=frozenset()):
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
                gh.call("PUT", f"{base}/issues/{num}/lock", {},
                        confirm=lambda num=num: _issue_locked(gh, base, num))
                done["locked"].append(num)
    # Mirrored items whose Gitea source is gone (a direct read says 404).
    # Skipped items are deleted on the Gitea read's own evidence, not here.
    missing = [n for n in have["items"] if n not in by_n and n not in kept_off]
    if missing and not items:
        REPORT["not_deleted_unverifiable"] = REPORT.get("not_deleted_unverifiable", []) \
            + [f"Gitea #{n}" for n in missing]
        missing = []                # no known item to sanity-check the route with
    if missing:
        st = _gitea_status(g, f"/repos/{quote(owner)}/{quote(repo)}/issues/{items[0]['n']}")
        if st != 200:
            die(f"Gitea's issue read answered HTTP {st} for one that exists; "
                f"no issue whose Gitea item is gone was deleted")
    for n in sorted(missing):
        if _held_open(gt, n):
            continue
        status = _gitea_status(g, f"/repos/{quote(owner)}/{quote(repo)}/issues/{n}")
        if status != 404:
            if status == 200:
                continue
            die(f"cannot tell whether Gitea #{n} still exists (HTTP {status}); "
                f"its copy, GitHub #{have['items'][n]['number']}, was not deleted")
        num = have["items"][n]["number"]
        plan["delete"].append(num)
        if not dry:
            delete_issue(gh, have["items"][n]["node_id"], num)
            done["deleted"].append(num)


def _issue_locked(gh: GitHub, base, num) -> bool:
    """Whether GitHub shows the issue locked (a lock that got no answer, then
    a refusal of its repeat: the first one may have landed)."""
    _, i, _ = gh.call("GET", f"{base}/issues/{num}")
    return isinstance(i, dict) and bool(i.get("locked"))


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
                f"exists; no GitHub copy of a Gitea #{n} comment was deleted")
        st = _gitea_status(g, route(kind, n, cid))
        if st == 404:
            out[(kind, cid)] = True
        elif st == 200:
            out[(kind, cid)] = False
        else:
            die(f"cannot tell whether Gitea {kind} {cid} still exists (HTTP "
                f"{st}); no GitHub copy of a Gitea #{n} comment was deleted")
    return out


def _review_comment_gone(g, base, n, cid) -> bool:
    for rv in g.all(f"{base}/pulls/{n}/reviews"):
        for rc in g.all(f"{base}/pulls/{n}/reviews/{rv['id']}/comments"):
            if rc.get("id") == cid:
                return False
    return True


def delete_issue(gh: GitHub, node_id, number):
    if not node_id:
        die(f"GitHub #{number} has no node id; it was not deleted")
    gh.graphql("mutation($id:ID!){deleteIssue(input:{issueId:$id}){clientMutationId}}",
               {"id": node_id})


def refuse_missing_node_ids(numbers_and_nodes) -> None:
    """Before the first deletion of a list: every issue in it must carry a
    node id, so a refusal leaves nothing deleted."""
    bad = [num for num, node in numbers_and_nodes if not node]
    if bad:
        die(f"GitHub {', '.join(f'#{b}' for b in bad)} {'has' if len(bad) == 1 else 'have'} "
            f"no node id; nothing was deleted")


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
# terminal for most of an hour.
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
            "without --wiki)")
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
    the run: an unreadable wiki must never read as an empty one. Gitea answers
    a switched-on wiki that never had a page with "wiki doesn't exist, please
    initialize the wiki by creating a new page first"."""
    rc, out, err = cache.git(["ls-remote", "--symref", url, "HEAD"], token=token,
                             what=f"reading the {side} wiki")
    if rc:
        low = err.lower()
        if "not found" in low or "does not appear to be a git repository" in low \
                or "wiki is disabled" in low or "wiki doesn't exist" in low:
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
                f"earlier state to restore. Delete its pages in the Wiki tab, or run "
                f"without --wiki")
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
            f"settings, or run without --wiki")
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
    REPORT["writes_sent"] = REPORT.get("writes_sent", 0) + 1
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
    REPORT["writes_sent"] = REPORT.get("writes_sent", 0) + 1
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
        gh.graphql(M_UPDATE, {"p": pid, "d": desc, "pub": public}, repeatable=True)
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
        plan["skipped"] = "the fork has no open Gitea boards with columns"   # removals: plan["remove"]
        remove_unwanted(set())
        return
    if cur["desc"] != desc or bool(cur["public"]) != public:
        plan["project_settings"] += 1
        if not dry:
            gh.graphql(M_UPDATE, {"p": pid, "d": desc, "pub": public}, repeatable=True)
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
                    gh.graphql(M_SET, {"p": pid, "i": it["id"], "f": fid, "v": oid},
                               repeatable=True)
        order.append(it["id"])
    remove_unwanted({node_of.get(n) for n, _, _ in want["items"]})
    if dry and plan["add"]:
        return
    if plan["add"] and not dry:
        # Where GitHub places a new item is not measured: read the order back.
        cur = read_board(gh, pid)
    lag_waited = 0
    while True:
        listed = {i["id"] for i in cur["items"]}
        unlisted = [i for i in order if i not in listed]
        if not unlisted:
            break
        if dry or lag_waited + LIST_LAG_WAIT_S > LIST_LAG_MAX_S:
            die(f"GitHub does not list {len(unlisted)} new project item(s) yet; run again "
                f"shortly to put them in order")
        say(f"  GitHub does not list {len(unlisted)} new project item(s) yet; reading "
            f"the board again in {LIST_LAG_WAIT_S} s")
        _wait(LIST_LAG_WAIT_S, "GitHub's board lags behind the new items")
        lag_waited += LIST_LAG_WAIT_S
        cur = read_board(gh, pid)
    moves = plan_moves([i["id"] for i in cur["items"]], order)
    plan["move"] = len(moves)
    if not dry:
        for item, after in moves:
            gh.graphql(M_MOVE, {"p": pid, "i": item, "a": after}, repeatable=True)


# ============================================================================
# The remove
# ============================================================================

def remove(args, env) -> None:
    """Delete what rs-sync mirrored for the named fork, from GitHub alone:
    the marked issues by the token's account (whatever the fork's repo id) and
    the tagged labels no remaining issue carries; with --board also the marked
    project, with --wiki also the wiki as it was before rs-sync's first commit
    (the wiki and the project only with --wiki / --board). Every check comes
    before the first deletion."""
    owner, repo = parse_repo_arg(args.gitea, "the Gitea repo")
    target = "/".join(parse_repo_arg(args.github, "the GitHub repo"))
    gh = GitHub(GITHUB_API, env["RS_SYNC_GITHUB_TOKEN"])
    skip = {p for p in ("wiki", "board") if not getattr(args, p)}
    me, repo_info, _ = preflight_github(gh, target, board="board" not in skip)
    REPORT["github"] = {"repo": target, "account": me.get("login"),
                        "private": bool(repo_info.get("private"))}
    have = read_github(gh, target, me["id"], owner, repo, for_remove=True)
    wiki_state = None
    if "wiki" in skip:
        REPORT["wiki"] = {"skipped": "add --wiki to include it"}
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
              if made_by_rs_sync(l)
              and l["name"].lower() not in remaining_labels]
    REPORT["remove"]["labels"] = [l["name"] for l in labels]
    REPORT["remove"]["projects"] = [p.get("number") for p in projects]
    if "board" in skip:
        REPORT["board"] = {"skipped": "add --board to include it"}
    refuse_missing_node_ids((v["number"], v["node_id"]) for v in doomed)
    if not args.apply:
        REPORT["dry_run"] = True
        return
    # Each deletion is recorded as it happens, so a run that stops midway
    # reports what is already gone.
    rec = REPORT["remove"]
    rec["deleted"], rec["projects_deleted"], rec["labels_deleted"] = [], [], []
    for v in doomed:
        delete_issue(gh, v["node_id"], v["number"])
        rec["deleted"].append(v["number"])
    for p in projects:
        gh.graphql(M_DELETE_PROJECT, {"p": p["id"]})
        rec["projects_deleted"].append(p.get("number"))
    if wiki_state:
        wiki_restore(wiki_state, env["RS_SYNC_GITHUB_TOKEN"])
    for l in labels:
        gh.call("DELETE", f"{base}/labels/{quote(l['name'], safe='')}")
        rec["labels_deleted"].append(l["name"])
    gh.check_count()                # a save that failed after the last deletion
    REPORT["dry_run"] = False


# ============================================================================
# main
# ============================================================================

def print_report():
    print("===== PASTE FROM HERE =====")
    print(json.dumps(REPORT, indent=1, sort_keys=True, default=str))
    print("===== TO HERE =====")


def _ranges(nums) -> str:
    """GitHub numbers as compact ranges: #3, #10-#14."""
    nums = sorted({n for n in nums if isinstance(n, int)})
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(f"#{nums[i]}" if i == j else f"#{nums[i]}-#{nums[j]}")
        i = j + 1
    return ", ".join(out)


def _plural(n, one, many=None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _duration(seconds) -> str:
    s = int(round(seconds or 0))
    if s < 60:
        return f"{s} s"
    m = (s + 30) // 60
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h" + (f" {m % 60} min" if m % 60 else "")


def _projects(nums) -> str:
    nums = [n for n in nums or [] if isinstance(n, int)]
    return f"the GitHub project{'s' if len(nums) > 1 else ''} ({_ranges(nums)})" if nums \
        else "the GitHub project"


def _board_skip(b) -> list:
    """The board left out (and nothing of it to remove)."""
    if isinstance(b, dict) and b.get("skipped") and not b.get("remove"):
        return [f"Board: skipped ({b['skipped']})"]
    return []


def _board_lines(b, past: bool = False) -> list:
    """What the board step does (or did, `past`), from its counts: exact
    after a successful run, filled before their writes, so never shown after
    an error. Nothing when it has nothing to do or was left out."""
    if not isinstance(b, dict) or _board_skip(b):
        return []
    v = (lambda now, did: did if past else now)
    parts = []
    if b.get("create_project"):
        parts.append(f"{v('create', 'created')} the GitHub project with its "
                     f"{_plural(b.get('fields', 0), 'field')}")
    elif b.get("fields"):
        parts.append(f"{v('update', 'updated')} {_plural(b['fields'], 'field')}")
    if b.get("project_settings"):
        parts.append(f"{v('update', 'updated')} the project's settings")
    place = b.get("add", 0) + b.get("after_import", 0)
    if place:
        parts.append(f"{v('place', 'placed')} {_plural(place, 'issue')} on it")
    if b.get("set") and not place:
        parts.append(f"{v('set', 'set')} {_plural(b['set'], 'value')}")
    if b.get("remove"):
        why = " (the fork has no open Gitea boards with columns)" if b.get("skipped") else ""
        parts.append(f"{v('remove', 'removed')} {_plural(b['remove'], 'item')}{why}")
    if b.get("move"):
        parts.append(f"{v('reorder', 'reordered')} {_plural(b['move'], 'item')}")
    return ["Board: " + ", then ".join(parts)] if parts else []


def _wiki_skip(w) -> list:
    if isinstance(w, dict) and w.get("skipped"):
        return [f"Wiki: skipped ({w['skipped']})"]
    return []


def _wiki_lines(w, dry: bool) -> list:
    """What the wiki step does or did; nothing when it has nothing to do or
    was left out."""
    if not isinstance(w, dict) or w.get("skipped"):
        return []
    if "restore_to" in w:
        verb = "restored" if w.get("restored") else "restore"
        return [f"Wiki: {verb} to {w['restore_to']} (before rs-sync's "
                f"{_plural(w.get('rs_sync_commits', 0), 'commit')})"]
    if not w.get("changed"):
        return []
    what = f"{_plural(w.get('pages', 0), 'page')} and {_plural(w.get('files', 0), 'other file')}"
    if dry:
        return [f"Wiki: one commit ({what})"]
    if w.get("commit"):
        return [f"Wiki: committed {w['commit']} ({what})"]
    return []


def _checks(r: dict, dry: bool) -> list:
    """What the maintainer should look at, what becomes visible first. After
    an error nothing is claimed about what was published: the run did not
    finish."""
    out = []
    new = r.get("new_items") or {}
    private = (r.get("github") or {}).get("private")
    held = len(r.get("held_open_issues") or [])
    hold = f"RS_SYNC_HOLD_LABELS={r.get('hold') or '(empty: holds nothing)'}"
    if (new.get("issues_open") or held) and not r.get("error"):
        seen = "visible to the repo's collaborators" if private else "public"
        if dry:
            out.append(f"Open issues: {new.get('issues_open', 0)} will be {seen}, "
                       f"{held} held back ({hold})")
        else:
            out.append(f"Open issues: {new.get('issues_open', 0)} made {seen} by this run, "
                       f"{held} held back ({hold})")
    sk = r.get("skipped") or {}
    k_i, k_p = len(sk.get("issues") or []), len(sk.get("prs") or [])
    # after an error a flagged copy may still be on GitHub: claim nothing
    if (k_i or k_p) and not r.get("error"):
        out.append("Kept off GitHub (sync-skip on Gitea): "
                   + ", ".join(p for p in (_plural(k_i, "issue"), _plural(k_p, "merged PR"))
                               if not p.startswith("0 ")))
    pc = r.get("pr_commits") or {}
    missing = r.get("pr_bases_missing") or {}
    def be(n):  # "will be" is also true of the next run after an error: nothing is claimed done
        return "will be" if dry or r.get("error") else ("is" if n == 1 else "are")
    if pc.get("unlinked"):
        line = (f"{_plural(pc['unlinked'], 'commit')} {be(pc['unlinked'])} listed without a link "
                f"to a GitHub commit")
        if missing:
            line += ("; PRs were merged into branches that do not exist on GitHub: "
                     + ", ".join(f"{b} ({_plural(d['prs'], 'PR')}, {_plural(d['unlinked'], 'commit')})"
                                 for b, d in sorted(missing.items())))
        out.append(line)
    if pc.get("ambiguous"):
        out.append(f"{_plural(pc['ambiguous'], 'commit')} {be(pc['ambiguous'])} listed without a link "
                   f"(more than one GitHub commit matches)")
    if r.get("label_descriptions_cut"):
        out.append(f"{_plural(r['label_descriptions_cut'], 'label description')} cut to "
                   f"GitHub's length limit")
    reused = r.get("labels_reused") or []
    if reused and not r.get("error"):
        k = len(reused)
        out.append(f"{_plural(k, 'label')} already on GitHub used as "
                   f"{'it is' if k == 1 else 'they are'}: {', '.join(reused)}")
    w = r.get("wiki") or {}
    if w.get("github_edits_overwritten") and (w.get("changed") or "restore_to" in w):
        done_ = w.get("commit") or w.get("restored")
        k = w["github_edits_overwritten"]
        past = "was" if k == 1 else "were"
        out.append(f"{_plural(k, 'edit')} made on the GitHub wiki "
                   f"{past if done_ else 'will be'} overwritten (--wiki-overwrite)")
    if r.get("wiki_entries_skipped"):
        out.append(f"Wiki entries left out (not files): {', '.join(r['wiki_entries_skipped'])}")
    if r.get("wiki_pages_not_text"):
        k = r["wiki_pages_not_text"]
        out.append(f"{_plural(k, 'wiki page')} not UTF-8 text: copied as "
                   f"{'it is' if k == 1 else 'they are'}, references not converted")
    if r.get("not_deleted_unverifiable"):
        k = len(r["not_deleted_unverifiable"])
        out.append(f"Not deleted, because Gitea could not confirm "
                   f"{'it is' if k == 1 else 'they are'} gone: "
                   + ", ".join(r["not_deleted_unverifiable"]))
    return out


# Shown FIRST in "Would write", "Written" and the after-error lines, ahead
# of the imports and new labels: these deletions run first.
_SKIP_KIND = ("skip_delete", "skip_deleted", "issue deleted (sync-skip on Gitea)",
              "issues deleted (sync-skip on Gitea)")

_UPDATE_KINDS = [  # (plan key, done key, one, many)
    ("edit", "edited", "issue edited", "issues edited"),
    ("comment_new", "comments_posted", "new comment", "new comments"),
    ("comment_edit", "comments_edited", "comment edited", "comments edited"),
    ("comment_delete", "comments_deleted", "comment deleted", "comments deleted"),
    ("state", "closed_or_reopened", "issue closed or reopened", "issues closed or reopened"),
    ("labels", "relabelled", "issue relabelled", "issues relabelled"),
    ("lock", "locked", "issue locked", "issues locked"),
    ("delete", "deleted", "issue deleted (its Gitea item is gone)",
     "issues deleted (their Gitea items are gone)"),
]


def _new_items_line(k: dict, n: int) -> str:
    parts = [p for p in (_plural(k.get("issues_closed", 0), "closed issue"),
                         _plural(k.get("issues_open", 0), "open issue"),
                         _plural(k.get("prs", 0), "merged-PR record"))
             if not p.startswith("0 ")]
    return f"{_plural(n, 'new issue')}: {', '.join(parts)}"


def _done_lines(r: dict, after_error: bool) -> list:
    """What the report records as written. After an error, only real writes:
    the board's counts and a wiki that was not committed are left out."""
    d = r.get("done") or {}
    out = []
    _, key, one, many = _SKIP_KIND
    n = len(d.get(key) or [])
    if n:
        out.append(f"{n} {one if n == 1 else many}")
    imp = d.get("imported") or {}
    if imp:
        out.append(f"{_plural(len(imp), 'issue')} imported (GitHub {_ranges(imp.values())})")
    if d.get("labels_created"):
        out.append(f"{_plural(len(d['labels_created']), 'label')} created: "
                   f"{', '.join(d['labels_created'])}")
    for _, key, one, many in _UPDATE_KINDS:
        v = d.get(key)
        n = len(v) if isinstance(v, list) else (v or 0)
        if n:
            out.append(f"{n} {one if n == 1 else many}")
    w = r.get("wiki") or {}
    if after_error:
        if w.get("commit"):
            out += _wiki_lines(w, dry=False)
        return out
    out += _board_lines(r.get("board"), past=True)
    out += _wiki_lines(w, dry=False)
    return out


def _to_line(r: dict, dry: bool) -> list:
    gh = r.get("github") or {}
    if not gh:
        return []
    vis = "" if "private" not in gh else ("private" if gh["private"] else "public")
    trusted = r.get("trusted")
    there = None
    if trusted is not None and r.get("command") != "remove":
        # trusted is read at the run's start: say so once the run has written
        when = "" if dry else " before this run"
        there = (("nothing mirrored there yet" if dry else "nothing mirrored before this run")
                 if not trusted else f"{_plural(len(trusted), 'item')} already mirrored{when}")
    extra = "; ".join(x for x in (vis, there) if x)
    return [f"To:   GitHub {gh.get('repo')}, as {gh.get('account')}" + (f" ({extra})" if extra else "")]


def summary(r: dict) -> str:
    """The run in plain words, from the report alone: where from, where to,
    what to check, what was or would be written, and how it ended."""
    L = []
    remove = r.get("command") == "remove"
    err = r.get("error")
    dry = not r.get("apply")
    if err:
        L.append(f"rs-sync stopped: {err}")
    elif remove and dry:
        L.append("rs-sync remove, dry run: nothing was deleted. Run again with --apply to do it.")
    elif remove:
        L.append(f"rs-sync remove done in {_duration(r.get('seconds'))}.")
    elif dry:
        L.append("rs-sync dry run: nothing was written. Run again with --apply to do it.")
    else:
        L.append(f"rs-sync done in {_duration(r.get('seconds'))}.")
    gi = r.get("gitea") or {}
    head = []
    if gi:
        head.append(f"From: Gitea {gi.get('repo')} ({_plural(gi.get('issues', 0), 'issue')}, "
                    f"{_plural(gi.get('merged_prs', 0), 'merged PR')})")
    head += _to_line(r, dry)
    if head:
        L.append("")
        L.extend(head)

    def section(title, lines):
        if lines:
            L.append("")
            L.append(title)
            L.extend(f"  - {x}" for x in lines)

    if remove:
        rm = r.get("remove") or {}
        skipped = _board_skip(r.get("board")) + _wiki_skip(r.get("wiki"))
        section("Check before applying:" if dry and not err else "Note:",
                [l for l in _checks(r, dry) if "GitHub wiki" in l])
        if dry and not err:
            would = []
            if rm.get("issues"):
                would.append(f"{_plural(len(rm['issues']), 'issue')} (GitHub {_ranges(rm['issues'])})")
            if rm.get("labels"):
                would.append(f"{_plural(len(rm['labels']), 'label')}: {', '.join(rm['labels'])}")
            if rm.get("projects"):
                would.append(_projects(rm["projects"]))
            would += _wiki_lines(r.get("wiki"), dry=True)
            section("Would delete:", would or ["nothing to delete"])
            section("Left out:", skipped)
            return "\n".join(L)
        gone = []
        if rm.get("deleted"):
            gone.append(f"{_plural(len(rm['deleted']), 'issue')} (GitHub {_ranges(rm['deleted'])})")
        if rm.get("projects_deleted"):
            gone.append(_projects(rm["projects_deleted"]))
        if rm.get("labels_deleted"):
            gone.append(f"{_plural(len(rm['labels_deleted']), 'label')}: "
                        f"{', '.join(rm['labels_deleted'])}")
        if (r.get("wiki") or {}).get("restored"):
            gone += _wiki_lines(r["wiki"], dry=False)
        if err:
            L.append("")
            L.extend(_stopped_lines(r, gone, remove=True))
        else:
            section("Deleted:", gone or ["nothing to delete"])
            section("Left out:", skipped)
        return "\n".join(L)

    section("Check before applying:" if dry and not err else "Note:", _checks(r, dry))
    if err:
        L.append("")
        L.extend(_stopped_lines(r, _done_lines(r, after_error=True), remove=False))
        return "\n".join(L)
    skipped = _board_skip(r.get("board")) + _wiki_skip(r.get("wiki"))
    if not dry:
        section("Written:", _done_lines(r, after_error=False)
                or ["nothing: " + ("the rest of " if skipped else "")
                    + "GitHub was already level with Gitea"])
        section("Left out:", skipped)
        return "\n".join(L)
    would = []
    pc, pi = r.get("plan_counts") or {}, r.get("plan_issues") or {}
    key, _, one, many = _SKIP_KIND
    if pc.get(key):
        would.append(f"{pc[key]} {one if pc[key] == 1 else many}")
    if pc.get("import"):
        would.append(_new_items_line(r.get("new_items") or {}, pc["import"]))
    if pc.get("create_labels"):
        would.append(f"{_plural(pc['create_labels'], 'new label')}: "
                     f"{', '.join((r.get('plan') or {}).get('create_labels') or [])}")
    for key, _, one, many in _UPDATE_KINDS:
        n = pc.get(key, 0)
        if n:
            line = f"{n} {one if n == 1 else many}"
            if key.startswith("comment") and pi.get(key, n) != n:
                line += f" on {_plural(pi[key], 'issue')}"
            would.append(line)
    would += _board_lines(r.get("board"))
    would += _wiki_lines(r.get("wiki"), dry=True)
    if not would:
        L.append("")
        L.append("The rest of GitHub is already level with Gitea; nothing else to write."
                 if skipped else "GitHub is already level with Gitea; nothing to write.")
        section("Left out:", skipped)
        return "\n".join(L)
    section("Would write:", would)
    section("Left out:", skipped)
    w = r.get("estimated_writes") or 0
    if w:      # the wiki's one push is not in the estimate; a wiki-only run has none
        m = r.get("estimated_minutes") or 0
        L.append("")
        L.append(f"About {_plural(w, 'GitHub write')}, "
                 f"{'under a minute' if m < 1 else 'about ' + _duration(m * 60)} "
                 f"(GitHub limits how fast content can be created).")
        used = r.get("writes_this_hour") or 0
        if used:
            L.append(f"GitHub has had {used} of its {CONTENT_PER_HOUR} writes this hour "
                     f"from earlier rs-sync runs here; the estimate counts them.")
    return "\n".join(L)


def _stopped_lines(r: dict, written: list, remove: bool) -> list:
    n = r.get("writes_sent") or 0
    if not n:
        return ["Nothing was written to GitHub."]
    out = [f"{_plural(n, 'write request')} {'was' if n == 1 else 'were'} sent before it "
           f"stopped (the last one may not have completed)."]
    if written:
        out.append("Deleted before it stopped:" if remove else "Recorded as done:")
        out.extend(f"  - {x}" for x in written)
    b = r.get("board")
    if not remove and isinstance(b, dict) and not _board_skip(b):
        out.append("The board may be partway done; the next run with --board brings it level.")
    out.append("Run the same command again to continue the remove." if remove else
               "Run again to continue: rs-sync reads what is already on GitHub.")
    return out


def import_advice() -> str:
    """The last line of a run that stopped while an import was being sent or
    awaited; empty when none was. Worded to hold whether or not GitHub
    received it (the record also covers the wait before sending)."""
    lag = REPORT.get("lagging")
    if lag:
        more = f" and {len(lag) - 1} more" if len(lag) > 1 else ""
        return (f"GitHub had not yet listed Gitea #{lag[0]}{more}, imported by this run, or "
                f"not all of its comments, when rs-sync stopped. Wait a few minutes before "
                f"running again, so the next run sees them and neither imports the item nor "
                f"posts its comments a second time.")
    n = REPORT.get("importing")
    if n is None:
        return ""
    return (f"rs-sync was importing Gitea #{n} when it stopped. GitHub may or may "
            f"not have received it. Wait a few minutes before running again; if it "
            f"then appears twice, the next run names both copies, and you delete "
            f"one on GitHub.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="rs-sync",
        description="mirror a Gitea fork's issues and merged pull requests (and, "
                    "when asked, its wiki and board) to a GitHub repo (a dry run "
                    "unless --apply); an issue or "
                    "merged pull request labelled sync-skip on Gitea stays off GitHub, "
                    "and a copy already there is deleted")
    ap.add_argument("gitea", help="the Gitea fork, <owner>/<repo>")
    ap.add_argument("github", help="the GitHub repo, <owner>/<repo>")
    ap.add_argument("--apply", action="store_true", help="write to GitHub")
    ap.add_argument("--remove", action="store_true",
                    help="delete what rs-sync mirrored for this fork (the wiki and "
                         "the project only with --wiki / --board)")
    ap.add_argument("--wiki", action="store_true",
                    help="also mirror the wiki; with --remove, also restore it")
    ap.add_argument("--board", action="store_true",
                    help="also mirror the board as a GitHub project; with --remove, "
                         "also delete it")
    ap.add_argument("--wiki-overwrite", action="store_true",
                    help="with --wiki: commit over (or, with --remove, force-push "
                         "past) edits made on the GitHub wiki")
    ap.add_argument("--env", default=str(DEFAULT_ENV),
                    help=f"the settings file (default {DEFAULT_ENV})")
    ap.add_argument("--report", action="store_true",
                    help="also print the full report as JSON, for pasting back to the agent")
    args = ap.parse_args(argv)
    if args.wiki_overwrite and not args.wiki:
        ap.error("--wiki-overwrite needs --wiki")
    REPORT["command"] = "remove" if args.remove else "sync"
    REPORT["apply"] = bool(args.apply)
    PROGRESS.start()
    started = time.time()
    rc = 0
    env = None
    try:
        env = read_env(Path(args.env))
        if args.remove:
            remove(args, env)
        else:
            sync(args, env)
    except SyncError as e:
        PROGRESS.end()
        REPORT["error"] = str(e)
        print(f"rs-sync: {e}", file=sys.stderr)
        rc = 1
    except KeyboardInterrupt:
        # Ctrl-C reaches a running git child too (one process group), and git
        # cleans up its own lock files; this only replaces the traceback.
        PROGRESS.end()
        REPORT["error"] = "interrupted"
        print("rs-sync: interrupted", file=sys.stderr)
        rc = 130
    except Exception as e:      # a defect: say so, keep the report
        PROGRESS.end()
        # The same secrets sync() keeps out of everything it sends.
        secrets = [env["RS_SYNC_GITHUB_TOKEN"]] if env else []
        try:
            secrets += all_dev_tokens()
        except Exception:
            pass
        REPORT["error"] = _safe(f"unexpected {type(e).__name__}: {e}", *secrets)
        sys.stderr.write(_safe(traceback.format_exc(), *secrets))
        rc = 1
    REPORT["seconds"] = round(time.time() - started, 1)
    PROGRESS.end()
    try:
        print(summary(REPORT))
    except Exception as e:      # a defect in the summary: keep the report and the advice
        print(f"rs-sync: the summary failed ({type(e).__name__}: {e}); "
              f"run with --report for the full report", file=sys.stderr)
        print(f"rs-sync stopped: {REPORT['error']}" if REPORT.get("error") else "rs-sync finished.")
    if args.report:
        print()
        print_report()
    advice = import_advice()
    if rc and advice:
        print(advice)
    return rc


if __name__ == "__main__":
    sys.exit(main())
