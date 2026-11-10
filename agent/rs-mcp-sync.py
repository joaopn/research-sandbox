#!/usr/bin/env python3
"""rs-mcp-sync — give this project's agents the MCP servers they are wired to.

RS stages this file and the wanted sets into a project container's home
(~/.rs-mcp/sync.py, ~/.rs-mcp/wanted.json) and runs it there: after a wiring
change, at each project start, and after each box start. It makes each
deployed agent's OWN per-user server list (the one `claude mcp add --scope
user` and `pi mcp add` edit) hold exactly the wired gateway servers, one name
at a time, through the agent's own command:

  - an entry carrying the header `X-RS-MCP: 1` is RS's; nothing else is ever
    changed or removed;
  - a wired name the agent already uses for a server of its own — in its user
    list, or declared at the top of /workspace — is a `conflict` and is left
    exactly as it is;
  - what happened is written, without any secret, to
    /workspace/.orchestrator/mcp-applied.json, which is all the host reads.

Usage (in a project container, as its user):
    sync.py --agent           the project's own agents
    sync.py --box NAME        one box (waits for it to finish booting)
    sync.py                   the project's agents and every running box
Inside a box the same file is run as `python3 - --apply` with the wanted set
in RS_MCP_WANTED.

Stdlib only; runs on the image's python3.
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MARK_HEADER = "X-RS-MCP"
GATEWAY_PORT = 8888
WANTED = Path.home() / ".rs-mcp" / "wanted.json"
# The project's (or box's) workspace. The override exists for tests only.
WORKSPACE = Path(os.environ.get("RS_MCP_WORKSPACE") or "/workspace")
APPLIED = WORKSPACE / ".orchestrator" / "mcp-applied.json"
# tmpfs: a container restart clears it, so a killed run never leaves it held.
LOCK = "/dev/shm/rs-mcp-sync.lock"
BOX_HOME = "/home/worker"
# One agent command (add or remove) measures 0.2-0.4 s. A minute is two
# hundred times that: it only names a hung agent command, so one cannot hold
# the lock for ever; a tenth of it would misfire on a loaded host's cold start.
CLI_TIMEOUT_S = 60
# A box boots in seconds (it copies its agents in); polled once a second, as
# the other box-boot waits are. The wait holds this container's lock, so it is
# bounded: five minutes is far past any healthy boot, and a box that hangs
# before its marker then reads "pending" instead of blocking every later run.
BOOT_POLL_S = 1
BOOT_WAIT_S = 300


# --- applying to the agents of ONE home ---------------------------------------

def _is_rs(entry) -> bool:
    headers = entry.get("headers") if isinstance(entry, dict) else None
    return isinstance(headers, dict) and any(
        isinstance(h, str) and h.lower() == MARK_HEADER.lower() for h in headers)


def _servers_in(path: Path) -> dict:
    """The `mcpServers` object of a JSON file; {} when there is none."""
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return {}
    found = data.get("mcpServers") if isinstance(data, dict) else None
    return found if isinstance(found, dict) else {}


def _pi_agent_dir(home: Path) -> Path:
    given = os.environ.get("PI_CODING_AGENT_DIR") or ""
    if given == "~":
        return home
    if given.startswith("~/"):
        return home / given[2:]
    return Path(given) if given else home / ".local/share/pi-agent/agent"


def _run(cmd: list, home: Path) -> bool:
    env = dict(os.environ, HOME=str(home))
    try:
        r = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=CLI_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _entry_matches(entry: dict, url: str, token: str) -> bool:
    headers = {h.lower(): v for h, v in (entry.get("headers") or {}).items()
               if isinstance(h, str)}
    return entry.get("url") == url and headers.get("authorization") == "Bearer " + token


# --- RS's lock on pi's home mcp.json -------------------------------------------
# pi's own `pi mcp add/remove` writes this file without any lock (pi 1.5.0,
# dist/extensions/mcp/config.js), and RS has two writers of it: a box's MCP
# render and the MCP gateway sync. Both take THIS lock around their whole
# read-decide-write, so neither loses the other's update. MIRROR: this block is
# byte-identical in agent/box-render-mcp.py and agent/rs-mcp-sync.py (pinned).
#
# The lock is the directory `<mcp.json>.rs-lock` (a name pi never uses), made
# with mkdir; its holder records `<pid> <start time>` in it and is the only one
# that removes it (the record by name, then rmdir — never a recursive delete).
# A lock whose recorded process is gone (no such pid, or a pid reused by a
# process that started at another time) was left by a killed writer: it is
# taken over by renaming it aside under a name unique to the taker, so two
# takers can never both win, and the move is undone when the moved lock turns
# out to name a live holder. Every writer of this file runs in the same
# container (the box's own boot, an exec into it), so its pids are comparable.
RS_LOCK_SUFFIX = ".rs-lock"
RS_LOCK_OWNER = "owner"
# How long a writer waits for the lock. The longest holder is the gateway sync,
# which runs one pi command per wired server, each bounded by its own
# one-minute timeout and measured at 0.2-0.4 s: one minute covers a healthy
# pass of over a hundred servers, and a holder past that is stuck on a command
# its own timeout is about to kill. A render that gives up only leaves pi's
# servers as they were until the next boot or refresh (a warning).
RS_LOCK_WAIT_S = 60
# A holder keeps the lock for a read and a write (the render: milliseconds) or
# for its pi commands (tenths of seconds each): a tenth of a second between
# tries costs nothing next to either and keeps a waiter from spinning.
RS_LOCK_POLL_S = 0.1


def _rs_proc_start(pid: int):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return None


def _rs_lock_owner(lock: Path):
    try:
        parts = (lock / RS_LOCK_OWNER).read_text().split()
    except OSError:
        return None
    return parts if len(parts) == 2 and parts[0].isdigit() else None


def _rs_lock_stale(lock: Path) -> bool:
    owner = _rs_lock_owner(lock)
    if owner is None:
        # Made but not yet recorded: a holder writes its record right after
        # its mkdir, so only one this old was left half-made by a killed writer.
        try:
            return time.time() - os.lstat(lock).st_mtime > RS_LOCK_WAIT_S
        except OSError:
            return False
    return _rs_proc_start(int(owner[0])) != owner[1]


def _rs_lock_clear(lock: Path) -> None:
    aside = lock.with_name(f"{lock.name}.stale.{os.getpid()}.{time.monotonic_ns()}")
    try:
        os.rename(lock, aside)
    except OSError:
        return                                   # another writer cleared it first
    if not _rs_lock_stale(aside):
        try:
            os.rename(aside, lock)               # a live holder's lock: put it back
        except OSError:
            pass
        return
    try:
        os.unlink(aside / RS_LOCK_OWNER)
    except OSError:
        pass
    try:
        os.rmdir(aside)
    except OSError:
        pass


def rs_lock_take(target: Path):
    """The lock on `target`, or None when it could not be had within
    RS_LOCK_WAIT_S (the caller fails soft)."""
    lock = target.with_name(target.name + RS_LOCK_SUFFIX)
    me = f"{os.getpid()} {_rs_proc_start(os.getpid())}"
    give_up = time.monotonic() + RS_LOCK_WAIT_S
    while True:
        try:
            os.mkdir(lock, 0o700)
        except FileExistsError:
            if _rs_lock_stale(lock):
                _rs_lock_clear(lock)
                continue
            if time.monotonic() >= give_up:
                return None
            time.sleep(RS_LOCK_POLL_S)
            continue
        except OSError:
            return None
        try:
            fd = os.open(lock / RS_LOCK_OWNER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(me + "\n")
        except OSError:
            try:
                os.rmdir(lock)
            except OSError:
                pass
            return None
        return lock


def rs_lock_release(lock) -> None:
    """Remove our own lock: its record by name, then the folder — only while
    the record is still ours."""
    if lock is None or _rs_lock_owner(lock) != [str(os.getpid()), str(_rs_proc_start(os.getpid()))]:
        return
    try:
        os.unlink(lock / RS_LOCK_OWNER)
        os.rmdir(lock)
    except OSError:
        pass


class Claude:
    name = "claude"

    def __init__(self, home: Path):
        self.home = home
        self.launcher = home / ".local/bin/claude"

    def existing(self) -> dict:
        return _servers_in(self.home / ".claude.json")

    def add(self, name: str, url: str, token: str) -> bool:
        entry = {"type": "http", "url": url,
                 "headers": {"Authorization": "Bearer " + token, MARK_HEADER: "1"}}
        return _run([str(self.launcher), "mcp", "add-json", "--scope", "user",
                     name, json.dumps(entry)], self.home)

    def remove(self, name: str) -> bool:
        return _run([str(self.launcher), "mcp", "remove", "--scope", "user", name],
                    self.home)


class Pi:
    name = "pi"

    def __init__(self, home: Path):
        self.home = home
        self.launcher = home / ".local/bin/pi"

    def existing(self) -> dict:
        return _servers_in(_pi_agent_dir(self.home) / "mcp.json")

    def add(self, name: str, url: str, token: str) -> bool:
        return _run([str(self.launcher), "mcp", "add", name, "--url", url,
                     "--header", "Authorization=Bearer " + token,
                     "--header", MARK_HEADER + "=1", "--exposure", "direct"], self.home)

    def remove(self, name: str) -> bool:
        return _run([str(self.launcher), "mcp", "remove", name], self.home)


def apply_local(wanted: dict, home: Path) -> dict:
    """{agent: {server: added | kept | removed | conflict | failed}} for the
    agents deployed in `home` ({} when there is none)."""
    # Servers declared at the top of the workspace shadow a user-list entry of
    # the same name; they are the user's (or the box's own), so they win.
    declared = set(_servers_in(WORKSPACE / ".mcp.json")) \
        | set(_servers_in(WORKSPACE / ".pi" / "mcp.json"))
    out: dict = {}
    for agent in (Claude(home), Pi(home)):
        if not agent.launcher.is_file():
            continue
        # pi's list is also written by a box's MCP render: RS's lock on it is
        # held around this agent's whole read-decide-write (its pi commands
        # included); a lock that cannot be had fails every wired name.
        lock = None
        if isinstance(agent, Pi):
            target = _pi_agent_dir(home) / "mcp.json"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                lock = rs_lock_take(target)
            except OSError:
                lock = None
            if lock is None:
                out[agent.name] = {name: "failed" for name in sorted(wanted)}
                continue
        try:
            out[agent.name] = _apply_agent(agent, wanted, declared)
        finally:
            rs_lock_release(lock)
    return out


def _apply_agent(agent, wanted: dict, declared: set) -> dict:
    """One agent's read-decide-write: RS's entries (the gateway header) only."""
    existing = agent.existing()
    result: dict = {}
    for name, want in sorted(wanted.items()):
        entry = existing.get(name)
        if name in declared or (entry is not None and not _is_rs(entry)):
            result[name] = "conflict"
            continue
        if entry is not None and _entry_matches(entry, want["url"], want["token"]):
            result[name] = "kept"
            continue
        if entry is not None and not agent.remove(name):
            result[name] = "failed"
            continue
        result[name] = "added" if agent.add(name, want["url"], want["token"]) else "failed"
    for name, entry in sorted(existing.items()):
        if name not in wanted and _is_rs(entry):
            result[name] = "removed" if agent.remove(name) else "failed"
    return result


# --- the project side ----------------------------------------------------------

def _uplink_network() -> ipaddress.IPv4Network:
    """The network of the interface the default route leaves by: in a project
    container that is the project's own network."""
    iface = None
    with open("/proc/net/route") as f:
        for line in f.readlines()[1:]:
            parts = line.split()
            if len(parts) > 7 and parts[1] == "00000000" and int(parts[3], 16) & 2:
                iface = parts[0]
                break
    if iface is None:
        raise OSError("no default route")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ifreq = struct.pack("256s", iface.encode()[:15])
        addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, ifreq)[20:24])   # SIOCGIFADDR
        mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891b, ifreq)[20:24])   # SIOCGIFNETMASK
    finally:
        s.close()
    return ipaddress.ip_network(f"{addr}/{mask}", strict=False)


def gateway_base() -> str:
    """The gateway's fixed address in this project: the last usable host
    address of the project network (the host attaches it exactly there)."""
    return f"http://{_uplink_network().broadcast_address - 1}:{GATEWAY_PORT}"


def _wanted_with_urls(servers: dict, base: str) -> dict:
    return {name: {"url": f"{base}/{name}", "token": token}
            for name, token in servers.items()}


def _docker(args: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], **kw)


def _box_running(container: str) -> bool:
    try:
        r = _docker(["container", "inspect", "-f", "{{.State.Running}}", container],
                    capture_output=True, text=True)
    except OSError:
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def _box_booted(container: str) -> bool:
    r = _docker(["exec", "-u", "worker", container, "test", "-e", BOX_HOME + "/.rs-role"],
                capture_output=True)
    return r.returncode == 0


def sync_box(box: str, servers: dict, base: str, wait: bool) -> dict:
    container = "rs-box-" + box
    if not _box_running(container):
        return {"state": "pending"}
    give_up = time.monotonic() + BOOT_WAIT_S
    while not _box_booted(container):
        if not wait or not _box_running(container) or time.monotonic() > give_up:
            return {"state": "pending"}
        time.sleep(BOOT_POLL_S)
    try:
        r = _docker(["exec", "-i", "-u", "worker", "-e", "HOME=" + BOX_HOME,
                     "-e", "RS_MCP_WANTED=" + json.dumps(_wanted_with_urls(servers, base)),
                     container, "python3", "-", "--apply"],
                    input=Path(__file__).read_bytes(), capture_output=True)
        agents = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError):
        agents = None
    if not isinstance(agents, dict):
        return {"state": "failed"}
    return {"state": "applied" if agents else "no agent", "agents": agents}


def sync_agent(servers: dict, base: str) -> dict:
    home = Path.home()
    # A container that is still booting has not copied its agents in yet (a
    # plain project writes the marker only after that copy): wait for it, with
    # the same bound as a box, rather than report "no agent" or give up.
    give_up = time.monotonic() + BOOT_WAIT_S
    while not (home / ".rs-role").exists():
        if time.monotonic() > give_up:
            return {"state": "pending"}
        time.sleep(BOOT_POLL_S)
    agents = apply_local(_wanted_with_urls(servers, base), home)
    return {"state": "applied" if agents else "no agent", "agents": agents}


def _record(done: dict) -> None:
    """Merge this run's outcomes into the applied file (temp + rename)."""
    try:
        current = json.loads(APPLIED.read_bytes())
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError, RecursionError):
        current = {}
    current.update(done)
    APPLIED.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=APPLIED.parent, prefix=APPLIED.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(current, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, APPLIED)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list) -> int:
    if argv == ["--apply"]:
        wanted = json.loads(os.environ.get("RS_MCP_WANTED") or "{}")
        sys.stdout.write(json.dumps(apply_local(wanted, Path.home())) + "\n")
        return 0
    only_agent = argv == ["--agent"]
    only_box = argv[1] if len(argv) == 2 and argv[0] == "--box" else None
    if argv and not only_agent and only_box is None:
        sys.stderr.write("usage: sync.py [--agent | --box NAME]\n")
        return 2
    lock = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)                # one run at a time in this container
    try:
        wanted = json.loads(WANTED.read_bytes())
        base = gateway_base()
        done: dict = {}
        if only_box is None:
            done["agent"] = sync_agent((wanted.get("agent") or {}).get("servers") or {}, base)
        if not only_agent:
            boxes = wanted.get("boxes") or {}
            names = [only_box] if only_box is not None else sorted(boxes)
            for box in names:
                if box in boxes:
                    done["box:" + box] = sync_box(
                        box, boxes[box].get("servers") or {}, base,
                        wait=only_box is not None)
        _record(done)
        sys.stdout.write(json.dumps(done) + "\n")
        return 0
    finally:
        os.close(lock)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
