#!/usr/bin/env python3
"""Short evals of research agents: a few scripted sessions played to one or
more project types, then probes or workspace checks scored against a key
written from the data's planted facts.

Each eval is one file under tests/bench/evals/ (its sessions, its probes or
checks, how each is scored). By default the short evals run on the
`researcher` type and the plain `sandbox-dind` floor; an eval marked optional
(the full study, `full`) runs only when named.

Run from the repository root of an RS whose stack is up (`research.py start`),
with the agent downloads and the reader pulled:

    python3 tests/bench/bench.py --run r1 --dry-run
    python3 tests/bench/bench.py --run r1 --claude-token-file /workspace/.bench/claude-token
    python3 tests/bench/bench.py --run r1 --evals recall --arms researcher researcher-pi \\
        --claude-token-file /workspace/.bench/claude-token \\
        --pi-model anthropic/claude-sonnet-4-5 --pi-key-file /workspace/.bench/pi-key
    python3 tests/bench/bench.py --run dry --standin

`--dry-run` prints what a run will do (agent turns, grader calls) and its caps,
and creates nothing. Every eval on every project type runs in its own fresh
project, `bench-<run>-<eval>-<arm>`, destroyed when it is done (--keep keeps
it); a run refuses to start when one of those names exists. A token cap and a
time cap per eval and project type stop it and report it over budget. Real
tokens are spent unless --standin is given (stand-in models: the plumbing,
zero tokens, scores meaningless). Results go to tests/bench/results/<run>/
(results.json, table.md), which git ignores. Secrets never reach a command
line: each project gets them in a 0600 file under its own /dev/shm, written
over stdin and sourced by the agent's shell.

Arms:
  researcher        the researcher type with Claude
  floor             a plain sandbox-dind project with Claude (no memory kit;
                    Claude's built-in memory on)
  researcher-pi     the researcher type with pi
  researcher-nohook the researcher type with Claude, its start hook removed
  research          the existing research flow (Claude as supervisor; the
                    PI's /log at each session's end, its own memory)
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EVALS = HERE / "evals"
sys.path.insert(0, str(ROOT / "cli"))
sys.path.insert(0, str(ROOT))

RH = "/home/research"
PI_DIR = f"{RH}/.local/share/pi-agent/agent"
ENV_FILE = "/dev/shm/bench-env"
MARKER = "/dev/shm/rs-memory-consolidating-1000"
STANDIN_PORT = 8719
STANDIN_KEY = "sk-ant-stub-0123456789abcdefghij"
PI_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY",
              "openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY"}
ARMS = {
    "researcher": {"workflow": "researcher", "agent": "claude", "memory": True},
    "floor": {"workflow": "sandbox-dind", "agent": "claude", "memory": False},
    "researcher-pi": {"workflow": "researcher", "agent": "pi", "memory": True},
    "researcher-nohook": {"workflow": "researcher", "agent": "claude", "memory": True, "nohook": True},
    "research": {"workflow": "research", "agent": "claude", "memory": False, "log": True},
}
DEFAULT_ARMS = ("researcher", "floor")
# A short eval's turn is a small analysis (a package install the slowest
# part); half of this cuts an install short, ten times it lets a hung session
# hold the run for an hour.
TURN_TIMEOUT = 600
# The interrupted session is killed this long after its last message started
# working, unless the session sets its own: long enough to be mid-task.
KILL_AFTER = 60
CHECK_TYPES = ("number", "contains", "grader", "files")
# what each check type must carry (a files check may also carry a list fact
# and tol: file i then holds a number within tol of the fact's item i)
CHECK_FIELDS = {"number": ("fact", "tol"), "contains": ("any",), "grader": ("key",), "files": ("paths",)}
# eval and session names become project and terminal names (rscore's project-name rule)
NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z")
# What a probe must not inherit from the probe before it: the memory (the
# researcher's, and Claude's built-in one), restored from a snapshot each time.
PROBE_STATE = ("/workspace/memory", f"{RH}/.claude/projects/-workspace/memory")
GRADE_PROMPT = """You grade one answer of a research agent against a key. GRADE-AS-JSON.
Score 1 when the answer states what the key says (same facts; numbers within a few percent),
0.5 when it is partly right or right but hedged into uselessness, 0 when it is wrong, missing
or claims something the key contradicts. Reply with one JSON object and nothing else:
{{"score": <0|0.5|1>, "why": "<one sentence>"}}

KEY: {key}

QUESTION: {question}

ANSWER: {answer}
"""
# The instruction rs-memory's background run is started with (container/researcher/rs-memory).
CONSOLIDATION_TEXT = "and do exactly what it says. It is the whole task."
PATH_RE = re.compile(r"(/workspace/[^\s`'\"()\[\],;]+)")


class EvalError(Exception):
    """An eval file that does not say what a run needs."""


class ArmFailed(Exception):
    """A project whose agent never came up: the run records it and moves on
    instead of typing into a dead session."""


class OverBudget(Exception):
    """An eval past its token or time cap on one project type."""


# ---------------------------------------------------------------- eval files
def load_evals(folder: Path = EVALS) -> "dict[str, dict]":
    """Every eval file, checked: its sessions, probes and checks must say
    what a run needs. A bad file stops the run before anything is created."""
    out = {}
    for f in sorted(folder.glob("*.json")):
        try:
            e = json.loads(f.read_text())
        except ValueError as x:
            raise EvalError(f"{f.name}: not JSON ({x})")
        name = e.get("name")
        if name != f.stem:
            raise EvalError(f"{f.name}: its name must be {f.stem!r}")
        if not NAME_RE.match(name):
            raise EvalError(f"{f.name}: an eval's name is letters, digits, - and _ (it names projects)")
        if not isinstance(e.get("sessions"), list) or not e["sessions"]:
            raise EvalError(f"{f.name}: needs sessions")
        for s in e["sessions"]:
            if s.get("end") not in ("exit", "kill") or not s.get("messages") or not s.get("name"):
                raise EvalError(f"{f.name}: a session needs a name, messages and end exit|kill")
            if not NAME_RE.match(str(s["name"])):
                raise EvalError(f"{f.name}: a session's name is letters, digits, - and _ (it names a terminal)")
            if "kill_after" in s and not (s["end"] == "kill" and isinstance(s["kill_after"], int) and s["kill_after"] > 0):
                raise EvalError(f"{f.name}: kill_after is a number of seconds, on a killed session only")
        items = list(e.get("probes", [])) + list(e.get("checks", []))
        if not items:
            raise EvalError(f"{f.name}: needs probes or checks")
        for it in e.get("probes", []):
            if not it.get("id") or not it.get("question"):
                raise EvalError(f"{f.name}: a probe needs an id and a question")
        ids = [it.get("id") for it in items]
        if not all(ids) or len(set(ids)) != len(ids):
            raise EvalError(f"{f.name}: every probe and check needs its own id")
        for it in items:
            c = it.get("check") or {}
            if c.get("type") not in CHECK_TYPES:
                raise EvalError(f"{f.name}: {it.get('id')}: check type is one of {', '.join(CHECK_TYPES)}")
            need = CHECK_FIELDS[c["type"]]
            if not all(c.get(k) for k in need):
                raise EvalError(f"{f.name}: {it['id']}: a {c['type']} check needs {', '.join(need)}")
            if "fact" in c and "tol" not in c:
                raise EvalError(f"{f.name}: {it['id']}: a check against a fact needs its tol")
            if c["type"] == "files" and it in e.get("probes", []):
                raise EvalError(f"{f.name}: {it['id']}: a files check is a check, not a probe")
            if c["type"] != "files" and it in e.get("checks", []):
                raise EvalError(f"{f.name}: {it['id']}: only a files check needs no question")
        out[name] = e
    return out


def check_against_key(e: dict, key: dict) -> None:
    """Every fact and key answer the eval scores against exists in its key
    (run before any project is created)."""
    for it in e.get("probes", []) + e.get("checks", []):
        c = it["check"]
        if "fact" in c and c["fact"] not in key["facts"]:
            raise EvalError(f"{e['name']}: {it['id']}: the key has no fact {c['fact']!r}")
        v = key["facts"].get(c.get("fact"))
        num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
        if c["type"] == "number" and not num(v):
            raise EvalError(f"{e['name']}: {it['id']}: {c['fact']} is not a number")
        if c["type"] == "files" and "fact" in c and not (isinstance(v, list) and all(map(num, v))
                                                          and len(v) == len(c["paths"])):
            raise EvalError(f"{e['name']}: {it['id']}: {c['fact']} is not one number per path")
        if c["type"] == "grader" and c["key"] not in key["answers"]:
            raise EvalError(f"{e['name']}: {it['id']}: the key has no answer {c['key']!r}")


def plan(evals: "list[dict]", arms: "list[str]") -> dict:
    """What a run will do, counted from the eval files (no estimate of tokens
    is invented: the first real run measures them)."""
    rows, grader = [], 0
    for e in evals:
        for arm in arms:
            turns = sum(len(s["messages"]) for s in e["sessions"])
            if ARMS[arm].get("log"):
                turns += sum(1 for s in e["sessions"] if s["end"] == "exit")
            probes = len(e.get("probes", []))
            g = sum(1 for p in e.get("probes", []) if p["check"]["type"] == "grader")
            # every session's end, and every probe's (a probe is a session too)
            cons = len(e["sessions"]) + probes if ARMS[arm]["memory"] else 0
            grader += g
            rows.append({"eval": e["name"], "arm": arm, "sessions": len(e["sessions"]),
                         "turns": turns, "probes": probes, "grader_calls": g, "consolidations": cons})
    return {"rows": rows, "grader_calls": grader + (4 if grader else 0),   # + the grader self-check
            "agent_calls": sum(r["turns"] + r["probes"] for r in rows)}


class Bench:
    def __init__(self, a):
        self.a = a
        self.out = ROOT / "tests" / "bench" / "results" / a.run
        self.made: "list[str]" = []
        self.grader_c = None

    # ------------------------------------------------------------ plumbing
    def rs(self, *args, check=True):
        r = subprocess.run([sys.executable, str(ROOT / "research.py"), *args], cwd=ROOT,
                           capture_output=True, text=True, input="yes\n")
        if check and r.returncode != 0:
            raise SystemExit(f"research.py {' '.join(args)} failed:\n{(r.stderr or r.stdout)[-2000:]}")
        return r

    def ex(self, c, cmd: str, *, inp: "bytes | None" = None, timeout=None):
        """A shell command in a project as its agent user (never a secret on
        its command line: those are in the project's env file)."""
        return subprocess.run(["docker", "exec", "-i", "-u", "research", "-w", "/workspace",
                               "-e", f"HOME={RH}", c, "sh", "-c", cmd],
                              input=inp, capture_output=True, timeout=timeout)

    def projects_dir(self) -> Path:
        if not hasattr(self, "_pd"):
            from research import load_config
            self._pd = Path(load_config().projects_dir)
        return self._pd

    def summary(self, p) -> dict:
        try:
            return json.loads((self.projects_dir() / p / "workspace" / ".rs" / "activity.json").read_text())
        except (OSError, ValueError):
            return {}

    def count(self, p, k) -> int:
        try:
            return int(self.summary(p).get(k, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def secrets(self, arm) -> dict:
        if self.a.standin:
            return {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{STANDIN_PORT}", "ANTHROPIC_API_KEY": STANDIN_KEY}
        if ARMS[arm]["agent"] == "pi":
            prov = self.a.pi_model.split("/", 1)[0]
            var = self.a.pi_key_env or PI_KEY_ENV.get(prov)
            if not var:
                raise SystemExit(f"no key variable known for pi provider {prov!r}; pass --pi-key-env")
            return {var: Path(self.a.pi_key_file).read_text().strip()}
        return {"CLAUDE_CODE_OAUTH_TOKEN": self.claude_token()}

    def claude_token(self) -> str:
        return Path(self.a.claude_token_file).read_text().strip()

    def agent_cmd(self, arm) -> str:
        if ARMS[arm]["agent"] == "pi":
            return f"pi --model {shlex.quote('stub/stub-1' if self.a.standin else self.a.pi_model)}"
        return "claude"

    def wait(self, pred, limit) -> bool:
        end = time.time() + limit
        while time.time() < end:
            if pred():
                return True
            time.sleep(2)
        return False

    def tmux(self, c, *args):
        return subprocess.run(["docker", "exec", "-u", "research", "-e", f"HOME={RH}", c, "tmux", *args],
                              capture_output=True, text=True)

    def consolidating(self, c) -> bool:
        """A consolidation run is alive (its marker names a live rs-memory _run)."""
        r = self.ex(c, f'p=$(cat {MARKER} 2>/dev/null) && tr "\\0" " " < /proc/$p/cmdline | grep -q " _run "')
        return r.returncode == 0

    # ------------------------------------------------------------ setup
    def project_name(self, ev, arm) -> str:
        return f"bench-{self.a.run}-{ev}-{arm}"

    def refuse_existing(self, names) -> None:
        for name in names:
            if subprocess.run(["docker", "container", "inspect", f"rs-project-{name}"],
                              capture_output=True).returncode == 0 or (self.projects_dir() / name).exists():
                raise SystemExit(f"project {name} exists already (a kept run?): pick another --run "
                                 f"or destroy it first")

    def create(self, name, *args) -> str:
        # refuse_existing proved the name free: whatever exists under it from
        # here on is this run's, a create that failed half-way included
        self.made.append(name)
        self.rs("project", "create", name, *args)
        return f"rs-project-{name}"

    def destroy(self, name) -> None:
        if not self.a.keep and name in self.made:
            self.rs("project", "destroy", name, check=False)
            self.made.remove(name)

    def stage_secrets(self, c, arm, extra: "dict | None" = None) -> None:
        env = {**self.secrets(arm), **(extra or {})}
        lines = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items())
        self.ex(c, f"umask 077; cat > {ENV_FILE}", inp=lines.encode())

    def seed_claude(self, c) -> None:
        seed = ('.hasCompletedOnboarding = true | .bypassPermissionsModeAccepted = true'
                ' | .projects["/workspace"] = ((.projects["/workspace"] // {}) + {"hasTrustDialogAccepted": true})')
        if self.a.standin:
            seed += f' | .customApiKeyResponses = {{"approved": ["{STANDIN_KEY[-20:]}"], "rejected": []}}'
        self.ex(c, f"f={RH}/.claude.json; [ -f $f ] || echo '{{}}' > $f; jq '{seed}' $f > $f.t && mv $f.t $f")

    def start_standin(self, c) -> None:
        self.ex(c, "cat > /tmp/bench-standin.py", inp=(HERE / "standin.py").read_bytes())
        subprocess.run(["docker", "exec", "-d", "-u", "research", c, "/opt/conda/bin/python3",
                        "/tmp/bench-standin.py", str(STANDIN_PORT)], check=True)
        time.sleep(1)

    def setup(self, ev, arm, gen: Path) -> "tuple[str, str]":
        spec = ARMS[arm]
        p = self.project_name(ev, arm)
        args = ["--workflow", spec["workflow"]]
        if spec["workflow"] != "research":
            args += ["--agent", spec["agent"]]
        c = self.create(p, *args)
        buf = io.BytesIO()                        # the data, as the PI would give it: /workspace/data
        with tarfile.open(fileobj=buf, mode="w") as t:
            t.add(gen / "data", arcname="data")
        self.ex(c, "tar -x -C /workspace", inp=buf.getvalue())
        self.seed_claude(c)
        self.stage_secrets(c, arm)
        if spec.get("nohook"):
            self.ex(c, f"f={RH}/.claude/settings.json; jq '.hooks.SessionStart |= map(select(all(.hooks[]?; "
                       f"(.command // \"\") | test(\"rs-memory open\") | not)))' $f > $f.t && mv $f.t $f")
        if spec["workflow"] == "research" and not self.a.standin:
            # the supervisor hands its credentials file to the workers it
            # spawns: the token is written there (the project is destroyed at
            # the end of the eval)
            cred = json.dumps({"claudeAiOauth": {"accessToken": self.claude_token(), "refreshToken": "",
                                                 "expiresAt": 4102444800000, "scopes": ["user:inference"]}})
            self.ex(c, f"umask 077; mkdir -p {RH}/.claude && cat > {RH}/.claude/.credentials.json", inp=cred.encode())
        if self.a.standin:
            self.start_standin(c)
            if spec["agent"] == "pi":
                models = {"providers": {"stub": {"baseUrl": f"http://127.0.0.1:{STANDIN_PORT}/v1",
                          "api": "openai-completions", "apiKey": "stub",
                          "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
                          "models": [{"id": "stub-1"}]}}}
                self.ex(c, f"cat > {PI_DIR}/models.json", inp=json.dumps(models).encode())
                self.ex(c, f"f={PI_DIR}/settings.json; jq '.defaultProvider=\"stub\" | .defaultModel=\"stub-1\"' $f > $f.t && mv $f.t $f")
        elif spec["agent"] == "pi":
            prov, model = self.a.pi_model.split("/", 1)
            self.ex(c, f"f={PI_DIR}/settings.json; jq --arg p {shlex.quote(prov)} --arg m {shlex.quote(model)} "
                       f"'.defaultProvider=$p | .defaultModel=$m' $f > $f.t && mv $f.t $f")
        return p, c

    def setup_grader(self) -> str:
        c = self.create(f"bench-{self.a.run}-grader", "--workflow", "sandbox-dind", "--agent", "claude")
        self.seed_claude(c)
        # the grader sees only the key, the question and the answer: no
        # built-in memory, no tools, an empty folder
        self.stage_secrets(c, "floor", {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        self.ex(c, "mkdir -p /tmp/grader")
        if self.a.standin:
            self.start_standin(c)
        return c

    # ------------------------------------------------------------ usage
    def transcripts(self, c, arm) -> "dict[str, tuple[float, int]]":
        root = f"{PI_DIR}/sessions" if ARMS[arm]["agent"] == "pi" else f"{RH}/.claude/projects"
        if ARMS[arm]["workflow"] == "research":
            root += " /workspace/workers"          # the research flow's workers do the analysis
        r = self.ex(c, f"find {root} -name '*.jsonl' -printf '%T@ %s %p\\n' 2>/dev/null")
        out = {}
        for line in r.stdout.decode().splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[1].isdigit():
                out[parts[2]] = (float(parts[0]), int(parts[1]))
        return out

    def usage_since(self, c, arm, before: dict) -> dict:
        """Tokens of every model call written since `before`: plain input,
        cached input (read + write), output; the input side of the first call
        of the main session (what it loaded cold), never a subagent's or a
        worker's; and, apart, the tokens of any consolidation run."""
        tot = {"input": 0, "cached": 0, "output": 0, "first_input": None}
        cons = {"input": 0, "cached": 0, "output": 0}
        now = self.transcripts(c, arm)
        side = {p: side_transcript(p, now) for p in now}
        order = sorted(now, key=lambda p: (side[p], now[p][0]))
        cons_sessions: "set[str]" = set()
        for path in order:
            start = before.get(path, (0, 0))[1]
            if now[path][1] <= start:
                continue
            data = self.ex(c, f"tail -c +{start + 1} {shlex.quote(path)}").stdout.decode("utf-8", "replace")
            # a consolidation run's own session (rs-memory's background run),
            # and any subagent it started (its transcript sits under the run's)
            is_cons = (start == 0 and CONSOLIDATION_TEXT in data) or any(
                path.startswith(q[:-len(".jsonl")] + "/") for q in cons_sessions)
            if is_cons and not side[path]:
                cons_sessions.add(path)
            for u in usage_records(data):
                if is_cons:
                    for k in cons:
                        cons[k] += u[k]
                    continue
                if tot["first_input"] is None and not side[path]:
                    tot["first_input"] = u["input"] + u["cached"]
                for k in ("input", "cached", "output"):
                    tot[k] += u[k]
        if any(cons.values()):
            tot["consolidation"] = cons
        return tot

    # ------------------------------------------------------------ budget
    def spend(self, rec: dict, usage: "dict | None") -> None:
        """Count a step's tokens against the eval's budget; stop it when it
        goes past either cap."""
        for u in (usage or {}, (usage or {}).get("consolidation") or {}):
            rec["tokens"] += sum(int(u.get(k, 0) or 0) for k in ("input", "cached", "output"))
        secs = time.time() - rec["_t0"]
        if rec["tokens"] > self.a.max_tokens:
            raise OverBudget(f"{rec['tokens']} tokens, past the cap of {self.a.max_tokens}")
        if secs > self.a.max_seconds:
            raise OverBudget(f"{secs:.0f} s, past the cap of {self.a.max_seconds}")

    # ------------------------------------------------------------ sessions
    def send(self, c, name, text) -> None:
        self.tmux(c, "send-keys", "-t", name, "-l", text)
        time.sleep(1)
        self.tmux(c, "send-keys", "-t", name, "Enter")

    def turn(self, arm, p, c, name, text) -> dict:
        hb = self.count(p, "handbacks")
        before = self.transcripts(c, arm)
        t0 = time.time()
        self.send(c, name, text)
        done = self.wait(lambda: self.count(p, "handbacks") > hb, self.a.turn_timeout)
        return {"seconds": round(time.time() - t0, 1), "handed_back": done,
                "usage": self.usage_since(c, arm, before)}

    def play(self, arm, p, c, session, budget: dict, watch: "list[str]" = ()) -> dict:
        name = session["name"]
        w0 = self.count(p, "waiting")
        self.ex(c, f"tmux new-session -d -s {name} -x 200 -y 50 "
                   + shlex.quote(f". {ENV_FILE}; export PATH={RH}/.local/bin:$PATH; exec {self.agent_cmd(arm)}"))
        rec = {"session": name, "messages": []}
        t_start = time.time()
        if not self.wait(lambda: self.count(p, "waiting") > w0, 120):
            pane = self.tmux(c, "capture-pane", "-p", "-t", name).stdout
            rec["error"] = "the session never came up; its screen ends: " + " / ".join(
                l.strip() for l in pane.splitlines() if l.strip())[-400:]
            self.tmux(c, "kill-session", "-t", name)
            raise ArmFailed(rec)
        for _ in range(15):                    # Claude Code's first-start question: keep bypass mode
            pane = self.tmux(c, "capture-pane", "-p", "-t", name).stdout
            if "Make auto mode your default" in pane:
                self.tmux(c, "send-keys", "-t", name, "Down")
                time.sleep(0.5)
                self.tmux(c, "send-keys", "-t", name, "Enter")
                time.sleep(2)
                rec["answered_auto_mode_question"] = "No, keep bypass permissions"
                break
            time.sleep(1)
        msgs = session["messages"]
        killed_before = None
        try:
            for i, msg in enumerate(msgs):
                if i == len(msgs) - 1 and session["end"] == "kill":
                    if self.a.standin:          # the stand-in answers this one slowly (standin.py)
                        self.ex(c, "cat > /tmp/bench-standin-slow", inp=" ".join(msg.split())[:60].encode())
                    hb = self.count(p, "handbacks")
                    before = killed_before = self.transcripts(c, arm)
                    t0 = time.time()
                    self.send(c, name, msg)
                    started = self.wait(lambda: self.count(p, "working") > 0, 120)
                    time.sleep(session.get("kill_after", self.a.kill_after))
                    # still working when killed (a message that never started a turn was not interrupted)
                    mid = started and self.count(p, "handbacks") == hb
                    self.tmux(c, "kill-session", "-t", name)
                    m = {"i": i, "killed_after_s": round(time.time() - t0, 1),
                         "working_seen": started, "mid_turn": mid, "usage": self.usage_since(c, arm, before)}
                    # the consolidation the kill starts is counted once, below
                    m["usage"].pop("consolidation", None)
                    rec["messages"].append(m)
                    self.wait(lambda: self.count(p, "waiting") <= w0, 60)
                    self.spend(budget, m["usage"])
                    break
                m = {"i": i, **self.turn(arm, p, c, name, msg)}
                rec["messages"].append(m)
                self.spend(budget, m["usage"])
            # the consolidation starts at the session's end event and can be
            # done before the next line runs: count from before the end (for a
            # killed session, from before its last message: the kill is its end)
            before_end = killed_before if killed_before is not None else self.transcripts(c, arm)
            if session["end"] == "exit":
                if ARMS[arm].get("log"):
                    rec["log"] = self.turn(arm, p, c, name, "/log")   # the research flow's own memory
                    self.spend(budget, rec["log"]["usage"])
                    before_end = self.transcripts(c, arm)
                if ARMS[arm]["agent"] == "pi":
                    self.tmux(c, "send-keys", "-t", name, "C-d")
                else:
                    self.send(c, name, "/exit")
                if not self.wait(lambda: self.tmux(c, "has-session", "-t", name).returncode != 0, 60):
                    rec["exit"] = "did not end by itself; killed"
                    self.tmux(c, "kill-session", "-t", name)
            if ARMS[arm]["memory"]:
                time.sleep(2)
                rec["consolidation_done"] = self.wait(lambda: not self.consolidating(c), self.a.turn_timeout)
                # the run's own session, told apart by its instruction (usage_since)
                rec["consolidation_usage"] = self.usage_since(c, arm, before_end).get(
                    "consolidation", {"input": 0, "cached": 0, "output": 0})
                rec["consolidated"] = self.ex(c, "cat /workspace/memory/consolidated 2>/dev/null").stdout.decode().strip()
                self.spend(budget, rec["consolidation_usage"])
            if killed_before is not None and watch:
                # what the cut-off task left for the next session, read once the
                # session and anything it ran are done (not at the kill itself)
                rec["messages"][-1]["present_at_kill"] = [
                    x for x in watch if self.ex(c, f"test -s {shlex.quote(x)}").returncode == 0]
        except OverBudget as x:
            x.session = rec                     # which turn went over, kept in the results
            raise
        finally:
            rec["seconds"] = round(time.time() - t_start, 1)
            self.tmux(c, "kill-session", "-t", name)
        return rec

    # ------------------------------------------------------------ probes
    def ask(self, arm, c, question: str, grader: bool = False) -> dict:
        before = self.transcripts(c, arm)
        T = self.a.turn_timeout
        if ARMS[arm]["agent"] == "pi":
            model = "stub/stub-1" if self.a.standin else self.a.pi_model
            cmd = f"pi -p --model {shlex.quote(model)} {shlex.quote(question)} </dev/null"
        elif grader:
            # the prompt before --tools, which takes a list and would swallow it
            cmd = f"cd /tmp/grader && claude -p {shlex.quote(question)} --tools '' </dev/null"
        else:
            cmd = f"claude -p {shlex.quote(question)} </dev/null"
        t0 = time.time()
        try:
            # the limit is enforced inside the container (killing only the
            # docker client would leave the agent running into the next probe)
            r = self.ex(c, f". {ENV_FILE}; export PATH={RH}/.local/bin:$PATH; timeout -k 10 {T} sh -c {shlex.quote(cmd)}",
                        timeout=T + 30)
        except subprocess.TimeoutExpired:
            return {"answer": None, "error": "timed out", "seconds": round(time.time() - t0, 1),
                    "usage": self.usage_since(c, arm, before)}
        # pi starts its session-end close detached; it must be done before
        # anything is restored
        self.wait(lambda: self.ex(c, "for f in /proc/[0-9]*/cmdline; do tr '\\0' ' ' < $f 2>/dev/null; echo; done "
                                     # [ ]: this scan's own command line must not match
                                     "| grep -q 'rs-memory[ ]close'").returncode != 0, 60)
        # and for the consolidation that close may have started: its tokens
        # are reported apart (usage_since), never as the probe's cost
        self.wait(lambda: not self.consolidating(c), T)
        if r.returncode in (124, 137):                # timeout, or its forced kill
            return {"answer": None, "error": "timed out", "seconds": round(time.time() - t0, 1),
                    "usage": self.usage_since(c, arm, before)}
        out = {"seconds": round(time.time() - t0, 1), "usage": self.usage_since(c, arm, before)}
        text = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode != 0 or not text:
            out.update(answer=None, error=f"exit {r.returncode}: {r.stderr.decode('utf-8', 'replace')[-300:]}")
        else:
            out["answer"] = text
        return out

    def snapshot(self, c) -> None:
        # the files the sessions wrote (what provenance may point at), before any probe
        r = self.ex(c, "find /workspace -type f -not -path '/workspace/memory/*' -not -path '/workspace/data/*' "
                       "-not -path '/workspace/.*' -not -path '*/.*/*' 2>/dev/null")
        self.session_files = set(r.stdout.decode("utf-8", "replace").split("\n")) - {""}
        paths = " ".join(shlex.quote(x) for x in PROBE_STATE)
        self.ex(c, f"for d in {paths}; do [ -d \"$d\" ] && echo \"$d\"; done > /tmp/bench-state.list; "
                   "tar -c -f /tmp/bench-state.tar -P -T /tmp/bench-state.list")

    def restore(self, c, n) -> None:
        """Back to the snapshot: the probe's own changes moved aside (a rename,
        nothing deleted), the snapshot extracted in place."""
        self.wait(lambda: not self.consolidating(c), self.a.turn_timeout)
        # workers a research probe started do not run on into the next one
        self.ex(c, "command -v docker >/dev/null && docker ps -q --filter name=rs-worker- | xargs -r docker stop >/dev/null 2>&1 || true")
        paths = " ".join(shlex.quote(x) for x in PROBE_STATE)
        self.ex(c, f"for d in {paths}; do [ -e \"$d\" ] && mv \"$d\" \"/tmp/bench-probe-{n}-$(echo $d | tr / _)\"; done; "
                   "tar -x -f /tmp/bench-state.tar -P")

    def score(self, it: dict, key: dict, res: dict, c) -> dict:
        chk = it["check"]
        if chk["type"] == "files":
            m = len(chk["paths"])
            if "fact" in chk:                   # file i holds item i of the fact
                vals = key["facts"][chk["fact"]]
                n = sum(1 for x, v in zip(chk["paths"], vals)
                        if grade_number(self.ex(c, f"cat {shlex.quote(x)} 2>/dev/null").stdout.decode("utf-8", "replace")[:2000],
                                        v, chk["tol"])["score"] == 1)
                what = f"{n} of {m} files hold their value"
            else:
                n = sum(1 for x in chk["paths"] if self.ex(c, f"test -s {shlex.quote(x)}").returncode == 0)
                what = f"{n} of {m} files there"
            return {"score": 1 if n == m else 0.5 if n else 0, "why": what}
        ans = res.get("answer")
        if ans is None:
            return {"score": None, "why": res.get("error", "no answer")}
        if chk["type"] == "number":
            return grade_number(ans, key["facts"][chk["fact"]], chk["tol"])
        if chk["type"] == "contains":
            return grade_contains(ans, chk.get("any", []), chk.get("none", []))
        g = self.grade_text(key["answers"][chk["key"]], it["question"], ans)
        if chk.get("paths") == "written-by-sessions":
            # the PI's input data neither counts for nor against an answer, and
            # naming the workspace itself names nothing
            named = sorted({x for x in map(clean_path, PATH_RE.findall(ans))
                            if x != "/workspace" and x != "/workspace/data" and not x.startswith("/workspace/data/")})
            # a file the sessions wrote, or a folder holding one
            there = [x for x in named if x in self.session_files
                     or any(f.startswith(x + "/") for f in self.session_files)]
            g["paths"] = {"named": named, "written_in_sessions": there}
            if not there or len(there) < len(named):
                g["score"], g["why"] = 0, ("names no file the sessions wrote" if not there
                                           else "names a file the sessions did not write")
        return g

    def grade_text(self, key_text: str, question: str, answer: str) -> dict:
        prompt = GRADE_PROMPT.format(key=key_text, question=question, answer=answer)
        r = self.ask("floor", self.grader_c, prompt, grader=True)
        if r.get("answer") is None:
            g = {"score": None, "why": f"grader failed: {r.get('error')}"}
        else:
            g = parse_grade(r["answer"])
        g["grader_usage"] = r.get("usage")       # part of what the graded eval costs
        return g

    def self_check(self, key) -> dict:
        n = f"{key['facts']['income_per_year_of_age']:.0f}"
        num_right = grade_number(f"income rises by about {n} EUR per year of age", key["facts"]["income_per_year_of_age"], 0.05)
        num_wrong = grade_number("income does not change with age", key["facts"]["income_per_year_of_age"], 0.05)
        q = "Should I control for both region and region_code in a regression?"
        text_right = self.grade_text(key["answers"]["negative_knowledge"], q,
                                     "No: region_code is just the region numbered, so the two are perfectly collinear and the model fails.")
        text_wrong = self.grade_text(key["answers"]["negative_knowledge"], q,
                                     "Yes, controlling for both is the most robust choice.")
        ok = (num_right["score"] == 1 and num_wrong["score"] == 0
              and text_right.get("score") == 1 and text_wrong.get("score") == 0)
        return {"numeric": [num_right, num_wrong], "text": [text_right, text_wrong], "ok": ok}

    # ------------------------------------------------------------ the run
    def generate(self, e) -> "tuple[Path, dict]":
        d = e.get("data", {})
        gen = self.out / "generated" / e["name"]
        args = [sys.executable, str(HERE / "generate.py"), "--seed", str(d.get("seed", 7)), "--out", str(gen)]
        if d.get("respondents"):
            args += ["--respondents", str(d["respondents"])]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:                   # a seed whose data breaks an eval's premise
            raise SystemExit(f"eval {e['name']}: the data for seed {d.get('seed', 7)} breaks a premise "
                             f"of the evals; pick another seed in its eval file:\n{r.stderr.strip()[-1500:]}")
        return gen, json.loads((gen / "key.json").read_text())

    def run_one(self, e, arm, gen: Path, key: dict) -> dict:
        res = {"sessions": [], "probes": [], "checks": [], "tokens": 0, "_t0": time.time()}
        watch = [x for ch in e.get("checks", []) for x in ch["check"]["paths"]]
        p, c = self.setup(e["name"], arm, gen)
        try:
            for s in e["sessions"]:
                res["sessions"].append(self.play(arm, p, c, s, res, watch))
            if ARMS[arm]["memory"]:
                self.wait(lambda: not self.consolidating(c), self.a.turn_timeout)
            # the workspace as the sessions left it, before any probe can write to it
            kills = [m for s in res["sessions"] for m in s["messages"] if "mid_turn" in m]
            for ch in e.get("checks", []):
                g = self.score(ch, key, {}, c)
                paths = ch["check"]["paths"]
                if nothing_left_to_resume(kills, paths):
                    g = {"score": None, "why": "the kill did not land mid-task: nothing was left to resume"}
                res["checks"].append({"id": ch["id"], **g})
            self.snapshot(c)
            for n, pr in enumerate(e.get("probes", [])):
                self.restore(c, n)
                r = self.ask(arm, c, pr["question"])
                entry = {"id": pr["id"], **r}
                res["probes"].append(entry)        # kept even when it takes the eval over its cap
                self.spend(res, r.get("usage"))
                entry.update(self.score(pr, key, r, c))
                self.spend(res, entry.get("grader_usage"))
        except ArmFailed as x:
            res["sessions"].append(x.args[0])
            res["failed"] = x.args[0]["error"]
        except OverBudget as x:
            if getattr(x, "session", None) is not None:
                res["sessions"].append(x.session)
            res["over_budget"] = str(x)
        finally:
            res["seconds"] = round(time.time() - res.pop("_t0"), 1)
            self.destroy(p)
        return res

    def run(self, evals: "list[dict]") -> None:
        a = self.a
        self.refuse_existing([self.project_name(e["name"], arm) for e in evals for arm in a.arms]
                             + [f"bench-{a.run}-grader"])
        self.out.mkdir(parents=True, exist_ok=True)
        data = {}
        for e in evals:                         # every key checked before any project exists
            data[e["name"]] = self.generate(e)
            try:
                check_against_key(e, data[e["name"]][1])
            except EvalError as x:
                raise SystemExit(f"eval file: {x}")
        results = {"run": a.run, "standin": a.standin, "arms": a.arms, "caps": {
            "tokens": a.max_tokens, "seconds": a.max_seconds}, "evals": {}}
        try:
            if plan(evals, a.arms)["grader_calls"]:
                self.grader_c = self.setup_grader()
                _gen, key = self.generate({"name": "self-check", "data": {"seed": 7, "respondents": 60}})
                results["grader_self_check"] = self.self_check(key)
                if not a.standin and not results["grader_self_check"]["ok"]:
                    raise SystemExit(f"the grader misgraded the self-check: {results['grader_self_check']}")
            for e in evals:
                results["evals"][e["name"]] = {}
                for arm in a.arms:
                    results["evals"][e["name"]][arm] = self.run_one(e, arm, *data[e["name"]])
                    self.save(results)
        finally:
            self.save(results)
            if not a.keep:
                for p in list(self.made):
                    self.rs("project", "destroy", p, check=False)
        print(f"results: {self.out / 'results.json'}\n{(self.out / 'table.md').read_text()}")

    def save(self, results) -> None:
        (self.out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        (self.out / "table.md").write_text(table(results))


# ---------------------------------------------------------------- pure helpers
def nothing_left_to_resume(kills: "list[dict]", paths: "list[str]") -> bool:
    """A killed session that was not mid-turn, or had written every file the
    check looks at, left nothing to resume: the check would measure nothing."""
    return any(not m.get("mid_turn") or set(paths) <= set(m.get("present_at_kill", [])) for m in kills)


def side_transcript(p: str, others: "dict | set | None" = None) -> bool:
    """A transcript that is not the session's own: a research worker's log,
    or a subagent's, which both agents keep under the session that started it
    (Claude: <session>/subagents/…, pi-subagents: <session>/<run>/…)."""
    if p.startswith("/workspace/workers/") or "/subagents/" in p:
        return True
    return any(q != p and p.startswith(q[:-len(".jsonl")] + "/") for q in (others or ()))


def usage_records(jsonl: str) -> "list[dict]":
    """One record per model call in an agent transcript (Claude Code's and
    pi's session files): plain input, cached input (read + write) and output
    tokens. Claude Code writes one line per content block with the same
    message id and usage: counted once per (message id, request id). Usage
    quoted inside a tool's result (a subagent's summary) and a stream-json
    run's total are never counted; the subagent's own transcript is."""
    out, seen = [], set()
    for line in jsonl.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") == "result":
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        ident = (msg.get("id"), obj.get("requestId"))
        if msg.get("id") is not None:
            if ident in seen:
                continue
            seen.add(ident)
        for u in _usages(obj):
            num = lambda *ks: sum(int(u[k]) for k in ks if isinstance(u.get(k), (int, float)))
            rec = {"input": num("input_tokens", "input"),
                   "cached": num("cache_read_input_tokens", "cache_creation_input_tokens", "cacheRead", "cacheWrite"),
                   "output": num("output_tokens", "output")}
            if any(rec.values()):
                out.append(rec)
    return out


def _usages(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "toolUseResult":
                continue
            if k == "usage" and isinstance(v, dict):
                yield v
            else:
                yield from _usages(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _usages(v)


NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def grade_number(answer: str, value: float, tol: float) -> dict:
    """1 when a number in the answer is within `tol` (relative) of the key's."""
    for m in NUM_RE.findall(answer or ""):
        try:
            x = float(m.replace(",", ""))
        except ValueError:
            continue
        if value and abs(x - value) <= tol * abs(value):
            return {"score": 1, "why": f"{x:g} is within {tol:.0%} of {value:g}"}
    return {"score": 0, "why": f"no number within {tol:.0%} of {value:g}"}


def grade_contains(answer: str, any_of: "list[str]", none_of: "list[str]") -> dict:
    """1 when the answer holds one of `any_of` and none of `none_of` (case
    ignored)."""
    a = (answer or "").lower()
    hit = [w for w in any_of if w.lower() in a]
    bad = [w for w in none_of if w.lower() in a]
    if hit and not bad:
        return {"score": 1, "why": f"says {hit[0]!r}"}
    return {"score": 0, "why": f"says {bad[0]!r}" if bad else f"says none of {any_of}"}


def clean_path(p: str) -> str:
    """A path as an answer writes it, without a line number or the markdown
    and punctuation around it (`/workspace/a.py:42`, `**/workspace/a.py**`)."""
    p = re.sub(r":\d+(:\d+)?$", "", p)
    return p.rstrip("*:|>.!?/")


def parse_grade(text: str) -> dict:
    """The grader's JSON object, from the first `{` to the last `}`; anything
    else is a grading failure, scored as missing (never as 0)."""
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < i:
        return {"score": None, "why": "grader gave no JSON", "raw": text[:500]}
    try:
        d = json.loads(text[i:j + 1])
    except ValueError:
        return {"score": None, "why": "grader gave bad JSON", "raw": text[:500]}
    s = d.get("score")
    if isinstance(s, bool) or s not in (0, 0.5, 1):
        return {"score": None, "why": f"grader gave score {s!r}", "raw": text[:500]}
    return {"score": s, "why": str(d.get("why", ""))[:500]}


def _tok(u: "dict | None") -> str:
    u = u or {}
    return f"{u.get('input', 0)}+{u.get('cached', 0)}c/{u.get('output', 0)}"


def table(results: dict) -> str:
    evals = results.get("evals", {})
    head = (f"Run {results['run']}" + (" (STAND-IN models: scores mean nothing)" if results.get("standin") else ""))
    if not evals:
        return head + "\n\nno eval started\n"
    lines = [head, "", "Scores (1 right, 0.5 partly, 0 wrong, — missing) and what each eval cost per project type:", ""]
    for name, by_arm in evals.items():
        arms = list(by_arm)
        sep = "|---|" + "---|" * len(arms)
        lines += [f"### {name}", "", "| | " + " | ".join(arms) + " |", sep]
        ids = []
        for arm in arms:
            for x in by_arm[arm].get("probes", []) + by_arm[arm].get("checks", []):
                if x["id"] not in ids:
                    ids.append(x["id"])
        for i in ids:
            row = []
            for arm in arms:
                x = next((y for y in by_arm[arm].get("probes", []) + by_arm[arm].get("checks", []) if y["id"] == i), {})
                s = x.get("score")
                cell = "—" if s is None else f"{s:g}"
                if x.get("usage"):
                    cell += f" ({_tok(x['usage'])}, {x.get('seconds', '—')}s)"
                row.append(cell)
            lines.append(f"| {i} | " + " | ".join(row) + " |")
        cost = []
        for arm in arms:
            r = by_arm[arm]
            cell = f"{r.get('tokens', 0)} tok, {r.get('seconds', '—')}s"
            kills = [m for s in r.get("sessions", []) for m in s.get("messages", []) if "mid_turn" in m]
            if kills:
                cell += ", kill mid-turn " + ("yes" if all(m["mid_turn"] for m in kills) else "NO")
            cold = next((m["usage"].get("first_input") for s in r.get("sessions", [])
                         for m in s.get("messages", []) if (m.get("usage") or {}).get("first_input") is not None), None)
            if cold is not None:
                cell += f", cold {cold}"
            if r.get("over_budget"):
                cell += f" — OVER BUDGET: {r['over_budget']}"
            if r.get("failed"):
                cell += f" — FAILED: {r['failed'][:120]}"
            cost.append(cell)
        lines += ["| **cost** | " + " | ".join(cost) + " |", ""]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", required=True, help="a name for this run (results folder, project names)")
    ap.add_argument("--evals", nargs="+", help="the evals to run (default: every one not marked optional)")
    ap.add_argument("--arms", nargs="+", default=list(DEFAULT_ARMS), choices=list(ARMS))
    ap.add_argument("--dry-run", action="store_true", help="print what the run will do and exit")
    ap.add_argument("--standin", action="store_true", help="stand-in models: zero tokens, plumbing only")
    ap.add_argument("--claude-token-file", help="a `claude setup-token` token, one line")
    ap.add_argument("--pi-model", help="provider/model for pi")
    ap.add_argument("--pi-key-file", help="the pi provider's API key, one line")
    ap.add_argument("--pi-key-env", help="the variable pi reads the key from (known providers need none)")
    ap.add_argument("--turn-timeout", type=int, default=TURN_TIMEOUT, help=f"seconds one turn may take (default {TURN_TIMEOUT})")
    ap.add_argument("--kill-after", type=int, default=KILL_AFTER,
                    help=f"seconds into the last message of a killed session that sets none (default {KILL_AFTER})")
    # Caps per eval and project type, until the first real run measures what
    # one costs: a short eval is about 10-15 model calls of mostly cached
    # input (estimate), so twice that estimate stops a runaway, and its time
    # is mostly the project's create.
    ap.add_argument("--max-tokens", type=int, default=2_000_000, help="tokens per eval and project type (default 2,000,000)")
    ap.add_argument("--max-seconds", type=int, default=1200, help="seconds per eval and project type (default 1200); "
                    "the caps are checked after each step, so one step can run past them by up to --turn-timeout")
    ap.add_argument("--keep", action="store_true", help="keep the projects")
    a = ap.parse_args()
    try:
        all_evals = load_evals()
    except EvalError as e:
        ap.error(f"eval file: {e}")
    names = a.evals or [n for n, e in all_evals.items() if not e.get("optional")]
    unknown = [n for n in names if n not in all_evals]
    if unknown:
        ap.error(f"unknown eval(s) {', '.join(unknown)}; there are {', '.join(all_evals)}")
    evals = [all_evals[n] for n in names]
    if a.dry_run:
        p = plan(evals, a.arms)
        print(f"{len(evals)} eval(s) × {len(a.arms)} project type(s); caps per pair: "
              f"{a.max_tokens} tokens, {a.max_seconds} s")
        for r in p["rows"]:
            print(f"  {r['eval']:<14} {r['arm']:<18} {r['sessions']} session(s), {r['turns']} turn(s), "
                  f"{r['probes']} probe(s), {r['grader_calls']} grader call(s), up to {r['consolidations']} consolidation(s)")
        print(f"agent calls: {p['agent_calls']} (plus the consolidations), grader calls: {p['grader_calls']}")
        return
    if not a.standin:
        need = []
        if not a.claude_token_file:
            need.append("--claude-token-file (the Claude arms and the grader)")
        if "researcher-pi" in a.arms and not (a.pi_model and a.pi_key_file):
            need.append("--pi-model and --pi-key-file (the pi arm)")
        if need:
            ap.error("a real run needs " + ", ".join(need) + " (or --standin)")
    # a TERM (or a hang-up) still runs the clean-up: projects never outlive the run
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(130))
    Bench(a).run(evals)


if __name__ == "__main__":
    main()
