"""search — RS's search service: names, pins, settings and pure helpers.

Stdlib only, and it imports nothing from RS (rscore and mcp_servers import it;
the reverse would be circular). RS runs ONE search service for the whole
installation, turned on by the operator (Services → Search):

  - ``rs-search``, a container of the inner-Docker image projects use
    (rs-sandbox-dind) plus the service program (container/search/). Its inner
    Docker keeps its images on the named volume ``rs-search-docker``, so the
    search images are staged into it once (at Turn on, and again only when
    the host's image changes) and survive a restart and a re-create; the
    operator's Quick Search conversations live on ``rs-search-conversations``,
    kept the same way;
  - on ``rs-search-net``, its own ``--internal`` network with RS's router on
    it: the route out is injected after every start and the router holds a
    locked project's rules for its range (TCP 80/443, DNS to the configured
    resolvers) — the fail-closed egress shape of the servers RS runs. Made
    through rscore.network_create_args like theirs (its egress goes through
    the router, so the operator's `rsn` host rule applies to it);
  - on ``rs-search-front``, an ``--internal`` network shared ONLY with the
    MCP gateway (rs-mcpgw), which reaches the service there by name. Made by
    name, like Aldine's networks. Never on rs-sandbox: the service has no
    login of its own, and it believes the caller identity the gateway hands
    it.

Each search runs inside the service's inner Docker in containers made for it
alone (container/search/service.py). The service is offered to agents as the
MCP gateway server ``search`` and, while the VPN version is on and a VPN
configuration is stored, as a second server ``search-vpn`` (the same tools at
a second path, whose searches reach sites only through the VPN); each is wired
to projects on its own. The gateway passes the caller's project and consumer
on to these two (CALLER_HEADER) and to no other server.

Host state lives under ``~/.research-sandbox/search/`` (never mounted into a
container): the "on" record, the searcher's settings, the long-lived Claude
token (0600) and the VPN configuration (0600, it holds the tunnel's private
key). The settings, the token and (while the VPN version is on) the VPN
configuration reach the service over a `docker exec` stdin, into a tmpfs only
root in the service reads, at every start and at every change; the VPN
configuration goes on from there only to the service's VPN gateway, over
stdin again.

The lifecycle (on / off / status / resume at `research start`) lives in
rscore; this module decides names, the container's exact shape, and the
contents of the files.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# --- names -------------------------------------------------------------------

SERVICE_CONTAINER = "rs-search"
# Built from this tree on rs-sandbox-dind (container/search/Dockerfile.service)
# at Turn on and at every `research start` (cached: seconds): the host's wiring
# code and the service program are one version.
SERVICE_IMAGE = "rs-search:latest"
# The search agent's image (container/search/Dockerfile.agent), built on the
# host beside the service image and staged into the service's inner Docker.
AGENT_IMAGE = "rs-search-agent:latest"
# The two browser tiers' sidecar images (container/search/Dockerfile.browser,
# Dockerfile.stealth), built and staged the same way: a search whose searcher
# has a tier on runs one container of that image beside its agent.
BROWSER_IMAGE = "rs-search-browser:latest"
STEALTH_IMAGE = "rs-search-stealth:latest"
# The VPN gateway's image (container/search/Dockerfile.vpn: WireGuard's tools,
# iproute2 and iptables on Alpine), built and staged the same way. The service
# runs one container of it while the VPN version is on with a VPN configuration
# stored (the gateway VPN searches route through), and one-shot containers of it that set a VPN
# search's sidecars' route.
VPN_IMAGE = "rs-search-vpn:latest"
# Every search image the host builds from this tree, with its Dockerfile (in
# container/search/); the service image is first (the others are staged into
# it).
SEARCH_DOCKERFILES = (("Dockerfile.service", SERVICE_IMAGE),
                      ("Dockerfile.agent", AGENT_IMAGE),
                      ("Dockerfile.browser", BROWSER_IMAGE),
                      ("Dockerfile.stealth", STEALTH_IMAGE),
                      ("Dockerfile.vpn", VPN_IMAGE))
# The built images staged into the service's inner Docker under the same names.
STAGED_BUILT_IMAGES = (AGENT_IMAGE, BROWSER_IMAGE, STEALTH_IMAGE, VPN_IMAGE)
# The service's inner Docker store (images, search volumes): kept at Turn off,
# so turning the service on again does not stage the images again.
DOCKER_VOLUME = "rs-search-docker"
# The operator's conversations (the Search page's Quick Search), one file the
# service alone writes, on a volume of their own mounted at this path in the
# service container (container/search/service.py CONVERSATIONS_DIR — MIRROR).
# Made by RS with OWNER_LABEL before the container that mounts it, and kept at
# Turn off like DOCKER_VOLUME, so the conversations outlive a restart and a
# re-create of the service.
CONVERSATIONS_VOLUME = "rs-search-conversations"
SERVICE_CONVERSATIONS_DIR = "/var/lib/rs-search-conversations"
EGRESS_NETWORK = "rs-search-net"
FRONT_NETWORK = "rs-search-front"
# Every network and the container carry this label; a thing of one of these
# names without it is not RS's and is refused, never used or removed.
OWNER_LABEL = "research.search"
OWNER_VALUE = "1"
# What the running container was made from (a container whose labels differ
# from the installation's is re-created at the next Turn on or start).
SEARXNG_LABEL = "research.search.searxng"
IMAGE_LABEL = "research.search.image"          # the service image's content identity

# The caller-facing server (the MCP endpoint and the file route below it):
# normal searches at MCP_PATH, VPN searches at MCP_VPN_PATH (service.py —
# MIRRORS).
SERVICE_PORT = 8080
MCP_PATH = "/mcp"
MCP_VPN_PATH = "/mcp-vpn"

# The MCP gateway entry RS keeps for it (an ordinary entry of the server list;
# RS recognises its own by the address, as it does Aldine's).
MCP_SERVER_NAME = "search"
MCP_URL = f"http://{SERVICE_CONTAINER}:{SERVICE_PORT}{MCP_PATH}"
MCP_DESCRIPTION = ("RS's search service: hand over a question, collect a "
                   "written answer with its sources and files")
# The VPN version's entry: listed while the service is on, the VPN version is
# ticked and a VPN configuration is stored; taken off when any of the three
# stops holding.
MCP_VPN_SERVER_NAME = "search-vpn"
MCP_VPN_URL = f"http://{SERVICE_CONTAINER}:{SERVICE_PORT}{MCP_VPN_PATH}"
MCP_VPN_DESCRIPTION = ("RS's search service through a VPN: the same as search, with "
                       "every site reached from the VPN's address")
# RS's own entries, each at exactly its address.
RS_ENTRIES = {MCP_SERVER_NAME: (MCP_URL, MCP_DESCRIPTION),
              MCP_VPN_SERVER_NAME: (MCP_VPN_URL, MCP_VPN_DESCRIPTION)}
# The gateway sets this header (`<project>/<consumer>`) on requests to an
# entry the rendered list marks with PASS_CALLER_KEY, strips it from every
# caller's request to every server, and sends it to no other server. MIRROR
# of container/mcp-gateway/gateway.py CALLER_HEADER / PASS_CALLER_KEY (the
# gateway image carries no cli/).
CALLER_HEADER = "X-RS-Caller"
PASS_CALLER_KEY = "pass_caller"

# Where the service container reads what the host hands it (a tmpfs only root
# reads; see container/search/service.py RUN_DIR — MIRROR).
SERVICE_RUN_DIR = "/run/rs-search"
SETTINGS_FILE_NAME = "settings.json"
TOKEN_FILE_NAME = "claude.env"
# The VPN configuration as the service is handed it (service.py VPN_PATH —
# MIRROR): over stdin into the same tmpfs, and from there only to the VPN
# gateway container, over stdin again.
VPN_FILE_NAME = "vpn.json"
# The host's agent bundles, read-only, so each search agent runs the Claude
# Code the operator pulled (a MIRROR of service.py AGENT_DIST_DIR).
SERVICE_AGENT_DIST = "/opt/agent-dist"

# The inner image names the service runs (handed to it in its environment).
INNER_SEARXNG_REPO = "rs-search-searxng"

# --- SearXNG pins ----------------------------------------------------------------

SEARXNG_REPO = "searxng/searxng"
SEARXNG_VERSION_KEY = "SEARXNG_VERSION"
SEARXNG_DIGEST_KEY = "SEARXNG_DIGEST"
# SearXNG versions are a date and a commit (2026.11.23-a7163d091): the value
# becomes an image tag and a container label.
_SEARXNG_VERSION_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}-[0-9a-f]{7,40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# --- the browser sidecars' pins ------------------------------------------------------
# Build args of Dockerfile.browser and Dockerfile.stealth (the keys are their
# ARG names). Each value goes into a package specifier or a download name, so
# it is exactly a version: digits and dots, and for camoufox's browser build
# its release suffix (158.0.1-beta.41) and its download's sha256.
_DOTTED_RE = re.compile(r"^\d+(\.\d+){1,3}$")
_CAMOUFOX_BROWSER_RE = re.compile(r"^\d+(\.\d+){1,3}-[a-z]+\.\d+$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
# An Alpine package's version with its release (1.0.20210914-r4): the VPN
# gateway's packages are installed as `name=version`.
_APK_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}-r\d+$")
SIDECAR_PIN_KEYS = (
    ("NODE_VERSION", _DOTTED_RE, "not a Node version"),
    ("SEARCH_PLAYWRIGHT_MCP_VERSION", _DOTTED_RE, "not a Playwright tool server version"),
    ("SEARCH_PLAYWRIGHT_PY_VERSION", _DOTTED_RE, "not a Playwright version"),
    ("CAMOUFOX_VERSION", _DOTTED_RE, "not a camoufox version"),
    ("CAMOUFOX_BROWSER_VERSION", _CAMOUFOX_BROWSER_RE, "not a camoufox browser version"),
    ("CAMOUFOX_BROWSER_SHA256", _SHA256_HEX_RE, "not a sha256 checksum"),
    # The VPN gateway (Dockerfile.vpn): its Alpine base and its three packages.
    ("SEARCH_VPN_ALPINE_VERSION", _DOTTED_RE, "not an Alpine version"),
    ("SEARCH_VPN_WIREGUARD_TOOLS_VERSION", _APK_VERSION_RE, "not an Alpine package version"),
    ("SEARCH_VPN_IPROUTE2_VERSION", _APK_VERSION_RE, "not an Alpine package version"),
    ("SEARCH_VPN_IPTABLES_VERSION", _APK_VERSION_RE, "not an Alpine package version"),
)
_PIN_BAD = ("the search service's version setting ({key}) is {what}; under "
            "Host → Software, Use repo version on its row puts the value this "
            "installation ships back")

# --- host state ----------------------------------------------------------------

STATE_DIR = Path.home() / ".research-sandbox" / "search"
# Present = the operator turned the service on. `research start` resumes it
# only then — never creates it on its own.
ON_RECORD_PATH = STATE_DIR / "on"
SETTINGS_PATH = STATE_DIR / "settings.json"
# The long-lived token `claude setup-token` prints, as the one line of an env
# file (0600): it is handed to the service over stdin and from there to each
# search agent through `docker run --env-file`, never on a command line.
TOKEN_PATH = STATE_DIR / "claude.env"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
# The VPN configuration (WireGuard), normalized by RS from the provider's file
# or the fields typed by hand, with the VPN server's address resolved once at
# save (0600, like the token: it holds the tunnel's private key). Handed to
# the service over stdin, never mounted, never on a command line.
VPN_PATH = STATE_DIR / "vpn.json"
# Serializes the writers of the host files (settings, token, VPN) between the
# broker and the host command. One non-blocking attempt on the broker.
LOCK_PATH = STATE_DIR / "settings.lock"
# A setup-token is one line of letters, digits, `-` and `_` (sk-ant-oat01-…).
# The charset is what an env file and the service carry literally; nothing
# else about the token is checked (a bad one fails at the first search, which
# says so plainly).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# --- the searcher ------------------------------------------------------------------

SETTINGS_VERSION = 1
# A searcher's name: what a caller names in its submit. The server-name rule
# (a lowercase letter, then lowercase letters, digits and dashes).
SEARCHER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
DEFAULT_SEARCHER_NAME = "general"
# Sonnet at high effort: the setting RS's other helper that searches the web
# (the surveyor) ships with, measured adequate for a literature survey and
# several times cheaper than Opus at the same effort.
DEFAULT_MODEL = "sonnet"
DEFAULT_EFFORT = "high"
# How many searches run at once (the rest wait their turn, in order).
#   - Every search shares the service's ONE credential, so they share one
#     account's rate limit, and they all leave from the host's one address,
#     where SearXNG's engines suspend a caller that asks too often (measured:
#     arXiv answered "too many requests" from the first query; Semantic
#     Scholar degraded after six).
#   - At 1 a single long search (a deep literature question at high effort
#     runs for many minutes) holds every other caller's question back, and
#     two agents of one project asking at once wait on each other.
#   - At 20 (ten times 2) the engines' suspensions and the account's rate
#     limit become what every search hits, and each search agent is a Claude
#     Code process (hundreds of MB) on the operator's machine.
# Two lets two callers' questions run side by side; the operator raises it on
# the Search page for a busier installation.
DEFAULT_AT_ONCE = 2
# The highest value the page accepts. Each running search holds one network
# of the service's inner Docker, whose address pool has 256 (the inner
# daemon's 10.201.0.0/16 in /24 slices); past a few dozen at once the shared
# rate limit and the engines' suspensions bound the work anyway. 32 keeps
# well inside the pool with every search of the queue holding its network.
MAX_AT_ONCE = 32
# How many ended searches the Search page's "Recent searches" list keeps. The
# list lives in the search service's memory only (a restart of the service
# clears it), newest first; 0 keeps none.
#   - 20 is a few days of a small lab's questions, or one busy afternoon: enough
#     to see what was asked lately and how each search ended, short enough to
#     read without scrolling far under the live table.
#   - The list rides every 5-second read of the live table, so its size is paid
#     on every read: one row (project, consumer, the question cut at 200
#     characters, searcher, state, the reason cut at 200, ended_at, ran_s,
#     tokens) is about 500 bytes, and 100 rows (RECENT_MAX) are about 50 KB,
#     which stays inside the 64 KiB the broker keeps its own replies to. Ten
#     times that would make every read of the page a half-megabyte transfer for
#     rows nobody scrolls to.
DEFAULT_RECENT = 20
RECENT_MAX = 100
# Unpaywall requires an email address and Crossref's polite pool asks for
# one. When the operator leaves the contact empty, the requests carry this
# generic address (a reserved documentation domain: no person behind it).
GENERIC_CONTACT = "user@example.org"
_CONTACT_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
# The browser tiers a searcher may use, each on or off: the headless browser
# (Playwright's tool server with Chromium) and the stealth browser (camoufox).
# Both on by default: the maintainer called Playwright and camoufox fair game,
# and the searcher tries them only after the plain tier failed. A settings
# file saved before the setting existed reads as both on.
TIER_NAMES = ("browser", "stealth")


class SearchError(Exception):
    """A refusal whose text is safe to show in the browser: it names a setting
    or a page, never a host path, a host command or a secret."""


# --- pins ----------------------------------------------------------------------

def pins_from(versions: dict[str, str]) -> dict[str, str]:
    """{searxng_version, searxng_digest} and, under their own key names, the
    browser sidecars' build pins (SIDECAR_PIN_KEYS), from the effective
    version pins, each checked. SearchError naming the key on a missing or
    malformed one."""
    out = {}
    for key, field_name, rx, what in (
            (SEARXNG_VERSION_KEY, "searxng_version", _SEARXNG_VERSION_RE,
             "not a SearXNG version"),
            (SEARXNG_DIGEST_KEY, "searxng_digest", _DIGEST_RE, "not an image digest"),
            *((k, k, rx_, w) for k, rx_, w in SIDECAR_PIN_KEYS)):
        value = versions.get(key, "")
        if not value:
            raise SearchError(_PIN_BAD.format(key=key, what="missing"))
        if not rx.match(value):
            raise SearchError(_PIN_BAD.format(key=key, what=what))
        out[field_name] = value
    return out


def build_args(pins: dict[str, str]) -> list[str]:
    """The checked sidecar pins as `docker build --build-arg` arguments (the
    other search Dockerfiles declare none of them, so passing them to every
    search build changes nothing there)."""
    out: list[str] = []
    for key, _rx, _what in SIDECAR_PIN_KEYS:
        out += ["--build-arg", f"{key}={pins[key]}"]
    return out


def searxng_image(pins: dict[str, str]) -> str:
    """The SearXNG image by digest: a re-pointed tag is never used."""
    return f"{SEARXNG_REPO}@{pins['searxng_digest']}"


def inner_searxng_image(pins: dict[str, str]) -> str:
    """The name the service's inner Docker knows the pinned SearXNG by."""
    return f"{INNER_SEARXNG_REPO}:{pins['searxng_version']}"


# --- docker argv (pure) ----------------------------------------------------------

def owner_labels() -> list[str]:
    return ["--label", f"{OWNER_LABEL}={OWNER_VALUE}"]


def egress_network_create_argv(base: list[str]) -> list[str]:
    """`base` is rscore.network_create_args(EGRESS_NETWORK) (the RS bridge
    naming). `--internal`: no route out at all until RS injects one through
    the router, so a service that (re)started before RS did reaches nothing."""
    assert base[:3] == ["docker", "network", "create"] and base[-1] == EGRESS_NETWORK
    return base[:3] + ["--internal"] + owner_labels() + base[3:]


def conversations_volume_create_argv() -> list[str]:
    """The conversations' volume, by name and labelled as RS's."""
    return ["docker", "volume", "create"] + owner_labels() + [CONVERSATIONS_VOLUME]


def front_network_create_argv() -> list[str]:
    """The gateway's way in. `--internal` (nothing leaves by it), by name."""
    return ["docker", "network", "create", "--internal"] + owner_labels() + [FRONT_NETWORK]


def service_run_argv(*, dind_mode: str, dns: list[str], pins: dict[str, str],
                     image_content: str, agent_dist_dir: Path, pids_limit: int) -> list[str]:
    """The service container. Its inner Docker runs privileged where the host
    has no sysbox (as a project's does); its store is the named volume; the
    entrypoint of the dind image needs a /workspace, which is a tmpfs here
    (nothing of the service lives there). Restart policy `no` on purpose:
    `research start` brings it back and re-injects its route, so it can never
    come up with a route nobody set. The token and the settings are NOT on
    this command line: they arrive later over stdin."""
    argv = ["docker", "run", "-d",
            "--name", SERVICE_CONTAINER,
            "--hostname", SERVICE_CONTAINER,
            "--network", EGRESS_NETWORK,
            "--network", FRONT_NETWORK,
            "--restart", "no"]
    argv += owner_labels()
    argv += ["--label", f"{SEARXNG_LABEL}={pins['searxng_version']}",
             "--label", f"{IMAGE_LABEL}={image_content}"]
    if dind_mode == "sysbox":
        argv += ["--runtime=sysbox-runc"]
    else:
        argv += ["--privileged"]
    argv += ["-v", f"{DOCKER_VOLUME}:/var/lib/docker",
             "-v", f"{CONVERSATIONS_VOLUME}:{SERVICE_CONVERSATIONS_DIR}",
             "--tmpfs", "/workspace",
             "--tmpfs", f"{SERVICE_RUN_DIR}:mode=0700",
             "-v", f"{agent_dist_dir}:{SERVICE_AGENT_DIST}:ro",
             "-e", "DOCKER_DIND=true",
             "-e", f"RS_SEARCH_AGENT_IMAGE={AGENT_IMAGE}",
             "-e", f"RS_SEARCH_BROWSER_IMAGE={BROWSER_IMAGE}",
             "-e", f"RS_SEARCH_STEALTH_IMAGE={STEALTH_IMAGE}",
             "-e", f"RS_SEARCH_VPN_IMAGE={VPN_IMAGE}",
             "-e", f"RS_SEARCH_SEARXNG_IMAGE={inner_searxng_image(pins)}",
             f"--pids-limit={pids_limit}"]
    for s in dns:
        argv += ["--dns", s]
    if dns:
        # The inner daemon resolves through the same resolvers (the dind
        # entrypoint starts `dockerd --dns <each>` from this), the only ones
        # the locked router rules admit.
        argv += ["-e", f"RS_SANDBOX_DNS={','.join(dns)}"]
    argv.append(SERVICE_IMAGE)
    return argv


# --- the MCP gateway entry (pure) --------------------------------------------------

CONFLICT_TEXT = (
    "the MCP server list already has a server named {name} at another address, "
    "so the search service cannot be added under that name; rename or remove "
    "that server under Services → MCP and turn the search service on again "
    "under Services → Search")
CONFLICT_VPN_TEXT = (
    "the MCP server list already has a server named {name} at another address, "
    "so the VPN version of the search service cannot be added under that name; "
    "rename or remove that server under Services → MCP and save the search "
    "settings again under Services → Search")


def conflict_text(name: str) -> str:
    return (CONFLICT_VPN_TEXT if name == MCP_VPN_SERVER_NAME else CONFLICT_TEXT).format(
        name=name)


def is_rs_entry(name: str, entry: Any) -> bool:
    """Whether (name, entry) of the server list is one of RS's own search
    entries: `search` or `search-vpn`, an address entry (not a server RS
    runs) at exactly that name's address."""
    return (name in RS_ENTRIES and isinstance(entry, dict)
            and "run" not in entry and entry.get("url") == RS_ENTRIES[name][0])


def gateway_entry_conflict(data: dict[str, Any], name: str = MCP_SERVER_NAME) -> bool:
    """True when the list holds an entry of that name (`search` or
    `search-vpn`) that is not RS's."""
    entry = data["servers"].get(name)
    return entry is not None and not is_rs_entry(name, entry)


def apply_gateway_entry(data: dict[str, Any], name: str = MCP_SERVER_NAME) -> None:
    """Put RS's entry of that name into a loaded server list (in place). RS's
    own entry is kept as it is; any other of that name is a conflict
    (SearchError) and the list is left untouched."""
    if gateway_entry_conflict(data, name):
        raise SearchError(conflict_text(name))
    url, description = RS_ENTRIES[name]
    data["servers"].setdefault(name, {"url": url, "description": description})


# --- settings (pure) -----------------------------------------------------------------

def default_tiers() -> dict[str, bool]:
    return {t: True for t in TIER_NAMES}


def default_settings() -> dict[str, Any]:
    return {"version": SETTINGS_VERSION,
            "searcher": {"name": DEFAULT_SEARCHER_NAME, "model": DEFAULT_MODEL,
                         "effort": DEFAULT_EFFORT, "at_once": DEFAULT_AT_ONCE,
                         "tiers": default_tiers()},
            "contact": "", "recent": DEFAULT_RECENT, "sidebar": False, "vpn": False}


def tiers_problem(value: Any) -> str | None:
    if not isinstance(value, dict) or set(value) != set(TIER_NAMES) \
            or not all(isinstance(value[t], bool) for t in TIER_NAMES):
        return ("the searcher's browser tiers are two switches, browser and "
                "stealth, each on or off, and nothing else")
    return None


def searcher_name_problem(name: Any) -> str | None:
    if not isinstance(name, str) or not SEARCHER_NAME_RE.match(name):
        return ("a searcher's name starts with a lowercase letter and holds only "
                "lowercase letters, digits and dashes")
    return None


def at_once_problem(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) \
            or not 1 <= value <= MAX_AT_ONCE:
        return f"searches at once is a whole number from 1 to {MAX_AT_ONCE}"
    return None


def recent_problem(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) \
            or not 0 <= value <= RECENT_MAX:
        return f"recent searches kept is a whole number from 0 to {RECENT_MAX}"
    return None


def sidebar_problem(value: Any) -> str | None:
    if not isinstance(value, bool):
        return "showing Search in the sidebar is either on or off"
    return None


def vpn_problem(value: Any) -> str | None:
    if not isinstance(value, bool):
        return "the VPN version of the search service is either on or off"
    return None


def contact_problem(value: Any) -> str | None:
    if not isinstance(value, str):
        return "the contact address must be text"
    if value and not _CONTACT_RE.match(value):
        return ("the contact address must be one email address (or empty, for "
                "RS's generic one)")
    return None


def settings_problem(data: Any, model_ok) -> str | None:
    """Why `data` is not a valid settings record, or None. `model_ok(model,
    effort)` is the catalog's check (rscore passes it in: this module does
    not import the model catalog). A record without `recent`, `sidebar` or
    `vpn`, or a searcher without `tiers`, is valid: it was saved before that
    setting existed (read_settings fills the default)."""
    if not isinstance(data, dict) \
            or set(data) - {"recent", "sidebar", "vpn"} != {"version", "searcher", "contact"}:
        return "it does not have the expected shape"
    if data["version"] != SETTINGS_VERSION or isinstance(data["version"], bool):
        return "it was written by another version of RS"
    s = data["searcher"]
    if not isinstance(s, dict) or set(s) - {"tiers"} != {"name", "model", "effort", "at_once"}:
        return "its searcher does not have the expected shape"
    for problem in (searcher_name_problem(s["name"]), at_once_problem(s["at_once"]),
                    tiers_problem(s["tiers"]) if "tiers" in s else None,
                    contact_problem(data["contact"]),
                    recent_problem(data["recent"]) if "recent" in data else None,
                    sidebar_problem(data["sidebar"]) if "sidebar" in data else None,
                    vpn_problem(data["vpn"]) if "vpn" in data else None):
        if problem:
            return problem
    if not isinstance(s["model"], str) or not isinstance(s["effort"], str):
        return "its model and effort must be text"
    return model_ok(s["model"], s["effort"])


SETTINGS_DAMAGED = ("the search settings kept on the host cannot be used ({why}); "
                    "save the searcher again on the Search page to replace them")


def read_settings(model_ok, path: Path | None = None) -> dict[str, Any]:
    """The settings (the defaults when none were saved). SearchError when the
    file is there but unreadable or not one RS wrote."""
    path = SETTINGS_PATH if path is None else path
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return default_settings()
    except OSError as e:
        raise SearchError(SETTINGS_DAMAGED.format(
            why=f"it cannot be read: {e.strerror or 'read failed'}")) from None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise SearchError(SETTINGS_DAMAGED.format(why="it is not valid JSON")) from None
    problem = settings_problem(data, model_ok)
    if problem:
        raise SearchError(SETTINGS_DAMAGED.format(why=problem))
    data.setdefault("recent", DEFAULT_RECENT)       # saved before the setting existed
    data.setdefault("sidebar", False)               # likewise
    data.setdefault("vpn", False)                   # likewise
    data["searcher"].setdefault("tiers", default_tiers())     # likewise
    return data


def service_settings(data: dict[str, Any]) -> dict[str, Any]:
    """What the service is handed: the settings with the contact filled (the
    searcher carries its `tiers`, filled by read_settings) and whether the
    VPN version is on."""
    return {"version": SETTINGS_VERSION, "searcher": dict(data["searcher"]),
            "contact": data["contact"] or GENERIC_CONTACT, "recent": data["recent"],
            "vpn": data["vpn"]}


# --- host files ------------------------------------------------------------------

def _write_atomic(target: Path, text: str) -> None:
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


def write_settings(data: dict[str, Any], path: Path | None = None) -> None:
    """Raises OSError (the caller words it)."""
    _write_atomic(SETTINGS_PATH if path is None else path,
                  json.dumps(data, indent=2, sort_keys=True) + "\n")


TOKEN_BAD = ("that is not a token `claude setup-token` prints (one line of "
             "letters, digits, - and _); nothing was stored")
_TOKEN_DAMAGED = ("the search service's sign-in kept on the host is not one RS "
                  "wrote, so it was not used; store the token again on the "
                  "Search page")


def token_problem(token: Any) -> str | None:
    if not isinstance(token, str) or not _TOKEN_RE.match(token.strip()):
        return TOKEN_BAD
    return None


def token_line(token: str) -> str:
    return f"{TOKEN_ENV}={token.strip()}\n"


def write_token(token: str, path: Path | None = None) -> None:
    """Raises OSError (the caller words it); the token is checked first."""
    _write_atomic(TOKEN_PATH if path is None else path, token_line(token))


def read_token_file(path: Path | None = None) -> str | None:
    """The token file's text (exactly the line RS writes), or None when no
    token is stored. SearchError when the file holds anything else."""
    path = TOKEN_PATH if path is None else path
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise SearchError(_TOKEN_DAMAGED) from None
    key, sep, value = text.rstrip("\n").partition("=")
    if key != TOKEN_ENV or not sep or "\n" in value or not _TOKEN_RE.match(value):
        raise SearchError(_TOKEN_DAMAGED)
    return text


def clear_token(path: Path | None = None) -> None:
    path = TOKEN_PATH if path is None else path
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def is_on(path: Path | None = None) -> bool:
    return (ON_RECORD_PATH if path is None else path).exists()


def set_on(on: bool, path: Path | None = None) -> None:
    path = ON_RECORD_PATH if path is None else path
    if on:
        if not path.exists():
            _write_atomic(path, "")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# --- the VPN configuration (WireGuard) ---------------------------------------------
# The operator pastes the configuration file their VPN provider gives, or
# types its fields by hand; both become ONE normalized configuration, checked
# here (pure). The host resolves the VPN server's name once, at save
# (rscore), and stores the result in VPN_PATH (0600). What RS applies is
# IPv4 only (the service's networks are IPv4): an IPv6 address, resolver or
# allowed range is kept out and the status says so.

VPN_RECORD_VERSION = 1
# The lines of a configuration file RS reads, per section (WireGuard reads
# key names without regard to case), and the ones that may repeat (their
# values add up, as wg-quick reads them).
_VPN_INTERFACE_KEYS = {"privatekey": "PrivateKey", "address": "Address", "dns": "DNS",
                       "listenport": "ListenPort", "mtu": "MTU"}
_VPN_PEER_KEYS = {"publickey": "PublicKey", "endpoint": "Endpoint", "allowedips": "AllowedIPs",
                  "presharedkey": "PresharedKey", "persistentkeepalive": "PersistentKeepalive"}
_VPN_LIST_KEYS = ("Address", "DNS", "AllowedIPs")
# wg-quick's lines that run commands or change the host's routing or files:
# RS runs none of them, and refuses a configuration that has one rather than
# quietly leaving it out.
_VPN_REFUSED_KEYS = {"preup": "PreUp", "postup": "PostUp", "predown": "PreDown",
                     "postdown": "PostDown", "table": "Table", "saveconfig": "SaveConfig"}
# The manual form's fields, each to the configuration file's line it fills;
# the last two may be left empty.
VPN_MANUAL_FIELDS = {"private_key": "PrivateKey", "address": "Address", "dns": "DNS",
                     "public_key": "PublicKey", "endpoint": "Endpoint",
                     "allowed_ips": "AllowedIPs", "preshared_key": "PresharedKey",
                     "keepalive": "PersistentKeepalive"}
VPN_MANUAL_OPTIONAL = ("preshared_key", "keepalive")
# What the operator sees each field called.
_VPN_SHOWN = {"PrivateKey": "the private key", "Address": "the address", "DNS": "the DNS",
              "PublicKey": "the server's public key", "Endpoint": "the endpoint",
              "AllowedIPs": "the allowed addresses", "PresharedKey": "the preshared key",
              "PersistentKeepalive": "the keepalive", "ListenPort": "the listen port",
              "MTU": "the MTU", "EndpointPort": "the endpoint's port"}
# A port and a keepalive interval are 16-bit numbers in WireGuard (the
# kernel's own bounds); an MTU is any positive number the kernel takes (it
# refuses one out of its range when the tunnel is set up, and the status
# says so).
_PORT_MAX = 65535
_KEEPALIVE_MAX = 65535
# A WireGuard key: base64 of exactly 32 bytes.
_WG_KEY_BYTES = 32
_WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")
# The VPN server's name: DNS labels (letters, digits, dashes), or an IPv4
# address.
_ENDPOINT_HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
                               r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
# A line's name as WireGuard spells one (quoted back in a refusal only then).
_VPN_LINE_NAME_RE = re.compile(r"^[A-Za-z]{1,40}$")
VPN_IPV6_NOTE = ("the configuration's IPv6 parts ({parts}) are not used: the search "
                 "service's networks are IPv4 only")
VPN_NO_DNS = ("the configuration names no IPv4 DNS server; a VPN search resolves names "
              "only through the tunnel, so RS needs one (the DNS line your VPN provider "
              "gives)")
VPN_DAMAGED = ("the VPN configuration kept on the host is not one RS wrote, so it was "
               "not used; store it again under VPN")


def _vpn_key_problem(field: str, value: str) -> str | None:
    if not _WG_KEY_RE.match(value):
        return (f"{_VPN_SHOWN[field]} is not a WireGuard key (44 characters of base64 "
                "that stand for 32 bytes)")
    try:
        if len(base64.b64decode(value, validate=True)) != _WG_KEY_BYTES:
            raise ValueError
    except ValueError:
        return (f"{_VPN_SHOWN[field]} is not a WireGuard key (44 characters of base64 "
                "that stand for 32 bytes)")
    return None


def _vpn_items(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _vpn_int(field: str, value: str, low: int, high: int | None) -> int:
    if not value.isascii() or not value.isdigit() or str(int(value)) != value \
            or int(value) < low or (high is not None and int(value) > high):
        bound = f"from {low} to {high}" if high is not None else f"of at least {low}"
        raise SearchError(f"{_VPN_SHOWN[field]} must be a whole number {bound}")
    return int(value)


def _vpn_normalize(fields: dict[str, str]) -> dict[str, Any]:
    """The one normalized configuration from the configuration file's values
    (each a string; a repeated line's values joined by commas). SearchError
    in plain words on the first problem."""
    if not fields.get("DNS"):
        raise SearchError(VPN_NO_DNS)
    missing = [_VPN_SHOWN[k] for k in ("PrivateKey", "Address", "PublicKey",
                                        "Endpoint", "AllowedIPs") if not fields.get(k)]
    if missing:
        raise SearchError("the VPN configuration needs " + ", ".join(missing))
    for k in ("PrivateKey", "PublicKey", "PresharedKey"):
        if fields.get(k):
            problem = _vpn_key_problem(k, fields[k])
            if problem:
                raise SearchError(problem)
    ipv6: list[str] = []
    addresses = []
    for a in _vpn_items(fields["Address"]):
        try:
            iface = ipaddress.ip_interface(a)
        except ValueError:
            raise SearchError(f"the address {a} is not an IP address with its prefix "
                              "(as in 10.64.0.2/32)") from None
        if iface.version == 6:
            ipv6.append(a)
        else:
            addresses.append(str(iface))
    if not addresses:
        raise SearchError("the configuration has no IPv4 address for the tunnel; the "
                          "search service's networks are IPv4 only")
    dns = []
    for d in _vpn_items(fields["DNS"]):
        try:
            ip = ipaddress.ip_address(d)
        except ValueError:
            raise SearchError(f"the DNS entry {d} is not an IP address (RS uses only "
                              "the DNS servers, no search domains)") from None
        if ip.version == 6:
            ipv6.append(d)
        elif str(ip) not in dns:
            dns.append(str(ip))
    if not dns:
        raise SearchError(VPN_NO_DNS)
    allowed = []
    for n in _vpn_items(fields["AllowedIPs"]):
        try:
            net = ipaddress.ip_network(n, strict=False)
        except ValueError:
            raise SearchError(f"the allowed address {n} is not an address range (as in "
                              "0.0.0.0/0)") from None
        if net.version == 6:
            ipv6.append(n)
        elif str(net) not in allowed:
            allowed.append(str(net))
    if not allowed:
        raise SearchError("the allowed addresses hold no IPv4 range, so no search could "
                          "pass the tunnel")
    for d in dns:
        if not any(ipaddress.ip_address(d) in ipaddress.ip_network(n) for n in allowed):
            raise SearchError(f"the DNS server {d} is outside the allowed addresses, so "
                              "it cannot be reached through the tunnel")
    endpoint = fields["Endpoint"]
    if endpoint.startswith("["):
        raise SearchError("the endpoint is an IPv6 address; RS reaches the VPN server "
                          "over IPv4 only (its name or its IPv4 address)")
    host, sep, port = endpoint.rpartition(":")
    if not sep or not _ENDPOINT_HOST_RE.match(host):
        raise SearchError("the endpoint is the VPN server's name or IPv4 address and its "
                          "port, as in vpn.example.net:51820")
    port_n = _vpn_int("EndpointPort", port, 1, _PORT_MAX)
    keepalive = None
    ka = fields.get("PersistentKeepalive", "")
    if ka and ka.lower() != "off":
        keepalive = _vpn_int("PersistentKeepalive", ka, 0, _KEEPALIVE_MAX) or None
    listen = fields.get("ListenPort")
    mtu = fields.get("MTU")
    return {"version": VPN_RECORD_VERSION,
            "private_key": fields["PrivateKey"], "addresses": addresses, "dns": dns,
            "listen_port": _vpn_int("ListenPort", listen, 1, _PORT_MAX) if listen else None,
            "mtu": _vpn_int("MTU", mtu, 1, None) if mtu else None,
            "public_key": fields["PublicKey"], "preshared_key": fields.get("PresharedKey") or None,
            "endpoint_host": host.lower(), "endpoint_port": port_n,
            "allowed_ips": allowed, "keepalive": keepalive, "ipv6_ignored": ipv6}


def parse_vpn_file(text: Any) -> dict[str, Any]:
    """A WireGuard configuration file, as a VPN provider gives it, as the one
    normalized configuration (without the resolved endpoint address).
    SearchError in plain words."""
    if not isinstance(text, str) or not text.strip():
        raise SearchError("the configuration file is empty")
    section = None
    seen: dict[str, int] = {}
    iface: dict[str, list[str]] = {}
    peer: dict[str, list[str]] = {}
    refused: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section not in ("interface", "peer"):
                raise SearchError("the configuration has a section other than [Interface] "
                                  "and [Peer]")
            seen[section] = seen.get(section, 0) + 1
            if seen[section] > 1:
                raise SearchError("RS uses one VPN server: the configuration needs exactly "
                                  "one [Peer] section" if section == "peer" else
                                  "the configuration needs exactly one [Interface] section")
            continue
        key, eq, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not eq or not key:
            # The line itself is never quoted: it could hold a key.
            raise SearchError("the configuration has a line that is neither a setting "
                              "(name = value) nor a section")
        lk = key.lower()
        if lk in _VPN_REFUSED_KEYS:
            if _VPN_REFUSED_KEYS[lk] not in refused:
                refused.append(_VPN_REFUSED_KEYS[lk])
            continue
        if section is None:
            raise SearchError("the configuration has settings before its [Interface] section")
        known = _VPN_INTERFACE_KEYS if section == "interface" else _VPN_PEER_KEYS
        if lk not in known:
            # The name is quoted only when it is a name: a key line that lost
            # its `=` splits at the key's own padding.
            raise SearchError(f"the configuration's [{section.capitalize()}] section has a "
                              "line RS does not use"
                              + (f" ({key}); remove it" if _VPN_LINE_NAME_RE.match(key)
                                 else "; remove it"))
        name = known[lk]
        target = iface if section == "interface" else peer
        if name in target and name not in _VPN_LIST_KEYS:
            raise SearchError(f"the configuration has the {name} line twice")
        target.setdefault(name, []).append(value)
    if refused:
        raise SearchError("RS does not run commands from a VPN configuration: remove the "
                          + ", ".join(refused) + (" lines" if len(refused) > 1 else " line"))
    if seen.get("interface", 0) != 1:
        raise SearchError("the configuration needs exactly one [Interface] section")
    if seen.get("peer", 0) != 1:
        raise SearchError("RS uses one VPN server: the configuration needs exactly one "
                          "[Peer] section")
    fields = {k: ", ".join(v) for k, v in {**iface, **peer}.items()}
    return _vpn_normalize(fields)


def parse_vpn_manual(value: Any) -> dict[str, Any]:
    """The manual form's fields (VPN_MANUAL_FIELDS; each a text, the address,
    DNS and allowed addresses also a list of texts, the keepalive also a
    number) as the one normalized configuration. SearchError in plain words."""
    if not isinstance(value, dict):
        raise SearchError("the VPN fields did not arrive as RS sends them")
    unknown = sorted(set(value) - set(VPN_MANUAL_FIELDS))
    if unknown:
        raise SearchError(f"the VPN fields have one RS does not use ({unknown[0]})")
    fields: dict[str, str] = {}
    for key, line in VPN_MANUAL_FIELDS.items():
        v = value.get(key)
        if v is None or v == "":
            if key in VPN_MANUAL_OPTIONAL or key not in value:
                continue
            v = ""
        if key == "keepalive" and isinstance(v, int) and not isinstance(v, bool):
            v = str(v)
        if isinstance(v, list) and line in _VPN_LIST_KEYS \
                and all(isinstance(x, str) for x in v):
            v = ", ".join(v)
        if not isinstance(v, str):
            raise SearchError(f"{_VPN_SHOWN[line]} must be text")
        if "\n" in v or "\r" in v:
            raise SearchError(f"{_VPN_SHOWN[line]} must be one line")
        if v.strip():
            fields[line] = v.strip()
    return _vpn_normalize(fields)


def vpn_record_problem(data: Any) -> str | None:
    """Why `data` is not a VPN record RS wrote (the normalized configuration
    with its resolved `endpoint_ip`), or None."""
    keys = {"version", "private_key", "addresses", "dns", "listen_port", "mtu", "public_key",
            "preshared_key", "endpoint_host", "endpoint_port", "allowed_ips", "keepalive",
            "ipv6_ignored", "endpoint_ip"}
    if not isinstance(data, dict) or set(data) != keys \
            or data["version"] != VPN_RECORD_VERSION or isinstance(data["version"], bool):
        return "it does not have the expected shape"
    try:
        fields = {"PrivateKey": data["private_key"], "PublicKey": data["public_key"],
                  "Address": ", ".join(data["addresses"]), "DNS": ", ".join(data["dns"]),
                  "AllowedIPs": ", ".join(data["allowed_ips"]),
                  "Endpoint": f"{data['endpoint_host']}:{data['endpoint_port']}"}
        for k, line in (("preshared_key", "PresharedKey"), ("keepalive", "PersistentKeepalive"),
                        ("listen_port", "ListenPort"), ("mtu", "MTU")):
            if data[k] is not None:
                fields[line] = str(data[k])
        again = _vpn_normalize(fields)
        ipaddress.IPv4Address(data["endpoint_ip"])
    except (SearchError, TypeError, ValueError, AttributeError):
        return "its values are not ones RS checked"
    if not (isinstance(data["ipv6_ignored"], list)
            and all(isinstance(x, str) for x in data["ipv6_ignored"])):
        return "it does not have the expected shape"
    if {k: v for k, v in again.items() if k != "ipv6_ignored"} != \
            {k: v for k, v in data.items() if k not in ("ipv6_ignored", "endpoint_ip")}:
        return "its values are not ones RS checked"
    return None


def read_vpn_file(path: Path | None = None) -> dict[str, Any] | None:
    """The stored VPN configuration, or None when none is stored. SearchError
    when the file is there but not one RS wrote."""
    path = VPN_PATH if path is None else path
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise SearchError(VPN_DAMAGED) from None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise SearchError(VPN_DAMAGED) from None
    if vpn_record_problem(data):
        raise SearchError(VPN_DAMAGED)
    return data


def write_vpn_file(record: dict[str, Any], path: Path | None = None) -> None:
    """Raises OSError (the caller words it). 0600, through a fresh temp."""
    _write_atomic(VPN_PATH if path is None else path,
                  json.dumps(record, indent=2, sort_keys=True) + "\n")


def clear_vpn_file(path: Path | None = None) -> None:
    path = VPN_PATH if path is None else path
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def vpn_destination(record: dict[str, Any]) -> tuple[str, int]:
    """The service's router destination for the tunnel: (address, UDP port)."""
    return record["endpoint_ip"], record["endpoint_port"]


def vpn_public(record: dict[str, Any] | None) -> dict[str, Any]:
    """What may be shown of a stored configuration (never a key)."""
    if record is None:
        return {"configured": False, "endpoint": None, "endpoint_ip": None, "address": None,
                "dns": [], "note": None}
    return {"configured": True,
            "endpoint": f"{record['endpoint_host']}:{record['endpoint_port']}",
            "endpoint_ip": record["endpoint_ip"], "address": ", ".join(record["addresses"]),
            "dns": list(record["dns"]),
            "note": (VPN_IPV6_NOTE.format(parts=", ".join(record["ipv6_ignored"]))
                     if record["ipv6_ignored"] else None)}


def vpn_service_record(record: dict[str, Any]) -> dict[str, Any]:
    """What the service is handed: what the gateway applies (the server by
    its resolved address; IPv4 parts only)."""
    return {k: record[k] for k in ("version", "private_key", "addresses", "dns", "listen_port",
                                   "mtu", "public_key", "preshared_key", "endpoint_ip",
                                   "endpoint_port", "allowed_ips", "keepalive")}
