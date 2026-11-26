"""mcp_run — MCP servers RS runs itself, from a container image or from an npm
or PyPI package: names, shapes, the settings file and the docker argv (pure
helpers).

Stdlib only, and it imports nothing from RS (mcp_servers and rscore import
it). A server RS runs is an entry of the gateway's server list
(cli/mcp_servers.py) carrying a ``run`` object instead of a typed address:

  - its container ``rs-mcpsrv-<name>`` runs the image pinned by content digest
    at Add (or Update), with every capability dropped, no privilege
    escalation, no published port and no host folder — only the optional named
    volume ``rs-mcpsrv-<name>-data`` at the operator's data folder;
  - it sits on a network of its own, ``rs-mcpsrv-<name>``, created
    ``--internal`` (no route out until RS injects one through the router), and
    shared only with the MCP gateway and RS's egress router;
  - its settings (environment values, secret or not) reach it ONLY through
    ``docker run --env-file`` from a 0600 file RS writes under
    ``~/.research-sandbox/mcp-run/`` — never a command line, never the folder
    the gateway mounts, never a project;
  - its gateway address is derived, never typed:
    ``http://rs-mcpsrv-<name>:<port><path>``.

A server RS runs from a PACKAGE (``run.source == "package"``) is the same
container, network, settings file and hardening, with RS's own image
``rs-mcp-runner`` in place of the operator's: the package is installed ONCE
(at Add, Edit of its version and Update) by a one-shot install container that
receives no setting and no secret, into a package volume of its own
(``rs-mcpsrv-<name>-pkg-<n>``, a fresh one per install), and the server
container runs the installed command from that volume (mounted read-only)
behind RS's bridge on RS's own port and path. Nothing is fetched at start.

The lifecycle (bring up, stop, remove, the start/stop hooks) lives in rscore;
this module decides names, shapes, the file and the argv.

Refusal texts reach the browser verbatim: they name a field or a setting NAME,
never a value, a host path or a command.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import posixpath
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

# --- names -------------------------------------------------------------------

# Container, network and volume names share the prefix. It is outside both the
# older shared servers' `rs-mcp-<name>` namespace and the gateway's `rs-mcpgw`.
PREFIX = "rs-mcpsrv-"
VOLUME_SUFFIX = "-data"
# Every container, network and volume RS makes for a run server carries both:
# the fleet stop finds the containers by the first (no list read), and the
# second says which server a found object belongs to. Deliberately NOT the old
# `research.mcp` label, which the older shared-server sweep keys on.
RUN_LABEL = "research.mcp-run"
NAME_LABEL = "research.mcp-run-name"

# "no", deliberately: a container Docker restarts by itself (a crash, a daemon
# or host restart) comes back without the default route RS injects, so it
# would read "running" while it reaches nothing. Left stopped, its row shows
# the exit and its reason; Start, Restart or `research start` bring it back
# through _mcp_run_up, which always injects the route.
RESTART_POLICY = "no"


def container_name(name: str) -> str:
    return PREFIX + name


def network_name(name: str) -> str:
    return PREFIX + name


def volume_name(name: str) -> str:
    return PREFIX + name + VOLUME_SUFFIX


def url_for(name: str, run: dict[str, Any]) -> str:
    """The address the gateway forwards to: the container by name on the
    server's own network (a package server: the bridge's own port and path)."""
    if is_package(run):
        return f"http://{container_name(name)}:{BRIDGE_PORT}{BRIDGE_PATH}"
    return f"http://{container_name(name)}:{run['port']}{run['path']}"


# --- a server RS runs from a package --------------------------------------------

SOURCE_KEY = "source"
# An image entry carries no `source` (its shape is PR 1's, unchanged); a
# package entry carries this value.
SOURCE_PACKAGE = "package"
REGISTRIES = ("npm", "pypi")
# RS's image for package servers (agent/Dockerfile.mcp-runner). Not in the
# fleet's build specs: built where it is used, like the gateway's.
RUNNER_IMAGE = "rs-mcp-runner:latest"
RUNNER_DOCKERFILE = "Dockerfile.mcp-runner"
# Inside the runner (LOCKSTEP with container/mcp-runner/*.py): where the
# package volume is mounted, where each registry's install puts its commands,
# and the two programs.
PKG_MOUNT = "/rs/pkg"
_COMMAND_DIRS = {"npm": PKG_MOUNT + "/node_modules/.bin", "pypi": PKG_MOUNT + "/venv/bin"}
INSTALL_PROGRAM = "/opt/rs-mcp/install.py"
SERVE_PROGRAM = "/opt/rs-mcp/serve.py"
# The install container idles while RS gives it its route out, then the
# install runs in it by `docker exec` (its output streams into the log).
INSTALL_IDLE = ["sleep", "infinity"]
# `_` cannot be in a server name, so no server's container is ever named so.
INSTALL_SUFFIX = "_install"
PKG_VOLUME_INFIX = "-pkg-"
# The bridge's port and path: RS's own, the same for every package server
# (each is alone in its container), so they are not on the list.
BRIDGE_PORT = 8000
BRIDGE_PATH = "/mcp"
# How long the bridge keeps a session (and its server process) that nobody
# uses. Claude Code never ends its sessions (measured: no DELETE at the end
# of a session), so every agent session leaves one process — tens of MB for
# a Python server — until this runs out; an expired session costs the next
# call one re-initialize, which Claude Code does by itself (measured: a 404
# for its session makes it initialize again and retry). At half, an operator
# pausing for five minutes pays one process start (seconds) more often; at ten
# times, a busy project's short agent sessions keep ten times as many idle
# processes (and their memory) for over an hour and a half.
BRIDGE_SESSION_IDLE_S = 600

# npm's package-name rules (validate-npm-package-name): optional @scope/,
# lowercase, URL-safe characters, at most 214 characters. The first character
# of the scope and of the name is a letter or digit here (npm also allows `-`,
# `~`, which could read as an option or a path).
NPM_NAME_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$", re.ASCII)
NPM_NAME_MAX = 214
# semver 2.0.0's own grammar (semver.org): MAJOR.MINOR.PATCH, an optional
# pre-release and build — nothing else (no range, no tag, no x, no v).
_SEMVER_ID = r"(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
NPM_VERSION_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    rf"(?:-{_SEMVER_ID}(?:\.{_SEMVER_ID})*)?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$", re.ASCII)
# PEP 508's distribution name (case-insensitive): letters and digits, with
# . _ - only inside.
PYPI_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$", re.ASCII)
# PEP 440's version grammar (the packaging library's VERSION_PATTERN), as one
# exact version: no operator, no wildcard, no spaces.
PYPI_VERSION_RE = re.compile(r"""^
    v?
    (?:[0-9]+!)?                                   # epoch
    [0-9]+(?:\.[0-9]+)*                            # release
    (?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?[0-9]*)?   # pre
    (?:-[0-9]+|[-_.]?(?:post|rev|r)[-_.]?[0-9]*)?   # post
    (?:[-_.]?dev[-_.]?[0-9]*)?                      # dev
    (?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?            # local
$""", re.ASCII | re.IGNORECASE | re.VERBOSE)
# The program a package installs: a file name (it is looked up only in the
# package's own command folder), never a path or an option.
COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$", re.ASCII)
_RUNNER_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$", re.ASCII)
PACKAGE_KEYS = frozenset({SOURCE_KEY, "registry", "package", "version", "command",
                          "args", "data", "runner_id", "install", "settings",
                          "destinations", "resolved", "stopped"})
PACKAGE_REQUIRED = PACKAGE_KEYS - {"data"}


def is_package(run: dict[str, Any]) -> bool:
    return run.get(SOURCE_KEY) == SOURCE_PACKAGE


def install_container_name(name: str) -> str:
    return PREFIX + name + INSTALL_SUFFIX


def pkg_volume_name(name: str, install: int) -> str:
    return f"{PREFIX}{name}{PKG_VOLUME_INFIX}{install}"


def is_pkg_volume_of(name: str, volume: str) -> bool:
    """Whether `volume` is one of the package volumes RS names for `name`."""
    head = PREFIX + name + PKG_VOLUME_INFIX
    n = volume[len(head):]
    return volume.startswith(head) and n.isascii() and n.isdigit() \
        and not n.startswith("0")


def command_path(run: dict[str, Any]) -> str:
    """Where the package's command is inside the runner."""
    return f"{_COMMAND_DIRS[run['registry']]}/{run['command']}"


def registry_problem(registry: Any) -> str | None:
    if registry not in REGISTRIES:
        return "the registry must be npm or pypi"
    return None


def package_problem(registry: str, package: Any) -> str | None:
    """A registry's own package-name grammar: no flag, URL, path or VCS spec
    can be one."""
    if registry == "npm":
        if not isinstance(package, str) or not NPM_NAME_RE.fullmatch(package) \
                or len(package) > NPM_NAME_MAX:
            return ("the package must be an npm package name, as in "
                    "@scope/server or server (lowercase, no spaces, no path or address)")
    elif not isinstance(package, str) or not PYPI_NAME_RE.fullmatch(package):
        return ("the package must be a PyPI project name, as in mcp-server-fetch "
                "(letters, digits, . _ -, no path or address)")
    return None


def version_problem(registry: str, version: Any) -> str | None:
    """Exactly one version: never a range, a tag or a wildcard (the no-drift
    rule: what Add installs is what every Update installs again)."""
    if registry == "npm":
        if not isinstance(version, str) or not NPM_VERSION_RE.fullmatch(version):
            return ("the version must be one exact npm version, as in 1.2.3 "
                    "(no range such as ^1.2 or ~1.2, no tag such as latest)")
    elif not isinstance(version, str) or not PYPI_VERSION_RE.fullmatch(version):
        return ("the version must be one exact PyPI version, as in 1.2.3 "
                "(no operator such as >= or ~=, no wildcard)")
    return None


def command_problem(command: Any) -> str | None:
    if not isinstance(command, str) or not COMMAND_RE.fullmatch(command):
        return ("the command must be the name of the program the package "
                "installs, as in mcp-server-fetch (no path, no spaces)")
    return None


# What identifies the runner image a package was installed with: a digest of
# its layers and its configuration, computed by RS from `docker image
# inspect`. Not the image Id: on Docker's containerd image store (the default
# since Engine 29) every build mints a new Id even when nothing changed, and
# the old one is deleted once the tag moves (both measured) — `research
# start` rebuilds the runner (cached) every time, so an Id would refuse every
# package server after every start.
RUNNER_INSPECT_FORMAT = "{{.Id}}|{{json .RootFS.Layers}}|{{json .Config}}"


def runner_identity(inspect_out: str) -> tuple[str, str]:
    """(the image Id to run, the content identity to record and compare) from
    `docker image inspect -f RUNNER_INSPECT_FORMAT`; ("", "") when unusable."""
    image_id, sep, content = inspect_out.strip().partition("|")
    if not sep or not _RUNNER_ID_RE.fullmatch(image_id) or not content:
        return "", ""
    return image_id, "sha256:" + hashlib.sha256(content.encode()).hexdigest()


def runner_id_problem(runner_id: Any) -> str | None:
    if not isinstance(runner_id, str) or not _RUNNER_ID_RE.fullmatch(runner_id):
        return "the recorded image of RS's package runner is not one RS wrote"
    return None


def install_problem(install: Any) -> str | None:
    if isinstance(install, bool) or not isinstance(install, int) or install < 1:
        return "the recorded install of the package is not one RS wrote"
    return None


# --- host state ------------------------------------------------------------------

STATE_DIR = Path.home() / ".research-sandbox"
# One settings file per server, OUTSIDE mcp-gateway/ (the folder the gateway
# container mounts): no setting value may ever land where a container reads.
ENV_DIR = STATE_DIR / "mcp-run"
ENV_SUFFIX = ".env"


def env_path(name: str) -> Path:
    return ENV_DIR / (name + ENV_SUFFIX)


class RunError(Exception):
    """A refusal whose text is safe to show in the browser."""


# --- validation ---------------------------------------------------------------

# The environment names a POSIX shell and docker's env-file both accept.
SETTING_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$", re.ASCII)
# Docker's reference grammar (distribution/reference): an optional registry
# host[:port]/, lowercase path components joined by `/`, an optional :tag and
# an optional @sha256 digest. Nothing in it can start with `-` or hold
# whitespace, so a reference can never be read as a docker option.
_DOMAIN_COMPONENT = r"(?:[a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9])"
_DOMAIN = rf"{_DOMAIN_COMPONENT}(?:\.{_DOMAIN_COMPONENT})*(?::[0-9]+)?"
_PATH_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
_TAG = r"[\w][\w.-]{0,127}"
IMAGE_RE = re.compile(
    rf"^(?:{_DOMAIN}/)?{_PATH_COMPONENT}(?:/{_PATH_COMPONENT})*"
    rf"(?::{_TAG})?(?:@sha256:[0-9a-f]{{64}})?$", re.ASCII)
# What a server is run from: `<repository>@sha256:<64 hex>` (a registry
# image's content digest) or `sha256:<64 hex>` (the Id of an image that exists
# only on this host).
_HEX64 = r"[0-9a-f]{64}"
PINNED_RE = re.compile(
    rf"^(?:(?:{_DOMAIN}/)?{_PATH_COMPONENT}(?:/{_PATH_COMPONENT})*@)?sha256:{_HEX64}$",
    re.ASCII)
# A destination host: an IPv4 literal or a DNS name (letters, digits, dots
# and dashes; no leading or trailing dot or dash).
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
RUN_KEYS = frozenset({"image", "digest", "args", "port", "path", "data",
                      "settings", "destinations", "resolved", "stopped"})
RUN_REQUIRED = RUN_KEYS - {"data"}


def image_problem(ref: Any) -> str | None:
    if not isinstance(ref, str) or not IMAGE_RE.fullmatch(ref):
        return ("the image must be a container image reference, as in "
                "ghcr.io/owner/server:1.2 (no spaces)")
    return None


def pinned_problem(pinned: Any) -> str | None:
    if not isinstance(pinned, str) or not PINNED_RE.fullmatch(pinned):
        return "the image's recorded digest is not one RS wrote"
    return None


def args_problem(args: Any) -> str | None:
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return "the arguments must be a list of words"
    if any("\0" in a for a in args):
        return "an argument may not hold a NUL character"
    return None


def port_problem(port: Any) -> str | None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return "the port must be a number from 1 to 65535"
    return None


def path_problem(path: Any) -> str | None:
    """The URL path the server serves MCP on: plain ASCII, no spaces, no
    query or fragment (the gateway appends nothing to it but the caller's
    rest)."""
    if not isinstance(path, str) or not path.startswith("/"):
        return "the path must start with /"
    if any(ord(c) < 0x21 or ord(c) >= 0x7f for c in path) or "?" in path or "#" in path:
        return ("the path may hold only plain ASCII characters without spaces, "
                "? or #")
    return None


def data_problem(data: Any) -> str | None:
    """The folder INSIDE the container where the server's named volume is
    mounted. Never a host folder: it is only ever the target of `-v
    <volume>:<data>`, so `:` and `,` (which would change that option's
    meaning) are refused, as is anything not in its plain absolute form."""
    if not isinstance(data, str) or not data.startswith("/") or data == "/":
        return "the data folder must be an absolute folder inside the server, as in /data"
    if any(c in data for c in ":,\0\r\n") or posixpath.normpath(data) != data \
            or data.startswith("//"):
        return ("the data folder must be written plainly (no :, no commas, no "
                ".. or doubled /)")
    return None


def setting_value_problem(name: str, value: Any) -> str | None:
    """A value docker's env-file carries literally: one line, no NUL."""
    if not isinstance(value, str):
        return f"the setting {name} needs a text value"
    if any(c in value for c in "\r\n\0"):
        return f"the value of the setting {name} must be a single line"
    return None


def setting_name_problem(name: Any) -> str | None:
    if not isinstance(name, str) or not SETTING_NAME_RE.fullmatch(name):
        return ("a setting name starts with a letter or _ and holds only "
                "letters, digits and _")
    return None


def settings_problem(settings: Any) -> str | None:
    """The list's record of the settings: names, secret flags, and the value
    of each setting that is not secret."""
    if not isinstance(settings, dict):
        return "the settings must be an object"
    for sn, spec in settings.items():
        problem = setting_name_problem(sn)
        if problem:
            return problem
        if not isinstance(spec, dict) or not isinstance(spec.get("secret"), bool):
            return f"the setting {sn} must say whether it is secret"
        if spec["secret"]:
            if set(spec) != {"secret"}:
                return f"the secret setting {sn} must not be kept on the list"
        else:
            if set(spec) != {"secret", "value"}:
                return f"the setting {sn} needs a value"
            problem = setting_value_problem(sn, spec["value"])
            if problem:
                return problem
    return None


def split_destination(dest: Any) -> tuple[str, int] | None:
    """(host, port) of a `host:port` destination, or None."""
    if not isinstance(dest, str):
        return None
    host, sep, port = dest.rpartition(":")
    if not sep or not _HOST_RE.fullmatch(host) or not port.isascii() or not port.isdigit():
        return None
    n = int(port)
    if not 1 <= n <= 65535 or str(n) != port:
        return None
    return host, n


def destinations_problem(dests: Any) -> str | None:
    if not isinstance(dests, list):
        return "the destinations must be a list of host:port"
    for d in dests:
        if split_destination(d) is None:
            return ("a destination is a host name or IPv4 address and a port, "
                    "as in db.lab.example:5432")
    if len(set(dests)) != len(dests):
        return "a destination is named twice"
    return None


def resolved_problem(dests: list, resolved: Any) -> str | None:
    if not isinstance(resolved, dict) or set(resolved) != set(dests):
        return "the destinations' recorded addresses do not match them"
    for ip in resolved.values():
        try:
            ipaddress.IPv4Address(ip)
        except (ValueError, TypeError):
            return "a destination's recorded address is not an IPv4 address"
    return None


def run_problem(run: Any) -> str | None:
    """Why `run` is not a valid run object, or None (input and file alike)."""
    if not isinstance(run, dict):
        return "a server RS runs must be described by an object"
    if SOURCE_KEY in run:
        return package_run_problem(run)
    if set(run) - RUN_KEYS or RUN_REQUIRED - set(run):
        return "a server RS runs holds an unknown or a missing field"
    for problem in (image_problem(run["image"]), pinned_problem(run["digest"]),
                    args_problem(run["args"]), port_problem(run["port"]),
                    path_problem(run["path"]),
                    data_problem(run["data"]) if "data" in run else None,
                    settings_problem(run["settings"]),
                    destinations_problem(run["destinations"])):
        if problem:
            return problem
    problem = resolved_problem(run["destinations"], run["resolved"])
    if problem:
        return problem
    if not isinstance(run["stopped"], bool):
        return "a server RS runs must say whether it was stopped"
    return None


def package_run_problem(run: dict) -> str | None:
    """Why a package server's run object is not valid, or None."""
    if run.get(SOURCE_KEY) != SOURCE_PACKAGE:
        return "a server RS runs comes from an image or a package"
    if set(run) - PACKAGE_KEYS or PACKAGE_REQUIRED - set(run):
        return "a server RS runs holds an unknown or a missing field"
    problem = registry_problem(run["registry"])
    if problem:
        return problem
    for problem in (package_problem(run["registry"], run["package"]),
                    version_problem(run["registry"], run["version"]),
                    command_problem(run["command"]), args_problem(run["args"]),
                    data_problem(run["data"]) if "data" in run else None,
                    runner_id_problem(run["runner_id"]),
                    install_problem(run["install"]),
                    settings_problem(run["settings"]),
                    destinations_problem(run["destinations"])):
        if problem:
            return problem
    problem = resolved_problem(run["destinations"], run["resolved"])
    if problem:
        return problem
    if not isinstance(run["stopped"], bool):
        return "a server RS runs must say whether it was stopped"
    return None


# --- the settings file ----------------------------------------------------------

_ENV_UNUSABLE = ("the settings file kept on the host for the server {name} is "
                 "not as RS wrote it ({why}), so it was not used; remove the "
                 "server and add it again")


def render_env(values: dict[str, str]) -> str:
    """docker's env-file format: one NAME=VALUE per line, read literally (no
    quotes, no `$` expansion)."""
    for sn, value in values.items():
        problem = setting_name_problem(sn) or setting_value_problem(sn, value)
        if problem:
            raise RunError(problem)
    return "".join(f"{sn}={values[sn]}\n" for sn in sorted(values))


def write_env(name: str, values: dict[str, str]) -> None:
    """Write the server's settings file: 0600 in a 0700 folder, through a fresh
    temp file and a rename (never opens an existing name). OSError on a write
    failure (the caller words it)."""
    text = render_env(values)
    ENV_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ENV_DIR, 0o700)
    target = env_path(name)
    fd, tmp = tempfile.mkstemp(dir=ENV_DIR, prefix=target.name + ".")
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


def read_env(name: str) -> dict[str, str] | None:
    """The server's settings as RS wrote them, or None when there is no file.
    RunError when the file is not a plain 0600 file of this user holding
    NAME=VALUE lines (a link, another mode, another owner, a damaged line)."""
    path = env_path(name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise RunError(_ENV_UNUSABLE.format(
            name=name, why=e.strerror or "it cannot be opened")) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RunError(_ENV_UNUSABLE.format(name=name, why="it is not a file"))
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise RunError(_ENV_UNUSABLE.format(
                name=name, why="others than you could read or change it"))
        if st.st_uid != os.getuid():
            raise RunError(_ENV_UNUSABLE.format(name=name, why="it belongs to another user"))
        with os.fdopen(fd, "rb", closefd=False) as f:
            raw = f.read()
    except OSError as e:
        raise RunError(_ENV_UNUSABLE.format(
            name=name, why=e.strerror or "it cannot be read")) from None
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8")
    except ValueError:
        raise RunError(_ENV_UNUSABLE.format(name=name, why="it is not text")) from None
    values: dict[str, str] = {}
    # Lines end at "\n" only (docker's env-file reader splits there; a value
    # holds no CR, LF or NUL). An empty file is a server with no settings.
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    for line in lines:
        sn, sep, value = line.partition("=")
        if not sep or setting_name_problem(sn) or "\r" in value or "\0" in value \
                or sn in values:
            raise RunError(_ENV_UNUSABLE.format(name=name, why="a line is damaged"))
        values[sn] = value
    return values


def remove_env(name: str) -> None:
    """Delete the server's settings file, by its name (absent is no error)."""
    try:
        env_path(name).unlink()
    except FileNotFoundError:
        pass


def env_matches(run: dict[str, Any], values: dict[str, str]) -> bool:
    """Whether the file holds exactly the list's settings, with the list's
    value for every setting that is not secret."""
    settings = run["settings"]
    if set(values) != set(settings):
        return False
    return all(spec["secret"] or values[sn] == spec["value"]
               for sn, spec in settings.items())


# --- docker argv (pure) ------------------------------------------------------------

def labels(name: str) -> list[str]:
    return ["--label", f"{RUN_LABEL}=1", "--label", f"{NAME_LABEL}={name}"]


def network_create_argv(name: str, base: list[str]) -> list[str]:
    """`base` is rscore.network_create_args(<network>) (the RS bridge naming);
    the network is `--internal`: no default route at all until RS injects one
    through the router, so a container that (re)started before RS did — a
    host reboot — reaches nothing."""
    assert base[:3] == ["docker", "network", "create"] and base[-1] == network_name(name)
    return base[:3] + ["--internal"] + labels(name) + base[3:]


def volume_create_argv(name: str, volume: str | None = None) -> list[str]:
    """The data volume, or (`volume`) one of the server's package volumes."""
    return ["docker", "volume", "create"] + labels(name) + [volume or volume_name(name)]


def _hardening(dns: list[str]) -> list[str]:
    argv = ["--restart", RESTART_POLICY,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges"]
    for server in dns:
        argv += ["--dns", server]
    return argv


def install_run_argv(name: str, runner_image: str, install: int, dns: list[str]) -> list[str]:
    """The one-shot install container: the runner image (by Id), the server's network
    (its route out is injected before the install runs), the FRESH package
    volume — and no settings file, no environment value, no secret: what a
    package's install scripts run beside is RS's image and nothing of the
    server's configuration. It idles until the install is run in it."""
    return (["docker", "run", "-d", "--name", install_container_name(name),
             "--network", network_name(name)] + _hardening(dns) + labels(name)
            + ["-v", f"{pkg_volume_name(name, install)}:{PKG_MOUNT}",
               runner_image] + INSTALL_IDLE)


def install_exec_argv(name: str, run: dict[str, Any]) -> list[str]:
    """The install, inside the install container: each value one argv
    element, validated before it gets here, never read by a shell."""
    return ["docker", "exec", install_container_name(name), "python3", INSTALL_PROGRAM,
            run["registry"], run["package"], run["version"], run["command"]]


def run_argv(name: str, run: dict[str, Any], dns: list[str],
             runner_image: str = "") -> list[str]:
    """The server's container. Values arrive only through the env file (never
    this command line); the image is the PINNED digest, never the operator's
    tag; the operator's arguments come after it, where docker hands them to
    the server as its own command and never reads them as options."""
    argv = ["docker", "run", "-d",
            "--name", container_name(name),
            "--network", network_name(name)] + _hardening(dns)
    argv += labels(name)
    argv += ["--env-file", str(env_path(name))]
    if "data" in run:
        argv += ["-v", f"{volume_name(name)}:{run['data']}"]
    if is_package(run):
        # RS's runner by the image Id checked to hold the content the
        # package was installed with (`runner_image`), the installed package
        # read-only (a start installs and fetches nothing), and the bridge in
        # front of the package's command.
        assert _RUNNER_ID_RE.fullmatch(runner_image)
        argv += ["-v", f"{pkg_volume_name(name, run['install'])}:{PKG_MOUNT}:ro",
                 runner_image, "python3", SERVE_PROGRAM,
                 "--port", str(BRIDGE_PORT), "--path", BRIDGE_PATH,
                 "--idle-s", str(BRIDGE_SESSION_IDLE_S), "--", command_path(run)]
        return argv + list(run["args"])
    argv.append(run["digest"])
    argv += list(run["args"])
    return argv
