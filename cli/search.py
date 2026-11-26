"""search — RS's search service: names, pins, settings and pure helpers.

Stdlib only, and it imports nothing from RS (rscore and mcp_servers import it;
the reverse would be circular). RS runs ONE search service for the whole
installation, turned on by the operator (Services → Search):

  - ``rs-search``, a container of the inner-Docker image projects use
    (rs-sandbox-dind) plus the service program (container/search/). Its inner
    Docker keeps its images on the named volume ``rs-search-docker``, so the
    search images are staged into it once (at Turn on, and again only when
    the host's image changes) and survive a restart and a re-create;
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
MCP gateway server ``search``; the gateway passes the caller's project and
consumer on to it (CALLER_HEADER) and to no other server.

Host state lives under ``~/.research-sandbox/search/`` (never mounted into a
container): the "on" record, the searcher settings, and the long-lived Claude
token (0600). The settings and the token reach the service over a `docker
exec` stdin, into a tmpfs only root in the service reads, at every start and
at every change.

The lifecycle (on / off / status / resume at `research start`) lives in
rscore; this module decides names, the container's exact shape, and the
contents of the files.
"""

from __future__ import annotations

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
# The service's inner Docker store (images, search volumes): kept at Turn off,
# so turning the service on again does not stage the images again.
DOCKER_VOLUME = "rs-search-docker"
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

# The caller-facing server (the MCP endpoint and the file route below it).
SERVICE_PORT = 8080
MCP_PATH = "/mcp"

# The MCP gateway entry RS keeps for it (an ordinary entry of the server list;
# RS recognises its own by the address, as it does Aldine's).
MCP_SERVER_NAME = "search"
MCP_URL = f"http://{SERVICE_CONTAINER}:{SERVICE_PORT}{MCP_PATH}"
MCP_DESCRIPTION = ("RS's search service: hand over a question, collect a "
                   "written answer with its sources and files")
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
# Serializes the two writers of the host files (settings, token) between the
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
# Unpaywall requires an email address and Crossref's polite pool asks for
# one. When the operator leaves the contact empty, the requests carry this
# generic address (a reserved documentation domain: no person behind it).
GENERIC_CONTACT = "research-sandbox-search@example.org"
_CONTACT_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


class SearchError(Exception):
    """A refusal whose text is safe to show in the browser: it names a setting
    or a page, never a host path, a host command or a secret."""


# --- pins ----------------------------------------------------------------------

def pins_from(versions: dict[str, str]) -> dict[str, str]:
    """{searxng_version, searxng_digest} from the effective version pins, each
    checked. SearchError naming the key on a missing or malformed one."""
    out = {}
    for key, field_name, rx, what in (
            (SEARXNG_VERSION_KEY, "searxng_version", _SEARXNG_VERSION_RE,
             "not a SearXNG version"),
            (SEARXNG_DIGEST_KEY, "searxng_digest", _DIGEST_RE, "not an image digest")):
        value = versions.get(key, "")
        if not value:
            raise SearchError(_PIN_BAD.format(key=key, what="missing"))
        if not rx.match(value):
            raise SearchError(_PIN_BAD.format(key=key, what=what))
        out[field_name] = value
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
             "--tmpfs", "/workspace",
             "--tmpfs", f"{SERVICE_RUN_DIR}:mode=0700",
             "-v", f"{agent_dist_dir}:{SERVICE_AGENT_DIST}:ro",
             "-e", "DOCKER_DIND=true",
             "-e", f"RS_SEARCH_AGENT_IMAGE={AGENT_IMAGE}",
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


def is_rs_entry(name: str, entry: Any) -> bool:
    """Whether (name, entry) of the server list is RS's own search entry: the
    name, an address entry (not a server RS runs) at exactly RS's address."""
    return (name == MCP_SERVER_NAME and isinstance(entry, dict)
            and "run" not in entry and entry.get("url") == MCP_URL)


def gateway_entry_conflict(data: dict[str, Any]) -> bool:
    """True when the list holds a `search` entry that is not RS's."""
    entry = data["servers"].get(MCP_SERVER_NAME)
    return entry is not None and not is_rs_entry(MCP_SERVER_NAME, entry)


def apply_gateway_entry(data: dict[str, Any]) -> None:
    """Put RS's search entry into a loaded server list (in place). RS's own
    entry is kept as it is; any other `search` is a conflict (SearchError)
    and the list is left untouched."""
    if gateway_entry_conflict(data):
        raise SearchError(CONFLICT_TEXT.format(name=MCP_SERVER_NAME))
    data["servers"].setdefault(MCP_SERVER_NAME, {"url": MCP_URL,
                                                 "description": MCP_DESCRIPTION})


# --- settings (pure) -----------------------------------------------------------------

def default_settings() -> dict[str, Any]:
    return {"version": SETTINGS_VERSION,
            "searcher": {"name": DEFAULT_SEARCHER_NAME, "model": DEFAULT_MODEL,
                         "effort": DEFAULT_EFFORT, "at_once": DEFAULT_AT_ONCE},
            "contact": ""}


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
    not import the model catalog)."""
    if not isinstance(data, dict) or set(data) != {"version", "searcher", "contact"}:
        return "it does not have the expected shape"
    if data["version"] != SETTINGS_VERSION or isinstance(data["version"], bool):
        return "it was written by another version of RS"
    s = data["searcher"]
    if not isinstance(s, dict) or set(s) != {"name", "model", "effort", "at_once"}:
        return "its searcher does not have the expected shape"
    for problem in (searcher_name_problem(s["name"]), at_once_problem(s["at_once"]),
                    contact_problem(data["contact"])):
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
    return data


def service_settings(data: dict[str, Any]) -> dict[str, Any]:
    """What the service is handed: the settings with the contact filled."""
    return {"version": SETTINGS_VERSION, "searcher": dict(data["searcher"]),
            "contact": data["contact"] or GENERIC_CONTACT}


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
