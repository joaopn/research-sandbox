#!/usr/bin/env python3
"""The researcher benchmark: the same scripted sessions played to several
project types, then probes asked of a cold-started agent and graded against
a key written from the data's planted facts.

Run from the repository root of an RS whose stack is up (`research.py start`),
with the agent downloads and the reader pulled. It creates its own projects,
`bench-<run>-<arm>` (and `-grader`), refuses to start when one exists, and
destroys them at the end (--keep keeps them).

    python3 tests/bench/researcher/bench.py --run r1 --seed 7 \
        --claude-token-file /workspace/.bench/claude-token \
        --pi-model anthropic/claude-sonnet-4-5 --pi-key-file /workspace/.bench/pi-key

    python3 tests/bench/researcher/bench.py --run dry --seed 7 --standin

It spends real tokens on every run unless --standin is given (stand-in
models: the plumbing, zero tokens, scores meaningless). Results go to
tests/bench/results/<run>/ (results.json and table.md), which git ignores.
Secrets never reach a command line: each project gets them in a 0600 file
under its own /dev/shm, written over stdin and sourced by the agent's shell.

Arms:
  research          the existing research flow (Claude as supervisor; the
                    PI's /log at each session's end, its own memory)
  researcher        the researcher type with Claude
  researcher-pi     the researcher type with pi
  researcher-nohook the researcher type with Claude, its start hook removed
  floor             a plain sandbox-dind project with Claude (no memory kit;
                    Claude's built-in memory on)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
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
    "research": {"workflow": "research", "agent": "claude", "memory": False, "log": True},
    "researcher": {"workflow": "researcher", "agent": "claude", "memory": True},
    "researcher-pi": {"workflow": "researcher", "agent": "pi", "memory": True},
    "researcher-nohook": {"workflow": "researcher", "agent": "claude", "memory": True, "nohook": True},
    "floor": {"workflow": "sandbox-dind", "agent": "claude", "memory": False},
}
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
PATH_PROBES = ("provenance", "interruption")


class ArmFailed(Exception):
    """An arm that cannot go on (its agent never came up): the run records it
    and moves to the next arm instead of typing into a dead session for hours."""


class Bench:
    def __init__(self, a):
        self.a = a
        self.out = ROOT / "tests" / "bench" / "results" / a.run
        self.made: "list[str]" = []

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
    def project_name(self, arm) -> str:
        return f"bench-{self.a.run}-{arm}"

    def refuse_existing(self) -> None:
        for name in [self.project_name(x) for x in self.a.arms] + [self.project_name("grader")]:
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

    def setup(self, arm) -> "tuple[str, str]":
        spec = ARMS[arm]
        p = self.project_name(arm)
        args = ["--workflow", spec["workflow"]]
        if spec["workflow"] != "research":
            args += ["--agent", spec["agent"]]
        c = self.create(p, *args)
        buf = io.BytesIO()                        # the data, as the PI would give it: /workspace/data
        with tarfile.open(fileobj=buf, mode="w") as t:
            t.add(self.gen / "data", arcname="data")
        self.ex(c, "tar -x -C /workspace", inp=buf.getvalue())
        self.seed_claude(c)
        self.stage_secrets(c, arm)
        if spec.get("nohook"):
            self.ex(c, f"f={RH}/.claude/settings.json; jq '.hooks.SessionStart |= map(select(all(.hooks[]?; "
                       f"(.command // \"\") | test(\"rs-memory open\") | not)))' $f > $f.t && mv $f.t $f")
        if spec["workflow"] == "research" and not self.a.standin:
            # the supervisor hands its credentials file to the workers it
            # spawns: the token is written there (the project is destroyed at
            # the end of the run)
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
        cached input (read + write), output; and the input side of the first
        call of the main session (what it loaded cold), never a subagent's."""
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

    def play(self, arm, p, c, session) -> dict:
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
        for i, msg in enumerate(msgs):
            if i == len(msgs) - 1 and session["end"] == "kill":
                hb = self.count(p, "handbacks")
                before = killed_before = self.transcripts(c, arm)
                t0 = time.time()
                self.send(c, name, msg)
                started = self.wait(lambda: self.count(p, "working") > 0, 120)
                time.sleep(self.a.kill_after)
                mid = self.count(p, "handbacks") == hb      # still working when killed
                self.tmux(c, "kill-session", "-t", name)
                rec["messages"].append({"i": i, "killed_after_s": round(time.time() - t0, 1),
                                        "working_seen": started, "mid_turn": mid,
                                        "usage": self.usage_since(c, arm, before)})
                self.wait(lambda: self.count(p, "waiting") <= w0, 60)
                break
            rec["messages"].append({"i": i, **self.turn(arm, p, c, name, msg)})
        # the consolidation starts at the session's end event and can be done
        # before the next line runs: count from before the end (for a killed
        # session, from before its last message: the kill is its end)
        before_end = killed_before if killed_before is not None else self.transcripts(c, arm)
        if session["end"] == "exit":
            if ARMS[arm].get("log"):
                rec["log"] = self.turn(arm, p, c, name, "/log")   # the research flow's own memory
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
        rec["seconds"] = round(time.time() - t_start, 1)
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
            return {"answer": None, "error": "timed out", "seconds": round(time.time() - t0, 1)}
        # pi starts its session-end close detached; it must be done before
        # anything is restored
        self.wait(lambda: self.ex(c, "for f in /proc/[0-9]*/cmdline; do tr '\\0' ' ' < $f 2>/dev/null; echo; done "
                                     # [ ]: this scan's own command line must not match
                                     "| grep -q 'rs-memory[ ]close'").returncode != 0, 60)
        # and for the consolidation that close may have started: its tokens
        # are reported apart (usage_since), never as the probe's cost
        self.wait(lambda: not self.consolidating(c), T)
        if r.returncode in (124, 137):                # timeout, or its forced kill
            return {"answer": None, "error": "timed out", "seconds": round(time.time() - t0, 1)}
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

    def grade(self, pr: dict, key: dict, res: dict, c) -> dict:
        ans = res.get("answer")
        if ans is None:
            return {"score": None, "why": res.get("error", "no answer")}
        if pr["key"] in key.get("numeric", {}):
            fact, tol = key["numeric"][pr["key"]]
            return grade_number(ans, key["facts"][fact], tol)
        g = self.grade_text(key["answers"][pr["key"]], pr["question"], ans)
        if pr["id"] in PATH_PROBES:
            # the PI's input data neither counts for nor against an answer, and
            # naming the workspace itself names nothing
            named = sorted({clean_path(x) for x in PATH_RE.findall(ans)}
                           - {"/workspace"} - {x for x in map(clean_path, PATH_RE.findall(ans))
                                               if x == "/workspace/data" or x.startswith("/workspace/data/")})
            # a file the sessions wrote, or a folder holding one
            there = [x for x in named if x in self.session_files
                     or any(f.startswith(x + "/") for f in self.session_files)]
            g["paths"] = {"named": named, "written_in_sessions": there}
            if pr["id"] == "provenance" and (not there or len(there) < len(named)):
                g["score"], g["why"] = 0, ("names no file the sessions wrote" if not there
                                           else "names a file the sessions did not write")
        return g

    def grade_text(self, key_text: str, question: str, answer: str) -> dict:
        prompt = GRADE_PROMPT.format(key=key_text, question=question, answer=answer)
        r = self.ask("floor", self.grader_c, prompt, grader=True)
        if r.get("answer") is None:
            return {"score": None, "why": f"grader failed: {r.get('error')}"}
        return parse_grade(r["answer"])

    # ------------------------------------------------------------ the run
    def setup_grader(self) -> str:
        c = self.create(self.project_name("grader"), "--workflow", "sandbox-dind", "--agent", "claude")
        self.seed_claude(c)
        # the grader sees only the key, the question and the answer: no
        # built-in memory, no tools, an empty folder
        self.stage_secrets(c, "floor", {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        self.ex(c, "mkdir -p /tmp/grader")
        if self.a.standin:
            self.start_standin(c)
        return c

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

    def run(self) -> None:
        a = self.a
        self.refuse_existing()
        self.out.mkdir(parents=True, exist_ok=True)
        self.gen = self.out / "generated"
        subprocess.run([sys.executable, str(HERE / "generate.py"), "--seed", str(a.seed), "--out", str(self.gen)],
                       check=True, capture_output=True)
        key = json.loads((self.gen / "key.json").read_text())
        script = json.loads((HERE / "script.json").read_text())
        probes = json.loads((HERE / "probes.json").read_text())["probes"]
        results = {"run": a.run, "seed": a.seed, "standin": a.standin, "facts": key["facts"], "arms": {}}
        try:
            self.grader_c = self.setup_grader()
            results["grader_self_check"] = self.self_check(key)
            if not a.standin and not results["grader_self_check"]["ok"]:
                raise SystemExit(f"the grader misgraded the self-check: {results['grader_self_check']}")
            for arm in a.arms:
                p, c = self.setup(arm)
                arm_res = {"sessions": [], "probes": []}
                results["arms"][arm] = arm_res
                try:
                    for s in script["sessions"]:
                        arm_res["sessions"].append(self.play(arm, p, c, s))
                        self.save(results)
                except ArmFailed as e:
                    arm_res["sessions"].append(e.args[0])
                    arm_res["failed"] = e.args[0]["error"]
                    self.save(results)
                    continue
                self.wait(lambda: not self.consolidating(c), a.turn_timeout)
                self.snapshot(c)
                for n, pr in enumerate(probes):
                    self.restore(c, n)
                    res = self.ask(arm, c, pr["question"])
                    arm_res["probes"].append({"id": pr["id"], **res, **self.grade(pr, key, res, c)})
                    self.save(results)
        finally:
            self.save(results)
            if not a.keep:
                for p in self.made:
                    self.rs("project", "destroy", p, check=False)
        print(f"results: {self.out / 'results.json'}\n{(self.out / 'table.md').read_text()}")

    def save(self, results) -> None:
        (self.out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        (self.out / "table.md").write_text(table(results))


# ---------------------------------------------------------------- pure helpers
def usage_records(jsonl: str) -> "list[dict]":
    """One record per model call in an agent transcript (Claude Code's and
    pi's session files): plain input, cached input (read + write) and output
    tokens. Claude Code writes one line per content block with the same
    message id and usage: counted once per (message id, request id). Usage
    quoted inside a tool's result (a subagent's summary) is never counted;
    the subagent's own transcript is."""
    out, seen = [], set()
    for line in jsonl.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") == "result":
            continue                              # a stream-json run's total repeats its calls
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


def side_transcript(p: str, others: "dict | set | None" = None) -> bool:
    """A transcript that is not the session's own: a research worker's log,
    or a subagent's, which both agents keep under the session that started it
    (Claude: <session>/subagents/…, pi-subagents: <session>/<run>/…)."""
    if p.startswith("/workspace/workers/") or "/subagents/" in p:
        return True
    return any(q != p and p.startswith(q[:-len(".jsonl")] + "/") for q in (others or ()))


NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def clean_path(p: str) -> str:
    """A path as an answer writes it, without a line number or the markdown
    and punctuation around it (`/workspace/a.py:42`, `**/workspace/a.py**`)."""
    p = re.sub(r":\d+(:\d+)?$", "", p)
    return p.rstrip("*:|>.!?/")


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
    arms = list(results.get("arms", {}))
    if not arms:
        return "no arm started\n"
    head = (f"Run {results['run']}, seed {results['seed']}"
            + (" (STAND-IN models: scores mean nothing)" if results.get("standin") else ""))
    sep = "|---|" + "---|" * len(arms)
    lines = [head, "", "Scores (1 right, 0.5 partly, 0 wrong, — missing):", "",
             "| probe | " + " | ".join(arms) + " |", sep]
    ids = []
    for arm in arms:
        for x in results["arms"][arm]["probes"]:
            if x["id"] not in ids:
                ids.append(x["id"])
    for pid in ids:
        row = []
        for arm in arms:
            x = next((y for y in results["arms"][arm]["probes"] if y["id"] == pid), {})
            s = x.get("score")
            row.append("—" if s is None else f"{s:g}")
        lines.append(f"| {pid} | " + " | ".join(row) + " |")
    tot = []
    for arm in arms:
        sc = [x.get("score") for x in results["arms"][arm]["probes"]]
        tot.append(f"{sum(s for s in sc if s is not None):g}/{len(sc)}")
    lines.append("| **total** | " + " | ".join(tot) + " |")
    lines += ["", "What each probe cost (tokens plain+cached c/output; seconds): a remembered answer is cheap, a recomputed one is not.", "",
              "| probe | " + " | ".join(arms) + " |", sep]
    for pid in ids:
        row = []
        for arm in arms:
            x = next((y for y in results["arms"][arm]["probes"] if y["id"] == pid), {})
            row.append(f"{_tok(x.get('usage'))}, {x.get('seconds', '—')}s" if x else "—")
        lines.append(f"| {pid} | " + " | ".join(row) + " |")
    lines += ["", "Sessions (tokens; cold = what the first call loaded; mid-turn = the kill landed while it worked):", "",
              "| session | " + " | ".join(arms) + " |", sep]
    names = []
    for arm in arms:
        for s in results["arms"][arm]["sessions"]:
            if s["session"] not in names:
                names.append(s["session"])
    for n in names:
        row = []
        for arm in arms:
            s = next((y for y in results["arms"][arm]["sessions"] if y["session"] == n), None)
            if s is None:
                row.append("—")
                continue
            us = [m.get("usage") or {} for m in s.get("messages", [])]
            agg = {k: sum(u.get(k, 0) for u in us) for k in ("input", "cached", "output")}
            cold = next((u["first_input"] for u in us if u.get("first_input") is not None), None)
            cell = f"{_tok(agg)}, cold {cold if cold is not None else '—'}"
            if s.get("consolidation_usage"):
                cell += f", consolidation {_tok(s['consolidation_usage'])}"
            if (s.get("log") or {}).get("usage"):
                cell += f", log {_tok(s['log']['usage'])}"
            if any("mid_turn" in m for m in s.get("messages", [])):
                cell += ", mid-turn " + ("yes" if all(m.get("mid_turn", True) for m in s["messages"]) else "NO")
            if s.get("error") or s.get("note") or s.get("exit"):
                cell += f" ({s.get('error') or s.get('note') or s.get('exit')})"
            row.append(cell)
        lines.append(f"| {n} | " + " | ".join(row) + " |")
    lines += ["", "Capability reuse (the per-wave table, built in s2, asked again in s5):", "",
              "| use | " + " | ".join(arms) + " |", sep]
    for label, sname, i in (("first (s2)", "s2", 1), ("again (s5)", "s5", 0)):
        row = []
        for arm in arms:
            s = next((y for y in results["arms"][arm]["sessions"] if y["session"] == sname), {})
            m = next((y for y in s.get("messages", []) if y.get("i") == i), None)
            row.append(f"{m.get('seconds', '—')}s, {_tok(m.get('usage'))}" if m else "—")
        lines.append(f"| {label} | " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", required=True, help="a name for this run (results folder, project names)")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--arms", nargs="+", default=list(ARMS), choices=list(ARMS))
    ap.add_argument("--standin", action="store_true", help="stand-in models: zero tokens, plumbing only")
    ap.add_argument("--claude-token-file", help="a `claude setup-token` token, one line")
    ap.add_argument("--pi-model", help="provider/model for pi")
    ap.add_argument("--pi-key-file", help="the pi provider's API key, one line")
    ap.add_argument("--pi-key-env", help="the variable pi reads the key from (known providers need none)")
    # A research turn can run long (installs, model fits); half of this cuts
    # real work short, ten times it lets a hung session hold the run for hours.
    ap.add_argument("--turn-timeout", type=int, default=1800, help="seconds one turn may take (default 1800)")
    # The interrupted session is killed this long after its last message
    # started working: long enough to be mid-task, short enough not to finish.
    ap.add_argument("--kill-after", type=int, default=60, help="seconds into the last message of a killed session")
    ap.add_argument("--keep", action="store_true", help="keep the projects")
    a = ap.parse_args()
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
    Bench(a).run()


if __name__ == "__main__":
    main()
