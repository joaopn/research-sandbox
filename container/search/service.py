#!/opt/conda/bin/python3
"""rs-search — RS's search service, inside its own container.

Runs as root in the `rs-search` container (container/search/boot.sh starts it
once the inner Docker answers), and does two things:

  - the caller-facing MCP server on SERVICE_PORT, behind the MCP gateway's
    `search` entry (MCP_PATH) and, while the VPN version is on, its
    `search-vpn` entry (MCP_VPN_PATH, the same tools; a search asked there
    is a VPN search): submit a question, check a search, collect its result,
    and a file route (GET <path>/files/<search id>/<file>) under the server's
    registered path, which is where the gateway relays a caller's
    `/search/files/...` (`/search-vpn/files/...`). The caller is the
    `<project>/<consumer>` the gateway puts in CALLER_HEADER (the gateway
    strips any copy a caller sends). Searches are keyed on the caller's
    PROJECT and on the path they were asked through: another project's
    search is "not found", whatever its id, and the caller's own search
    asked through the other path is refused with that path's server name
    (its files are not found);
  - each search, in the inner Docker: a private network, one search-agent
    container, a browser sidecar per browser tier that is on, and a named
    volume for its files, all named after the search id and removed BY NAME
    once the caller collected the result or the operator cleared it. At
    start, whatever an earlier run left is found by label and removed by
    name. SearXNG (the plain tier's web search) runs beside them and is
    connected to a second, internal network of each search that only the
    search's agent joins (never to the search's own network, where its
    sidecars are), never the searches to each other;
  - while the VPN's exits are delivered (the host delivers them only while
    the VPN version is ticked), one VPN gateway per exit (kernel WireGuard,
    NET_ADMIN only), each with its own tunnel and its own background check,
    and a second SearXNG routed through the first exit's gateway. A VPN
    search (one asked through MCP_VPN_PATH) gets, for every exit whose
    tunnel is up when it starts, that exit's own sidecars (the plain tier
    too, over HTTP) on a network of the search and that exit, `--internal`,
    each routed through that exit's gateway by a one-shot helper and
    resolving through its tunnel; its tool list names each server after its
    tier and exit, and its instructions list the exits in order and say how
    to go from one to the next. Its agent reaches only its sidecars and, by a
    network of its own, its model provider. With a tunnel down its gateway
    routes nothing (`unreachable`); with none up, a VPN search fails before
    it starts.

`service.py put NAME` / `drop NAME` (the host, over `docker exec -i`) write or
remove one of the files the host hands over (settings, token, VPN
configuration) in RUN_DIR;
`service.py admin status|clear|ensure|vpn|vpn-apply|list|test`, `admin
vpn-renew FINGERPRINT` (an exit's, exit_fingerprint) and `admin view|stop ID` ask
the running server from inside the container (the admin routes answer
127.0.0.1 only): the operator's list of waiting and running searches, a
read-only view of one search's work, stopping one, and testing the sign-in.
`admin ask` (its body on stdin), `admin conversations|conversations-clear`
and `admin conversation|conversation-delete ID` are the Search page's Quick
Ask: the operator's own questions, searches in the same queue under a project
no caller can be, collected by the service itself at their end and kept, with
their answers, in conversations on a volume of their own.

Stdlib only (the dind image's conda Python).
"""

from __future__ import annotations

import collections
import errno
import hashlib
import ipaddress
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
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- names (MIRRORS of cli/search.py where noted) ------------------------------

PORT = 8080                                   # search.SERVICE_PORT
MCP_PATH = "/mcp"                             # search.MCP_PATH
MCP_VPN_PATH = "/mcp-vpn"                     # search.MCP_VPN_PATH
# Each caller-facing path: whether its searches are VPN searches.
MCP_PATHS = {MCP_PATH: False, MCP_VPN_PATH: True}
FILES_SUFFIX = "/files/"
# The servers' names on the gateway, as the tool descriptions and refusals
# name them (search.MCP_SERVER_NAME / MCP_VPN_SERVER_NAME — MIRRORS).
SERVER_NAMES = {False: "search", True: "search-vpn"}
CALLER_HEADER = "X-RS-Caller"                 # search.CALLER_HEADER
RUN_DIR = "/run/rs-search"                    # search.SERVICE_RUN_DIR
SETTINGS_PATH = os.path.join(RUN_DIR, "settings.json")
TOKEN_PATH = os.path.join(RUN_DIR, "claude.env")
# The VPN configuration (search.VPN_FILE_NAME — MIRROR): its keys reach only
# the VPN gateway container, over `docker exec` stdin.
VPN_PATH = os.path.join(RUN_DIR, "vpn.json")
DELIVERED = ("settings.json", "claude.env", "vpn.json")
CLAUDE_DIST = "/opt/agent-dist/claude"        # search.SERVICE_AGENT_DIST + /claude
STATE_DIR = "/var/lib/rs-search"              # this container's own disk
SEARXNG_DIR = os.path.join(STATE_DIR, "searxng")

AGENT_IMAGE = os.environ.get("RS_SEARCH_AGENT_IMAGE", "")
SEARXNG_IMAGE = os.environ.get("RS_SEARCH_SEARXNG_IMAGE", "")
# The browser tiers (search.TIER_NAMES — MIRROR) and the image of each one's
# sidecar (search.BROWSER_IMAGE / STEALTH_IMAGE, handed over by the host).
TIER_NAMES = ("browser", "stealth")
SIDECAR_IMAGES = {"browser": os.environ.get("RS_SEARCH_BROWSER_IMAGE", ""),
                  "stealth": os.environ.get("RS_SEARCH_STEALTH_IMAGE", "")}
# The VPN gateway's image (search.VPN_IMAGE), also the image of the one-shot
# helper that routes a VPN search's sidecars through the gateway.
VPN_IMAGE = os.environ.get("RS_SEARCH_VPN_IMAGE", "")

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
# A search's second network, internal (no route out), named after the search
# with this suffix and labelled like its other things: only the search's
# agent and SearXNG are on it. SearXNG is on every search's such network, so
# it is kept off the search's own network, where the browser sidecars'
# tool servers listen: nothing outside a search reaches its browsers.
SEARXNG_NET_SUFFIX = "-sx"
# A search's browser sidecar is named SEARCH_PREFIX + its id + "-" + its tier
# (sidecar_name), carries ID_LABEL like the search's other things, and
# TIER_LABEL naming its tier.
TIER_LABEL = "research.search.tier"
# The uid:gid the agent and the sidecars run as (each image's own user).
RUN_AS = "1000:1000"
AGENT_UID = 1000
# The sidecars' tool server: its port and path (Dockerfile.browser's
# ENTRYPOINT, stealth-start.py TOOL_PORT — MIRRORS), and where each mounts the
# search volume's files folder as its output folder (Dockerfile.browser /
# stealth-start.py OUTPUT_DIR — MIRROR).
SIDECAR_PORT = 8931
SIDECAR_PATH = "/mcp"
SIDECAR_OUTPUT = "/output"
# The plain tier's tool server, as the agent's tool list names it (the
# server's file is in the agent image).
PLAIN_SERVER = {"type": "stdio", "command": "python3", "args": ["/opt/rs-search-agent/tools.py"]}
# In a VPN search the plain tier runs as a third sidecar (the same tool
# server, served over HTTP on SIDECAR_PORT; agent/tools.py --http — MIRROR):
# every tool that touches a site runs on the VPN's route, and the agent's own
# container keeps only its route to its model provider.
PLAIN_TIER = "plain"
PLAIN_SIDECAR_CMD = ["python3", "/opt/rs-search-agent/tools.py", "--http"]

# --- the VPN's exits and their gateways --------------------------------------------
# The VPN is an ordered list of exits (the host delivers them only while the
# VPN version is ticked), each a name and a WireGuard configuration. Each
# exit has one gateway container inside this service, named
# VPN_GATEWAY_PREFIX + the exit's fingerprint (exit_fingerprint: its name
# has no length or character bound a container name could carry) and
# labelled with VPN_LABEL and EXIT_LABEL (the exit's name): kernel WireGuard
# with NET_ADMIN and IPv4 forwarding only (vpn/up.sh). The gateways share one network, their way to their VPN
# servers (through this container's egress and RS's router, which lets each
# server's UDP port out; each gateway takes nothing in there but its own
# server's answers and forwards nothing from there); each joins its exit's
# network of every VPN search, which is `--internal` (no way out but the one
# the helper sets: through that gateway), and the FIRST exit's gateway also
# the network of the VPN's own SearXNG.
VPN_GATEWAY_PREFIX = "rs-search-vpn-"
# The one gateway of the release before exits (no EXIT_LABEL): an earlier
# run's is removed at start like the others.
V1_GATEWAY = "rs-search-vpn"
VPN_WAN_NETWORK = "rs-search-vpn-wan"
VPN_LABEL = "research.search.vpn"
# The exit a gateway, or a VPN search's sidecar or network, belongs to.
EXIT_LABEL = "research.search.exit"
# What an exit's name never holds (Unicode categories): the controls and the
# line and paragraph separators (it is written on one line: a log line, a
# docker label read back line by line, the searcher's instructions), and a
# lone surrogate (no text; it has no UTF-8 form). The rule cli/search.py
# exit_name_problem applies (its _EXIT_NAME_REFUSED and its lone-surrogate
# check — MIRROR).
_EXIT_NAME_REFUSED = ("Cc", "Zl", "Zp", "Cs")
# An exit's fingerprint (exit_fingerprint): the SHA-256 of its name, in hex.
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{%d}$" % (2 * hashlib.sha256().digest_size))
# The names exits had before any name was allowed: an earlier run's gateway
# is named VPN_GATEWAY_PREFIX + such a name (and carries it as its
# EXIT_LABEL); gateways_present finds it and the sweep removes it by name.
OLD_EXIT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
VPN_SCRIPTS = "/opt/rs-search-vpn"        # Dockerfile.vpn — MIRROR
# How many exits a VPN search tries on one site, when the host sent none
# (search.DEFAULT_EXITS_PER_SITE — MIRROR).
DEFAULT_EXITS_PER_SITE = 2
# The VPN's SearXNG: the web search of VPN searches, routed through the
# first exit's gateway (its own `--internal` network holds it and that
# gateway), joined to each VPN search's SearXNG network, where only that
# search's first exit's plain-tier sidecar is (whose tool server takes no new
# connection from there).
SEARXNG_VPN_CONTAINER = "rs-search-searxng-vpn"
SEARXNG_VPN_NETWORK = "rs-search-vpn-sx"
SEARXNG_VPN_DIR = os.path.join(STATE_DIR, "searxng-vpn")
# A VPN search's agent's own way out (the model provider), named after the
# search with this suffix, which only the agent is on.
OUT_NET_SUFFIX = "-out"
# A VPN search's network of one exit, named after the search with this
# infix and the exit's position in the search (exit_net_name), `--internal`,
# labelled with LANE_LABEL: that exit's sidecars, its gateway and the agent
# are on it.
EXIT_NET_INFIX = "-x-"
# A VPN search's per-exit pieces (its sidecars, its networks and its tool
# servers' names) carry the exit's POSITION in that search (1, 2, …, fixed at
# its start), never the exit's name: the agent reaches a sidecar by its
# container name, a DNS label of at most 63 characters, and the server's
# name is inside every tool name, so an exit's name, which has no length
# bound, is kept out of both. The search keeps which exit each position is
# (AgentStream.set_exits). LANE_LABEL carries the position, EXIT_LABEL the
# exit's name.
LANE_LABEL = "research.search.lane"
LANE_RE = re.compile(r"^[1-9][0-9]*$")
# The one-shot helper that sets a VPN search's sidecar's route, named after
# the search with this suffix (one at a time) and labelled like its things.
ROUTE_SUFFIX = "-route"
# WireGuard drops a session this many seconds after its handshake
# (REJECT_AFTER_TIME, the protocol's own constant): a tunnel whose last
# handshake is older carries nothing until it shakes hands again.
VPN_SESSION_MAX_AGE_S = 180
# How long a check of the tunnel waits for a renewed handshake. WireGuard
# sends its handshake at once and repeats it after 5 seconds when unanswered
# (REKEY_TIMEOUT); a handshake is one round trip to the VPN server (well under
# a second). Six seconds covers the first try and one repeat; a VPN search
# with a dead tunnel fails after this long. The page's status never waits it:
# the check runs in the background and the page reads its last result.
VPN_HANDSHAKE_WAIT_S = 6
# Why a VPN search does not run (its outcome's reason starts so).
VPN_NOT_UP = "no VPN exit's tunnel is up: "
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

# --- the browser sidecars -----------------------------------------------------------

# How long a search waits for its sidecars' tool servers to answer before its
# agent starts. Measured: Playwright's tool server answers about a second
# after its container starts (Chromium starts later, at the first page; the
# stealth sidecar starts camoufox's browser at its first connection, so its
# tool server comes up as fast). Thirty times that leaves room for a host
# starting several searches at once (each start is a Node process loading the
# tool server); past it the sidecar is taken as not coming, its tier is
# reported unavailable and the search runs without it. Much shorter fails a
# healthy sidecar on a loaded host; much longer makes every search whose
# sidecar is broken wait that long before it starts.
SIDECAR_READY_S = 30
# Between two checks of a sidecar that has not answered yet: a check is one
# local HTTP request and one docker inspect (milliseconds); a quarter second
# adds at most that to a search's start.
SIDECAR_POLL_S = 0.25
# One readiness request's own bound: a tool server that is up answers its
# initialize in milliseconds; one that accepted the connection but does not
# answer is retried at the next poll until SIDECAR_READY_S.
SIDECAR_PROBE_S = 2
# How much of a readiness answer is read (the initialize reply is about a
# KiB; the sidecar will render hostile pages later, so nothing it sends is
# read unbounded).
SIDECAR_PROBE_MAX_BYTES = 64 * 1024
# The browser tiers' tool calls a search keeps one by one (for the work view
# and the outcome's failures; every call is counted either way). A call is at
# least one model turn, which takes seconds (about two for the shortest), so
# SEARCH_MAX_S holds under 2000 of them; past this a search's further calls
# are counted but not kept. One kept call is at most about a KiB (the trace's
# caps), so a search keeps at most about 2 MiB of them.
BROWSER_CALLS_MAX = 2000
# The first line of an error the browser tier's tool returned, as a work
# entry's detail (the trace's cap for a detail).
BROWSER_DETAIL_MAX_CHARS = TRACE_TARGET_MAX_CHARS
# A tool_result in the agent's stream that is longer than STREAM_LINE_MAX_BYTES
# is skipped unread; its calls' ids are taken from its first piece (the
# result's id comes before its content). A tool's error message is a few
# hundred bytes, so a result of that length is a page's content: its calls
# are recorded as ok.
_TOOL_USE_ID_RE = re.compile(rb'"tool_use_id"\s*:\s*"([A-Za-z0-9_-]{1,100})"')
NO_RESULT = "the search ended before the call returned"
# Why a file in the files folder is not offered (FILE_NAME_RE refuses its
# name; the browser saves a download under the page's own name, made safe
# for a path but not always this rule's: an accented letter, a leading dash).
DROPPED_WHY = ("its name is not one the service offers for download (letters, digits, "
               "dots, dashes and underscores, starting with a letter or a digit), so "
               "it is not listed; it is removed with the search")

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
# How much of one of the agent's texts the view shows, from its end: all of
# what the service keeps of it (AGENT_TEXT_MAX_CHARS), a paragraph or two of
# the box, so a check reads the agent's reasoning and not only its last
# sentence (the maintainer asked for more than the first 300). A longer text
# would need the service to keep more of it per message.
VIEW_TEXT_TAIL_CHARS = AGENT_TEXT_MAX_CHARS
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

# --- the operator's asks and their conversations ------------------------------------

# The identity an ask from the Search page is recorded under. A project name
# starts with a letter or a digit (PROJECT_RE, the gateway's caller header is
# checked against it), so no caller of the MCP server can ever be this
# project: an ask is never found, checked, downloaded from or collected by an
# agent.
OPERATOR_PROJECT = "-operator-"
OPERATOR_CONSUMER = "webui"
# Where the conversations are kept: a named volume of the service container
# (search.CONVERSATIONS_VOLUME at search.SERVICE_CONVERSATIONS_DIR — MIRROR),
# kept at Turn off like its inner Docker's. Only this service writes there,
# and nothing of a search reaches it.
CONVERSATIONS_DIR = "/var/lib/rs-search-conversations"
CONVERSATIONS_NAME = "conversations.json"
CONVERSATIONS_VERSION = 1
# Every conversation is kept until the operator deletes it or clears them
# all: no count and no size is capped, here or when the file is read (the
# file is the operator's own questions and their answers, written only by
# this service).
# One ask's request body (`admin ask`'s stdin): the question of at most
# QUESTION_MAX_CHARS characters as JSON, where one character can take 12 bytes
# (a character outside the BMP written as two \u escapes), plus the
# conversation id and the include flag (well under a KiB).
ASK_BODY_MAX_BYTES = QUESTION_MAX_CHARS * 12 + 1024
NO_SUCH_CONVERSATION = "no conversation with that id is kept"
TOO_LONG_TO_INCLUDE = ("this conversation is too long to include; start a new one or "
                       "untick the box")
CONVERSATIONS_DAMAGED = ("the conversations kept by the search service cannot be read "
                         "({why}), so none is shown and no question is asked until they "
                         "are cleared; Clear conversations removes the file")
ASK_LOST = "the search service stopped before this question was answered"
ASK_NOT_ENDED = "it had not ended when this question was asked"

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
    the host writes only valid ones). `vpn`: whether the VPN version is
    ticked (an older host sends none: off); `exits_per_site`: how many exits
    a VPN search tries on one site (an older host sends none: the default)."""
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
        t = s.get("tiers")
        if t is None:
            s["tiers"] = {n: True for n in TIER_NAMES}      # an older host: both on
        elif not (isinstance(t, dict) and set(t) == set(TIER_NAMES)
                  and all(isinstance(t[n], bool) for n in TIER_NAMES)):
            return None
        if not isinstance(data.setdefault("vpn", False), bool):
            return None
        n = data.get("exits_per_site")
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            data["exits_per_site"] = DEFAULT_EXITS_PER_SITE  # missing (an older host) or bad
        return data
    except (OSError, ValueError, RecursionError, KeyError, TypeError):
        return None


def vpn_on() -> bool:
    """Whether VPN searches can be asked: the VPN version ticked and at least
    one exit delivered (whether a tunnel is up is checked when a search
    starts)."""
    settings = read_settings()
    return bool(settings and settings["vpn"]) and bool(read_vpn())


VPN_OFF = ("the VPN version of the search service is off: the operator turns it on and "
           "adds a VPN exit on the Search page")


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


# --- the VPN's gateways and the VPN's SearXNG ------------------------------------------

# Held while the gateways are brought in line, and for a moment by every look
# at one: never across a wait for a handshake.
_vpn_lock = threading.Lock()
# Why the delivered exits could not be used at all (None when they could).
_vpn_error: str | None = None
_WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")
# A port and a keepalive interval are 16-bit numbers in WireGuard.
_U16_MAX = 65535


class Tunnel:
    """One exit's tunnel as the service follows it: why its gateway's last
    setup failed, what was last applied to it, and its checks (status.sh's
    renew-and-wait). One check of it runs at a time (it holds run_lock for its
    whole wait); `lock` guards the fields below it and is held only for a
    moment, so a read never waits on a check. Each exit's checks are its own:
    another exit's change or check never touches them."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.error: str | None = None
        # A digest of the settings last applied to the gateway (in memory
        # only; None when none were): a change of another exit leaves an
        # unchanged one as it is.
        self.applied: str | None = None
        self.run_lock = threading.Lock()
        self.lock = threading.Lock()
        # The last finished check since the configuration was last applied
        # ({tunnel, handshake_s, error}, None when none finished) and when it
        # finished (time.monotonic()).
        self.result: dict | None = None
        self.at = 0.0
        # Bumped at every setup of the gateway: a check started before it
        # records nothing (it looked at a tunnel that is gone).
        self.gen = 0
        # Checks running or waiting to run, and whether the background one is out.
        self.running = 0
        self.background = False

    def forget(self) -> None:
        """The gateway was set up again (or removed): no check has finished
        on it, and a check that runs now records nothing."""
        with self.lock:
            self.result = None
            self.gen += 1


# The exits' tunnels by name (under _vpn_lock): one per exit delivered.
TUNNELS: dict[str, Tunnel] = {}


def _u16(v, low: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and low <= v <= _U16_MAX


def _exit_ok(d) -> bool:
    """One delivered exit as the host writes it (search.vpn_service_record)."""
    return (isinstance(d, dict)
            and exit_name_ok(d["name"])
            and all(isinstance(d[k], str) and _WG_KEY_RE.match(d[k])
                    for k in ("private_key", "public_key"))
            and (d["preshared_key"] is None
                 or (isinstance(d["preshared_key"], str)
                     and _WG_KEY_RE.match(d["preshared_key"]) is not None))
            and isinstance(d["addresses"], list) and bool(d["addresses"])
            and all(isinstance(a, str) and ipaddress.ip_interface(a).version == 4
                    and "/" in a for a in d["addresses"])
            and isinstance(d["dns"], list) and bool(d["dns"])
            and all(isinstance(x, str) and ipaddress.ip_address(x).version == 4
                    for x in d["dns"])
            and isinstance(d["allowed_ips"], list) and bool(d["allowed_ips"])
            and all(isinstance(n, str) and ipaddress.ip_network(n).version == 4
                    for n in d["allowed_ips"])
            and isinstance(d["endpoint_ip"], str)
            and ipaddress.ip_address(d["endpoint_ip"]).version == 4
            and _u16(d["endpoint_port"], 1)
            and (d["listen_port"] is None or _u16(d["listen_port"], 1))
            and (d["keepalive"] is None or _u16(d["keepalive"], 1))
            and (d["mtu"] is None or (isinstance(d["mtu"], int)
                                      and not isinstance(d["mtu"], bool) and d["mtu"] >= 1)))


def read_vpn() -> list | None:
    """The exits the host delivered, the first one first, or None (none
    delivered, or not the shape the host writes: search.vpn_service_record,
    the exits record with at least one exit, each name once)."""
    try:
        with open(VPN_PATH, "rb") as f:
            d = json.loads(f.read(DELIVERY_MAX_BYTES + 1))
        if not (isinstance(d, dict) and d.get("version") == 2
                and isinstance(d.get("exits"), list) and d["exits"]
                and all(_exit_ok(e) for e in d["exits"])):
            return None
        names = [e["name"] for e in d["exits"]]
        return d["exits"] if len(set(names)) == len(names) else None
    except (OSError, ValueError, RecursionError, KeyError, TypeError):
        return None


def exit_names() -> list[str]:
    """The delivered exits' names, in order ([] when none)."""
    return [e["name"] for e in read_vpn() or []]


def exit_name_ok(name) -> bool:
    """An exit's name as the host allows it: text, not empty, on one line
    (search.exit_name_problem — MIRROR)."""
    return (isinstance(name, str) and bool(name)
            and not any(unicodedata.category(c) in _EXIT_NAME_REFUSED for c in name))


def exit_fingerprint(name: str) -> str:
    """The exit as this service's names and command lines carry it: the
    SHA-256 of its name's UTF-8 bytes, in hex (search.exit_fingerprint —
    MIRROR)."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def gateway_name(exit_name: str) -> str:
    return VPN_GATEWAY_PREFIX + exit_fingerprint(exit_name)


def is_gateway(cname: str) -> bool:
    """Whether a container's name is one of an exit's gateways (named by its
    fingerprint, or after the exit's name by an earlier run), or the release
    before exits' one gateway."""
    rest = cname[len(VPN_GATEWAY_PREFIX):] if cname.startswith(VPN_GATEWAY_PREFIX) else None
    return cname == V1_GATEWAY or (rest is not None and (
        FINGERPRINT_RE.match(rest) is not None or OLD_EXIT_NAME_RE.match(rest) is not None))


def vpn_stdin(cfg: dict) -> bytes:
    """What a gateway's up.sh reads on its stdin: the header (addresses,
    resolvers, MTU, the server's address), then the tunnel's settings in
    WireGuard's format. Built only from values read_vpn checked."""
    lines = [f"ADDRESS={a}" for a in cfg["addresses"]]
    lines += [f"DNS={d}" for d in cfg["dns"]]
    if cfg["mtu"] is not None:
        lines.append(f"MTU={cfg['mtu']}")
    lines.append(f"ENDPOINT={cfg['endpoint_ip']}:{cfg['endpoint_port']}")
    lines += ["---", "[Interface]", f"PrivateKey = {cfg['private_key']}"]
    if cfg["listen_port"] is not None:
        lines.append(f"ListenPort = {cfg['listen_port']}")
    lines += ["[Peer]", f"PublicKey = {cfg['public_key']}"]
    if cfg["preshared_key"] is not None:
        lines.append(f"PresharedKey = {cfg['preshared_key']}")
    lines += [f"Endpoint = {cfg['endpoint_ip']}:{cfg['endpoint_port']}",
              f"AllowedIPs = {', '.join(cfg['allowed_ips'])}"]
    if cfg["keepalive"] is not None:
        lines.append(f"PersistentKeepalive = {cfg['keepalive']}")
    return ("\n".join(lines) + "\n").encode()


def container_ip(name: str, network: str) -> str | None:
    """A container's IPv4 address on one of its networks, or None."""
    r = docker("container", "inspect", "-f",
               "{{(index .NetworkSettings.Networks \"" + network + "\").IPAddress}}", name)
    ip = r.stdout.decode("utf-8", "replace").strip()
    return ip if r.returncode == 0 and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) else None


def route_through_gateway(target: str, gateway_ip: str, labels: list[str],
                          helper_name: str, closed_ip: str | None = None) -> str | None:
    """Set a container's default route through a VPN gateway, from a
    one-shot helper in its network namespace (the target keeps no
    capability; the helper has NET_ADMIN and nothing else, and is gone when
    it is done). `closed_ip`: the target's address on a network it only calls
    out on, where no new connection is let in. None, or why it failed."""
    args = [gateway_ip] + ([closed_ip] if closed_ip else [])
    r = docker("run", "--rm", "--name", helper_name, *labels,
               "--network", f"container:{target}", "--restart", "no",
               "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
               "--security-opt", "no-new-privileges",
               "--entrypoint", "/bin/sh", VPN_IMAGE, f"{VPN_SCRIPTS}/route.sh", *args)
    if r.returncode != 0:
        docker("rm", "-f", helper_name)
        return first_line(r)
    return None


def _vpn_network(name: str, internal: bool) -> str | None:
    if docker("network", "inspect", name).returncode == 0:
        return None
    r = docker("network", "create", *(["--internal"] if internal else []),
               "--label", f"{VPN_LABEL}=1", name)
    return None if r.returncode == 0 else f"the VPN's network could not be created ({first_line(r)})"


def gateways_present() -> tuple[list[str], list[str]]:
    """The exits' gateway containers there are (by their label): ([the exit
    name of each named as its exit's gateway], [the container name of each an
    earlier run named after its exit's name, OLD_EXIT_NAME_RE]). One labelled
    but named neither way is left alone (and logged). The name is the rest of
    the line as it is (it may begin or end with a space; it holds no line
    break)."""
    r = docker("ps", "-a", "--filter", f"label={VPN_LABEL}", "--filter", f"label={EXIT_LABEL}",
               "--format", "{{.Names}} {{.Label \"" + EXIT_LABEL + "\"}}")
    out: tuple[list[str], list[str]] = ([], [])
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        cname, _, name = line.partition(" ")
        if exit_name_ok(name) and cname == gateway_name(name):
            out[0].append(name)
        elif OLD_EXIT_NAME_RE.match(name) and cname == VPN_GATEWAY_PREFIX + name:
            out[1].append(cname)
        elif cname:
            log(f"vpn: {cname} carries an exit's label but is not named as its gateway; "
                "left alone")
    return out


def remove_old_gateways(cnames: list[str]) -> None:
    """The gateways an earlier run named after their exits' names, by name."""
    for cname in cnames:
        docker("rm", "-f", cname)
        log(f"vpn: the gateway {cname} of an earlier run removed")


def remove_gateway(name: str) -> None:
    """One exit's gateway, by its name, and its tunnel's record."""
    if container_state(gateway_name(name))[0] != "absent":
        docker("rm", "-f", gateway_name(name))
    t = TUNNELS.pop(name, None)
    if t is not None:
        t.forget()


def remove_vpn() -> None:
    """Every exit's gateway, the VPN's SearXNG and their two networks, by
    name (the release before exits' one gateway too)."""
    for c in (SEARXNG_VPN_CONTAINER, V1_GATEWAY):
        if container_state(c)[0] != "absent":
            docker("rm", "-f", c)
    present, old = gateways_present()
    remove_old_gateways(old)
    for name in present:
        remove_gateway(name)
    for t in list(TUNNELS.values()):
        t.forget()
    TUNNELS.clear()
    for net in (SEARXNG_VPN_NETWORK, VPN_WAN_NETWORK):
        if docker("network", "inspect", net).returncode == 0:
            docker("network", "rm", net)


def _image_id(image: str) -> str:
    r = docker("image", "inspect", "-f", "{{.Id}}", image)
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""


def ensure_vpn(renew: str | None = None) -> str | None:
    """Bring the gateways in line with the delivered exits: none delivered,
    every gateway and the VPN's SearXNG go; else the gateway of an exit no
    longer delivered goes (by name), each delivered exit's gateway runs
    (re-created when its image changed) and is set up again when its
    configuration changed, its gateway is new or it is `renew` (the
    fingerprint of the exit the operator just saved, exit_fingerprint; every
    other unchanged exit is left as it is), then the VPN's SearXNG runs on
    the first exit's route, and a check of each tunnel set up again starts in
    the background. None, or why the exits could not be used at all (each
    exit's own failure is its status)."""
    global _vpn_error
    with _vpn_lock:
        exits = read_vpn()
        if exits is None:
            remove_vpn()
            _vpn_error = None
            if os.path.exists(VPN_PATH):
                _vpn_error = "the VPN exits the search service was handed are not usable"
            return _vpn_error
        _vpn_error = _ensure_vpn(exits, renew)
        if _vpn_error:
            log(f"vpn: {_vpn_error}")
        checks = [TUNNELS[e["name"]] for e in exits
                  if e["name"] in TUNNELS and TUNNELS[e["name"]].error is None]
    for t in checks:
        with t.lock:
            fresh = t.result is None
        if fresh:
            start_check(t)
    return _vpn_error


def _ensure_vpn(exits: list, renew: str | None) -> str | None:
    """Under _vpn_lock. None, or why no exit could be set up."""
    names = [e["name"] for e in exits]
    present, old = gateways_present()
    remove_old_gateways(old)
    for name in present:
        if name not in names:
            remove_gateway(name)
            log(f"vpn: the gateway of exit {name} removed")
    for name in list(TUNNELS):
        if name not in names:
            TUNNELS.pop(name).forget()
    for name in names:
        TUNNELS.setdefault(name, Tunnel(name))
    image_id = _image_id(VPN_IMAGE)
    problem = None if image_id else "the VPN gateway's image is not staged in the search service"
    for net, internal in ((VPN_WAN_NETWORK, False), (SEARXNG_VPN_NETWORK, True)):
        problem = problem or _vpn_network(net, internal)
    if problem:
        for name in names:
            TUNNELS[name].error = problem
            TUNNELS[name].applied = None
            TUNNELS[name].forget()
        return problem
    first_fresh = False
    for i, e in enumerate(exits):
        t = TUNNELS[e["name"]]
        fresh, t.error = _ensure_gateway(e, image_id, t, exit_fingerprint(e["name"]) == renew)
        if i == 0:
            first_fresh = fresh
        if t.error:
            log(f"vpn: exit {e['name']}: {t.error}")
    problem = ensure_searxng_vpn(exits[0], first_fresh)
    if problem:
        log(f"vpn: {problem}")
    return None


def _ensure_gateway(cfg: dict, image_id: str, t: Tunnel,
                    renew: bool) -> tuple[bool, str | None]:
    """One exit's gateway running and set up with its configuration (again
    only when it changed, the gateway is new or `renew`). (whether the
    gateway container is new, None or why it is not set up)."""
    cname = gateway_name(cfg["name"])
    state, labels = container_state(cname)
    fresh = False
    if state != "absent" and (state != "running" or labels.get(VPN_LABEL) != image_id
                              or labels.get(EXIT_LABEL) != cfg["name"]):
        docker("rm", "-f", cname)
        state = "absent"
    if state == "absent":
        # NET_ADMIN only (the tunnel, its routes and its firewall), IPv4
        # forwarding on, a read-only root and a tmpfs for the settings.
        r = docker("run", "-d", "--name", cname, "--label", f"{VPN_LABEL}={image_id}",
                   "--label", f"{EXIT_LABEL}={cfg['name']}",
                   "--network", VPN_WAN_NETWORK, "--restart", "no",
                   "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
                   "--security-opt", "no-new-privileges",
                   "--sysctl", "net.ipv4.ip_forward=1",
                   "--read-only", "--tmpfs", "/etc/wireguard:mode=0700",
                   VPN_IMAGE)
        if r.returncode != 0:
            t.applied = None
            t.forget()
            return False, f"the exit's VPN gateway could not be started ({first_line(r)})"
        fresh = True
        log(f"vpn gateway of exit {cfg['name']} started")
    stdin = vpn_stdin(cfg)
    digest = hashlib.sha256(stdin).hexdigest()
    if not fresh and not renew and t.error is None and t.applied == digest:
        return False, None
    r = docker("exec", "-i", cname, "/bin/sh", f"{VPN_SCRIPTS}/up.sh", input=stdin)
    t.forget()
    if r.returncode != 0:
        t.applied = None
        return fresh, f"the exit's VPN gateway could not set the tunnel up ({first_line(r)})"
    t.applied = digest
    log(f"vpn gateway of exit {cfg['name']} set up")
    return fresh, None


def ensure_searxng_vpn(first: dict, gateway_fresh: bool) -> str | None:
    """The VPN's SearXNG, on its route through the first exit's gateway and
    resolving through that tunnel: the other gateways leave its network;
    (re)created when it is not running, when its exit or resolvers changed or
    when that gateway is new or joined its network now; then joined again
    to the SearXNG network of every VPN search still running. None, or why
    not."""
    gw_name = gateway_name(first["name"])
    members = network_members(SEARXNG_VPN_NETWORK) or []
    for m in members:
        if is_gateway(m) and m != gw_name:
            docker("network", "disconnect", "-f", SEARXNG_VPN_NETWORK, m)
    joined = False
    if gw_name not in members:
        r = docker("network", "connect", SEARXNG_VPN_NETWORK, gw_name)
        if r.returncode != 0:
            return f"the first exit's gateway could not join the VPN's SearXNG's network ({first_line(r)})"
        joined = True
    want = f"{SEARXNG_IMAGE}|{','.join(first['dns'])}|{first['name']}"
    state, labels = container_state(SEARXNG_VPN_CONTAINER)
    if state == "running" and labels.get(VPN_LABEL) == want and not gateway_fresh \
            and not joined:
        return None
    if state != "absent":
        docker("rm", "-f", SEARXNG_VPN_CONTAINER)
    if not image_present(SEARXNG_IMAGE):
        return "the SearXNG image is not staged in the search service"
    gw = container_ip(gw_name, SEARXNG_VPN_NETWORK)
    if gw is None:
        return "the first exit's gateway has no address on the VPN's SearXNG's network"
    os.makedirs(SEARXNG_VPN_DIR, mode=0o755, exist_ok=True)
    tmp = os.path.join(SEARXNG_VPN_DIR, f".settings.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(searxng_settings())
    os.replace(tmp, os.path.join(SEARXNG_VPN_DIR, "settings.yml"))
    dns = [a for d in first["dns"] for a in ("--dns", d)]
    # Hardened as the other SearXNG; created on its internal network (no way
    # out until the helper routes it through the gateway), its names resolved
    # through the tunnel.
    r = docker("create", "--name", SEARXNG_VPN_CONTAINER, "--label", f"{VPN_LABEL}={want}",
               "--network", SEARXNG_VPN_NETWORK, "--restart", "no", *dns,
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "-v", f"{SEARXNG_VPN_DIR}:/etc/searxng:ro",
               "--tmpfs", "/var/cache/searxng", SEARXNG_IMAGE)
    if r.returncode == 0:
        r = docker("start", SEARXNG_VPN_CONTAINER)
    if r.returncode != 0:
        docker("rm", "-f", SEARXNG_VPN_CONTAINER)
        return f"the VPN's SearXNG could not be started ({first_line(r)})"
    problem = route_through_gateway(SEARXNG_VPN_CONTAINER, gw, ["--label", f"{VPN_LABEL}=1"],
                                    SEARXNG_VPN_CONTAINER + ROUTE_SUFFIX)
    if problem:
        docker("rm", "-f", SEARXNG_VPN_CONTAINER)
        return f"the VPN's SearXNG could not be routed through the gateway ({problem})"
    for net in SEARCHES.vpn_searxng_networks():
        docker("network", "connect", "--alias", SEARXNG_ALIAS, net, SEARXNG_VPN_CONTAINER)
    log(f"searxng (vpn) started, through exit {first['name']}")
    return None


def _vpn_look(name: str) -> tuple[dict | None, dict | None, str | None]:
    """(cfg, off-or-error reply, peek) of one exit: its delivered
    configuration; the reply when its tunnel cannot be checked (not
    delivered, its gateway not running, its setup failed), else None; and
    the tunnel's state from its current handshake alone (status.sh peek:
    nothing renewed or awaited), its age or None when it is not fresh or could
    not be read."""
    with _vpn_lock:
        cfg = next((e for e in read_vpn() or [] if e["name"] == name), None)
        t = TUNNELS.get(name)
        if cfg is None:
            return None, {"tunnel": "off", "handshake_s": None, "checking": False,
                          "error": "this exit is not stored (the operator adds exits "
                                   "under Search → Settings → VPN)"}, None
        if container_state(gateway_name(name))[0] != "running":
            return cfg, {"tunnel": "off", "handshake_s": None, "checking": False,
                         "error": (t.error if t else None) or _vpn_error
                         or "the exit's VPN gateway is not running"}, None
        if t is None or t.error:
            return cfg, {"tunnel": "down", "handshake_s": None, "checking": False,
                         "error": (t.error if t else None)
                         or "the exit's VPN gateway is not set up yet"}, None
        r = docker("exec", gateway_name(name), "/bin/sh", f"{VPN_SCRIPTS}/status.sh",
                   "peek", str(VPN_SESSION_MAX_AGE_S))
    words = r.stdout.decode("utf-8", "replace").split() if r.returncode == 0 else []
    up = len(words) == 2 and words[0] == "up" and words[1].isdigit()
    return cfg, None, (int(words[1]) if up else None)


def _tunnel_check(cfg: dict) -> dict:
    """One check of one exit: status.sh renews a handshake older than
    VPN_SESSION_MAX_AGE_S and awaits it up to VPN_HANDSHAKE_WAIT_S.
    {tunnel: up | down, handshake_s, error}."""
    r = docker("exec", gateway_name(cfg["name"]), "/bin/sh", f"{VPN_SCRIPTS}/status.sh",
               str(VPN_HANDSHAKE_WAIT_S), str(VPN_SESSION_MAX_AGE_S))
    words = r.stdout.decode("utf-8", "replace").split() if r.returncode == 0 else []
    server = f"{cfg['endpoint_ip']}:{cfg['endpoint_port']}"
    if len(words) == 2 and words[0] == "up" and words[1].isdigit():
        return {"tunnel": "up", "handshake_s": int(words[1]), "error": None}
    if words == ["down", "none"]:
        return {"tunnel": "down", "handshake_s": None,
                "error": f"the VPN server at {server} did not answer the tunnel's handshake "
                         f"within {VPN_HANDSHAKE_WAIT_S} seconds (check the configuration's "
                         "keys and endpoint, that the VPN account is active, and that the "
                         "network this host is on lets UDP to port "
                         f"{cfg['endpoint_port']} out)"}
    if len(words) == 3 and words[:2] == ["down", "stale"] and words[2].isdigit():
        return {"tunnel": "down", "handshake_s": int(words[2]),
                "error": f"the VPN server at {server} has not answered for {words[2]} seconds"}
    if words == ["down", "nolink"]:
        return {"tunnel": "down", "handshake_s": None, "error": "the tunnel is not set up"}
    return {"tunnel": "down", "handshake_s": None,
            "error": f"the exit's VPN gateway's state could not be read ({first_line(r)})"}


def _run_check(t: Tunnel) -> dict:
    """Run one check of an exit, after any check of it already running (at
    most one runs at a time per exit), and record its result as the last
    finished one unless the gateway was set up again meanwhile. A check that
    finished while this one waited for its turn answers for it. The result."""
    asked = time.monotonic()
    with t.lock:
        gen = t.gen
        t.running += 1
    try:
        with t.run_lock:
            with t.lock:
                if gen == t.gen and t.result is not None and t.at >= asked:
                    return dict(t.result)
            cfg, off, _peek = _vpn_look(t.name)
            res = off if off is not None else _tunnel_check(cfg)
            res = {k: res[k] for k in ("tunnel", "handshake_s", "error")}
            with t.lock:
                if gen == t.gen:
                    t.result, t.at = dict(res), time.monotonic()
            return res
    finally:
        with t.lock:
            t.running -= 1


def _background_check(t: Tunnel) -> None:
    while True:
        with t.lock:
            gen = t.gen
        try:
            _run_check(t)
        except Exception as e:      # the thread ends; the next read starts another
            log(f"vpn: exit {t.name}: the tunnel's check failed ({type(e).__name__})")
        with t.lock:
            if gen == t.gen:
                t.background = False
                return
        # The gateway was set up again while it ran: check the new tunnel.


def start_check(t: Tunnel) -> None:
    """Start a check of an exit's tunnel in the background, unless one is out."""
    with t.lock:
        if t.background:
            return
        t.background = True
    threading.Thread(target=_background_check, args=(t,), daemon=True).start()


def exit_status(name: str) -> dict:
    """One exit's tunnel as the operator's page reads it, at once (never
    awaiting a handshake): {tunnel: up | down | off | checking, handshake_s,
    error, checking}. off when the exit was not delivered or its gateway does
    not run; up when its handshake is younger than VPN_SESSION_MAX_AGE_S;
    else the last finished check's result (checking: no check has finished
    since its configuration was last applied), and a check starts in the
    background unless one runs or the last one found the tunnel down less
    than VPN_SESSION_MAX_AGE_S ago (a result is trusted as long as a
    handshake would be). `checking` (the field): a check runs now."""
    _cfg, off, peek = _vpn_look(name)
    if off is not None:
        return off
    with _vpn_lock:
        t = TUNNELS.get(name)
    if t is None:
        return {"tunnel": "off", "handshake_s": None, "error": "the exit's VPN gateway is not "
                "set up", "checking": False}
    with t.lock:
        busy = t.running > 0 or t.background
        if peek is not None:
            return {"tunnel": "up", "handshake_s": peek, "error": None, "checking": busy}
        last = None if t.result is None else dict(t.result)
        trusted = (last is not None and last["tunnel"] == "down"
                   and time.monotonic() - t.at < VPN_SESSION_MAX_AGE_S)
    if not busy and not trusted:
        start_check(t)
        busy = True
    if last is None:
        return {"tunnel": "checking", "handshake_s": None, "error": None, "checking": busy}
    return dict(last, checking=busy)


def vpn_status() -> dict:
    """Every delivered exit's tunnel, in order: {"exits": [{name, tunnel,
    handshake_s, error, checking}, …]} (exit_status each, at once)."""
    return {"exits": [dict(exit_status(n), name=n) for n in exit_names()]}


def vpn_wait_up(name: str) -> dict:
    """One exit's tunnel as a VPN search's start needs it: a fresh handshake
    as it is, else a check (renewed and awaited up to VPN_HANDSHAKE_WAIT_S),
    whose result is recorded as the last finished one. {tunnel, handshake_s,
    error}."""
    _cfg, off, peek = _vpn_look(name)
    if off is not None:
        return off
    if peek is not None:
        return {"tunnel": "up", "handshake_s": peek, "error": None}
    with _vpn_lock:
        t = TUNNELS.get(name)
    if t is None:
        return {"tunnel": "down", "handshake_s": None,
                "error": "the exit's VPN gateway is not set up"}
    return _run_check(t)


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


# A normal search's browser tiers' tool servers: {server name: (tier, exit)}.
BROWSER_SERVERS = {t: (t, None) for t in TIER_NAMES}


def server_name(tier: str, lane: int | None) -> str:
    """A tier's tool server as the agent's tool list names it: the tier, and
    in a VPN search the tier and the exit's position in the search
    (`plain-<n>`, `browser-<n>`, `stealth-<n>`; LANE_LABEL)."""
    return tier if lane is None else f"{tier}-{lane}"


def _browser_call(name, args, servers: dict) -> tuple[str, str | None, str, str] | None:
    """(tier, exit, tool, target) of a tool_use of a browser tier's server
    (`servers`: {server name: (tier, exit)}), or None. The target is the
    call's URL argument when it has one, else a short text of its
    arguments."""
    if not isinstance(name, str) or not name.startswith("mcp__"):
        return None
    server, sep, tool = name[len("mcp__"):].partition("__")
    if not sep or server not in servers:
        return None
    tier, exit_name = servers[server]
    args = args if isinstance(args, dict) else {}
    url = args.get("url")
    if isinstance(url, str):
        target = url
    else:
        try:
            target = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
        except (ValueError, TypeError, RecursionError):
            target = ""
    return (tier, exit_name, tool[:TRACE_NAME_MAX_CHARS], target[:TRACE_TARGET_MAX_CHARS])


def _result_first_line(content) -> str:
    """The first non-empty line of a tool_result's content (text or text
    blocks), past the "### Error" heading Playwright's tool server puts
    first."""
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and isinstance(b.get("text"), str))
    if not isinstance(content, str):
        return ""
    for line in content.splitlines():
        if line.strip() and not line.startswith("### Error"):
            return line.strip()[:BROWSER_DETAIL_MAX_CHARS]
    return ""


class AgentStream:
    """What the operator sees of one search agent while it runs: the token
    count (the latest usage per message id, summed; the `result` event's
    totals once it arrives) and its text blocks, each with the time it was
    read (bounded: the newest VIEW_TAIL_ENTRIES, all the view shows).

    It also records the browser tiers' tool calls, which no RS code of the
    plain tier's kind traces: each `tool_use` of a browser tier's server
    (`servers`; in a VPN search one per tier and exit, and the entry names the
    exit) in an assistant event, completed by its `tool_result`'s error flag
    in a later user event, as a work entry of the trace's shape (kept up to
    BROWSER_CALLS_MAX; every call counted per tier, tool and outcome).
    `exits`: a VPN search's exits it ran on, in order (set at its start)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.servers: dict = dict(BROWSER_SERVERS)
        self.exits: tuple = ()
        self.per_message: dict[str, int] = {}
        self.final: int | None = None
        self.texts: collections.deque = collections.deque(maxlen=VIEW_TAIL_ENTRIES)
        self.dropped = 0
        self.pending: dict[str, tuple[str, str | None, str, str]] = {}
        self.seen: set = set()
        self.calls: list[dict] = []
        self.calls_dropped = 0
        self.counts: dict = {}

    def set_exits(self, servers: dict, exits: list) -> None:
        """A VPN search's start: its browser tiers' servers and its exits."""
        with self.lock:
            self.servers = dict(servers)
            self.exits = tuple(exits)

    def exit_names(self) -> tuple:
        with self.lock:
            return self.exits

    def _complete(self, uid: str, error: bool, detail: str, now: float) -> None:
        """Under the lock: a pending call's result arrived (or never will)."""
        tier, exit_name, tool, target = self.pending.pop(uid)
        outcome = "error" if error else "ok"
        t = self.counts.setdefault(tier, {}).setdefault(tool, {})
        t[outcome] = t.get(outcome, 0) + 1
        if len(self.calls) >= BROWSER_CALLS_MAX:
            self.calls_dropped += 1
            return
        call = {"ts": now, "kind": "tool", "tool": tool, "tier": tier, "target": target,
                "outcome": outcome, "detail": detail if error else ""}
        if exit_name is not None:
            call["exit"] = exit_name
        self.calls.append(call)

    def work_calls(self) -> tuple[list[dict], bool]:
        with self.lock:
            return [dict(c) for c in self.calls[-VIEW_TAIL_ENTRIES:]], (
                self.calls_dropped > 0 or len(self.calls) > VIEW_TAIL_ENTRIES)

    def end_calls(self) -> None:
        """At the search's end: a call whose result never came failed."""
        now = time.time()
        with self.lock:
            for uid in list(self.pending):
                self._complete(uid, True, NO_RESULT, now)

    def tried_into(self, tried: dict) -> None:
        """Add the browser tiers' calls to the outcome's `tried` (the trace
        summary's shape: per tier and tool the outcomes, and the failures)."""
        with self.lock:
            for tier, tools in self.counts.items():
                for tool, outcomes in tools.items():
                    t = tried["tiers"].setdefault(tier, {}).setdefault(tool, {})
                    for k, n in outcomes.items():
                        t[k] = t.get(k, 0) + n
            tried["failures"] += [{k: c[k] for k in ("tier", "exit", "tool", "target",
                                                     "outcome", "detail") if k in c}
                                  for c in self.calls if c["outcome"] != "ok"]

    def feed_oversized(self, head: bytes) -> None:
        """A line past STREAM_LINE_MAX_BYTES, skipped: the calls whose ids its
        first piece names returned a result that long, so they succeeded."""
        now = time.time()
        with self.lock:
            for m in _TOOL_USE_ID_RE.finditer(head):
                uid = m.group(1).decode()
                if uid in self.pending:
                    self._complete(uid, False, "", now)

    def _feed_results(self, msg: dict, now: float) -> None:
        """Under the lock: the tool_result blocks of a user event."""
        content = msg.get("content")
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result" \
                    and b.get("tool_use_id") in self.pending:
                error = b.get("is_error") is True
                self._complete(b["tool_use_id"], error,
                               _result_first_line(b.get("content")) if error else "", now)

    def _feed_uses(self, msg: dict) -> None:
        """Under the lock: the browser tiers' tool_use blocks of an assistant
        event (each id once; pending ones bounded like the kept calls)."""
        content = msg.get("content")
        for b in content if isinstance(content, list) else []:
            if not (isinstance(b, dict) and b.get("type") == "tool_use"
                    and isinstance(b.get("id"), str) and b["id"] not in self.seen):
                continue
            call = _browser_call(b.get("name"), b.get("input"), self.servers)
            if call is None or len(self.pending) >= BROWSER_CALLS_MAX:
                continue
            self.seen.add(b["id"])
            self.pending[b["id"]] = call

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
        if ev.get("type") == "user" and isinstance(ev.get("message"), dict):
            with self.lock:
                self._feed_results(ev["message"], now)
            return
        if ev.get("type") != "assistant" or not isinstance(ev.get("message"), dict):
            return
        msg = ev["message"]
        texts = [b["text"][:AGENT_TEXT_MAX_CHARS] for b in msg.get("content") or []
                 if isinstance(b, dict) and b.get("type") == "text"
                 and isinstance(b.get("text"), str) and b["text"].strip()]
        with self.lock:
            self._feed_uses(msg)
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
                    stream.feed_oversized(piece)
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
        # Each line also keeps its search's work view as it ended (the dict
        # view() returned at that moment), opened from the page after the search
        # was collected too; the list read never carries it. What one costs at
        # most: the question (QUESTION_MAX_CHARS, 32k characters), the reason
        # (the agent's words are AGENT_TEXT_MAX_CHARS of it, the rest is RS's
        # own sentence) and VIEW_TAIL_ENTRIES entries of at most 3 x
        # TRACE_NAME_MAX_CHARS + 2 x TRACE_TARGET_MAX_CHARS (a tool call) or
        # VIEW_TEXT_TAIL_CHARS (a text), about 20k characters for the ten: some
        # 55k characters, 220 KB in memory when every one is a 4-byte character.
        # RECENT_MAX of them stay under 23 MB at worst; real questions are a
        # paragraph, so a full list is a few MB at most.
        # A line whose search ended with a report also keeps that report in
        # full (its summary, sources and tiers as the agent wrote them, as
        # compact JSON bytes), opened from the page's Report button; the list
        # read carries only `has_report`. What it costs: at most about
        # REPORT_MAX_BYTES per kept search (the report was read with that cap),
        # so RECENT_MAX x that, about 100 MB, when every search left a report at
        # the cap; real reports are a few KB, a full list well under a MB.
        self.recent: collections.deque = collections.deque()
        # The operator's conversations (load_conversations at start), the one
        # asked in last first; each ask in it keeps what its message shows once
        # it ended (its work view as it ended and its full report). `conv_error`
        # is the damaged-file text while the file on disk cannot be read as RS
        # wrote it: the file is then left as it is, the reads answer with the
        # text and every ask is refused, until Clear conversations.
        self.conversations: list[dict] = []
        self.conv_error: str | None = None

    # --- the caller's three tools ---

    def submit(self, project: str, consumer: str, question: str, searcher: str,
               vpn: bool) -> dict:
        settings = self._submit_settings(question, searcher, vpn)
        sid = secrets.token_hex(8)
        with self.lock:
            self._enqueue(sid, project, consumer, question.strip(), question.strip(),
                          settings, vpn, None)
        s = settings["searcher"]
        return {"search_id": sid, "searcher": s["name"], "vpn": vpn, "state": "queued",
                "next": "go on with your work; search_check tells how it goes"}

    @staticmethod
    def _submit_settings(question, searcher: str, vpn: bool) -> dict:
        """The settings a new search runs with, after the checks every search
        passes (settings saved, the searcher named, a question of a size the
        agent's environment carries; a VPN search, the VPN version on).
        ToolRefusal otherwise."""
        settings = read_settings()
        if settings is None:
            raise ToolRefusal("the search service has no settings yet; the operator "
                              "saves the searcher on the Search page")
        s = settings["searcher"]
        if searcher and searcher != s["name"]:
            raise ToolRefusal(f"there is no searcher named {searcher}; the searchers "
                              f"are: {s['name']}")
        if vpn and not vpn_on():
            raise ToolRefusal(VPN_OFF)
        if not isinstance(question, str) or not question.strip():
            raise ToolRefusal("the question is empty")
        if len(question) > QUESTION_MAX_CHARS:
            raise ToolRefusal(f"the question is longer than {QUESTION_MAX_CHARS} "
                              "characters; put long material in a file the "
                              "question names instead")
        return settings

    def _enqueue(self, sid: str, project: str, consumer: str, question: str, prompt: str,
                 settings: dict, vpn: bool, conversation: str | None) -> None:
        """Under the lock: a new search in the table and at the end of the one
        queue, and its thread. `question` is what its rows show, `prompt` what
        its agent is asked (the same but for an ask that includes its
        conversation); `vpn` whether it is a VPN search; `conversation` is an
        ask's conversation id."""
        s = settings["searcher"]
        self.table[sid] = {
            "id": sid, "project": project, "consumer": consumer,
            "question": question, "prompt": prompt, "conversation": conversation,
            "searcher": s["name"], "model": s["model"], "effort": s["effort"],
            "tiers": dict(s["tiers"]), "vpn": vpn, "contact": settings["contact"],
            "exits_per_site": settings["exits_per_site"],
            "state": "queued", "submitted_at": time.time(),
            "started_at": None, "finished_at": None, "outcome": None,
            "stream": AgentStream(), "stop": False}
        self.queue.append(sid)
        self.lock.notify_all()
        threading.Thread(target=self._run, args=(sid,), daemon=True).start()
        log(f"submit {sid} for {project}/{consumer}")

    # --- the operator's asks (the Search page's Quick Search) ---

    def ask(self, question, conversation, include: bool, vpn: bool) -> dict:
        """The operator's question: a search like any other (the same queue,
        searcher and sign-in) under OPERATOR_PROJECT, a VPN search when `vpn`,
        added to a conversation (a new one when `conversation` is None). With `include`
        the searcher is asked the conversation's earlier questions and
        answers too; the rows show only what the operator typed. The
        conversation is saved before the search is queued: a save that fails
        starts nothing."""
        settings = self._submit_settings(question, "", vpn)
        question = question.strip()
        with self.lock:
            if self.conv_error:
                raise ToolRefusal(self.conv_error)
            conv = None
            if conversation is not None:
                conv = next((c for c in self.conversations if c["id"] == conversation), None)
                if conv is None:
                    raise ToolRefusal(NO_SUCH_CONVERSATION)
            prompt = question
            if include and conv is not None:
                prompt = self._compose(conv, question)
                if len(prompt) > QUESTION_MAX_CHARS:
                    raise ToolRefusal(TOO_LONG_TO_INCLUDE)
            sid = secrets.token_hex(8)
            now = time.time()
            entry = {"id": sid, "question": question, "searcher": settings["searcher"]["name"],
                     "vpn": vpn, "submitted_at": now, "view": None, "report": None}
            if conv is None:
                cid = secrets.token_hex(8)
                conv = {"id": cid, "created_at": now, "updated_at": now, "asks": [entry]}
                kept = [conv] + self.conversations
            else:
                cid = conv["id"]
                conv = dict(conv, updated_at=now, asks=conv["asks"] + [entry])
                kept = [conv] + [c for c in self.conversations if c["id"] != cid]
            problem = self._save_conversations(kept)
            if problem:
                raise ToolRefusal(problem)
            self.conversations = kept
            self._enqueue(sid, OPERATOR_PROJECT, OPERATOR_CONSUMER, question, prompt,
                          settings, vpn, cid)
        return {"conversation": cid, "ask": sid, "state": "queued"}

    @staticmethod
    def _compose(conv: dict, question: str) -> str:
        """What the searcher is asked when the conversation is included: every
        earlier question with its answer (the summary and the numbered
        sources, or why there is none), oldest first, then the new question."""
        lines = ["The earlier questions of this conversation and their answers, oldest "
                 "first:", ""]
        for n, a in enumerate(conv["asks"], 1):
            lines.append(f"Question {n}: {a['question']}")
            rep, view = a["report"], a["view"]
            if rep is not None:
                lines.append(f"Answer {n}: {_as_text(rep.get('summary'))}")
                sources = rep.get("sources")
                items = sources if isinstance(sources, list) else [sources]
                if items:
                    lines.append(f"Sources of answer {n}:")
                for i, s in enumerate(items, 1):
                    num = s.get("n") if isinstance(s, dict) else None
                    num = num if isinstance(num, int) and not isinstance(num, bool) else i
                    lines.append(f"[{num}] {_source_line(s)}")
            elif view is not None:
                lines.append(f"Answer {n}: (no answer: {view['reason']})")
            else:
                lines.append(f"Answer {n}: (no answer: {ASK_NOT_ENDED})")
            lines.append("")
        lines.append(f"The new question: {question}")
        return "\n".join(lines)

    def _ask_state(self, a: dict) -> str:
        """Under the lock: an ask's state (live while it is in the table)."""
        if a["view"] is not None:
            return a["view"]["state"]
        rec = self.table.get(a["id"])
        return rec["state"] if rec is not None else "failed"

    def conversations_list(self) -> dict:
        with self.lock:
            if self.conv_error:
                return {"conversations": [], "error": self.conv_error}
            return {"conversations": [
                {"id": c["id"], "title": c["asks"][0]["question"][:LIST_QUESTION_MAX_CHARS],
                 "updated_at": c["updated_at"], "asks": len(c["asks"]),
                 "last_state": self._ask_state(c["asks"][-1])}
                for c in self.conversations], "error": None}

    def conversation(self, cid: str) -> dict:
        """One conversation, its asks oldest first: an ended one as its entry
        keeps it, a waiting or running one live from the table (its place in
        the queue, its work so far). ToolRefusal when it is not kept (or the
        file is damaged)."""
        with self.lock:
            if self.conv_error:
                raise ToolRefusal(self.conv_error)
            conv = next((c for c in self.conversations if c["id"] == cid), None)
            if conv is None:
                raise ToolRefusal(NO_SUCH_CONVERSATION)
            queue = list(self.queue)
            live = {a["id"]: dict(self.table[a["id"]]) for a in conv["asks"]
                    if a["view"] is None and a["id"] in self.table}
        asks = []
        for a in conv["asks"]:
            row = {"id": a["id"], "question": a["question"], "searcher": a["searcher"],
                   "vpn": a["vpn"], "ahead": None, "reason": None, "summary": None, "sources": [], "tiers": []}
            rec = live.get(a["id"])
            if a["view"] is not None:
                v, rep = a["view"], a["report"]
                row.update(state=v["state"], waited_s=v["waited_s"], ran_s=v["ran_s"],
                           tokens=v["tokens"], reason=v["reason"], work=v["work"])
                if rep is not None:
                    # The summary as text, so null means "no report".
                    row.update(summary=_as_text(rep.get("summary")),
                               sources=rep.get("sources"), tiers=rep.get("tiers"))
            elif rec is not None:
                tools = (self._trace_tools(a["id"], rec["stream"].exit_names())
                         if rec["state"] != "queued" else [])
                v = self._build_view(rec, tools)
                row.update(state=v["state"], waited_s=v["waited_s"], ran_s=v["ran_s"],
                           tokens=v["tokens"], reason=v["reason"], work=v["work"])
                if rec["state"] == "queued":
                    row["ahead"] = queue.index(a["id"]) if a["id"] in queue else 0
            else:
                row.update(state="failed", waited_s=0, ran_s=0, tokens=0, reason=ASK_LOST,
                           work=[])
            asks.append(row)
        return {"id": cid, "title": conv["asks"][0]["question"][:LIST_QUESTION_MAX_CHARS],
                "asks": asks}

    def conversations_clear(self) -> dict:
        """Clear conversations: the file goes, by its name (a damaged one
        too), and the store is empty. An ask still running keeps running and
        is collected at its end as any ask is; it is no longer in any
        conversation."""
        with self.lock:
            n = len(self.conversations)
            try:
                os.unlink(os.path.join(CONVERSATIONS_DIR, CONVERSATIONS_NAME))
            except FileNotFoundError:
                pass
            except OSError as e:
                raise ToolRefusal("the conversations could not be removed "
                                  f"({e.strerror or 'remove failed'})")
            self.conversations = []
            self.conv_error = None
        log(f"conversations cleared ({n})")
        return {"cleared": n}

    def conversation_delete(self, cid: str) -> dict:
        """Delete one conversation: it goes from the store, which is saved
        (the file is written again; nothing is removed). An ask of it still
        running keeps running and is collected at its end as any ask is; it
        is no longer in any conversation. ToolRefusal when it is not kept, the
        file is damaged, or the save fails (the store is then unchanged)."""
        with self.lock:
            if self.conv_error:
                raise ToolRefusal(self.conv_error)
            kept = [c for c in self.conversations if c["id"] != cid]
            if len(kept) == len(self.conversations):
                raise ToolRefusal(NO_SUCH_CONVERSATION)
            problem = self._save_conversations(kept)
            if problem:
                raise ToolRefusal(problem)
            self.conversations = kept
        log(f"conversation {cid} deleted")
        return {"deleted": cid}

    def _find_ask(self, sid: str) -> dict | None:
        """Under the lock: an ended ask's entry in the conversations."""
        for c in self.conversations:
            for a in c["asks"]:
                if a["id"] == sid and a["view"] is not None:
                    return a
        return None

    def _ask_ended(self, rec: dict, line: dict) -> None:
        """Under the lock, once per ask, when it ends (any way), after its
        recent line: what its message shows from now on goes into its
        conversation entry, which is saved (a failed save is logged; the
        store keeps it, and the next save writes it)."""
        sid, cid = rec["id"], rec["conversation"]
        report = ({k: rec["outcome"][k] for k in ("summary", "sources", "tiers")}
                  if "summary" in rec["outcome"] else None)
        kept = []
        found = False
        for c in self.conversations:
            if c["id"] == cid:
                found = True
                c = dict(c, asks=[dict(a, view=line["view"], report=report) if a["id"] == sid
                                  else a for a in c["asks"]])
            kept.append(c)
        if not found:
            return                          # deleted or cleared meanwhile
        self.conversations = kept
        problem = self._save_conversations(kept)
        if problem:
            log(f"ask {sid}: {problem}")

    def _collect_ask(self, sid: str) -> None:
        """Outside the lock, after _ask_ended: no agent collects an ask, so the
        service does, at once: its container, network and volume (its files)
        by name, and its row in the table."""
        problems = remove_search(sid)
        with self.lock:
            self.table.pop(sid, None)
        log(f"collected ask {sid}" + (f" ({'; '.join(problems)})" if problems else ""))

    # --- the conversations file ---

    def load_conversations(self) -> None:
        """At start: the conversations file, or the damaged-file state (the
        file is left untouched). An ask that had not ended when the service
        stopped ends now, as failed, and the file is saved."""
        convs, why = _read_conversations()
        with self.lock:
            if why is not None:
                self.conv_error = CONVERSATIONS_DAMAGED.format(why=why)
                log(f"conversations: {why}; kept as they are until cleared")
                return
            lost = False
            for c in convs:
                for a in c["asks"]:
                    # An ask saved before VPN searches existed was a normal one.
                    a.setdefault("vpn", False)
                    if a["view"] is not None:
                        a["view"].setdefault("vpn", a["vpn"])
                    if a["view"] is None:
                        lost = True
                        a["view"] = {"id": a["id"], "state": "failed",
                                     "question": a["question"], "searcher": a["searcher"],
                                     "vpn": a["vpn"],
                                     "tokens": 0, "waited_s": 0, "ran_s": 0,
                                     "reason": ASK_LOST, "work": [], "truncated": False}
            self.conversations = convs
            if lost:
                problem = self._save_conversations(convs)
                if problem:
                    log(f"conversations: {problem}")
        log(f"conversations: {len(convs)} kept")

    def _save_conversations(self, convs: list[dict]) -> str | None:
        """Under the lock: write the conversations (fresh temp + rename in
        their folder; never opens an existing name), all of them. None, or
        why it could not be written."""
        data = json.dumps({"version": CONVERSATIONS_VERSION, "conversations": convs},
                          ensure_ascii=False, separators=(",", ":")).encode()
        tmp = os.path.join(CONVERSATIONS_DIR, f".{CONVERSATIONS_NAME}.{secrets.token_hex(4)}")
        try:
            os.makedirs(CONVERSATIONS_DIR, mode=0o700, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, os.path.join(CONVERSATIONS_DIR, CONVERSATIONS_NAME))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            return f"the conversations could not be saved ({e.strerror or 'write failed'})"
        return None

    def get(self, project: str, sid: str, vpn: bool) -> dict:
        """The caller's own search asked through this path (`vpn`: the
        search-vpn server's), or a not-found refusal (another project's search
        is not found either). The caller's own search asked through the other
        server is refused with that server's name."""
        if not isinstance(sid, str) or not ID_RE.match(sid):
            raise ToolRefusal("no search with that id was found for this project")
        with self.lock:
            rec = self.table.get(sid)
            if rec is None or rec["project"] != project:
                raise ToolRefusal("no search with that id was found for this project")
            if rec["vpn"] != vpn:
                raise ToolRefusal(f"that search was asked through the {SERVER_NAMES[rec['vpn']]} "
                                  "MCP server; check and collect it there")
            return dict(rec)

    @staticmethod
    def _times(rec: dict) -> dict:
        """How long a search waited in the queue and has run (or ran)."""
        now = time.time()
        return {"waited_s": round((rec["started_at"] or rec["finished_at"] or now)
                                  - rec["submitted_at"]),
                "ran_s": (round((rec["finished_at"] or now) - rec["started_at"])
                          if rec["started_at"] else 0)}

    def check(self, project: str, sid: str, vpn: bool) -> dict:
        rec = self.get(project, sid, vpn)
        out = {"search_id": sid, "state": rec["state"], "searcher": rec["searcher"],
               "vpn": rec["vpn"], "question": rec["question"], **self._times(rec),
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

    def collect(self, project: str, sid: str, keep: bool, vpn: bool) -> dict:
        rec = self.get(project, sid, vpn)
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
            recent = [{**{k: v for k, v in e.items() if k not in ("view", "report")},
                       "has_report": e["report"] is not None} for e in self.recent]
        return {"searches": [
            {"id": r["id"], "project": r["project"], "consumer": r["consumer"],
             "question": r["question"][:LIST_QUESTION_MAX_CHARS], "searcher": r["searcher"],
             "vpn": r["vpn"], "state": r["state"], **self._times(r), "tokens": r["stream"].tokens()}
            for r in recs], "recent": recent, "recent_kept": kept}

    def _trim_recent(self) -> int:
        """Under the lock: drop the oldest recent lines past the setting's
        `recent` (read now: a lowered value trims at once). Returns it."""
        settings = read_settings()
        kept = settings["recent"] if settings else DEFAULT_RECENT
        while len(self.recent) > kept:
            self.recent.pop()
        return kept

    def _record_end(self, rec: dict, tools: list[dict]) -> dict:
        """Under the lock, once per search, when it ends (any way): its line
        in the recent list, with its work view as it ended (`tools`: its
        trace's tool calls, read outside the lock before it ended), and its
        report when it ended with one. Returns the line."""
        line = {
            "id": rec["id"], "view": self._build_view(rec, tools),
            "report": (json.dumps({k: rec["outcome"][k] for k in ("summary", "sources", "tiers")},
                                  ensure_ascii=False, separators=(",", ":")).encode()
                       if "summary" in rec["outcome"] else None),
            "project": rec["project"], "consumer": rec["consumer"],
            "question": rec["question"][:LIST_QUESTION_MAX_CHARS],
            "searcher": rec["searcher"], "vpn": rec["vpn"], "state": rec["state"],
            "reason": rec["outcome"]["reason"][:RECENT_REASON_MAX_CHARS],
            "ended_at": rec["finished_at"],
            "ran_s": (round(rec["finished_at"] - rec["started_at"])
                      if rec["started_at"] else 0),
            "tokens": rec["stream"].tokens()}
        self.recent.appendleft(line)
        self._trim_recent()
        return line

    def view(self, sid: str) -> dict | None:
        """The operator's read-only view of one search: live while it is in
        the table (ended and not collected too), else as it ended, from its
        recent line or (an ask) its conversation entry. None when it is in
        none of them."""
        with self.lock:
            rec = self.table.get(sid)
            rec = dict(rec) if rec is not None else None
            if rec is None:
                self._trim_recent()
                for e in self.recent:
                    if e["id"] == sid:
                        return dict(e["view"])
                a = self._find_ask(sid)
                return dict(a["view"]) if a is not None else None
        tools = (self._trace_tools(sid, rec["stream"].exit_names())
                 if rec["state"] != "queued" else [])
        return self._build_view(rec, tools)

    def report(self, sid: str) -> dict | None:
        """The operator's read of one search's report, as the agent wrote it:
        from the table while it is there (ended with a report), else from its
        recent line or (an ask) its conversation entry. `has_report` is False
        for a search the service knows that has none (still running, or ended
        without one). None when it is in none of them."""
        with self.lock:
            rec = self.table.get(sid)
            if rec is not None:
                out = {"id": sid, "state": rec["state"], "question": rec["question"],
                       "searcher": rec["searcher"], "vpn": rec["vpn"]}
                o = rec.get("outcome") or {}
                if rec["state"] in ENDED and "summary" in o:
                    return {**out, "has_report": True,
                            **{k: o[k] for k in ("summary", "sources", "tiers")}}
                raw = None
            else:
                self._trim_recent()
                for e in self.recent:
                    if e["id"] == sid:
                        out = {"id": sid, "state": e["state"], "question": e["view"]["question"],
                               "searcher": e["searcher"], "vpn": e["vpn"]}
                        raw = e["report"]
                        break
                else:
                    a = self._find_ask(sid)
                    if a is None:
                        return None
                    out = {"id": sid, "state": a["view"]["state"], "question": a["question"],
                           "searcher": a["view"]["searcher"], "vpn": a["vpn"]}
                    if a["report"] is None:
                        return {**out, "has_report": False}
                    return {**out, "has_report": True,
                            **{k: a["report"][k] for k in ("summary", "sources", "tiers")}}
        if raw is None:
            return {**out, "has_report": False}
        return {**out, "has_report": True, **json.loads(raw)}

    @staticmethod
    def _trace_tools(sid: str, exits) -> list[dict]:
        """Outside the lock: the tool calls of a search's trace (none when it
        has no volume); `exits`: a VPN search's exits, which its entries may
        name."""
        root = volume_root(SEARCH_PREFIX + sid)
        return _trace_work(read_volume_file(root, OUT_DIR, TRACE_NAME, TRACE_MAX_BYTES)
                           if root else None, exits)

    def _build_view(self, rec: dict, tools: list[dict]) -> dict:
        """One search's view from its record and its trace's tool calls: the
        tool calls and the agent's text blocks, oldest first: the newest
        VIEW_TAIL_ENTRIES, a text cut to its end. No reads of the volume, so
        it may run under the lock."""
        texts, dropped = rec["stream"].work_texts()
        calls, calls_dropped = rec["stream"].work_calls()
        dropped = dropped or calls_dropped
        work = sorted(tools + calls + texts, key=lambda e: e["ts"])
        out = {"id": rec["id"], "state": rec["state"], "question": rec["question"],
               "searcher": rec["searcher"], "vpn": rec["vpn"],
               "tokens": rec["stream"].tokens(), **self._times(rec),
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
                line = self._record_end(rec, [])    # never started: no volume, no work
                if rec["conversation"] is not None:
                    self._ask_ended(rec, line)
                self.lock.notify_all()
                state = "failed"
            else:
                rec["stop"] = True
                state = rec["state"]
        if state == "failed":
            log(f"search {sid} failed: {STOPPED_QUEUED}")
            if rec["project"] == OPERATOR_PROJECT:
                self._collect_ask(sid)
        else:
            docker("kill", SEARCH_PREFIX + sid)
            log(f"stop {sid} asked by the operator")
        return {"id": sid, "state": state, "stopped": True}

    def vpn_searxng_networks(self) -> list[str]:
        """The SearXNG networks of the VPN searches that are starting or
        running (the VPN's SearXNG joins them; a re-created one joins them
        again)."""
        with self.lock:
            return [searxng_net_name(r["id"]) for r in self.table.values()
                    if r["vpn"] and r["state"] in ("starting", "running")]

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
        # The trace is read before the search counts as ended: its container
        # is gone, so the trace is final, and no collect can remove the
        # volume yet.
        with self.lock:
            stream = self.table[sid]["stream"] if sid in self.table else None
        tools = self._trace_tools(sid, stream.exit_names() if stream else ())
        ask = False
        with self.lock:
            rec = self.table.get(sid)
            if rec is not None:
                rec["state"] = state
                rec["finished_at"] = time.time()
                rec["outcome"] = outcome
                line = self._record_end(rec, tools)
                ask = rec["project"] == OPERATOR_PROJECT
                if ask and rec["conversation"] is not None:
                    self._ask_ended(rec, line)
            self.running -= 1
            self.lock.notify_all()
        log(f"search {sid} {state}: {outcome['reason']}")
        if ask:
            self._collect_ask(sid)

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


def _as_text(value) -> str:
    """A value of a report as text: a string as it is, anything else as its
    JSON (the agent wrote it; any JSON value is possible)."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (ValueError, TypeError, RecursionError):
        return ""


def _source_line(s) -> str:
    """One source of a report as one line: title · address · DOI (the keys
    the agent is told to use and their likely variants, as the host reads
    them; cli/rscore.py _SEARCH_SOURCE_FIELDS), or the item as text when it is
    not an object."""
    if not isinstance(s, dict):
        return _as_text(s)
    parts = []
    for keys in (("title", "name"), ("url", "address", "link"), ("doi",)):
        key = next((k for k in keys if k in s), None)
        if key is not None and _as_text(s[key]).strip():
            parts.append(_as_text(s[key]).strip())
    return " · ".join(parts)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _ask_shape_ok(a) -> bool:
    """One ask of the conversations file as the service writes it."""
    # `vpn` is absent from an ask saved before VPN searches existed.
    if not (isinstance(a, dict) and set(a) - {"vpn"} == {"id", "question", "searcher",
                                                         "submitted_at", "view", "report"}
            and isinstance(a.get("vpn", False), bool)
            and isinstance(a["id"], str) and ID_RE.match(a["id"])
            and isinstance(a["question"], str) and a["question"]
            and isinstance(a["searcher"], str) and _is_num(a["submitted_at"])):
        return False
    v, rep = a["view"], a["report"]
    if v is not None and not (
            isinstance(v, dict) and v.get("id") == a["id"] and v.get("state") in ENDED
            and isinstance(v.get("question"), str) and isinstance(v.get("searcher"), str)
            and isinstance(v.get("vpn", False), bool)
            and all(_is_count(v.get(k)) for k in ("tokens", "waited_s", "ran_s"))
            and isinstance(v.get("reason"), str) and isinstance(v.get("truncated"), bool)
            and isinstance(v.get("work"), list)
            and all(isinstance(e, dict) and _is_num(e.get("ts"))
                    and e.get("kind") in ("tool", "text") for e in v["work"])):
        return False
    if rep is not None and not (isinstance(rep, dict) and set(rep) == {"summary", "sources",
                                                                       "tiers"}):
        return False
    return not (v is None and rep is not None)


def _conversations_shape(data) -> list | None:
    """The conversations of a parsed file, or None when it is not one the
    service writes (its version, keys, ids, counts and the fields every read
    uses)."""
    if not (isinstance(data, dict) and set(data) == {"version", "conversations"}
            and data["version"] == CONVERSATIONS_VERSION
            and not isinstance(data["version"], bool)
            and isinstance(data["conversations"], list)):
        return None
    seen: set = set()
    for c in data["conversations"]:
        if not (isinstance(c, dict) and set(c) == {"id", "created_at", "updated_at", "asks"}
                and isinstance(c["id"], str) and ID_RE.match(c["id"])
                and _is_num(c["created_at"]) and _is_num(c["updated_at"])
                and isinstance(c["asks"], list) and c["asks"]
                and all(_ask_shape_ok(a) for a in c["asks"])):
            return None
        ids = [c["id"]] + [a["id"] for a in c["asks"]]
        if seen & set(ids) or len(set(ids)) != len(ids):
            return None
        seen.update(ids)
    return data["conversations"]


def _read_conversations() -> tuple[list, str | None]:
    """(the kept conversations, None), or ([], why the file is not one the
    service wrote). No file is no conversations. Opened without following a
    link and never blocking on a FIFO, read whole (the service writes it,
    with no size cap)."""
    try:
        dfd = os.open(CONVERSATIONS_DIR, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return [], None
    except OSError as e:
        return [], f"their folder cannot be opened: {e.strerror or 'open failed'}"
    try:
        try:
            fd, st = _open_file(dfd, CONVERSATIONS_NAME)
        except FileNotFoundError:
            return [], None
        except OSError as e:
            if e.errno in (0, errno.ELOOP):
                return [], "the file is not a regular file"
            return [], f"the file cannot be read: {e.strerror or 'read failed'}"
        try:
            pieces = []
            while True:
                piece = os.read(fd, PIECE)
                if not piece:
                    break
                pieces.append(piece)
            raw = b"".join(pieces)
        finally:
            os.close(fd)
    except OSError as e:
        return [], f"the file cannot be read: {e.strerror or 'read failed'}"
    finally:
        os.close(dfd)
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return [], "the file is not valid JSON"
    convs = _conversations_shape(data)
    if convs is None:
        return [], "the file does not have the shape the service writes"
    return convs, None


def sidecar_name(sid: str, tier: str, lane: int | None = None) -> str:
    """A search's sidecar of one tier (in a VPN search, of one tier and the
    exit's position in the search)."""
    return f"{SEARCH_PREFIX}{sid}-{tier}" + (f"-{lane}" if lane is not None else "")


def searxng_net_name(sid: str) -> str:
    return f"{SEARCH_PREFIX}{sid}{SEARXNG_NET_SUFFIX}"


def out_net_name(sid: str) -> str:
    return f"{SEARCH_PREFIX}{sid}{OUT_NET_SUFFIX}"


def exit_net_name(sid: str, lane: int) -> str:
    return f"{SEARCH_PREFIX}{sid}{EXIT_NET_INFIX}{lane}"


def route_helper_name(sid: str) -> str:
    return f"{SEARCH_PREFIX}{sid}{ROUTE_SUFFIX}"


# The containers of the service's own that a search's networks may hold
# besides the search's (and every exit's gateway, is_gateway): they are
# disconnected before a network is removed.
_NETWORK_PEERS = (SEARXNG_CONTAINER, SEARXNG_VPN_CONTAINER)


def network_members(net: str) -> list[str] | None:
    r = docker("network", "inspect", "-f", "{{range .Containers}}{{.Name}} {{end}}", net)
    return r.stdout.decode("utf-8", "replace").split() if r.returncode == 0 else None


def _search_exit_things(sid: str) -> tuple[list[str], list[str]]:
    """A VPN search's per-exit containers and networks there are (found by
    its ID_LABEL and LANE_LABEL), each only when it is named as that
    position's: (containers, networks)."""
    found: tuple[list[str], list[str]] = ([], [])
    for kind, fmt, out in (("ps", "{{.Names}}", found[0]), ("network", "{{.Name}}", found[1])):
        args = (["ps", "-a"] if kind == "ps" else ["network", "ls"]) + [
            "--filter", f"label={ID_LABEL}={sid}", "--filter", f"label={LANE_LABEL}",
            "--format", fmt + " {{.Label \"" + LANE_LABEL + "\"}}"]
        r = docker(*args)
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            n, _, lane = line.strip().partition(" ")
            if not LANE_RE.match(lane):
                continue
            names = ({sidecar_name(sid, t, int(lane)) for t in (*TIER_NAMES, PLAIN_TIER)}
                     if kind == "ps" else {exit_net_name(sid, int(lane))})
            if n in names:
                out.append(n)
    return found


def remove_search(sid: str) -> list[str]:
    """Remove one search's containers (its agent, its sidecars — a VPN
    search's per exit — and a route helper a crash left), networks (its own,
    the one it shares with a SearXNG, a VPN search's network of each exit and
    its agent's own way out; the service's SearXNGs and the exits' gateways
    are disconnected first) and volume, each by its name. Idempotent; returns
    what could not be removed."""
    name = SEARCH_PREFIX + sid
    problems = []
    exit_containers, exit_nets = _search_exit_things(sid)
    for cname in (name, *(sidecar_name(sid, t) for t in (*TIER_NAMES, PLAIN_TIER)),
                  *exit_containers, route_helper_name(sid)):
        if container_state(cname)[0] != "absent":
            r = docker("rm", "-f", cname)
            if r.returncode != 0:
                problems.append(f"container {cname}: {first_line(r)}")
    for net in (name, searxng_net_name(sid), out_net_name(sid), *exit_nets):
        members = network_members(net)
        if members is not None:
            for peer in members:
                if peer in _NETWORK_PEERS or is_gateway(peer):
                    docker("network", "disconnect", "-f", net, peer)
            r = docker("network", "rm", net)
            if r.returncode != 0:
                problems.append(f"network {net}: {first_line(r)}")
    if docker("volume", "inspect", name).returncode == 0:
        r = docker("volume", "rm", name)
        if r.returncode != 0:
            problems.append(f"volume {name}: {first_line(r)}")
    return problems


# What follows a search's id in the name of one of its things: a sidecar
# (-<tier>, a VPN search's -<tier>-<n>), the SearXNG network (-sx), a VPN
# search's agent's network (-out) or network of one exit (-x-<n>), or a
# route helper.
_THING_TAIL_RE = re.compile(r"^(?:-(?:browser|stealth|plain)(?:-[1-9][0-9]*)?|-sx|-out|-route"
                            r"|-x-[1-9][0-9]*)?$")


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
            rest = n[len(SEARCH_PREFIX):] if n.startswith(SEARCH_PREFIX) else ""
            sid, tail = rest[:16], rest[16:]
            if ID_RE.match(sid) and _THING_TAIL_RE.match(tail):
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


def _trace_exit(ev: dict, exits) -> dict:
    """{"exit": …} when a trace line names one of the search's exits (a VPN
    search's plain-tier sidecar writes its exit), else {}."""
    e = ev.get("exit")
    return {"exit": e} if isinstance(e, str) and e in exits else {}


def _trace_summary(raw: bytes | None, exits=()) -> dict:
    """What was tried, from the plain tier's trace (one line per tool call,
    written by RS's code, not by the model): per tier and tool the calls and
    their outcomes, the failures with their tiers and targets, and every
    engine SearXNG reported unresponsive with its reasons. The browser tiers'
    calls are added from the agent's stream (AgentStream.tried_into)."""
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
            failures.append({"tier": tier, **_trace_exit(ev, exits), "tool": tool,
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


def _trace_work(raw: bytes | None, exits=()) -> list[dict]:
    """The tool calls of a trace as the operator's view lists them: each with
    its time and every field a string, capped as _trace_summary caps them,
    and the exit when the line names one of `exits` (a VPN search's). A
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
                    **_trace_exit(ev, exits),
                    "target": str(ev.get("target", ""))[:TRACE_TARGET_MAX_CHARS],
                    "outcome": str(ev.get("outcome", "?"))[:TRACE_NAME_MAX_CHARS],
                    "detail": str(ev.get("detail", ""))[:TRACE_TARGET_MAX_CHARS]})
    return out


def prepare_volume(name: str) -> str | None:
    """Make the fresh volume's two folders (FILES_DIR, OUT_DIR), owned by the
    agent's user, before any container mounts it: a sidecar mounts the files
    folder alone, so it has to exist, and docker copies nothing into a volume
    that is not empty (its root stays the service's). Nothing but the service
    has touched the volume yet. None, or why it could not be done."""
    root = volume_root(name)
    if root is None:
        return "its folder could not be found"
    try:
        rfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        return e.strerror or "open failed"
    try:
        for folder in (FILES_DIR, OUT_DIR):
            os.mkdir(folder, 0o755, dir_fd=rfd)
            os.chown(folder, AGENT_UID, AGENT_UID, dir_fd=rfd, follow_symlinks=False)
    except OSError as e:
        return e.strerror or "mkdir failed"
    finally:
        os.close(rfd)
    return None


def _sidecar_answers(address: str) -> bool:
    """Whether a sidecar's tool server answers an MCP initialize (a request
    from the service to the sidecar: its answer comes back on the
    connection the service opened; the service's guard still drops every
    connection the other way). The answer is read bounded and not used."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL_VERSIONS[0], "capabilities": {},
        "clientInfo": {"name": "rs-search", "version": "1"}}}).encode()
    req = urllib.request.Request(f"http://{address}:{SIDECAR_PORT}{SIDECAR_PATH}", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=SIDECAR_PROBE_S) as resp:
            resp.read(SIDECAR_PROBE_MAX_BYTES)
            session = resp.headers.get("Mcp-Session-Id")
            ok = resp.status == 200
    except (OSError, ValueError):
        return False
    if ok and session and re.fullmatch(r"[\x21-\x7e]{1,200}", session):
        # End the probe's session (best effort: a session holds no browser
        # until a tool is called).
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://{address}:{SIDECAR_PORT}{SIDECAR_PATH}", method="DELETE",
                headers={"Mcp-Session-Id": session}), timeout=SIDECAR_PROBE_S).close()
        except (OSError, ValueError):
            pass
    return ok


def start_sidecars(rec: dict, labels: list[str], lanes: list[dict]) -> tuple[dict, dict]:
    """Start the sidecars of one search, hardened as the agent (no
    capability, no privilege gain, its own user; the browsers run without
    their own sandbox, so the container is the boundary), the volume's files
    folder as a browser's output folder; then wait (all together, up to
    SIDECAR_READY_S) until each one's tool server answers. Returns ({server
    name: the agent's URL of its tool server}, {server name: why it is
    unavailable}); a sidecar that does not come up is removed and its server
    left out.

    `lanes`: where the sidecars go. A normal search has one lane ({"exit":
    None, "n": None, "net": the search's network}): a sidecar per browser
    tier the searcher has on. A VPN search has one lane per exit whose tunnel
    is up ({"exit": its name, "n": its position in the search, which names
    the lane's sidecars and servers, "net": the search's network of that
    exit, "gateway":
    the exit's gateway's address on it, "dns": the exit's resolvers, "env":
    the plain tier's settings, "web": whether its plain sidecar joins the
    search's SearXNG network}): each lane also has the plain tier as a sidecar
    (agent/tools.py over HTTP, the whole search volume at /search), every
    sidecar of the lane is created on the lane's `--internal` network,
    resolves names through the exit's tunnel, and gets its default route
    through the exit's gateway from a one-shot helper before anything uses it
    (a plain sidecar's SearXNG network takes no new connection in)."""
    up: dict = {}
    unavailable: dict = {}
    waiting: dict = {}          # server: (container, network)
    sid = rec["id"]
    for lane in lanes:
        ex, n = lane["exit"], lane["n"]
        tiers = [t for t in TIER_NAMES if rec["tiers"].get(t)]
        if ex is not None:
            tiers.insert(0, PLAIN_TIER)
        for tier in tiers:
            server = server_name(tier, n)
            cname = sidecar_name(sid, tier, n)
            image = AGENT_IMAGE if tier == PLAIN_TIER else SIDECAR_IMAGES[tier]
            if not image_present(image):
                unavailable[server] = (f"the {tier} sidecar's image is not staged in the "
                                       "search service; the operator turns the service on "
                                       "again")
                continue
            common = ["--name", cname, *labels, "--label", f"{TIER_LABEL}={tier}",
                      *(["--label", f"{LANE_LABEL}={n}", "--label", f"{EXIT_LABEL}={ex}"]
                        if ex is not None else []),
                      "--network", lane["net"], "--restart", "no",
                      "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                      "--user", RUN_AS]
            if tier == PLAIN_TIER:
                mount = ["-v", f"{SEARCH_PREFIX}{sid}:{AGENT_VOLUME_MOUNT}",
                         *(a for k, v in lane["env"].items() for a in ("-e", f"{k}={v}")),
                         "--entrypoint", PLAIN_SIDECAR_CMD[0], image, *PLAIN_SIDECAR_CMD[1:]]
            else:
                mount = ["--mount", f"type=volume,src={SEARCH_PREFIX}{sid},"
                                    f"dst={SIDECAR_OUTPUT},volume-subpath={FILES_DIR}", image]
            if ex is None:
                r = docker("run", "-d", *common, *mount)
            else:
                dns = [a for d in lane["dns"] for a in ("--dns", d)]
                web = tier == PLAIN_TIER and lane["web"]
                r = docker("create", *common, *dns, *mount)
                if r.returncode == 0 and web:
                    r = docker("network", "connect", searxng_net_name(sid), cname)
                if r.returncode == 0:
                    r = docker("start", cname)
                if r.returncode == 0:
                    closed = container_ip(cname, searxng_net_name(sid)) if web else None
                    problem = ("it has no address on the search's SearXNG network"
                               if web and closed is None else
                               route_through_gateway(cname, lane["gateway"], labels,
                                                     route_helper_name(sid), closed))
                    if problem:
                        unavailable[server] = (f"the {tier} sidecar could not be routed "
                                               f"through the exit {ex} ({problem})")
                        docker("rm", "-f", cname)
                        continue
            if r.returncode != 0:
                unavailable[server] = (f"the {tier} sidecar could not be started "
                                       f"({first_line(r)})")
                docker("rm", "-f", cname)
                continue
            waiting[server] = (cname, lane["net"])
    deadline = time.monotonic() + SIDECAR_READY_S
    addresses: dict = {}
    while waiting:
        for server, (cname, net) in list(waiting.items()):
            if container_state(cname)[0] != "running":
                unavailable[server] = f"the {server} sidecar stopped as it started"
                del waiting[server]
                continue
            if server not in addresses:
                ip = container_ip(cname, net)
                if ip:
                    addresses[server] = ip
            if server in addresses and _sidecar_answers(addresses[server]):
                up[server] = f"http://{cname}:{SIDECAR_PORT}{SIDECAR_PATH}"
                del waiting[server]
        if not waiting:
            break
        if time.monotonic() >= deadline:
            for server in waiting:
                unavailable[server] = (f"the {server} sidecar's tool server did not answer "
                                       f"within {SIDECAR_READY_S} seconds")
            break
        time.sleep(SIDECAR_POLL_S)
    for lane in lanes:
        for tier in (*TIER_NAMES, PLAIN_TIER):
            if server_name(tier, lane["n"]) in unavailable:
                cname = sidecar_name(sid, tier, lane["n"])
                if container_state(cname)[0] != "absent":
                    docker("rm", "-f", cname)
    for why in unavailable.values():
        log(f"search {sid}: {why}")
    return up, unavailable


def agent_tools(up: dict, vpn: bool) -> str:
    """The agent's tool list for this search (Claude Code's --mcp-config
    JSON): each tool server that is up (`up`: {server name: its URL}), over
    HTTP at the sidecar's name on the search's network (of that exit, in a
    VPN search), and a normal search's plain tier over stdio in the agent's
    own container."""
    servers: dict = {} if vpn else {PLAIN_TIER: PLAIN_SERVER}
    for server, url in up.items():
        servers[server] = {"type": "http", "url": url}
    return json.dumps({"mcpServers": servers}, separators=(",", ":"))


def exit_text(name: str) -> str:
    """An exit's name as the searcher's instructions write it: a JSON string
    (its quotes and backslashes escaped), its backticks written as \\u0060, so
    no name can close or open a code span or a quote there (it holds no line
    break, exit_name_ok); the agent gives it back exactly as JSON reads it."""
    return json.dumps(name, ensure_ascii=False).replace("`", "\\u0060")


def exits_instructions(ran: list[dict], left_out: list[tuple[str, str]], per_site: int,
                       web: bool) -> str:
    """The part of a VPN search agent's instructions about its exits (the
    agent's run.sh appends it to RS's instructions, whose tool names it has
    renamed for the first exit): the exits it has, in order, each with its
    tool servers (named by its position in this search); the ones left out and
    why; and how to use them: every site starts on the first exit, a page
    refused by the search's address is tried again through the next exit, at
    most `per_site` exits per site (by the page's host name), and every source
    names its exit (by its name, each written by exit_text). `ran`: [{"exit",
    "n", "servers"}] in order; `web`: the first exit's plain server has the web
    search."""
    first, first_n = exit_text(ran[0]["exit"]), ran[0]["n"]
    lines = ["## The exits", "",
             "This search reaches every site through a VPN, from one of its exits, never "
             "from the address of the computer it runs on. Each exit has its own tool "
             "servers with the same tools; a tool is called by the exit's server's name, "
             "`mcp__<server>__` and the tool's own name. Each exit's name is written as a "
             "JSON string. The tools named above are the "
             f"first exit's ({first}). The exits, in their order:", ""]
    for i, r in enumerate(ran, 1):
        names = ", ".join(f"`{s}`" for s in r["servers"])
        plain = server_name(PLAIN_TIER, r["n"])
        lines.append(f"{i}. {exit_text(r['exit'])}" + (" (the first exit)" if i == 1 else "")
                     + f": its servers {names} (as in `mcp__{plain}__fetch_page`).")
    if left_out:
        lines += ["", "Left out of this search: "
                  + "; ".join(f"{exit_text(n)} ({why})" for n, why in left_out) + "."]
    signs = ("refused because of where the request comes from (the signs under Your "
             "report: a page or an error that names your address or your network, \"not "
             "available in your region\", a block that names the address)")
    if per_site == 1:
        how = [f"- Try at most one exit on a site: a page {signs} on the first exit is not "
               "tried again through another exit; report it as blocked."]
    else:
        how = [f"- When a page is {signs}, call the same tool for the same page through the "
               "next exit in the order, on that exit's server."
               + ("" if len(ran) > 1 else " This search has no other exit: such a page "
                  "stays blocked."),
               f"- Try at most {per_site} exits on one site, counted by the host name in the "
               f"page's address (the first exit counts as one): once {per_site} exits were "
               "refused there, stop trying that site and report it as blocked."]
    lines += ["", "How to use them:", "",
              f"- Start every site on the first exit ({first}).",
              "- Tell an address block from a bot check before you move. A page refused "
              "because of where the request comes from is an address block: another tier "
              "on the same exit leaves from the same address, so only another exit can help "
              "(within the limit below). A "
              "page that checks whether you are a person (a captcha, a \"Verifying…\" or "
              "\"checking your browser\" page, a page that stays empty without a browser) "
              "is a bot check: climb the tiers you have on the current exit first (the "
              "browser, then the stealth browser), and try another exit only when every "
              "tier there was refused (within the same limit).",
              *how,
              (f"- The web search runs only on the first exit (`mcp__"
               f"{server_name(PLAIN_TIER, first_n)}__web_search`); the other exits' "
               "`web_search` answers that it is unavailable." if web else
               "- The web search is not available in this search (its SearXNG goes out "
               "through the VPN's first exit, which this search does not have, or it is "
               "not running): `web_search` answers that it is unavailable."),
              "- Report which exit each source came from: give each source an `exit`, "
              "the NAME of the exit whose server fetched or opened it (the string listed "
              "above, exactly as JSON reads it; not its servers' number); and name the "
              "exit on every line of `tiers` you tried through one."]
    return "\n".join(lines) + "\n"


def list_dropped_files(root: str) -> list[dict]:
    """The files in the volume's files folder whose names FILE_NAME_RE
    refuses (a browser download keeps the page's own name), each with why it
    is not listed. The name is shown as text (cut, its undecodable bytes
    replaced); the file is never opened."""
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
            if FILE_NAME_RE.match(name):
                continue
            shown = name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
            out.append({"name": shown[:TRACE_TARGET_MAX_CHARS], "why": DROPPED_WHY})
    finally:
        os.close(dfd)
        os.close(rfd)
    return out


def address_blocks(tiers) -> list[str]:
    """The sites a report's `tiers` lines say refused the search by its
    address (each line marked `"blocked_by_address": true` by the agent, as
    its instructions ask): the host of the line's address, or what it asked,
    each once, in order (the agent wrote them: cut like a trace's target)."""
    sites: list[str] = []
    for line in tiers if isinstance(tiers, list) else []:
        if not (isinstance(line, dict) and line.get("blocked_by_address") is True):
            continue
        asked = next((line[k] for k in ("asked", "url", "address", "query")
                      if isinstance(line.get(k), str) and line[k].strip()), "")
        try:
            host = urllib.parse.urlsplit(asked.strip()).hostname
        except ValueError:
            host = None
        site = (host or asked.strip() or "a site")[:TRACE_TARGET_MAX_CHARS]
        if site not in sites:
            sites.append(site)
    return sites


def address_block_note(n: int, vpn_search: bool) -> str:
    """One sentence for the asking agent: how many sites refused the search
    by its address and, for a search from this installation's address while
    the VPN version is on, that the search-vpn server reaches sites from
    another one."""
    head = f"{n} site{'s' if n != 1 else ''} refused this search by its address"
    if vpn_search:
        return head + " (the addresses of the VPN's exits it tried)"
    if vpn_on():
        return head + (f"; the {SERVER_NAMES[True]} MCP server reaches sites from the VPN's "
                       "address, if you have it")
    return head


def _exits_up(exits: list) -> list[tuple[dict, dict]]:
    """Each exit with its tunnel as a VPN search's start needs it
    (vpn_wait_up, the exits checked side by side), in order."""
    results: dict = {}

    def one(name: str) -> None:
        try:
            results[name] = vpn_wait_up(name)
        except Exception as e:  # noqa: BLE001 — one exit's failure leaves it out
            results[name] = {"tunnel": "down", "handshake_s": None,
                             "error": f"its check failed ({type(e).__name__})"}
    threads = [threading.Thread(target=one, args=(e["name"],), daemon=True) for e in exits]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [(e, results[e["name"]]) for e in exits]


def _source_exits(sources, exits) -> object:
    """A report's sources with each one's `exit` kept only when it names one
    of the search's exits (`exits`; none in a normal search, whose sources
    never carry one)."""
    if not isinstance(sources, list):
        return sources
    out = []
    for src in sources:
        if isinstance(src, dict) and "exit" in src and not (
                isinstance(src["exit"], str) and src["exit"] in exits):
            src = {k: v for k, v in src.items() if k != "exit"}
        out.append(src)
    return out


def run_search(rec: dict, started, stopped) -> tuple[str, dict]:
    """Create the search's networks, volume and container, follow and wait for
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
    vpn = bool(rec["vpn"])
    ups: list = []              # a VPN search's exits whose tunnel is up: [cfg]
    left_out: list = []         # the others: [(name, why)]
    stored_first = None
    if vpn:
        # A VPN search runs only through exits whose tunnel is up: never from
        # the host's address, and nothing is made for it when none is.
        exits = read_vpn() or []
        stored_first = exits[0]["name"] if exits else None
        for cfg, st in _exits_up(exits):
            if st["tunnel"] == "up":
                ups.append(cfg)
            else:
                left_out.append((cfg["name"], "its tunnel was not up when the search "
                                              "started: " + (st["error"] or "no handshake")))
        if not ups:
            return "failed", {"reason": VPN_NOT_UP + ("; ".join(f"{n}: {why}" for n, why
                                                                in left_out)
                                                      or "no exit is stored")}
        # The VPN's SearXNG goes out through the stored first exit: the web
        # search is there only when that exit is the search's first.
        web = ups[0]["name"] == stored_first
        searxng_problem = (None if web and container_state(SEARXNG_VPN_CONTAINER)[0] == "running"
                           else "the VPN's SearXNG is not running" if web
                           else "the VPN's first exit is not up, and the VPN's SearXNG "
                                "goes out through it")
        searxng = SEARXNG_VPN_CONTAINER
    else:
        searxng_problem = ensure_searxng()
        searxng = SEARXNG_CONTAINER
    labels = ["--label", f"{ID_LABEL}={sid}", "--label", f"{PROJECT_LABEL}={rec['project']}"]
    sx = searxng_net_name(sid)
    # A VPN search has one network per exit, each internal (its sidecars'
    # only way out is the exit's gateway the helper routes them through), and
    # its agent reaches its model provider by a network of its own; a normal
    # search has one network, its sidecars' and its agent's.
    if vpn:
        # Each exit up gets its position in this search (1, 2, …), which
        # names its network, sidecars and servers (LANE_LABEL).
        nets = [(["network", "create", "--internal", *labels,
                  "--label", f"{LANE_LABEL}={i}", "--label", f"{EXIT_LABEL}={cfg['name']}",
                  exit_net_name(sid, i)],
                 f"network of the exit {cfg['name']}") for i, cfg in enumerate(ups, 1)]
        nets.append((["network", "create", *labels, out_net_name(sid)], "agent's network"))
    else:
        nets = [(["network", "create", *labels, name], "network")]
    nets.append((["network", "create", "--internal", *labels, sx], "SearXNG network"))
    for args, what in ((["volume", "create", *labels, name], "volume"), *nets):
        r = docker(*args)
        if r.returncode != 0:
            return "failed", {"reason": f"the search's {what} could not be created "
                                        f"({first_line(r)})"}
    problem = prepare_volume(name)
    if problem:
        return "failed", {"reason": f"the search's volume could not be prepared ({problem})"}
    if searxng_problem is None:
        r = docker("network", "connect", "--alias", SEARXNG_ALIAS, sx, searxng)
        if r.returncode != 0:
            searxng_problem = f"SearXNG could not join the search's network ({first_line(r)})"
    lanes = []
    if vpn:
        for i, cfg in enumerate(ups, 1):
            net = exit_net_name(sid, i)
            r = docker("network", "connect", net, gateway_name(cfg["name"]))
            gw = container_ip(gateway_name(cfg["name"]), net) if r.returncode == 0 else None
            if gw is None:
                left_out.append((cfg["name"], "its gateway could not join the search's "
                                              f"network ({first_line(r)})"))
                continue
            web_lane = i == 1 and searxng_problem is None
            lanes.append({"exit": cfg["name"], "n": i, "net": net, "gateway": gw, "dns": cfg["dns"],
                          "web": web_lane,
                          "env": {"RS_SEARCH_CONTACT": rec["contact"],
                                  "RS_SEARCH_SEARXNG": "on" if web_lane else "",
                                  "RS_SEARCH_EXIT": cfg["name"]}})
        if not lanes:
            return "failed", {"reason": VPN_NOT_UP + "; ".join(f"{n}: {why}"
                                                               for n, why in left_out)}
    else:
        lanes.append({"exit": None, "n": None, "net": name})
    if stopped():
        return "failed", {"reason": STOPPED_NO_REPORT}
    # The sidecars, before the agent (its tool servers have to answer when
    # its session starts).
    up, unavailable = start_sidecars(rec, labels, lanes)
    # A VPN search's exits it runs on, in order: [{"exit", "n", "servers"}].
    ran: list = []
    if vpn:
        for lane in lanes:
            ex, n = lane["exit"], lane["n"]
            tiers = [t for t in (PLAIN_TIER, *TIER_NAMES) if server_name(t, n) in up]
            if PLAIN_TIER not in tiers:
                # An exit without its plain tier has no way to fetch or report.
                for t in tiers:
                    docker("rm", "-f", sidecar_name(sid, t, n))
                    del up[server_name(t, n)]
                left_out.append((ex, "its plain tier did not come up ("
                                 + unavailable.get(server_name(PLAIN_TIER, n),
                                                   "it did not answer") + ")"))
                continue
            ran.append({"exit": ex, "n": n, "servers": [server_name(t, n) for t in tiers]})
        # Which exit each server is: the view's calls and the report's
        # sources name the exit, never its position.
        rec["stream"].set_exits({server_name(t, r["n"]): (t, r["exit"])
                                 for r in ran for t in TIER_NAMES},
                                [r["exit"] for r in ran])

    def drop_sidecars() -> None:
        for lane in lanes:
            for tier in (PLAIN_TIER, *TIER_NAMES):
                if server_name(tier, lane["n"]) in up:
                    docker("rm", "-f", sidecar_name(sid, tier, lane["n"]))
    if vpn and not ran:
        drop_sidecars()
        return "failed", {"reason": "the plain tier could not run on any VPN exit's route ("
                                    + "; ".join(f"{n}: {why}" for n, why in left_out) + ")"}
    first_n = ran[0]["n"] if vpn else None
    tiers_up = [t for t in TIER_NAMES if server_name(t, first_n) in up]
    env_vpn = []
    if vpn:
        note = exits_instructions(ran, left_out, rec["exits_per_site"],
                                  web=ran[0]["exit"] == stored_first and searxng_problem is None)
        env_vpn = ["-e", f"RS_SEARCH_SERVER_SUFFIX=-{first_n}", "-e",
                   f"RS_SEARCH_EXITS_NOTE={note}"]
    # No capability at all, no privilege gain, the agent's own user; its
    # networks reach the internet and its own sidecars (the search's network,
    # its default route) and SearXNG (the internal one, which has no route
    # out), nothing else (the service's guard drops everything from an inner
    # network toward the service and toward any private address). Created,
    # joined to the SearXNG network, then started, so SearXNG answers from its
    # first call. A VPN search's agent is on its exits' internal networks
    # (their sidecars) and its own network instead of SearXNG's: its default
    # route, to its model provider. The token reaches it through an env file read by the docker
    # client here, never on this command line.
    if stopped():
        drop_sidecars()
        return "failed", {"reason": STOPPED_NO_REPORT}
    agent_nets = ([exit_net_name(sid, r["n"]) for r in ran] + [out_net_name(sid)] if vpn
                  else [name, sx])
    r = docker("create", "--name", name, *labels,
               "--network", agent_nets[0], "--restart", "no",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--user", RUN_AS,
               "--env-file", TOKEN_PATH,
               "-e", f"RS_SEARCH_ID={sid}",
               "-e", f"RS_SEARCH_MODEL={rec['model']}",
               "-e", f"RS_SEARCH_EFFORT={rec['effort']}",
               "-e", f"RS_SEARCH_CONTACT={rec['contact']}",
               "-e", f"RS_SEARCH_SEARXNG={'' if searxng_problem else 'on'}",
               "-e", f"RS_SEARCH_QUESTION={rec['prompt']}",
               "-e", f"RS_SEARCH_MCP_CONFIG={agent_tools(up, vpn)}",
               "-e", f"RS_SEARCH_TIERS={' '.join(tiers_up)}",
               *env_vpn,
               "-v", f"{name}:{AGENT_VOLUME_MOUNT}",
               "-v", f"{CLAUDE_DIST}:/opt/claude:ro",
               AGENT_IMAGE)
    # A normal search's agent calls SearXNG itself; a VPN search's agent
    # only reaches its sidecars and, by its own network, its model.
    for net in agent_nets[1:]:
        if r.returncode == 0:
            r = docker("network", "connect", net, name)
    if r.returncode == 0:
        r = docker("start", name)
    if r.returncode != 0:
        drop_sidecars()
        docker("rm", "-f", name)
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
    rec["stream"].end_calls()
    # The sidecars go as soon as the agent has ended (the collect removes
    # them too, by name, when this fails).
    drop_sidecars()
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
    exits_ran = [r["exit"] for r in ran]
    tried = _trace_summary(read_volume_file(root, OUT_DIR, TRACE_NAME, TRACE_MAX_BYTES)
                           if root else None, exits_ran)
    if searxng_problem:
        tried["searxng"] = searxng_problem
    rec["stream"].tried_into(tried)
    if unavailable:
        tried["unavailable"] = unavailable
    if vpn:
        tried["exits"] = exits_ran
        if left_out:
            tried["exits_left_out"] = {n: why for n, why in left_out}
    report = None
    if report_raw is not None:
        try:
            report = json.loads(report_raw)
        except (ValueError, RecursionError):
            report = None
    outcome: dict = {"tried": tried, "agent": agent}
    dropped = list_dropped_files(root) if root else []
    if dropped:
        outcome["dropped_files"] = dropped
    if isinstance(report, dict):
        outcome["summary"] = report.get("summary", "")
        outcome["sources"] = _source_exits(report.get("sources", []), exits_ran)
        outcome["tiers"] = report.get("tiers", [])
        if was_stopped:
            outcome["reason"] = STOPPED_WITH_REPORT
        elif timed_out:
            outcome["reason"] = (f"the search ran past {SEARCH_MAX_S} seconds and was "
                                 "stopped after it wrote its report")
        else:
            outcome["reason"] = "the search agent wrote its report"
        sites = address_blocks(outcome["tiers"])
        if sites:
            note = address_block_note(len(sites), rec["vpn"])
            outcome["blocked_by_address"] = {"sites": sites, "next": note}
            outcome["reason"] += "; " + note
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


def tools_listed(vpn: bool) -> list[dict]:
    """The tools as a caller lists them on one path. On search-vpn's,
    search_submit's description says that its searches reach sites from the
    VPN's address; on search's, while the VPN version is on, that the
    search-vpn server does."""
    if vpn:
        extra = (" This server's searches reach every site from the VPN's exits' addresses "
                 "(the first exit first, the next one for a page refused by its address), "
                 "never this installation's; its tools see only the searches asked through "
                 "it.")
    elif vpn_on():
        extra = (f" The {SERVER_NAMES[True]} MCP server, if you have it, runs the same "
                 "searches reaching every site from the VPN's address instead of this "
                 "installation's: use it when a search's outcome says sites refused it by "
                 "its address.")
    else:
        return TOOLS
    return [dict(TOOLS[0], description=TOOLS[0]["description"] + extra), *TOOLS[1:]]


def _rpc_error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_result(data, is_error: bool = False) -> dict:
    text = data if isinstance(data, str) else json.dumps(data, indent=1, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(caller: tuple[str, str] | None, name: str, args: dict, vpn: bool) -> dict:
    if caller is None:
        return _tool_result("this server is reached through RS's MCP gateway only", True)
    project, consumer = caller
    try:
        if name == "search_submit":
            return _tool_result(SEARCHES.submit(project, consumer, args.get("question"),
                                                args.get("searcher") or "", vpn))
        if name == "search_check":
            return _tool_result(SEARCHES.check(project, args.get("search_id"), vpn))
        if name == "search_collect":
            return _tool_result(SEARCHES.collect(project, args.get("search_id"),
                                                 args.get("keep") is True, vpn))
    except ToolRefusal as e:
        return _tool_result(str(e), True)
    return _tool_result(f"no tool named {name}", True)


def handle_rpc(msg, caller, vpn: bool) -> dict | None:
    """One JSON-RPC message on a caller-facing path (`vpn`: search-vpn's)."""
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
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools_listed(vpn)}}
    if method == "tools/call":
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        return {"jsonrpc": "2.0", "id": rid,
                "result": call_tool(caller, str(params.get("name")), args, vpn)}
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
        vpn = MCP_PATHS.get(path.rstrip("/"))
        if vpn is None:
            return self._json(404, {"error": "not found"})
        raw = self._body()
        try:
            msg = json.loads(raw or b"")
        except (ValueError, RecursionError):
            return self._json(400, _rpc_error(None, -32700, "parse error"))
        caller = parse_caller(self.headers.get(CALLER_HEADER))
        if isinstance(msg, list):
            replies = [r for r in (handle_rpc(m, caller, vpn) for m in msg) if r is not None]
            return self._json(200, replies) if replies else self._send(202)
        reply = handle_rpc(msg, caller, vpn)
        if reply is None:
            return self._send(202)
        return self._json(200, reply)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/admin/"):
            return self._admin(path, post=False)
        for mcp_path, vpn in MCP_PATHS.items():
            if path.startswith(mcp_path + FILES_SUFFIX):
                return self._file(path[len(mcp_path + FILES_SUFFIX):], vpn)
            if path.rstrip("/") == mcp_path:
                # No server-initiated stream: every reply is the POST's own.
                return self._send(405, b"", extra={"Allow": "POST"})
        return self._json(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_DELETE(self):
        return self._send(405, b"", extra={"Allow": "POST"})

    def _file(self, rest: str, vpn: bool) -> None:
        """A search's file, under the path the search was asked through."""
        caller = parse_caller(self.headers.get(CALLER_HEADER))
        sid, _, fname = urllib.parse.unquote(rest).partition("/")
        missing = {"error": "no such file for this project"}
        if caller is None or not ID_RE.match(sid) or not FILE_NAME_RE.match(fname):
            return self._json(404, missing)
        try:
            SEARCHES.get(caller[0], sid, vpn)
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

    def _ask(self) -> None:
        """POST /admin/ask: {"question", "conversation" (an id or null),
        "include", "vpn"} as the host sends it (an older host sends no vpn: a
        normal search). A refusal is 422 with its text."""
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if not 0 <= length <= ASK_BODY_MAX_BYTES:
            return self._json(422, {"error": f"the question is longer than "
                                              f"{QUESTION_MAX_CHARS} characters"})
        try:
            body = json.loads(self.rfile.read(length) or b"")
        except (ValueError, RecursionError):
            body = None
        if isinstance(body, dict) and "vpn" not in body:
            body["vpn"] = False
        if not (isinstance(body, dict)
                and set(body) == {"question", "conversation", "include", "vpn"}
                and isinstance(body["vpn"], bool)
                and isinstance(body["question"], str) and isinstance(body["include"], bool)
                and (body["conversation"] is None
                     or (isinstance(body["conversation"], str)
                         and ID_RE.match(body["conversation"])))):
            return self._json(422, {"error": "the question did not arrive as RS sends it"})
        try:
            return self._json(200, SEARCHES.ask(body["question"], body["conversation"],
                                                body["include"], body["vpn"]))
        except ToolRefusal as e:
            return self._json(422, {"error": str(e)})

    def _admin(self, path: str, post: bool) -> None:
        if not self._local():
            return self._json(404, {"error": "not found"})
        if path == "/admin/status":
            st, _labels = container_state(SEARXNG_CONTAINER)
            return self._json(200, {
                "searches": SEARCHES.counts(),
                "searxng": st,
                "images": {"agent": image_present(AGENT_IMAGE),
                           "searxng": image_present(SEARXNG_IMAGE),
                           **{t: image_present(SIDECAR_IMAGES[t]) for t in TIER_NAMES},
                           "vpn": image_present(VPN_IMAGE)},
                "settings": read_settings() is not None,
                "token": token_present()})
        if path == "/admin/ensure" and post:
            return self._json(200, {"searxng": ensure_searxng() or "running",
                                    "vpn": ensure_vpn() or "ok"})
        if path == "/admin/vpn" and not post:
            return self._json(200, vpn_status())
        if path == "/admin/vpn/apply" and post:
            ensure_vpn()                # the gateways in line; checks start
            return self._json(200, vpn_status())
        if path.startswith("/admin/vpn/apply/") and post:
            # The exit by its fingerprint (exit_fingerprint): no name in a route.
            fingerprint = path[len("/admin/vpn/apply/"):]
            if not FINGERPRINT_RE.match(fingerprint):
                return self._json(404, {"error": "not found"})
            ensure_vpn(renew=fingerprint)   # that exit set up again; a check of it starts
            return self._json(200, vpn_status())
        if path == "/admin/clear" and post:
            return self._json(200, SEARCHES.clear_ended())
        if path == "/admin/list" and not post:
            return self._json(200, SEARCHES.active())
        if path == "/admin/test" and post:
            return self._json(200, sign_in_test())
        if path == "/admin/ask" and post:
            return self._ask()
        if path == "/admin/conversations" and not post:
            return self._json(200, SEARCHES.conversations_list())
        if path == "/admin/conversations/clear" and post:
            try:
                return self._json(200, SEARCHES.conversations_clear())
            except ToolRefusal as e:
                return self._json(422, {"error": str(e)})
        if (path.startswith("/admin/conversation/") and path.endswith("/delete")
                and post):
            cid = path[len("/admin/conversation/"):-len("/delete")]
            if not ID_RE.match(cid):
                return self._json(404, {"error": NO_SUCH_CONVERSATION})
            try:
                return self._json(200, SEARCHES.conversation_delete(cid))
            except ToolRefusal as e:
                return self._json(404 if str(e) == NO_SUCH_CONVERSATION else 422,
                                  {"error": str(e)})
        if path.startswith("/admin/conversation/") and not post:
            cid = path[len("/admin/conversation/"):]
            if not ID_RE.match(cid):
                return self._json(404, {"error": NO_SUCH_CONVERSATION})
            try:
                return self._json(200, SEARCHES.conversation(cid))
            except ToolRefusal as e:
                return self._json(404 if str(e) == NO_SUCH_CONVERSATION else 422,
                                  {"error": str(e)})
        for prefix, is_post, act in (("/admin/view/", False, SEARCHES.view),
                                     ("/admin/report/", False, SEARCHES.report),
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
    SEARCHES.load_conversations()
    remove_leftovers()
    state, _labels = container_state(SEARXNG_CONTAINER)
    if state != "absent":
        docker("rm", "-f", SEARXNG_CONTAINER)        # a fresh one, fresh settings
    # The VPN gateway and its SearXNG, left by an earlier run: removed; the
    # host hands the VPN configuration over again and the ensure that follows
    # sets them up afresh (their settings live only in tmpfs).
    remove_vpn()
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
# error on stdout): no such search for view / report / stop, no such
# conversation, or an ask, a delete or a clear refused (its text in `error`).
ADMIN_REFUSED_EXIT = 3
# Each admin request: its route and whether it is a GET. `ask` sends its
# stdin as the body.
_ADMIN_ROUTES = {"status": ("status", True), "ensure": ("ensure", False),
                 "vpn": ("vpn", True), "vpn-apply": ("vpn/apply", False),
                 "vpn-renew": ("vpn/apply", False),
                 "clear": ("clear", False), "list": ("list", True), "test": ("test", False),
                 "view": ("view", True), "report": ("report", True), "stop": ("stop", False),
                 "ask": ("ask", False), "conversations": ("conversations", True),
                 "conversations-clear": ("conversations/clear", False),
                 "conversation": ("conversation", True),
                 "conversation-delete": ("conversation", False)}
_ADMIN_WITH_ID = ("view", "report", "stop", "conversation", "conversation-delete")
# What follows the id in a request's route (none for the others).
_ADMIN_ID_SUFFIX = {"conversation-delete": "/delete"}


def admin(what: str, sid: str | None = None) -> int:
    # `vpn-renew` takes an exit's fingerprint (exit_fingerprint: never its
    # name, on this command line) where the others take a search's id.
    takes = _ADMIN_WITH_ID + ("vpn-renew",)
    if what not in _ADMIN_ROUTES or (what in takes) != (sid is not None) \
            or (sid is not None and not (FINGERPRINT_RE if what == "vpn-renew"
                                         else ID_RE).match(sid)):
        sys.stderr.write("unknown admin request\n")
        return 2
    route, get = _ADMIN_ROUTES[what]
    body = b""
    if what == "ask":
        body = sys.stdin.buffer.read(ASK_BODY_MAX_BYTES + 1)
        if len(body) > ASK_BODY_MAX_BYTES:
            sys.stderr.write("too large\n")
            return 2
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/admin/{route}"
                                 + (f"/{sid}" if sid else "")
                                 + _ADMIN_ID_SUFFIX.get(what, ""),
                                 method="GET" if get else "POST",
                                 data=None if get else body,
                                 headers={} if get else {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=ADMIN_CALL_S) as resp:
            sys.stdout.write(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code not in (404, 422):
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
                     "status|ensure|vpn|vpn-apply|clear|list|test|conversations|"
                     "conversations-clear | admin vpn-renew FINGERPRINT | "
                     "admin ask (the body on stdin) | "
                     "admin view|report|stop|conversation|conversation-delete ID\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
