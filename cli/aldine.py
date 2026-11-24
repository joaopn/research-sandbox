"""aldine — the shared Aldine LaTeX editor service: names, pins and pure helpers.

Stdlib only, and it imports nothing from RS (rscore and mcp_servers import it;
the reverse would be circular). Aldine (github.com/trahloff/Aldine, AGPL) is a
self-hosted collaborative LaTeX editor shipped as two prebuilt images: the app
(Node, port 3000) and the compiler (TeX Live, runs untrusted LaTeX). RS runs
ONE Aldine for the whole installation, beside the shared Gitea:

  - ``rs-aldine`` (the app) on ``rs-aldine-front`` — its only network with a
    route out, shared with exactly the MCP gateway and the webui, which reach
    it by name there — and on ``rs-aldine-internal``. It is deliberately NOT
    on ``rs-sandbox``: Aldine runs with its own login off, so its REST API
    answers whoever reaches it, and rs-sandbox also holds the registry, Gitea
    and operator-registered third-party MCP containers;
  - ``rs-aldine-compiler`` ONLY on ``rs-aldine-internal``, an ``--internal``
    docker network (no route out), with upstream's hardening (memory and
    process caps, every capability dropped, no privilege escalation);
  - the papers in the named volumes ``rs-aldine-data`` (both containers) and
    ``rs-aldine-secrets`` (the app only). Turning Aldine off removes the
    containers and the network and keeps both volumes.

Host state lives under ``~/.research-sandbox/aldine/`` (never mounted into a
container): the "on" record, and the app's environment file holding the MCP
token (0600, read only by `docker run --env-file`). The tab record for the
webui lives in the broker's run folder, which the webui mounts read-only.

The lifecycle (on / off / status / resume at `research start`) lives in
rscore; this module decides names, the containers' exact shape, and the
contents of the files.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

# --- names -------------------------------------------------------------------

APP_CONTAINER = "rs-aldine"
COMPILER_CONTAINER = "rs-aldine-compiler"
DATA_VOLUME = "rs-aldine-data"
SECRETS_VOLUME = "rs-aldine-secrets"
# The app<->compiler network. Created with `--internal` and deliberately NOT
# through rscore.network_create_args: that builder names the bridge `rsn…`,
# which the operator's documented host-hardening rule keys on for PROJECT
# bridges; this one is neither a project bridge nor one with a route out.
INTERNAL_NETWORK = "rs-aldine-internal"
# The app's own bridge WITH a route out (Zotero, OpenAlex, venue kits need
# outbound 443). Its only members: the app, the MCP gateway and the webui —
# RS connects those two (rscore.aldine_connect_front) whenever any of the
# three is (re)created. Not through network_create_args either: like
# rs-sandbox, its egress must not match the operator's `rsn` project rule.
FRONT_NETWORK = "rs-aldine-front"

APP_IMAGE_REPO = "ghcr.io/trahloff/aldine-app"
COMPILER_IMAGE_REPO = "ghcr.io/trahloff/aldine-compiler"
# The ports the images listen on (their Dockerfiles' PORT/EXPOSE).
APP_PORT = 3000
COMPILER_PORT = 4020
APP_HEALTH_PATH = "/api/health"
COMPILER_HEALTH_PATH = "/health"
# The app's image defaults COMPILER_URL to http://compiler:4020 (upstream's
# compose service name); RS's compiler has its own container name.
COMPILER_URL = f"http://{COMPILER_CONTAINER}:{COMPILER_PORT}"

# Labels each container carries: what it was created from. A container whose
# labels differ from the pins is replaced at the next on or start (the version
# follows the installation, data kept).
VERSION_LABEL = "research.aldine.version"
DIGEST_LABEL = "research.aldine.digest"

# The MCP gateway entry RS keeps for it (an ordinary entry of the server list;
# RS recognises its own by the address).
MCP_SERVER_NAME = "aldine"
MCP_URL = f"http://{APP_CONTAINER}:{APP_PORT}/mcp"
MCP_DESCRIPTION = "The shared Aldine LaTeX editor (every project's papers)"
# Aldine reads the static token from `Authorization: Bearer …` or this header;
# the gateway strips the caller's Authorization, so the header is the one that
# reaches Aldine.
MCP_TOKEN_HEADER = "X-Aldine-Token"

# --- the compiler's sandbox: upstream's hardening, reproduced exactly ----------
# Upstream's compose (docker-compose.yml, the CasaOS entry) gives the container
# that runs untrusted LaTeX exactly these bounds; RS does not tune them: half
# the memory fails large documents upstream sized for, and the caps are a
# sandbox upstream reasons about as a whole.
COMPILER_MEMORY = "2g"
COMPILER_PIDS_LIMIT = 256

# Both containers come back after a host reboot, as Gitea does; `docker stop`
# (research stop, Turn off) keeps them down.
RESTART_POLICY = "unless-stopped"

# --- host state ----------------------------------------------------------------

STATE_DIR = Path.home() / ".research-sandbox" / "aldine"
# Present = the operator turned Aldine on (from the Sandboxes page or the
# CLI). `research start` resumes Aldine only then — never creates it on its own.
ON_RECORD_PATH = STATE_DIR / "on"
# The app's environment file (0600): the MCP token, generated once and kept
# across off/on so nothing about the connection changes when Aldine comes back.
ENV_FILE_PATH = STATE_DIR / "app.env"
TOKEN_ENV = "ALDINE_MCP_TOKEN"
# 32 random bytes, the same strength as the gateway's consumer tokens
# (mcp_servers.new_token); token_urlsafe gives 43 characters of [A-Za-z0-9_-],
# which an env file and an HTTP header carry literally.
TOKEN_BYTES = 32
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

# The webui's tab record: in the broker's run folder (the only part of the
# host state the webui mounts, read-only), never under a project folder
# (project folders are written by the projects' agents and are untrusted).
# MIRROR of broker.BROKER_RUN_DIR (the folder compose mounts as RS_BROKER_DIR;
# this module cannot import broker) — pinned equal by the unit test.
RUN_DIR = Path.home() / ".research-sandbox" / "run"
TAB_RECORD_PATH = RUN_DIR / "aldine.json"
# The tab record's shape (for the webui; version bumps on any change):
#   {"version": 1,
#    "on": true | false,            # the operator turned Aldine on
#    "projects": ["p1", "p2", ...]} # sorted; each project with at least one
#                                   # consumer (agent, box or workers) wired
#                                   # to the gateway server named "aldine"
# Written by RS only, atomically (temp + rename), whenever the MCP wiring
# table is rendered and at Aldine on / off / `research start`.
TAB_RECORD_VERSION = 1

# --- pins ----------------------------------------------------------------------

VERSION_KEY = "ALDINE_VERSION"
APP_DIGEST_KEY = "ALDINE_APP_DIGEST"
COMPILER_DIGEST_KEY = "ALDINE_COMPILER_DIGEST"
# Dotted digits only: the version becomes a container label.
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PIN_BAD = ("the Aldine version setting ({key}) is {what}; under Sandboxes → "
            "Software, Use repo version on its row puts the value this "
            "installation ships back")


class AldineError(Exception):
    """A refusal whose text is safe to show in the browser: it names a setting
    or a page, never a host path, a command or a secret."""


def pins_from(versions: dict[str, str]) -> dict[str, str]:
    """{version, app_digest, compiler_digest} from the effective version pins,
    each checked. AldineError naming the key on a missing or malformed one."""
    out = {}
    for key, field_name, rx, what in (
            (VERSION_KEY, "version", _VERSION_RE, "not a version number"),
            (APP_DIGEST_KEY, "app_digest", _DIGEST_RE, "not an image digest"),
            (COMPILER_DIGEST_KEY, "compiler_digest", _DIGEST_RE, "not an image digest")):
        value = versions.get(key, "")
        if not value:
            raise AldineError(_PIN_BAD.format(key=key, what="missing"))
        if not rx.match(value):
            raise AldineError(_PIN_BAD.format(key=key, what=what))
        out[field_name] = value
    return out


def app_image(pins: dict[str, str]) -> str:
    """The app image by digest: a re-pointed tag is never used."""
    return f"{APP_IMAGE_REPO}@{pins['app_digest']}"


def compiler_image(pins: dict[str, str]) -> str:
    return f"{COMPILER_IMAGE_REPO}@{pins['compiler_digest']}"


def wanted_labels(pins: dict[str, str], role: str) -> dict[str, str]:
    """The labels a current container of `role` ("app" | "compiler") carries."""
    digest = pins["app_digest"] if role == "app" else pins["compiler_digest"]
    return {VERSION_LABEL: pins["version"], DIGEST_LABEL: digest}


def is_current(labels: Any, pins: dict[str, str], role: str) -> bool:
    """Whether a container with these labels runs what the pins ask for."""
    if not isinstance(labels, dict):
        return False
    return all(labels.get(k) == v for k, v in wanted_labels(pins, role).items())


# --- docker argv (pure) ----------------------------------------------------------

def network_create_argv() -> list[str]:
    return ["docker", "network", "create", "--internal", INTERNAL_NETWORK]


def front_network_create_argv() -> list[str]:
    """The app's bridge with a route out: a plain bridge, no `--internal`."""
    return ["docker", "network", "create", FRONT_NETWORK]


def compiler_run_argv(pins: dict[str, str]) -> list[str]:
    """The compiler: on the internal network ONLY, upstream's hardening."""
    argv = ["docker", "run", "-d",
            "--name", COMPILER_CONTAINER,
            "--network", INTERNAL_NETWORK,
            "--restart", RESTART_POLICY,
            "--init",
            "--memory", COMPILER_MEMORY,
            "--pids-limit", str(COMPILER_PIDS_LIMIT),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges"]
    for k, v in wanted_labels(pins, "compiler").items():
        argv += ["--label", f"{k}={v}"]
    argv += ["-v", f"{DATA_VOLUME}:/data", compiler_image(pins)]
    return argv


def app_run_argv(pins: dict[str, str], env_file: Path) -> list[str]:
    """The app: on its front network (its only route out; the internal one
    has no gateway, so the default route is the front's) and on the internal
    network — never on rs-sandbox. The token arrives through the 0600 env
    file, never on this command line. No published host port: the MCP gateway
    and the webui reach it by name on the front network."""
    argv = ["docker", "run", "-d",
            "--name", APP_CONTAINER,
            "--network", FRONT_NETWORK,
            "--network", INTERNAL_NETWORK,
            "--restart", RESTART_POLICY,
            "--init"]
    for k, v in wanted_labels(pins, "app").items():
        argv += ["--label", f"{k}={v}"]
    argv += ["-v", f"{DATA_VOLUME}:/data",
             "-v", f"{SECRETS_VOLUME}:/secrets",
             "-e", "ALDINE_MCP=1",
             "-e", f"COMPILER_URL={COMPILER_URL}",
             "--env-file", str(env_file),
             app_image(pins)]
    return argv


def health_probe_argv(url: str, timeout_s: int) -> list[str]:
    """Fetch `url` from inside the app container (busybox wget in its image):
    the app's own health from 127.0.0.1, the compiler's across the internal
    network."""
    return ["docker", "exec", APP_CONTAINER, "wget", "-q", "-T", str(timeout_s),
            "-O", "-", url]


APP_HEALTH_URL = f"http://127.0.0.1:{APP_PORT}{APP_HEALTH_PATH}"
COMPILER_HEALTH_URL = f"{COMPILER_URL}{COMPILER_HEALTH_PATH}"


def health_ok(body: str, *, app: bool) -> bool:
    """Whether a health answer says healthy: the app answers
    {"ok": true, "name": "aldine"}, the compiler {"ok": true, ...}."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return False
    if not isinstance(data, dict) or data.get("ok") is not True:
        return False
    return data.get("name") == "aldine" if app else True


# --- the MCP gateway entry (pure) --------------------------------------------------

CONFLICT_TEXT = (
    "the MCP server list already has a server named {name} at another address, "
    "so Aldine cannot be added under that name; rename or remove that server "
    "under Sandboxes → MCP and turn Aldine on again")


def apply_gateway_entry(data: dict[str, Any], token: str) -> None:
    """Put RS's Aldine entry into a loaded server list (in place). An entry
    named `aldine` at RS's Aldine address gets the current token header and is
    otherwise kept; one at any other address is a conflict (AldineError) and
    the list is left untouched."""
    servers = data["servers"]
    entry = servers.get(MCP_SERVER_NAME)
    if entry is not None and entry.get("url") != MCP_URL:
        raise AldineError(CONFLICT_TEXT.format(name=MCP_SERVER_NAME))
    if entry is None:
        servers[MCP_SERVER_NAME] = {"url": MCP_URL, "description": MCP_DESCRIPTION,
                                    "headers": {MCP_TOKEN_HEADER: token}}
        return
    headers = {h: v for h, v in (entry.get("headers") or {}).items()
               if h.lower() != MCP_TOKEN_HEADER.lower()}
    headers[MCP_TOKEN_HEADER] = token
    entry["headers"] = headers


def gateway_entry_conflict(data: dict[str, Any]) -> bool:
    """True when the list holds an `aldine` entry that is not RS's."""
    entry = data["servers"].get(MCP_SERVER_NAME)
    return entry is not None and entry.get("url") != MCP_URL


# --- host files ------------------------------------------------------------------

def _write_atomic(target: Path, text: str, *, dir_mode: int = 0o700) -> None:
    """Write through a fresh 0600 temp + rename (never opens an existing name)."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
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


_TOKEN_DAMAGED = ("the Aldine access key kept on the host is not one RS wrote, "
                  "so it was not used; nothing was changed")


def read_token(path: Path | None = None) -> str | None:
    """The MCP token from the env file, or None when there is none yet.
    AldineError when the file holds anything but the one line RS writes."""
    path = ENV_FILE_PATH if path is None else path
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise AldineError(_TOKEN_DAMAGED) from None
    key, sep, value = text.rstrip("\n").partition("=")
    if key != TOKEN_ENV or not sep or not _TOKEN_RE.match(value) or "\n" in value:
        raise AldineError(_TOKEN_DAMAGED)
    return value


def ensure_token(path: Path | None = None) -> str:
    """The MCP token, generated and written (0600) the first time."""
    path = ENV_FILE_PATH if path is None else path
    token = read_token(path)
    if token is None:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        _write_atomic(path, f"{TOKEN_ENV}={token}\n")
    return token


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


def wired_projects(grants: dict[str, Any]) -> list[str]:
    """Projects with at least one consumer wired to the `aldine` server, from
    a wiring table (mcp_servers' grants shape), sorted."""
    out = []
    for project, row in (grants.get("projects") or {}).items():
        if any(MCP_SERVER_NAME in (c.get("servers") or [])
               for c in (row.get("consumers") or {}).values()):
            out.append(project)
    return sorted(out)


def tab_record(grants: dict[str, Any], on: bool) -> dict[str, Any]:
    return {"version": TAB_RECORD_VERSION, "on": bool(on),
            "projects": wired_projects(grants)}


def write_tab_record(grants: dict[str, Any], *, on: bool | None = None,
                     path: Path | None = None) -> None:
    """Write the webui's tab record (see TAB_RECORD_PATH). `on` defaults to the
    on record. Raises OSError on a write failure (the caller words it)."""
    path = TAB_RECORD_PATH if path is None else path
    record = tab_record(grants, is_on() if on is None else on)
    # The run folder is the broker's (0700 or as the broker made it); only
    # create it when absent, never change its mode.
    _write_atomic(path, json.dumps(record, indent=2, sort_keys=True) + "\n",
                  dir_mode=0o700)
