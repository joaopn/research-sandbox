#!/opt/conda/bin/python3
"""RS's activity recorder: counts the interactive agent sessions of one
container — working (busy on a turn), waiting (handed back to the user) — and
the subagents they run, for the webui's sidebar boxes. Staged root-owned at
/usr/local/lib/rs/rs-activity.py in every project container and agent box, beside
the status bar.

Called by:
- Claude Code's hooks (`rs-activity.py hook`, the hook JSON on stdin);
- the research supervisor's Stop hook (`rs-activity.py stop-audit`), which runs
  the supervision audit with the same input, passes its output and exit status
  on unchanged, and records the hand-back only when the audit let the stop
  through;
- pi, through rs-activity.ts (`rs-activity.py pi <event> <pid>`, a small JSON
  payload on stdin);
- RS at every start of the container (`rs-activity.py reset`).

One record per agent PROCESS lives in a folder under /dev/shm (fresh at every
container start), keyed by the process id plus its start time, so a reused id
never inherits an old record and a session-id change (/clear, /resume) keeps
its record. After every event, under one lock, records whose process is gone
are dropped and the container's summary is written to
/workspace/.rs/activity.json — what the webui reads.

It prints nothing of its own and always exits 0 (stop-audit: the audit's
status): Claude Code feeds some hooks' output into the conversation and an exit
status of 2 blocks the user's prompt.
"""
import fcntl
import json
import os
import signal
import subprocess
import sys

STATE_DIR = f"/dev/shm/rs-activity-{os.getuid()}"
WORKSPACE = "/workspace"
SUMMARY_DIR = ".rs"
SUMMARY_NAME = "activity.json"
AUDIT = "/usr/local/lib/rs/rs-audit-stop"
# The summary's shape, read by the webui (webui/server.py, pinned).
SUMMARY_VERSION = 1
# How far up from a hook the walk looks for the agent process: the hook runs
# under `sh -c` (measured), a fallback wrapper adds one more shell.
ANCESTOR_WALK = 8
AGENT_COMM = "claude"            # the CLI and the editor's bundled binary alike
SESSION_SOURCES = ("startup", "clear", "resume")
ENDED = "ended"                  # a session ended in a process still finishing


# -- the process key --------------------------------------------------------

def _stat_fields(pid: int) -> "list[str] | None":
    """/proc/<pid>/stat split after the command name: [state, ppid, ...]."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("ascii", "replace")
    except OSError:
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    return raw[end + 2:].split()


def start_time(pid: int) -> "str | None":
    f = _stat_fields(pid)
    # field 22 of stat is the 20th after the command name
    if not f or len(f) < 20 or f[0] == "Z":
        return None
    return f[19]


def _ppid(pid: int) -> int:
    f = _stat_fields(pid)
    try:
        return int(f[1]) if f else 0
    except ValueError:
        return 0


def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", "rb") as fh:
            return fh.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def agent_pid_from_hook() -> int:
    """The Claude Code process above this hook, or 0."""
    pid = os.getppid()
    for _ in range(ANCESTOR_WALK):
        if pid <= 1:
            return 0
        if _comm(pid) == AGENT_COMM:
            return pid
        pid = _ppid(pid)
    return 0


def run_id() -> str:
    """This container run: PID 1's start time (and the host boot), so the
    webui can tell a restart from a count that went down."""
    try:
        with open("/proc/sys/kernel/random/boot_id", "rb") as fh:
            boot = fh.read().decode("ascii", "replace").strip()[:36]
    except OSError:
        boot = ""
    return f"{boot}:{start_time(1) or ''}"


# -- state ------------------------------------------------------------------

class State:
    """The records folder, held under its lock for one event."""

    def __init__(self) -> None:
        try:
            os.mkdir(STATE_DIR, 0o700)
        except FileExistsError:
            pass
        self.dfd = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        st = os.fstat(self.dfd)
        if st.st_uid != os.getuid():
            os.close(self.dfd)
            raise OSError("state folder not ours")
        self.lfd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
                           dir_fd=self.dfd)
        fcntl.flock(self.lfd, fcntl.LOCK_EX)

    def close(self) -> None:
        os.close(self.lfd)
        os.close(self.dfd)

    def _read(self, name: str):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.dfd)
        except OSError:
            return None
        try:
            with os.fdopen(fd, "rb") as fh:
                return json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            return None

    def _write(self, name: str, data) -> None:
        tmp = f".{name}.tmp"
        try:
            os.unlink(tmp, dir_fd=self.dfd)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=self.dfd)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.rename(tmp, name, src_dir_fd=self.dfd, dst_dir_fd=self.dfd)

    def records(self) -> "dict[str, dict]":
        out = {}
        for n in os.listdir(self.dfd):
            if n.startswith("p-") and n.endswith(".json"):
                r = self._read(n)
                if isinstance(r, dict):
                    out[n] = r
        return out

    def get(self, key: str) -> "dict | None":
        r = self._read(key)
        return r if isinstance(r, dict) else None

    def put(self, key: str, rec: dict) -> None:
        self._write(key, rec)

    def drop(self, key: str) -> None:
        try:
            os.unlink(key, dir_fd=self.dfd)
        except FileNotFoundError:
            pass

    def handbacks(self) -> int:
        v = self._read("handbacks")
        return v if isinstance(v, int) and v >= 0 else 0

    def add_handback(self) -> None:
        self._write("handbacks", self.handbacks() + 1)


def record_key(pid: int) -> "str | None":
    st = start_time(pid)
    return f"p-{pid}-{st}.json" if st else None


def _alive(key: str) -> bool:
    try:
        _, pid, st = key[:-5].split("-", 2)
        return start_time(int(pid)) == st
    except ValueError:
        return False


def _subs(rec: dict) -> dict:
    s = rec.get("subs")
    return s if isinstance(s, dict) else {}


def summarise(state: State) -> None:
    """Drop the records of processes that are gone, then write the summary.
    An ended session's record (its process still finishing) counts nothing."""
    working = waiting = subagents = 0
    for key, rec in state.records().items():
        if not _alive(key):
            state.drop(key)
            continue
        if rec.get("state") == ENDED:
            continue
        if rec.get("state") == "working":
            working += 1
        else:
            waiting += 1
        subagents += sum(v for v in _subs(rec).values() if isinstance(v, int) and v > 0)
    write_summary({"v": SUMMARY_VERSION, "run": run_id(), "working": working,
                   "waiting": waiting, "subagents": subagents,
                   "handbacks": state.handbacks()})


def write_summary(data: dict) -> None:
    """/workspace/.rs/activity.json by a temp name and a rename, never through
    a link; the folder and file take the process umask (the workspace's group
    access carries through to the webui)."""
    wfd = os.open(WORKSPACE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.mkdir(SUMMARY_DIR, 0o777, dir_fd=wfd)
        except FileExistsError:
            pass
        dfd = os.open(SUMMARY_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=wfd)
    finally:
        os.close(wfd)
    try:
        tmp = f".{SUMMARY_NAME}.{os.getpid()}.tmp"
        try:
            os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666,
                     dir_fd=dfd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.rename(tmp, SUMMARY_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


# -- events -----------------------------------------------------------------

def _rec(state: State, key: str, create_state: "str | None") -> "dict | None":
    rec = state.get(key)
    if rec is None and create_state:
        rec = {"state": create_state, "subs": {}}
    return rec


def _ended(rec: "dict | None") -> bool:
    return isinstance(rec, dict) and rec.get("state") == ENDED


def session_end(state: State, key: str) -> None:
    """The session ended, but its process may still be finishing (pi's exit
    runs the aborted turn's agent_end before session_shutdown, and both calls
    can take the lock in either order): leave a mark that no later turn or
    subagent event revives; only a new session start replaces it, and the
    sweep drops it once the process is gone."""
    state.put(key, {"state": ENDED, "subs": {}})


def turn_start(state: State, key: str) -> None:
    rec = _rec(state, key, "working")
    if _ended(rec):
        return
    rec["state"] = "working"
    state.put(key, rec)


def turn_end(state: State, key: str, handback: bool = True) -> None:
    """Waiting; the hand-back count rises only on a working→waiting change (a
    `!` command's lone Stop, an interrupted pi turn are no hand-back)."""
    rec = _rec(state, key, "waiting")
    if _ended(rec):
        return
    if handback and rec.get("state") == "working":
        state.add_handback()
    rec["state"] = "waiting"
    state.put(key, rec)


def session_start(state: State, key: str) -> None:
    rec = _rec(state, key, "waiting")
    rec["state"] = "waiting"
    state.put(key, rec)


def sub_add(state: State, key: str, sid: str, n: int) -> None:
    rec = _rec(state, key, "working")
    if _ended(rec):
        return
    rec.setdefault("subs", {})
    if not isinstance(rec["subs"], dict):
        rec["subs"] = {}
    rec["subs"][sid] = max(0, int(n))
    state.put(key, rec)


def sub_remove(state: State, key: str, sid: str) -> None:
    """An end with no recorded start is ignored."""
    rec = state.get(key)
    if rec is None or sid not in _subs(rec):
        return
    del rec["subs"][sid]
    state.put(key, rec)


def claude_reconcile(state: State, key: str, tasks, stopping: str = "") -> None:
    """Claude Code's Stop/SubagentStop carry the running background tasks: a
    recorded Claude subagent not among them (or the one stopping) is dropped,
    so a lost SubagentStop heals at the next turn end. Never adds one: hooks
    run as separate processes and take the lock in either order, so a Stop
    built while a subagent ran can arrive after that subagent's own stop."""
    if not isinstance(tasks, list):
        return
    running = set()
    for t in tasks:
        if (isinstance(t, dict) and t.get("type") == "subagent"
                and t.get("status") == "running" and isinstance(t.get("id"), str)):
            running.add("c:" + t["id"])
    running.discard("c:" + stopping)
    rec = state.get(key)
    if rec is None:
        return
    rec["subs"] = {k: v for k, v in _subs(rec).items()
                   if not k.startswith("c:") or k in running}
    state.put(key, rec)


def claude_event(state: State, key: str, data: dict) -> None:
    ev = data.get("hook_event_name")
    if ev == "SessionStart":
        if data.get("source") in SESSION_SOURCES:
            session_start(state, key)
    elif ev == "UserPromptSubmit":
        turn_start(state, key)
    elif ev in ("Stop", "StopFailure"):
        turn_end(state, key)
        claude_reconcile(state, key, data.get("background_tasks"))
    elif ev == "SubagentStart":
        aid = data.get("agent_id")
        if isinstance(aid, str) and aid:
            sub_add(state, key, "c:" + aid, 1)
    elif ev == "SubagentStop":
        aid = data.get("agent_id")
        if isinstance(aid, str) and aid:
            sub_remove(state, key, "c:" + aid)
            claude_reconcile(state, key, data.get("background_tasks"), aid)
    elif ev == "SessionEnd":
        session_end(state, key)


def _int(v, default: int = 1) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else default


def async_count(data: dict) -> int:
    """How many agents a pi background run has running at once: none for a
    workflow (its agents announce themselves), else the largest parallel group,
    else one."""
    if data.get("mode") == "workflow":
        return 0
    groups = data.get("parallelGroups")
    best = 1
    if isinstance(groups, list):
        for g in groups:
            if isinstance(g, dict):
                best = max(best, _int(g.get("count"), 1))
    return best


def pi_event(state: State, key: str, ev: str, data: dict) -> None:
    if ev == "session_start":
        session_start(state, key)
    elif ev == "agent_start":
        turn_start(state, key)
    elif ev == "agent_end":
        turn_end(state, key, handback=data.get("aborted") is not True)
    elif ev == "session_shutdown":
        session_end(state, key)
    elif ev == "tool_start":
        # a subagent tool call that may run its children in the foreground;
        # management calls (an `action`) run none
        if data.get("action") is not True and isinstance(data.get("id"), str):
            sub_add(state, key, "pt:" + data["id"], _int(data.get("tasks"), 1) or 1)
    elif ev == "tool_end":
        if isinstance(data.get("id"), str):
            sub_remove(state, key, "pt:" + data["id"])
    elif ev == "async_started":
        if isinstance(data.get("toolCallId"), str):
            sub_remove(state, key, "pt:" + data["toolCallId"])
        if isinstance(data.get("id"), str):
            n = async_count(data)
            if n:
                sub_add(state, key, "pa:" + data["id"], n)
    elif ev == "async_complete":
        if isinstance(data.get("id"), str):
            sub_remove(state, key, "pa:" + data["id"])


# -- entry points -----------------------------------------------------------

def _load(raw: bytes) -> dict:
    try:
        d = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return {}
    return d if isinstance(d, dict) else {}


def with_state(fn) -> None:
    state = State()
    try:
        fn(state)
        summarise(state)
    finally:
        state.close()


def cmd_hook(raw: bytes) -> None:
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk-"):
        return                                  # a headless run is never counted
    data = _load(raw)
    pid = agent_pid_from_hook()
    key = record_key(pid) if pid else None
    if not key:
        return
    with_state(lambda s: claude_event(s, key, data))


def cmd_stop_audit(raw: bytes) -> int:
    """Run the audit with the hook's input; its output and status pass on
    unchanged; the stop is recorded only when the audit let it through."""
    try:
        r = subprocess.run([AUDIT], input=raw, capture_output=True)
        code, out, err = r.returncode, r.stdout, r.stderr
    except OSError as e:
        code, out, err = 127, b"", f"{AUDIT}: {e.strerror}\n".encode()
    try:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(err)
        sys.stderr.buffer.flush()
    except OSError:
        pass
    if code == 0:
        try:
            cmd_hook(raw)
        except Exception:
            pass
    return code


def cmd_pi(ev: str, pid_s: str, raw: bytes) -> None:
    try:
        pid = int(pid_s)
    except ValueError:
        return
    key = record_key(pid) if pid > 1 else None
    if not key:
        return
    data = _load(raw)
    with_state(lambda s: pi_event(s, key, ev, data))


def main(argv: "list[str]") -> int:
    op = argv[1] if len(argv) > 1 else ""
    try:
        raw = b"" if op == "reset" else sys.stdin.buffer.read()
    except Exception:
        raw = b""
    if op == "stop-audit":
        return cmd_stop_audit(raw)
    try:
        if op == "hook":
            cmd_hook(raw)
        elif op == "pi" and len(argv) == 4:
            cmd_pi(argv[2], argv[3], raw)
        elif op == "reset":
            with_state(lambda s: None)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    # A closed window hangs up the agent's terminal while this runs (a
    # SessionEnd hook, pi's shutdown call): finish the write regardless.
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (OSError, ValueError):
        pass
    try:
        code = main(sys.argv)
    except BaseException:
        code = 0
    sys.exit(code)
