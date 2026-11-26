#!/opt/conda/bin/python3
"""rs-search — RS's search service, inside its own container.

Runs as root in the `rs-search` container (container/search/boot.sh starts it
once the inner Docker answers), and does two things:

  - the caller-facing MCP server on SERVICE_PORT, behind the MCP gateway's
    `search` entry: submit a question, check a search, collect its result,
    and a file route (GET /mcp/files/<search id>/<file>) under the server's
    registered path, which is where the gateway relays a caller's
    `/search/files/...`. The caller is the `<project>/<consumer>` the gateway
    puts in CALLER_HEADER (the gateway strips any copy a caller sends).
    Searches are keyed on the caller's PROJECT: another project's search is
    "not found", whatever its id;
  - each search, in the inner Docker: a private network, one search-agent
    container and a named volume for its files, all three named after the
    search id and removed BY NAME once the caller collected the result or the
    operator cleared it. At start, whatever an earlier run left is found by
    label and removed by name. SearXNG (the plain tier's web search) runs
    beside them and is connected to each search's network, never the
    searches to each other.

`service.py put NAME` / `drop NAME` (the host, over `docker exec -i`) write or
remove one of the files the host hands over (settings, token) in RUN_DIR;
`service.py admin status|clear|ensure|list|test` and `admin view|stop ID` ask
the running server from inside the container (the admin routes answer
127.0.0.1 only): the operator's list of waiting and running searches, a
read-only view of one search's work, stopping one, and testing the sign-in.

Stdlib only (the dind image's conda Python).
"""

from __future__ import annotations

import collections
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- names (MIRRORS of cli/search.py where noted) ------------------------------

PORT = 8080                                   # search.SERVICE_PORT
MCP_PATH = "/mcp"                             # search.MCP_PATH
FILES_PREFIX = MCP_PATH + "/files/"
CALLER_HEADER = "X-RS-Caller"                 # search.CALLER_HEADER
RUN_DIR = "/run/rs-search"                    # search.SERVICE_RUN_DIR
SETTINGS_PATH = os.path.join(RUN_DIR, "settings.json")
TOKEN_PATH = os.path.join(RUN_DIR, "claude.env")
DELIVERED = ("settings.json", "claude.env")
CLAUDE_DIST = "/opt/agent-dist/claude"        # search.SERVICE_AGENT_DIST + /claude
STATE_DIR = "/var/lib/rs-search"              # this container's own disk
SEARXNG_DIR = os.path.join(STATE_DIR, "searxng")

AGENT_IMAGE = os.environ.get("RS_SEARCH_AGENT_IMAGE", "")
SEARXNG_IMAGE = os.environ.get("RS_SEARCH_SEARXNG_IMAGE", "")

# Every inner thing of one search is named SEARCH_PREFIX + its id and carries
# ID_LABEL; leftovers are found by the label and removed by the name.
SEARCH_PREFIX = "rs-search-s-"
ID_LABEL = "research.search.id"
PROJECT_LABEL = "research.search.project"
ID_RE = re.compile(r"^[0-9a-f]{16}$")
SEARXNG_CONTAINER = "rs-search-searxng"
SEARXNG_NETWORK = "rs-search-sx"
SEARXNG_ALIAS = "searxng"                     # agent/tools.py SEARXNG_URL's host
SEARXNG_LABEL = "research.search.searxng"
# Where a search's volume is mounted in its agent container, and the two
# folders the agent's tool server writes (agent/tools.py — MIRROR).
AGENT_VOLUME_MOUNT = "/search"
FILES_DIR = "files"
OUT_DIR = "out"
REPORT_NAME = "report.json"
TRACE_NAME = "trace.jsonl"
# The names the tool server gives saved files (agent/tools.py — MIRROR); a
# file of any other name in the folder is not offered (the agent can write
# its volume, so its contents are untrusted).
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# --- bounds ----------------------------------------------------------------------

# The whole of one search. A literature question at high effort runs tens of
# tool calls and many minutes; at half this a deep search on a slow model is
# cut off mid-way, at ten times a search agent that loops holds a slot (and
# spends tokens) for most of a working day. The search is then stopped and
# collected as failed, with whatever it saved.
SEARCH_MAX_S = 3600
# One docker CLI call of the service (create, connect, inspect, remove). Each
# takes milliseconds to a second on a healthy inner daemon; the bound only
# keeps a hung daemon from holding a search's thread (or a caller's request)
# for ever. At half a loaded host could still fail a removal; at ten times a
# caller's collect would hang for many minutes before saying so.
DOCKER_CALL_S = 60
# How often a queued search re-reads "searches at once": a change saved on
# the Search page is picked up within this many seconds. Shorter only spins;
# longer leaves a freed-up slot idle that long.
QUEUE_RECHECK_S = 5
# A question rides the search agent's environment, i.e. one argument of its
# `docker run`, which Linux caps at 128 KiB (MAX_ARG_STRLEN). 32k characters
# stay under that in any UTF-8 (at most 4 bytes each). A real question is a
# paragraph; a pasted document belongs in a file, not the question.
QUESTION_MAX_CHARS = 32 * 1024
# What the service reads back from a search's volume (the agent can write
# anything there): the report is a summary with every source, normally tens
# of KB — 1 MiB is about forty times that and still nothing for the service's
# memory; the trace is one short line per tool call — 4 MiB is tens of
# thousands of calls, far past any search inside SEARCH_MAX_S.
REPORT_MAX_BYTES = 1024 * 1024
TRACE_MAX_BYTES = 4 * 1024 * 1024
# The search agent's last output lines read back at its end: its final
# `result` event is the last stdout line, and the few lines before it carry a
# sign-in or start-up failure. More only costs memory.
AGENT_LOG_TAIL_LINES = 200
# One result text quoted back (an error message of the agent): enough for a
# sentence or two of the agent's own words.
AGENT_TEXT_MAX_CHARS = 2000
# What the host hands over (put): a settings record or a one-line token, both
# well under a KiB; the cap only bounds a broken writer.
DELIVERY_MAX_BYTES = 64 * 1024
# A file download is streamed in pieces of this size (a buffer, not a limit).
PIECE = 65536
# The admin client's own bound (inside the container): one local request. The
# longest is the sign-in test (SIGN_IN_TEST_S for Claude's answer plus its few
# docker calls, a second or two each); every other request answers in well
# under a second. The host's own deadline (25 s, cli/rscore.py
# MCP_INLINE_DEADLINE_S) is the shorter bound and ends the exec first.
ADMIN_CALL_S = 30
# The caps on what the trace hands back per tool call (_trace_summary and the
# operator's view): a tool, tier or outcome name is a short word of RS's own
# (the longest is under 20 characters); a target is a query, an identifier or
# an address, which the tool server cuts at 500 — 300 keeps every real query
# and DOI whole and the start of a long address.
TRACE_NAME_MAX_CHARS = 40
TRACE_TARGET_MAX_CHARS = 300

# --- the agent's live output and the operator's view ------------------------------

# One line of the search agent's output stream as it is read live. The lines
# wanted are the `assistant` events (one model message: its text, thinking and
# tool calls — tens of KB, a few hundred at the largest output settings) and
# the final `result`. Tool results ride `user` events of up to the tool
# server's TEXT_MAX_CHARS (80k characters, a few hundred KB encoded), which are
# not needed; a line past this cap is skipped as it streams, never held whole.
# At half, a long final answer could be skipped (the count then keeps the sum
# per message instead of the result's totals); at ten times one search's
# reader could hold 10 MiB at once.
STREAM_LINE_MAX_BYTES = 1024 * 1024
# The operator's view is a tail, for checking how a search goes and spotting
# what broke, not for auditing it: the newest entries (tool calls and the
# agent's texts together), and of a text only its end. One turn is a text and
# one to three tool calls, so 10 entries show the last few turns, enough to
# see a failing tool and the agent's reaction to it; at half one retry loop
# fills the view, at ten times it becomes the audit it is not meant to be.
VIEW_TAIL_ENTRIES = 10
# A text's end as the view shows it: two or three lines of the box. At half a
# sentence is cut before its point; at ten times one text fills the box.
VIEW_TEXT_TAIL_CHARS = 300
# The question as the operator's list shows it: one row of a table (the full
# question is in the view). At half a one-sentence question is cut mid-way; at
# ten times a row becomes a paragraph and a long queue's list grows by
# kilobytes per search.
LIST_QUESTION_MAX_CHARS = 200
# How many ended searches the operator's "Recent searches" list keeps, from the
# delivered settings' `recent` (search.DEFAULT_RECENT / search.RECENT_MAX —
# MIRRORS); a missing or bad value reads as the default.
DEFAULT_RECENT = 20
RECENT_MAX = 100
# A recent search's reason as its row keeps it (the page shows it on hover).
# Every reason RS writes itself is shorter (the longest, the timeout's, is
# under 100 characters) except the refused sign-in's, whose first 200 still
# say that the stored token was refused; the agent's own words after it are in
# the caller's search_check and the work view while the search is in the
# table. At half the sign-in sentence is cut before its point; at ten times
# 100 rows outgrow the 64 KiB the broker keeps its replies to (the list rides
# every read of the live table).
RECENT_REASON_MAX_CHARS = 200
# Reasons the operator's stop leaves (the caller's search_check and
# search_collect show them, worded like the timeout's).
STOPPED_QUEUED = "the search was stopped by the operator before it started"
STOPPED_WITH_REPORT = "the search was stopped by the operator after it wrote its report"
STOPPED_NO_REPORT = "the search was stopped by the operator before it wrote a report"
NO_SUCH_SEARCH = "no search with that id is waiting or running"

# --- the sign-in test -----------------------------------------------------------------

# The cheapest model: the test proves that the stored token signs in, not what
# a model can do (measured: ~2 s and $0.0006 for the call below).
SIGN_IN_TEST_MODEL = "haiku"
SIGN_IN_TEST_PROMPT = "Reply with the single word ok."
# How long the test waits for Claude's answer. Measured: ~2 s signed in, a few
# seconds to its error with a refused token (Claude Code's own start included).
# It must fit inside the host's 25 s inline deadline with the docker calls
# around it (network and container made and removed, the exec itself: a few
# seconds on a loaded host). At half a cold Claude Code start on a busy machine
# could be cut off; much longer and the host gives up before the answer.
SIGN_IN_TEST_S = 15

# --- the search agent's model and the caller's view -----------------------------

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "rs-search", "version": "1"}
INSTRUCTIONS = (
    "RS's search service. search_submit hands a question to a searcher and "
    "returns a search id at once; the search runs elsewhere, so go on with "
    "your work and come back with search_check. When it has ended, "
    "search_collect returns the written summary, every source, what was tried "
    "and the files the search saved. Download a file with an HTTP GET of this "
    "server's own address followed by /files/<search id>/<file name>, with the "
    "same Authorization header as for this server, BEFORE collecting without "
    "keep: collecting ends the search and removes its files.")

ENDED = ("done", "failed")


def log(msg: str) -> None:
    sys.stdout.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + msg + "\n")
    sys.stdout.flush()


def docker(*args: str, timeout: float = DOCKER_CALL_S,
           input: bytes | None = None) -> subprocess.CompletedProcess:
    """One inner docker call. Never raises for docker's own failure (the
    caller reads returncode); a call past its bound is reported as failed."""
    try:
        return subprocess.run(["docker", *args], capture_output=True,
                              timeout=timeout, input=input)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, b"", b"the inner docker did not answer in time")
    except OSError as e:
        return subprocess.CompletedProcess(args, 127, b"", (e.strerror or "docker").encode())


def first_line(r: subprocess.CompletedProcess) -> str:
    text = (r.stderr or r.stdout or b"").decode("utf-8", "replace")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[0][:300] if lines else f"exit {r.returncode}"


# --- delivered files ---------------------------------------------------------------

def read_settings() -> dict | None:
    """The settings the host delivered, or None (not delivered, or unusable:
    the host writes only valid ones)."""
    try:
        with open(SETTINGS_PATH, "rb") as f:
            data = json.loads(f.read(DELIVERY_MAX_BYTES + 1))
        s = data["searcher"]
        if not (isinstance(s["name"], str) and isinstance(s["model"], str)
                and isinstance(s["effort"], str) and isinstance(s["at_once"], int)
                and isinstance(data["contact"], str)):
            return None
        r = data.get("recent")
        if isinstance(r, bool) or not isinstance(r, int) or not 0 <= r <= RECENT_MAX:
            data["recent"] = DEFAULT_RECENT     # missing (an older host) or bad
        return data
    except (OSError, ValueError, RecursionError, KeyError, TypeError):
        return None


def token_present() -> bool:
    try:
        return stat.S_ISREG(os.stat(TOKEN_PATH).st_mode)
    except OSError:
        return False


# --- reading a search's volume (untrusted: the agent writes it) --------------------

def volume_root(name: str) -> str | None:
    r = docker("volume", "inspect", "-f", "{{.Mountpoint}}", name)
    path = r.stdout.decode("utf-8", "replace").strip()
    return path if r.returncode == 0 and path.startswith("/") else None


def _open_dir(parent_fd: int, name: str) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)


def _open_file(dir_fd: int, name: str) -> tuple[int, os.stat_result]:
    """A regular file directly in dir_fd, never through a link and never
    blocking on a planted FIFO. OSError otherwise."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(0, "not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def read_volume_file(root: str, folder: str, name: str, cap: int) -> bytes | None:
    """The bytes of <volume>/<folder>/<name>, or None when absent, not a
    regular file, or larger than `cap`."""
    try:
        rfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        dfd = _open_dir(rfd, folder)
        try:
            fd, st = _open_file(dfd, name)
            try:
                if st.st_size > cap:
                    return None
                data = os.read(fd, cap + 1)
                return None if len(data) > cap else data
            finally:
                os.close(fd)
        finally:
            os.close(dfd)
    except OSError:
        return None
    finally:
        os.close(rfd)


def list_volume_files(root: str) -> list[dict]:
    """The saved files a caller may download: regular files directly in the
    volume's files folder whose names follow FILE_NAME_RE."""
    out = []
    try:
        rfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return out
    try:
        dfd = _open_dir(rfd, FILES_DIR)
    except OSError:
        os.close(rfd)
        return out
    try:
        for name in sorted(os.listdir(dfd)):
            if not FILE_NAME_RE.match(name):
                continue
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                out.append({"name": name, "bytes": st.st_size})
    finally:
        os.close(dfd)
        os.close(rfd)
    return out


# --- SearXNG ------------------------------------------------------------------------

# Engines measured from a shared address (literature queries, 2026-11): these
# answered reliably; google, google scholar, qwant, brave, arxiv (through
# SearXNG) and semantic scholar were blocked, suspended or degraded and are
# not kept. arXiv itself is a tool of the plain tier (its own API).
SEARXNG_ENGINES = ("duckduckgo", "google cse", "bing", "openalex", "crossref", "pubmed")
# bing, openalex and crossref are off in SearXNG's defaults.
SEARXNG_ENABLE = ("bing", "openalex", "crossref")
SEARXNG_PORT = 8080


def searxng_settings() -> str:
    """SearXNG's settings: JSON output on, the engines above only, no rate
    limiter (it is reached only by this service's own search agents), and a
    fresh secret at every (re)creation (it signs SearXNG's own cookies and
    image proxy; it guards nothing of RS)."""
    keep = "\n".join(f"      - {e}" for e in SEARXNG_ENGINES)
    enable = "\n".join(f"  - name: {e}\n    disabled: false" for e in SEARXNG_ENABLE)
    return ("use_default_settings:\n  engines:\n    keep_only:\n" + keep + "\n"
            "server:\n"
            f"  secret_key: \"{secrets.token_hex(32)}\"\n"
            "  limiter: false\n"
            "  image_proxy: false\n"
            "  public_instance: false\n"
            "search:\n  formats:\n    - html\n    - json\n"
            "engines:\n" + enable + "\n")


_searxng_lock = threading.Lock()


def image_present(image: str) -> bool:
    return bool(image) and docker("image", "inspect", image).returncode == 0


def container_state(name: str) -> tuple[str, dict]:
    """(running | stopped | absent, labels)."""
    r = docker("container", "inspect", "-f", "{{.State.Running}} {{json .Config.Labels}}", name)
    if r.returncode != 0:
        return "absent", {}
    running, _, raw = r.stdout.decode("utf-8", "replace").strip().partition(" ")
    try:
        labels = json.loads(raw)
    except (ValueError, RecursionError):
        labels = {}
    return ("running" if running == "true" else "stopped"), (
        labels if isinstance(labels, dict) else {})


def ensure_searxng() -> str | None:
    """SearXNG running on the pinned image; None, or why it is not."""
    with _searxng_lock:
        state, labels = container_state(SEARXNG_CONTAINER)
        if state == "running" and labels.get(SEARXNG_LABEL) == SEARXNG_IMAGE:
            return None
        if state != "absent":
            docker("rm", "-f", SEARXNG_CONTAINER)
        if not image_present(SEARXNG_IMAGE):
            return "the SearXNG image is not staged in the search service"
        if docker("network", "inspect", SEARXNG_NETWORK).returncode != 0:
            r = docker("network", "create", "--label", f"{SEARXNG_LABEL}=1", SEARXNG_NETWORK)
            if r.returncode != 0:
                return f"SearXNG's network could not be created ({first_line(r)})"
        os.makedirs(SEARXNG_DIR, mode=0o755, exist_ok=True)
        tmp = os.path.join(SEARXNG_DIR, f".settings.{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as f:
            f.write(searxng_settings())
        os.replace(tmp, os.path.join(SEARXNG_DIR, "settings.yml"))
        # Every capability dropped (measured: it serves with none; its
        # entrypoint's chown of the read-only settings only warns), the
        # settings read-only, its cache a tmpfs (the image declares two
        # volumes, which would otherwise be left behind as anonymous ones).
        r = docker("run", "-d", "--name", SEARXNG_CONTAINER,
                   "--label", f"{SEARXNG_LABEL}={SEARXNG_IMAGE}",
                   "--network", SEARXNG_NETWORK, "--restart", "no",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "-v", f"{SEARXNG_DIR}:/etc/searxng:ro",
                   "--tmpfs", "/var/cache/searxng",
                   SEARXNG_IMAGE)
        if r.returncode != 0:
            return f"SearXNG could not be started ({first_line(r)})"
        log("searxng started")
        return None


# --- the agent's output, followed live -----------------------------------------------

def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def usage_tokens(usage) -> int:
    """Tokens of one usage record: input + output + cache write + cache read."""
    if not isinstance(usage, dict):
        return 0
    return sum(_count(usage.get(k)) for k in ("input_tokens", "output_tokens",
                                                "cache_creation_input_tokens",
                                                "cache_read_input_tokens"))


class AgentStream:
    """What the operator sees of one search agent while it runs: the token
    count (the latest usage per message id, summed; the `result` event's
    totals once it arrives) and its text blocks, each with the time it was
    read (bounded: the newest VIEW_TAIL_ENTRIES, all the view shows)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.per_message: dict[str, int] = {}
        self.final: int | None = None
        self.texts: collections.deque = collections.deque(maxlen=VIEW_TAIL_ENTRIES)
        self.dropped = 0

    def tokens(self) -> int:
        with self.lock:
            return self.final if self.final is not None else sum(self.per_message.values())

    def work_texts(self) -> tuple[list[dict], bool]:
        with self.lock:
            return list(self.texts), self.dropped > 0

    def feed(self, line: bytes) -> None:
        try:
            ev = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(ev, dict):
            return
        now = time.time()
        if ev.get("type") == "result":
            with self.lock:
                self.final = usage_tokens(ev.get("usage"))
            return
        if ev.get("type") != "assistant" or not isinstance(ev.get("message"), dict):
            return
        msg = ev["message"]
        texts = [b["text"][:AGENT_TEXT_MAX_CHARS] for b in msg.get("content") or []
                 if isinstance(b, dict) and b.get("type") == "text"
                 and isinstance(b.get("text"), str) and b["text"].strip()]
        with self.lock:
            if isinstance(msg.get("id"), str) and isinstance(msg.get("usage"), dict):
                self.per_message[msg["id"]] = usage_tokens(msg["usage"])
            for t in texts:
                if len(self.texts) == self.texts.maxlen:
                    self.dropped += 1
                self.texts.append({"ts": now, "kind": "text", "text": t})


def follow(name: str, stream: AgentStream) -> tuple[threading.Thread, subprocess.Popen | None]:
    """Follow a started container's output (`docker logs -f`, which ends when
    the container does) in a thread feeding `stream`; every line is read at
    most STREAM_LINE_MAX_BYTES at a time and a longer one is skipped. Returns
    the thread and the process (None when docker could not be run)."""
    try:
        proc = subprocess.Popen(["docker", "logs", "-f", name], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except OSError:
        proc = None

    def read() -> None:
        if proc is None:
            return
        skipping = False
        try:
            while True:
                piece = proc.stdout.readline(STREAM_LINE_MAX_BYTES)
                if not piece:
                    break
                whole = piece.endswith(b"\n")
                if skipping:
                    skipping = not whole        # inside a line past the cap, to its end
                    continue
                if not whole and len(piece) >= STREAM_LINE_MAX_BYTES:
                    skipping = True             # the start of a line past the cap
                    continue
                stream.feed(piece)
        finally:
            proc.stdout.close()
            proc.wait()

    t = threading.Thread(target=read, daemon=True)
    t.start()
    return t, proc


def end_follow(follower: tuple[threading.Thread, subprocess.Popen | None]) -> None:
    """After the container ended: let the follower read what is left (its
    `docker logs -f` ends with the container), killing it past one docker
    call's bound, and reap it."""
    thread, proc = follower
    thread.join(DOCKER_CALL_S)
    if thread.is_alive() and proc is not None:
        proc.kill()
        thread.join()


# --- searches -----------------------------------------------------------------------

class Searches:
    """Every search the service knows, in memory (a restart forgets them and
    its start removes what they left). One lock guards the table, the queue
    and the recent list; docker work happens outside it."""

    def __init__(self) -> None:
        self.lock = threading.Condition()
        self.table: dict[str, dict] = {}
        self.queue: collections.deque = collections.deque()
        self.running = 0
        # The searches that ended, newest first, one line each: they outlive
        # the caller's collect and the operator's clear (which remove the
        # search and its files, not this line).
        self.recent: collections.deque = collections.deque()

    # --- the caller's three tools ---

    def submit(self, project: str, consumer: str, question: str, searcher: str) -> dict:
        settings = read_settings()
        if settings is None:
            raise ToolRefusal("the search service has no settings yet; the operator "
                              "saves the searcher on the Search page")
        s = settings["searcher"]
        if searcher and searcher != s["name"]:
            raise ToolRefusal(f"there is no searcher named {searcher}; the searchers "
                              f"are: {s['name']}")
        if not isinstance(question, str) or not question.strip():
            raise ToolRefusal("the question is empty")
        if len(question) > QUESTION_MAX_CHARS:
            raise ToolRefusal(f"the question is longer than {QUESTION_MAX_CHARS} "
                              "characters; put long material in a file the "
                              "question names instead")
        sid = secrets.token_hex(8)
        rec = {"id": sid, "project": project, "consumer": consumer,
               "question": question.strip(), "searcher": s["name"],
               "model": s["model"], "effort": s["effort"],
               "contact": settings["contact"],
               "state": "queued", "submitted_at": time.time(),
               "started_at": None, "finished_at": None, "outcome": None,
               "stream": AgentStream(), "stop": False}
        with self.lock:
            self.table[sid] = rec
            self.queue.append(sid)
            self.lock.notify_all()
        threading.Thread(target=self._run, args=(sid,), daemon=True).start()
        log(f"submit {sid} for {project}/{consumer}")
        return {"search_id": sid, "searcher": s["name"], "state": "queued",
                "next": "go on with your work; search_check tells how it goes"}

    def get(self, project: str, sid: str) -> dict:
        """The caller's own search, or a not-found refusal (another project's
        search is not found either)."""
        if not isinstance(sid, str) or not ID_RE.match(sid):
            raise ToolRefusal("no search with that id was found for this project")
        with self.lock:
            rec = self.table.get(sid)
            if rec is None or rec["project"] != project:
                raise ToolRefusal("no search with that id was found for this project")
            return dict(rec)

    @staticmethod
    def _times(rec: dict) -> dict:
        """How long a search waited in the queue and has run (or ran)."""
        now = time.time()
        return {"waited_s": round((rec["started_at"] or rec["finished_at"] or now)
                                  - rec["submitted_at"]),
                "ran_s": (round((rec["finished_at"] or now) - rec["started_at"])
                          if rec["started_at"] else 0)}

    def check(self, project: str, sid: str) -> dict:
        rec = self.get(project, sid)
        out = {"search_id": sid, "state": rec["state"], "searcher": rec["searcher"],
               "question": rec["question"], **self._times(rec),
               "tokens": rec["stream"].tokens()}
        if rec["state"] == "queued":
            with self.lock:
                ahead = list(self.queue).index(sid) if sid in self.queue else 0
            out["ahead_in_queue"] = ahead
        if rec["state"] in ("starting", "running"):
            act = self._activity(sid)
            if act:
                out["doing"] = act
        if rec["state"] in ENDED:
            out["outcome"] = rec["outcome"]["reason"]
            root = volume_root(SEARCH_PREFIX + sid)
            out["files"] = [dict(f, path=f"files/{sid}/{f['name']}")
                            for f in (list_volume_files(root) if root else [])]
            out["next"] = ("download the files you want, then search_collect "
                           "(it ends the search and removes its files)")
        return out

    def collect(self, project: str, sid: str, keep: bool) -> dict:
        rec = self.get(project, sid)
        if rec["state"] not in ENDED:
            return {"search_id": sid, "state": rec["state"],
                    "next": "the search has not ended yet; search_check tells how it goes"}
        name = SEARCH_PREFIX + sid
        root = volume_root(name)
        result = dict(rec["outcome"])
        result.update({"search_id": sid, "state": rec["state"],
                       "question": rec["question"], "searcher": rec["searcher"],
                       "model": rec["model"] + (f" {rec['effort']}" if rec["effort"] else ""),
                       "tokens": rec["stream"].tokens()})
        result["files"] = [dict(f, path=f"files/{sid}/{f['name']}")
                           for f in (list_volume_files(root) if root else [])]
        if keep:
            result["kept"] = ("the search and its files stay until you collect "
                              "it without keep")
            return result
        problems = remove_search(sid)
        with self.lock:
            self.table.pop(sid, None)
        if problems:
            log(f"collect {sid}: {'; '.join(problems)}")
            result["cleanup"] = problems
        else:
            result["removed"] = "the search's container, network and files are gone"
        log(f"collected {sid} by {project}")
        return result

    # --- the operator's view ---

    def counts(self) -> dict:
        with self.lock:
            c = collections.Counter(r["state"] for r in self.table.values())
        return {"queued": c["queued"], "running": c["starting"] + c["running"],
                "ended": c["done"] + c["failed"]}

    def clear_ended(self) -> dict:
        """The operator's clear: every search that has ended and was not
        collected goes, by name. Running and queued ones are left alone."""
        with self.lock:
            ids = [sid for sid, r in self.table.items() if r["state"] in ENDED]
        problems = []
        for sid in ids:
            problems += remove_search(sid)
            with self.lock:
                self.table.pop(sid, None)
        return {"cleared": len(ids), "problems": problems}

    def active(self) -> dict:
        """The operator's list: every waiting and running search, oldest
        first, and the searches that ended, newest first (at most the
        setting's `recent`)."""
        with self.lock:
            recs = sorted((dict(r) for r in self.table.values() if r["state"] not in ENDED),
                          key=lambda r: r["submitted_at"])
            kept = self._trim_recent()
            recent = [dict(e) for e in self.recent]
        return {"searches": [
            {"id": r["id"], "project": r["project"], "consumer": r["consumer"],
             "question": r["question"][:LIST_QUESTION_MAX_CHARS], "searcher": r["searcher"],
             "state": r["state"], **self._times(r), "tokens": r["stream"].tokens()}
            for r in recs], "recent": recent, "recent_kept": kept}

    def _trim_recent(self) -> int:
        """Under the lock: drop the oldest recent lines past the setting's
        `recent` (read now: a lowered value trims at once). Returns it."""
        settings = read_settings()
        kept = settings["recent"] if settings else DEFAULT_RECENT
        while len(self.recent) > kept:
            self.recent.pop()
        return kept

    def _record_end(self, rec: dict) -> None:
        """Under the lock, once per search, when it ends (any way): its line
        in the recent list."""
        self.recent.appendleft({
            "project": rec["project"], "consumer": rec["consumer"],
            "question": rec["question"][:LIST_QUESTION_MAX_CHARS],
            "searcher": rec["searcher"], "state": rec["state"],
            "reason": rec["outcome"]["reason"][:RECENT_REASON_MAX_CHARS],
            "ended_at": rec["finished_at"],
            "ran_s": (round(rec["finished_at"] - rec["started_at"])
                      if rec["started_at"] else 0),
            "tokens": rec["stream"].tokens()})
        self._trim_recent()

    def view(self, sid: str) -> dict | None:
        """The operator's read-only view of one search still in the table
        (ended and not collected too): the tool calls of its trace and the
        agent's text blocks, oldest first: the newest VIEW_TAIL_ENTRIES, a
        text cut to its end. None when there is no such search."""
        with self.lock:
            rec = self.table.get(sid)
            rec = dict(rec) if rec is not None else None
        if rec is None:
            return None
        texts, dropped = rec["stream"].work_texts()
        root = volume_root(SEARCH_PREFIX + sid) if rec["state"] != "queued" else None
        tools = _trace_work(read_volume_file(root, OUT_DIR, TRACE_NAME, TRACE_MAX_BYTES)
                            if root else None)
        work = sorted(tools + texts, key=lambda e: e["ts"])
        out = {"id": sid, "state": rec["state"], "question": rec["question"],
               "searcher": rec["searcher"], "tokens": rec["stream"].tokens(),
               **self._times(rec),
               "reason": rec["outcome"]["reason"] if rec["state"] in ENDED else None,
               "work": [], "truncated": False}
        for entry in work[-VIEW_TAIL_ENTRIES:]:
            if entry["kind"] == "text" and len(entry["text"]) > VIEW_TEXT_TAIL_CHARS:
                entry = dict(entry, text="…" + entry["text"][-VIEW_TEXT_TAIL_CHARS:])
            out["work"].append(entry)
        out["truncated"] = dropped or len(work) > VIEW_TAIL_ENTRIES
        return out

    def stop(self, sid: str) -> dict | None:
        """The operator's stop. A queued search leaves the queue and ends at
        once (its waiting thread then exits without running); a starting or
        running one has its container killed, and its run ends it as stopped
        (a stop before its container exists is caught right after the
        container starts). None when no such search is waiting or running."""
        with self.lock:
            rec = self.table.get(sid)
            if rec is None or rec["state"] in ENDED:
                return None
            if rec["state"] == "queued":
                self.queue.remove(sid)
                rec["state"] = "failed"
                rec["finished_at"] = time.time()
                rec["outcome"] = {"reason": STOPPED_QUEUED}
                self._record_end(rec)
                self.lock.notify_all()
                state = "failed"
            else:
                rec["stop"] = True
                state = rec["state"]
        if state == "failed":
            log(f"search {sid} failed: {STOPPED_QUEUED}")
        else:
            docker("kill", SEARCH_PREFIX + sid)
            log(f"stop {sid} asked by the operator")
        return {"id": sid, "state": state, "stopped": True}

    def _stopped(self, sid: str) -> bool:
        with self.lock:
            return bool(self.table.get(sid, {}).get("stop"))

    # --- one search's run ---

    def _activity(self, sid: str) -> str | None:
        """What the agent is doing: its tool server's last trace line (one
        small read of the volume; nothing when it has not called a tool)."""
        root = volume_root(SEARCH_PREFIX + sid)
        data = read_volume_file(root, OUT_DIR, TRACE_NAME, TRACE_MAX_BYTES) if root else None
        if not data:
            return "starting the search agent"
        for line in reversed(data.decode("utf-8", "replace").splitlines()):
            try:
                ev = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(ev, dict) and isinstance(ev.get("tool"), str):
                return f"{ev['tool']}: {str(ev.get('target', ''))[:200]}"
        return None

    def _wait_turn(self, sid: str) -> bool:
        """Wait for this search's turn and take a slot; False when the
        operator stopped it while it waited (it took no slot)."""
        with self.lock:
            while True:
                if sid not in self.queue:
                    return False
                settings = read_settings()
                at_once = settings["searcher"]["at_once"] if settings else 1
                if self.queue and self.queue[0] == sid and self.running < at_once:
                    self.queue.popleft()
                    self.running += 1
                    self.table[sid]["state"] = "starting"
                    self.table[sid]["started_at"] = time.time()
                    self.lock.notify_all()
                    return True
                self.lock.wait(QUEUE_RECHECK_S)

    def _finish(self, sid: str, state: str, outcome: dict) -> None:
        with self.lock:
            rec = self.table.get(sid)
            if rec is not None:
                rec["state"] = state
                rec["finished_at"] = time.time()
                rec["outcome"] = outcome
                self._record_end(rec)
            self.running -= 1
            self.lock.notify_all()
        log(f"search {sid} {state}: {outcome['reason']}")

    def _run(self, sid: str) -> None:
        if not self._wait_turn(sid):
            return
        with self.lock:
            rec = dict(self.table[sid])
        try:
            state, outcome = run_search(rec, lambda: self._set_running(sid),
                                        lambda: self._stopped(sid))
        except Exception as e:  # noqa: BLE001 — a search never takes the service down
            state, outcome = "failed", {"reason": f"the service failed to run the search "
                                                  f"({type(e).__name__})"}
        self._finish(sid, state, outcome)

    def _set_running(self, sid: str) -> None:
        with self.lock:
            if sid in self.table:
                self.table[sid]["state"] = "running"


SEARCHES = Searches()


class ToolRefusal(Exception):
    """A tool error whose text the caller reads."""


def remove_search(sid: str) -> list[str]:
    """Remove one search's container, network and volume, each by its name.
    Idempotent; returns what could not be removed."""
    name = SEARCH_PREFIX + sid
    problems = []
    if container_state(name)[0] != "absent":
        r = docker("rm", "-f", name)
        if r.returncode != 0:
            problems.append(f"container {name}: {first_line(r)}")
    if docker("network", "inspect", name).returncode == 0:
        docker("network", "disconnect", "-f", name, SEARXNG_CONTAINER)
        r = docker("network", "rm", name)
        if r.returncode != 0:
            problems.append(f"network {name}: {first_line(r)}")
    if docker("volume", "inspect", name).returncode == 0:
        r = docker("volume", "rm", name)
        if r.returncode != 0:
            problems.append(f"volume {name}: {first_line(r)}")
    return problems


def remove_leftovers() -> None:
    """At start: everything an earlier run left (found by ID_LABEL), each
    removed by its name; a labelled thing whose name is not one of ours is
    reported, never removed."""
    found: set = set()
    for kind, fmt in (("ps", "{{.Names}}"), ("network", "{{.Name}}"), ("volume", "{{.Name}}")):
        args = (["ps", "-a"] if kind == "ps" else [kind, "ls"]) + [
            "--filter", f"label={ID_LABEL}", "--format", fmt]
        r = docker(*args)
        for n in r.stdout.decode("utf-8", "replace").split():
            sid = n[len(SEARCH_PREFIX):] if n.startswith(SEARCH_PREFIX) else ""
            if ID_RE.match(sid):
                found.add(sid)
            else:
                log(f"leftover {n} carries the search label but is not named as one "
                    "of the service's searches; left alone")
    for sid in sorted(found):
        problems = remove_search(sid)
        log(f"removed leftover search {sid}" + (f" ({'; '.join(problems)})" if problems else ""))


# The search agent's own failure words for a refused sign-in (Claude Code's
# result text and its first error lines).
_SIGN_IN_MARKERS = ("invalid api key", "oauth", "401", "authentication", "not logged in",
                    "please run /login", "unauthorized", "invalid bearer", "credential")


def _agent_end(name: str) -> dict:
    """The agent's own account of how it ended: its final `result` event and
    the last error lines (bounded tail of its output)."""
    r = docker("logs", "--tail", str(AGENT_LOG_TAIL_LINES), name)
    text = (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace")
    result, other = None, []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, RecursionError):
            other.append(line)
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            result = ev
    out: dict = {}
    if result is not None:
        out["is_error"] = bool(result.get("is_error"))
        out["text"] = str(result.get("result") or result.get("subtype") or "")[:AGENT_TEXT_MAX_CHARS]
        for k in ("num_turns", "total_cost_usd", "duration_ms"):
            if isinstance(result.get(k), (int, float)) and not isinstance(result.get(k), bool):
                out[k] = result[k]
    if other:
        out["last_lines"] = [l[:AGENT_TEXT_MAX_CHARS] for l in other[-5:]]
    return out


def _trace_summary(raw: bytes | None) -> dict:
    """What was tried, from the tool server's trace (one line per tool call,
    written by RS's code, not by the model): per tier and tool the calls and
    their outcomes, the failures with their targets, and every engine SearXNG
    reported unresponsive with its reasons."""
    tiers: dict = {}
    failures: list = []
    unresponsive: dict = {}
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        try:
            ev = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(ev, dict):
            continue
        tier = str(ev.get("tier", "?"))[:TRACE_NAME_MAX_CHARS]
        tool = str(ev.get("tool", "?"))[:TRACE_NAME_MAX_CHARS]
        outcome = str(ev.get("outcome", "?"))[:TRACE_NAME_MAX_CHARS]
        t = tiers.setdefault(tier, {}).setdefault(tool, {})
        t[outcome] = t.get(outcome, 0) + 1
        if outcome not in ("ok", "empty"):
            failures.append({"tool": tool,
                             "target": str(ev.get("target", ""))[:TRACE_TARGET_MAX_CHARS],
                             "outcome": outcome,
                             "detail": str(ev.get("detail", ""))[:TRACE_TARGET_MAX_CHARS]})
        for item in ev.get("unresponsive") or []:
            if isinstance(item, dict):
                eng = str(item.get("engine", "?"))[:60]
                why = str(item.get("reason", "?"))[:120]
                reasons = unresponsive.setdefault(eng, [])
                if why not in reasons:
                    reasons.append(why)
    return {"tiers": tiers, "failures": failures, "searxng_unresponsive": unresponsive}


def _trace_work(raw: bytes | None) -> list[dict]:
    """The tool calls of a trace as the operator's view lists them: each with
    its time and every field a string, capped as _trace_summary caps them. A
    line without a usable time is not listed (RS's tool server always writes
    one; the agent can write the file, so its contents are untrusted)."""
    out = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        try:
            ev = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(ev, dict):
            continue
        ts = ev.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts):
            continue
        out.append({"ts": ts, "kind": "tool",
                    "tool": str(ev.get("tool", "?"))[:TRACE_NAME_MAX_CHARS],
                    "tier": str(ev.get("tier", "?"))[:TRACE_NAME_MAX_CHARS],
                    "target": str(ev.get("target", ""))[:TRACE_TARGET_MAX_CHARS],
                    "outcome": str(ev.get("outcome", "?"))[:TRACE_NAME_MAX_CHARS],
                    "detail": str(ev.get("detail", ""))[:TRACE_TARGET_MAX_CHARS]})
    return out


def run_search(rec: dict, started, stopped) -> tuple[str, dict]:
    """Create the search's network, volume and container, follow and wait for
    the agent, and read back its report. `stopped()` says whether the operator
    stopped it. Returns (state, outcome)."""
    sid = rec["id"]
    name = SEARCH_PREFIX + sid
    if not token_present():
        return "failed", {"reason": "the search service has no sign-in: the operator "
                                    "stores a long-lived Claude token on the Search page"}
    if not image_present(AGENT_IMAGE):
        return "failed", {"reason": "the search agent's image is not staged in the "
                                    "search service; the operator turns the service on again"}
    if not os.path.exists(os.path.join(CLAUDE_DIST, "local", "bin", "claude")) and \
            not os.path.islink(os.path.join(CLAUDE_DIST, "local", "bin", "claude")):
        return "failed", {"reason": "the Claude download is missing on the host; the "
                                    "operator pulls it under Host → Software"}
    searxng_problem = ensure_searxng()
    labels = ["--label", f"{ID_LABEL}={sid}", "--label", f"{PROJECT_LABEL}={rec['project']}"]
    for args, what in ((["volume", "create", *labels, name], "volume"),
                       (["network", "create", *labels, name], "network")):
        r = docker(*args)
        if r.returncode != 0:
            return "failed", {"reason": f"the search's {what} could not be created "
                                        f"({first_line(r)})"}
    if searxng_problem is None:
        r = docker("network", "connect", "--alias", SEARXNG_ALIAS, name, SEARXNG_CONTAINER)
        if r.returncode != 0:
            searxng_problem = f"SearXNG could not join the search's network ({first_line(r)})"
    # No capability at all, no privilege gain, the agent's own user; its
    # network reaches the internet and SearXNG only (the service's guard drops
    # everything from an inner network toward the service and toward any
    # private address). The token reaches it through an env file read by the
    # docker client here, never on this command line.
    if stopped():
        return "failed", {"reason": STOPPED_NO_REPORT}
    r = docker("run", "-d", "--name", name, *labels,
               "--network", name, "--restart", "no",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--user", "1000:1000",
               "--env-file", TOKEN_PATH,
               "-e", f"RS_SEARCH_ID={sid}",
               "-e", f"RS_SEARCH_MODEL={rec['model']}",
               "-e", f"RS_SEARCH_EFFORT={rec['effort']}",
               "-e", f"RS_SEARCH_CONTACT={rec['contact']}",
               "-e", f"RS_SEARCH_SEARXNG={'' if searxng_problem else 'on'}",
               "-e", f"RS_SEARCH_QUESTION={rec['question']}",
               "-v", f"{name}:{AGENT_VOLUME_MOUNT}",
               "-v", f"{CLAUDE_DIST}:/opt/claude:ro",
               AGENT_IMAGE)
    if r.returncode != 0:
        return "failed", {"reason": f"the search agent could not be started ({first_line(r)})"}
    follower = follow(name, rec["stream"])
    started()
    if stopped():
        docker("kill", name)        # the operator's stop came before the container existed
    timed_out = False
    w = docker("wait", name, timeout=SEARCH_MAX_S)
    if w.returncode == 124:
        timed_out = True
        docker("kill", name)
        docker("wait", name)
    was_stopped = stopped()
    end_follow(follower)
    exit_code = None
    try:
        exit_code = int(w.stdout.decode().strip())
    except ValueError:
        pass
    agent = _agent_end(name)
    if exit_code is not None:
        agent["exit_code"] = exit_code
    root = volume_root(name)
    report_raw = read_volume_file(root, OUT_DIR, REPORT_NAME, REPORT_MAX_BYTES) if root else None
    tried = _trace_summary(read_volume_file(root, OUT_DIR, TRACE_NAME, TRACE_MAX_BYTES)
                           if root else None)
    if searxng_problem:
        tried["searxng"] = searxng_problem
    report = None
    if report_raw is not None:
        try:
            report = json.loads(report_raw)
        except (ValueError, RecursionError):
            report = None
    outcome: dict = {"tried": tried, "agent": agent}
    if isinstance(report, dict):
        outcome["summary"] = report.get("summary", "")
        outcome["sources"] = report.get("sources", [])
        outcome["tiers"] = report.get("tiers", [])
        if was_stopped:
            outcome["reason"] = STOPPED_WITH_REPORT
            return "done", outcome
        if timed_out:
            outcome["reason"] = (f"the search ran past {SEARCH_MAX_S} seconds and was "
                                 "stopped after it wrote its report")
            return "done", outcome
        outcome["reason"] = "the search agent wrote its report"
        return "done", outcome
    words = (agent.get("text", "") + " " + " ".join(agent.get("last_lines", []))).lower()
    if was_stopped:
        outcome["reason"] = STOPPED_NO_REPORT
    elif timed_out:
        outcome["reason"] = (f"the search ran past {SEARCH_MAX_S} seconds and was stopped "
                             "before it wrote a report")
    elif agent.get("is_error") and any(m in words for m in _SIGN_IN_MARKERS):
        outcome["reason"] = ("the search agent could not sign in to Claude: the token "
                             "stored on the Search page was refused (mint a new one with "
                             "`claude setup-token` and store it there). The agent said: "
                             + (agent.get("text") or "nothing"))
    elif agent.get("is_error"):
        outcome["reason"] = "the search agent stopped with an error: " + agent.get("text", "")
    else:
        outcome["reason"] = ("the search agent ended without writing a report"
                             + (f": {agent['text']}" if agent.get("text") else ""))
    return "failed", outcome


def sign_in_test() -> dict:
    """The operator's Test of the stored token: one throwaway agent container
    makes one minimal call to the cheapest model, hardened like a search's
    and on a network of its own, both named and labelled as a search (so a
    crash leaves nothing remove_leftovers misses) and removed afterwards. It
    never enters the searches table."""
    if not token_present():
        return {"ok": False, "text": "no token is stored in the search service; store "
                                     "a long-lived Claude token on the Search page"}
    if not image_present(AGENT_IMAGE):
        return {"ok": False, "text": "the search agent's image is not staged in the search "
                                     "service; turning the service on again stages it"}
    if not os.path.exists(os.path.join(CLAUDE_DIST, "local", "bin", "claude")) and \
            not os.path.islink(os.path.join(CLAUDE_DIST, "local", "bin", "claude")):
        return {"ok": False, "text": "the Claude download is missing on the host; pull it "
                                     "under Host → Software"}
    sid = secrets.token_hex(8)
    name = SEARCH_PREFIX + sid
    labels = ["--label", f"{ID_LABEL}={sid}"]
    try:
        r = docker("network", "create", *labels, name)
        if r.returncode != 0:
            return {"ok": False, "text": f"the test's network could not be created "
                                         f"({first_line(r)})"}
        # Hardened as a search's container (run_search); Claude itself is the
        # entrypoint (the image's launcher needs a question).
        r = docker("run", "-d", "--name", name, *labels,
                   "--network", name, "--restart", "no",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--user", "1000:1000",
                   "--env-file", TOKEN_PATH,
                   "-v", f"{CLAUDE_DIST}:/opt/claude:ro",
                   "--entrypoint", "/opt/claude/local/bin/claude",
                   AGENT_IMAGE,
                   "-p", SIGN_IN_TEST_PROMPT, "--model", SIGN_IN_TEST_MODEL,
                   "--tools", "", "--strict-mcp-config",
                   "--output-format", "stream-json", "--verbose")
        if r.returncode != 0:
            return {"ok": False, "text": f"the test agent could not be started ({first_line(r)})"}
        w = docker("wait", name, timeout=SIGN_IN_TEST_S)
        if w.returncode == 124:
            docker("kill", name)
            return {"ok": False, "text": f"Claude did not answer within {SIGN_IN_TEST_S} "
                                         "seconds, so the sign-in could not be tested; "
                                         "testing again retries"}
        agent = _agent_end(name)
    finally:
        problems = remove_search(sid)
        if problems:
            log(f"sign-in test {sid}: {'; '.join(problems)}")
    said = (agent.get("text") or " ".join(agent.get("last_lines", []))
            or "nothing")[:AGENT_TEXT_MAX_CHARS]
    words = (agent.get("text", "") + " " + " ".join(agent.get("last_lines", []))).lower()
    if "is_error" in agent and not agent["is_error"]:
        log("sign-in test: the sign-in works")
        return {"ok": True, "text": "the sign-in works"}
    if any(m in words for m in _SIGN_IN_MARKERS):
        log("sign-in test: the token was refused")
        return {"ok": False, "text": "the stored token was refused: Claude did not accept "
                                     "it as a sign-in (mint a new one with `claude "
                                     "setup-token` and store it here). Claude said: " + said}
    if agent.get("is_error"):
        return {"ok": False, "text": "the test call ended with an error: " + said}
    return {"ok": False, "text": "the test call ended without an answer: " + said}


# --- MCP (streamable HTTP, JSON replies) ----------------------------------------------

TOOLS = [
    {"name": "search_submit",
     "description": ("Hand a question to a searcher. Returns a search id at once; "
                     "the search runs on its own (minutes), so go on with your work "
                     "and come back with search_check. Ask one clear question with "
                     "what you need back (papers on X since 2020, the dataset behind "
                     "Y, the PDF of Z)."),
     "inputSchema": {"type": "object",
                     "properties": {"question": {"type": "string"},
                                    "searcher": {"type": "string",
                                                 "description": "the searcher's name; "
                                                                "the only one when omitted"}},
                     "required": ["question"]}},
    {"name": "search_check",
     "description": ("How a search goes: queued, running (and what it is doing), or "
                     "ended (done or failed) with the files it saved and their paths."),
     "inputSchema": {"type": "object",
                     "properties": {"search_id": {"type": "string"}},
                     "required": ["search_id"]}},
    {"name": "search_collect",
     "description": ("The result of an ended search: the written summary, every "
                     "source, which ways of searching were tried and what each hit "
                     "(including the web search engines that did not answer), and "
                     "its files. Collecting ends the search and removes its files, "
                     "unless keep is true; download the files first (GET this "
                     "server's address + /files/<search id>/<name>, same "
                     "Authorization)."),
     "inputSchema": {"type": "object",
                     "properties": {"search_id": {"type": "string"},
                                    "keep": {"type": "boolean"}},
                     "required": ["search_id"]}},
]


def _rpc_error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_result(data, is_error: bool = False) -> dict:
    text = data if isinstance(data, str) else json.dumps(data, indent=1, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(caller: tuple[str, str] | None, name: str, args: dict) -> dict:
    if caller is None:
        return _tool_result("this server is reached through RS's MCP gateway only", True)
    project, consumer = caller
    try:
        if name == "search_submit":
            return _tool_result(SEARCHES.submit(project, consumer, args.get("question"),
                                                args.get("searcher") or ""))
        if name == "search_check":
            return _tool_result(SEARCHES.check(project, args.get("search_id")))
        if name == "search_collect":
            return _tool_result(SEARCHES.collect(project, args.get("search_id"),
                                                 args.get("keep") is True))
    except ToolRefusal as e:
        return _tool_result(str(e), True)
    return _tool_result(f"no tool named {name}", True)


def handle_rpc(msg, caller) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" \
            or not isinstance(msg.get("method"), str):
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None,
                          -32600, "invalid request")
    rid, method = msg.get("id"), msg["method"]
    if "id" not in msg:
        return None                                   # a notification
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        return {"jsonrpc": "2.0", "id": rid,
                "result": call_tool(caller, str(params.get("name")), args)}
    # Claude Code's `server/discover` and anything else this server does not know.
    return _rpc_error(rid, -32601, f"method not found: {method}")


def parse_caller(value: str | None) -> tuple[str, str] | None:
    """`<project>/<consumer>` as the gateway sets it, or None."""
    if not value:
        return None
    project, sep, consumer = value.partition("/")
    if not sep or not PROJECT_RE.match(project) or not consumer \
            or any(ord(c) < 0x21 or ord(c) > 0x7e for c in consumer):
        return None
    return project, consumer


class Handler(BaseHTTPRequestHandler):
    server_version = "rs-search/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
              extra: dict | None = None) -> None:
        self.send_response(status)
        if body or status != 202:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, (json.dumps(obj) + "\n").encode())

    def _local(self) -> bool:
        return self.client_address[0] == "127.0.0.1"

    def _body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if length < 0:
            return None
        return self.rfile.read(length)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/admin/"):
            return self._admin(path, post=True)
        if path.rstrip("/") != MCP_PATH:
            return self._json(404, {"error": "not found"})
        raw = self._body()
        try:
            msg = json.loads(raw or b"")
        except (ValueError, RecursionError):
            return self._json(400, _rpc_error(None, -32700, "parse error"))
        caller = parse_caller(self.headers.get(CALLER_HEADER))
        if isinstance(msg, list):
            replies = [r for r in (handle_rpc(m, caller) for m in msg) if r is not None]
            return self._json(200, replies) if replies else self._send(202)
        reply = handle_rpc(msg, caller)
        if reply is None:
            return self._send(202)
        return self._json(200, reply)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/admin/"):
            return self._admin(path, post=False)
        if path.startswith(FILES_PREFIX):
            return self._file(path[len(FILES_PREFIX):])
        if path.rstrip("/") == MCP_PATH:
            # No server-initiated stream: every reply is the POST's own.
            return self._send(405, b"", extra={"Allow": "POST"})
        return self._json(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_DELETE(self):
        return self._send(405, b"", extra={"Allow": "POST"})

    def _file(self, rest: str) -> None:
        caller = parse_caller(self.headers.get(CALLER_HEADER))
        sid, _, fname = urllib.parse.unquote(rest).partition("/")
        missing = {"error": "no such file for this project"}
        if caller is None or not ID_RE.match(sid) or not FILE_NAME_RE.match(fname):
            return self._json(404, missing)
        try:
            SEARCHES.get(caller[0], sid)
        except ToolRefusal:
            return self._json(404, missing)
        root = volume_root(SEARCH_PREFIX + sid)
        if root is None:
            return self._json(404, missing)
        try:
            rfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                dfd = _open_dir(rfd, FILES_DIR)
                try:
                    fd, st = _open_file(dfd, fname)
                finally:
                    os.close(dfd)
            finally:
                os.close(rfd)
        except OSError:
            return self._json(404, missing)
        with os.fdopen(fd, "rb") as f:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(st.st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.end_headers()
            if self.command == "HEAD":
                return
            left = st.st_size
            while left > 0:
                piece = f.read(min(PIECE, left))
                if not piece:
                    break
                self.wfile.write(piece)
                left -= len(piece)

    def _admin(self, path: str, post: bool) -> None:
        if not self._local():
            return self._json(404, {"error": "not found"})
        if path == "/admin/status":
            st, _labels = container_state(SEARXNG_CONTAINER)
            return self._json(200, {
                "searches": SEARCHES.counts(),
                "searxng": st,
                "images": {"agent": image_present(AGENT_IMAGE),
                           "searxng": image_present(SEARXNG_IMAGE)},
                "settings": read_settings() is not None,
                "token": token_present()})
        if path == "/admin/ensure" and post:
            return self._json(200, {"searxng": ensure_searxng() or "running"})
        if path == "/admin/clear" and post:
            return self._json(200, SEARCHES.clear_ended())
        if path == "/admin/list" and not post:
            return self._json(200, SEARCHES.active())
        if path == "/admin/test" and post:
            return self._json(200, sign_in_test())
        for prefix, is_post, act in (("/admin/view/", False, SEARCHES.view),
                                     ("/admin/stop/", True, SEARCHES.stop)):
            if path.startswith(prefix) and post == is_post:
                sid = path[len(prefix):]
                found = act(sid) if ID_RE.match(sid) else None
                if found is None:
                    return self._json(404, {"error": NO_SUCH_SEARCH})
                return self._json(200, found)
        return self._json(404, {"error": "not found"})


def serve() -> None:
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    remove_leftovers()
    state, _labels = container_state(SEARXNG_CONTAINER)
    if state != "absent":
        docker("rm", "-f", SEARXNG_CONTAINER)        # a fresh one, fresh settings
    if image_present(SEARXNG_IMAGE):
        problem = ensure_searxng()
        if problem:
            log(f"searxng: {problem}")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    log(f"listening on {PORT}")
    httpd.serve_forever()


# --- the host's side doors (docker exec) ------------------------------------------------

def put(name: str) -> int:
    if name not in DELIVERED:
        sys.stderr.write("unknown file\n")
        return 2
    data = sys.stdin.buffer.read(DELIVERY_MAX_BYTES + 1)
    if len(data) > DELIVERY_MAX_BYTES:
        sys.stderr.write("too large\n")
        return 2
    os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
    tmp = os.path.join(RUN_DIR, f".{name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, os.path.join(RUN_DIR, name))
    return 0


def drop(name: str) -> int:
    if name not in DELIVERED:
        sys.stderr.write("unknown file\n")
        return 2
    try:
        os.unlink(os.path.join(RUN_DIR, name))
    except FileNotFoundError:
        pass
    return 0


# The exit code of `admin` when the service answered with a refusal (its JSON
# error on stdout): no such search for view / stop.
ADMIN_REFUSED_EXIT = 3
_ADMIN_GET = ("status", "list", "view")
_ADMIN_WITH_ID = ("view", "stop")


def admin(what: str, sid: str | None = None) -> int:
    known = ("status", "ensure", "clear", "list", "test") + _ADMIN_WITH_ID
    if what not in known or (what in _ADMIN_WITH_ID) != (sid is not None) \
            or (sid is not None and not ID_RE.match(sid)):
        sys.stderr.write("unknown admin request\n")
        return 2
    get = what in _ADMIN_GET
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/admin/{what}"
                                 + (f"/{sid}" if sid else ""),
                                 method="GET" if get else "POST",
                                 data=None if get else b"")
    try:
        with urllib.request.urlopen(req, timeout=ADMIN_CALL_S) as resp:
            sys.stdout.write(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            sys.stderr.write(f"the search service refused the request (HTTP {e.code})\n")
            return 1
        sys.stdout.write(e.read().decode("utf-8", "replace"))
        return ADMIN_REFUSED_EXIT
    except OSError as e:
        sys.stderr.write(f"the search service does not answer ({type(e).__name__})\n")
        return 1
    return 0


def main(argv: list) -> int:
    if argv == ["serve"]:
        serve()
        return 0
    if len(argv) == 2 and argv[0] == "put":
        return put(argv[1])
    if len(argv) == 2 and argv[0] == "drop":
        return drop(argv[1])
    if len(argv) in (2, 3) and argv[0] == "admin":
        return admin(*argv[1:])
    sys.stderr.write("usage: service.py serve | put NAME | drop NAME | admin "
                     "status|ensure|clear|list|test | admin view|stop ID\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
