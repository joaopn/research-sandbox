"""mcp_run — MCP servers RS runs itself from a container image: names, shapes,
the settings file and the docker argv (pure helpers).

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

The lifecycle (bring up, stop, remove, the start/stop hooks) lives in rscore;
this module decides names, shapes, the file and the argv.

Refusal texts reach the browser verbatim: they name a field or a setting NAME,
never a value, a host path or a command.
"""

from __future__ import annotations

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
    server's own network."""
    return f"http://{container_name(name)}:{run['port']}{run['path']}"


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


def volume_create_argv(name: str) -> list[str]:
    return ["docker", "volume", "create"] + labels(name) + [volume_name(name)]


def run_argv(name: str, run: dict[str, Any], dns: list[str]) -> list[str]:
    """The server's container. Values arrive only through the env file (never
    this command line); the image is the PINNED digest, never the operator's
    tag; the operator's arguments come after it, where docker hands them to
    the server as its own command and never reads them as options."""
    argv = ["docker", "run", "-d",
            "--name", container_name(name),
            "--network", network_name(name),
            "--restart", RESTART_POLICY,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges"]
    for server in dns:
        argv += ["--dns", server]
    argv += labels(name)
    argv += ["--env-file", str(env_path(name))]
    if "data" in run:
        argv += ["-v", f"{volume_name(name)}:{run['data']}"]
    argv.append(run["digest"])
    argv += list(run["args"])
    return argv
