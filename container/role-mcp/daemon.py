"""role-mcp daemon — long-lived MCP server inside a per-role container.

Runs inside `rs-<role>-mcp` containers on each supervisor's inner dockerd
(rs-inner bridge, pinned IP from `research.py`'s allocation table). Exposes
three MCP tools that workers reach via the per-supervisor mcp-proxy at
``http://mcp-proxy:8888/<role>/mcp``.

The daemon is a thin coordinator: callers (analysis workers) hit ``send_job``
with a task, the daemon spawns ``claude -p`` with the role's ``role.md`` as
its CLAUDE.md, captures stream-json on disk, and returns either the final
result (sync mode) or a job id to poll later (async mode). The spawned
``claude -p`` writes a per-call log under ``memories/<caller>/<call_id>.md``
following the five-section template; ``summarize_memories`` walks new logs
past a high-water mark, distills them into ``global.md``, and updates the
mark.

Why these design choices (see PLAN/STAGE_BACKEND_MCP_B0.md for the full
rationale):
- File-backed JobTable so daemon restarts don't lose state.
- One spawned ``claude -p`` per call, not a persistent session — the
  experience accumulation lives in markdown, not in claude-side memory.
- Per-call log is the role-worker's responsibility (it has the semantic
  context); daemon writes a stub on miss to bound the failure mode.
- High-water mark is per-caller because callers progress independently and
  a global mark would skip work for a caller that just appeared.

Listen port + role workspace are environment-driven so the same image can
host any role (per-role Dockerfile bakes in role.md + summarize.md, env
vars supply the rest)."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import web

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Listen port. Same constant inside every role-MCP container — each lives in
# its own network namespace, no contention. Overridable for testing.
PORT = int(os.environ.get("RS_ROLE_MCP_PORT", "8000"))

# Role name (echo-mcp, wrangler, librarian, websearcher). Set by the
# supervisor at `docker run` time; the daemon never guesses.
ROLE = os.environ.get("RS_ROLE_NAME") or ""

# Per-role workspace bind-mount. Contains jobs/, memories/, global.md,
# .summarize-watermark, and any role-specific cache the role-worker writes.
WORKSPACE = Path(os.environ.get("RS_ROLE_WORKSPACE", "/workspace"))

# Per-call dir baked from this template — the spawned claude -p sees these
# files as its CLAUDE.md + (when present) .mcp.json + system context. The
# per-role image bakes role.md and summarize.md at image build time.
ROLE_TEMPLATE_DIR = Path(os.environ.get("RS_ROLE_TEMPLATE_DIR",
                                         "/opt/role-mcp/role"))

# .mcp.json for the spawned claude -p, written by the entrypoint at start
# from the intersection of role-mcps.json's `upstream_mcps` and mcp-allow.json.
# Empty/missing => spawned claude has no MCP wiring (echo's case).
SPAWN_MCP_CONFIG = Path(os.environ.get("RS_ROLE_SPAWN_MCP_CONFIG",
                                        "/etc/role-mcp/spawn-mcp.json"))

SPAWN_SH = Path(os.environ.get("RS_ROLE_SPAWN_SH",
                                "/opt/role-mcp/spawn.sh"))

# Directories the daemon owns on the workspace bind-mount.
JOBS_DIR = WORKSPACE / "jobs"
MEMORIES_DIR = WORKSPACE / "memories"
CALLS_DIR = WORKSPACE / ".calls"
WATERMARK_PATH = WORKSPACE / ".summarize-watermark"
GLOBAL_MD = WORKSPACE / "global.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_call_id() -> str:
    """Sortable, collision-resistant call id. Timestamp prefix keeps
    `ls jobs/ | sort` chronological; 4-byte random suffix avoids a clash
    when two callers hit at the same second."""
    return f"{_iso_now().replace(':', '')}-{secrets.token_hex(4)}"


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Job table
# ---------------------------------------------------------------------------


class JobTable:
    """File-backed job state. Each job is one JSON file under jobs/. State
    survives daemon restart; in-memory cache is for hot access only and is
    rebuilt from disk on startup. On restart, jobs marked `running` whose
    pid is gone get flipped to `failed` with reason `daemon_restart_orphan`
    — otherwise a polling caller would block forever."""

    def __init__(self) -> None:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def register(self, caller: str, mode: str) -> tuple[str, str]:
        """Allocate (job_id, call_id). job_id == call_id in v1 (no retries
        means one allocation per call)."""
        call_id = _new_call_id()
        entry = {
            "job_id": call_id,
            "call_id": call_id,
            "caller": caller,
            "mode": mode,
            "status": "running",
            "started": _iso_now(),
            "completed": None,
            "pid": None,
            "result": None,
            "error": None,
        }
        async with self._lock:
            _atomic_write_json(JOBS_DIR / f"{call_id}.json", entry)
        return call_id, call_id

    async def mark_pid(self, job_id: str, pid: int) -> None:
        async with self._lock:
            entry = self._read(job_id)
            if entry is None:
                return
            entry["pid"] = pid
            _atomic_write_json(JOBS_DIR / f"{job_id}.json", entry)

    async def mark_done(self, job_id: str, result: str | None) -> None:
        async with self._lock:
            entry = self._read(job_id)
            if entry is None:
                return
            entry["status"] = "done"
            entry["completed"] = _iso_now()
            entry["result"] = result
            _atomic_write_json(JOBS_DIR / f"{job_id}.json", entry)

    async def mark_failed(self, job_id: str, error: str) -> None:
        async with self._lock:
            entry = self._read(job_id)
            if entry is None:
                return
            entry["status"] = "failed"
            entry["completed"] = _iso_now()
            entry["error"] = error
            _atomic_write_json(JOBS_DIR / f"{job_id}.json", entry)

    async def query(self, job_id: str) -> dict | None:
        async with self._lock:
            return self._read(job_id)

    def _read(self, job_id: str) -> dict | None:
        path = JOBS_DIR / f"{job_id}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def reconcile_on_startup(self) -> int:
        """Scan jobs/; for any `running` entry whose pid is gone, flip it to
        `failed`. Returns the number of orphans cleaned. Run synchronously
        before the event loop starts so no caller can poll a stale state."""
        cleaned = 0
        if not JOBS_DIR.is_dir():
            return 0
        for path in JOBS_DIR.glob("*.json"):
            try:
                entry = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if entry.get("status") != "running":
                continue
            pid = entry.get("pid")
            if pid is None or not _pid_alive(int(pid)):
                entry["status"] = "failed"
                entry["completed"] = _iso_now()
                entry["error"] = "daemon_restart_orphan"
                _atomic_write_json(path, entry)
                cleaned += 1
        return cleaned


# ---------------------------------------------------------------------------
# Spawner
# ---------------------------------------------------------------------------


class Spawner:
    """Manages the subprocess that runs ``claude -p`` for one call.
    Captures stream-json on disk and the last `result` event's body in
    memory for sync-mode return."""

    def __init__(self, jobs: JobTable) -> None:
        self.jobs = jobs

    def _stage_call_dir(self, call_id: str, caller: str, task: str,
                        *, summarize_mode: bool = False) -> Path:
        """Per-call working directory. send_job calls inherit CLAUDE.md
        from the role template (symlink so the template stays single-
        source-of-truth); summarize calls deliberately don't — they pass
        summarize.md as an explicit system prompt instead, because the
        role.md instructions ("echo task verbatim") would otherwise
        overpower the summarize task body and produce garbage in
        global.md. Task.md is written either way."""
        call_dir = CALLS_DIR / call_id
        call_dir.mkdir(parents=True, exist_ok=True)

        if not summarize_mode:
            role_md = ROLE_TEMPLATE_DIR / "role.md"
            link = call_dir / "CLAUDE.md"
            if link.is_symlink() or link.exists():
                link.unlink()
            if role_md.is_file():
                link.symlink_to(role_md)

        # Preamble — frontmatter values for the per-call log. role.md tells
        # the spawned claude to copy these verbatim into the log it writes.
        # Summarize calls don't write per-call logs (the daemon's stub
        # fallback will fire), but the preamble doesn't hurt.
        preamble = (
            f"caller: {caller}\n"
            f"call_id: {call_id}\n"
            f"ts: {_iso_now()}\n"
            f"memory_path: /workspace/memories/{caller}/{call_id}.md\n\n"
        )
        (call_dir / "task.md").write_text(preamble + task.rstrip() + "\n")
        return call_dir

    async def run(self, call_id: str, caller: str, task: str,
                  *, summarize_mode: bool = False) -> tuple[bool, str]:
        """Spawn claude -p, wait for completion, return (ok, last_result_body).
        The per-call log written by the spawned claude is independent of the
        return value — daemon checks for it after exit; if missing, writes a
        stub. Errors during spawn are mapped to (False, error_message) for
        the caller. summarize_mode swaps the spawn invocation to use
        summarize.md as the system prompt (via --system-prompt-file) and
        skips MCP config + CLAUDE.md auto-discovery."""
        call_dir = self._stage_call_dir(call_id, caller, task,
                                         summarize_mode=summarize_mode)
        env = dict(os.environ)
        env["RS_ROLE_NAME"] = ROLE
        env["RS_CALL_ID"] = call_id
        env["RS_CALLER"] = caller

        # The spawned claude writes its stream-json log to log.jsonl in the
        # per-call dir. The daemon parses the final `result` event from the
        # tail of that file after exit.
        log_path = call_dir / "log.jsonl"
        # Optional 6th arg to spawn.sh: when non-empty, treated as the
        # system-prompt file path and claude is run with --bare +
        # --system-prompt-file (no CLAUDE.md, no MCP wiring).
        system_prompt_file = ""
        if summarize_mode:
            system_prompt_file = str(ROLE_TEMPLATE_DIR / "summarize.md")
        try:
            proc = await asyncio.create_subprocess_exec(
                str(SPAWN_SH),
                call_id, caller,
                str(call_dir / "task.md"),
                str(log_path),
                str(SPAWN_MCP_CONFIG),
                system_prompt_file,
                env=env,
                cwd=str(call_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return False, f"spawn failed: {e}"

        await self.jobs.mark_pid(call_id, proc.pid)
        stdout, stderr = await proc.communicate()

        # Ensure the per-call log exists. If the role-worker didn't write
        # one, drop a stub so summarize sees an entry and the failure
        # mode is bounded.
        memory_path = MEMORIES_DIR / caller / f"{call_id}.md"
        if not memory_path.is_file():
            stub_outcome = "success" if proc.returncode == 0 else "failure"
            err_excerpt = (stderr.decode(errors="replace")[-500:]
                            if stderr else "")
            _atomic_write_text(
                memory_path,
                "---\n"
                f"caller: {caller}\n"
                f"call_id: {call_id}\n"
                f"ts: {_iso_now()}\n"
                f"mode: -\n"
                f"outcome: {stub_outcome}\n"
                f"note: no_log_produced\n"
                "---\n\n"
                "## Question\n\n(role-worker did not write a per-call log)\n\n"
                "## Approach\n\n-\n\n"
                "## What worked\n\n-\n\n"
                "## What failed\n\n"
                f"{err_excerpt or '-'}\n\n"
                "## Lessons\n\n-\n",
            )

        last_result = _extract_last_result(log_path)
        if proc.returncode != 0:
            return False, (last_result
                            or stderr.decode(errors="replace")
                            or f"claude exited {proc.returncode}")
        return True, last_result or ""


def _extract_last_result(log_path: Path) -> str:
    """Pull the last `result` event's body from a stream-json log file.
    claude -p with --output-format stream-json emits one JSON object per
    line; the final assistant message lives in the last event whose
    ``type`` is ``result``. Returns empty string if not found.

    Failure tolerance: bad JSON on any line is skipped. We do not parse
    the whole stream — only the tail until a `result` event is found."""
    if not log_path.is_file():
        return ""
    last = ""
    try:
        with log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "result":
                    last = obj.get("result", "") or ""
    except OSError:
        return ""
    return last if isinstance(last, str) else json.dumps(last)


# ---------------------------------------------------------------------------
# Memory / summarize
# ---------------------------------------------------------------------------


class Memory:
    """Per-call markdown logs + per-caller high-water mark + role-global
    skill memory in global.md. Summarize walks new logs past each caller's
    mark, asks a fresh claude -p (using the role's summarize.md prompt) to
    distill them, appends the result to global.md atomically, and bumps
    the mark."""

    def __init__(self) -> None:
        MEMORIES_DIR.mkdir(parents=True, exist_ok=True)

    def list_new(self) -> dict[str, list[Path]]:
        """Returns {caller: [paths-past-watermark, sorted]}. Files past
        the mark for each caller are sorted lexically (== chronologically
        because call_ids are timestamped). The synthetic `__summarize__`
        caller is excluded — those are the stubs written by summarize's
        own claude invocations and feeding them back in would cause
        unbounded self-recursion across summarize calls."""
        marks = self._load_watermarks()
        out: dict[str, list[Path]] = {}
        if not MEMORIES_DIR.is_dir():
            return out
        for caller_dir in sorted(MEMORIES_DIR.iterdir()):
            if not caller_dir.is_dir():
                continue
            caller = caller_dir.name
            if caller == "__summarize__":
                continue
            mark = marks.get(caller, "")
            files = sorted(p for p in caller_dir.glob("*.md") if p.is_file())
            new = [p for p in files if p.stem > mark]
            if new:
                out[caller] = new
        return out

    def _load_watermarks(self) -> dict[str, str]:
        if not WATERMARK_PATH.is_file():
            return {}
        try:
            data = json.loads(WATERMARK_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def bump_watermarks(self, new_marks: dict[str, str]) -> None:
        """Atomic merge — read current, overlay, write back."""
        marks = self._load_watermarks()
        marks.update(new_marks)
        _atomic_write_json(WATERMARK_PATH, marks)

    def append_global(self, text: str) -> None:
        """Append-only write to global.md. Parent is a bind-mounted
        directory (not single-file), so tmp+rename is safe."""
        prior = GLOBAL_MD.read_text() if GLOBAL_MD.is_file() else ""
        new_content = prior + (
            "" if prior.endswith("\n\n") or not prior else
            ("\n" if prior.endswith("\n") else "\n\n")
        ) + text.rstrip() + "\n"
        _atomic_write_text(GLOBAL_MD, new_content)


# ---------------------------------------------------------------------------
# MCP tool implementations
# ---------------------------------------------------------------------------


TOOLS_SCHEMA = [
    {
        "name": "send_job",
        "description": (
            f"Send a task to the {ROLE} role-MCP. Mode 'sync' blocks until "
            "the spawned role-worker exits and returns the result. Mode "
            "'async' returns a job_id immediately; poll with query_job_status. "
            "Every call gets a per-call log under memories/<caller>/<call_id>.md."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "caller": {
                    "type": "string",
                    "description": "Stable caller identity (e.g. analysis worker name). "
                                   "Used as the memory subdirectory key.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["sync", "async"],
                    "description": "sync: block + return result; async: return job_id.",
                },
                "task": {
                    "type": "string",
                    "description": "Free-form task description for the role-worker.",
                },
            },
            "required": ["caller", "mode", "task"],
        },
    },
    {
        "name": "query_job_status",
        "description": (
            "Look up an async job by id. Returns {status, call_id, result?, error?}. "
            "status is one of running | done | failed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "summarize_memories",
        "description": (
            f"Walk new per-call logs in memories/ past each caller's high-water mark, "
            f"distill them via the role's summarize prompt, append the distillation "
            f"to global.md, and bump the marks. Idempotent: re-invocation with no new "
            f"logs returns {{appended: 0, calls_processed: 0}}."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


async def tool_send_job(jobs: JobTable, spawner: Spawner,
                        args: dict) -> dict:
    caller = args.get("caller")
    mode = args.get("mode")
    task = args.get("task")
    if not isinstance(caller, str) or not caller:
        raise ValueError("'caller' is required")
    if mode not in ("sync", "async"):
        raise ValueError("'mode' must be 'sync' or 'async'")
    if not isinstance(task, str) or not task:
        raise ValueError("'task' is required")

    job_id, call_id = await jobs.register(caller, mode)

    if mode == "sync":
        ok, result = await spawner.run(call_id, caller, task)
        if ok:
            await jobs.mark_done(call_id, result)
            return {"call_id": call_id, "result": result}
        await jobs.mark_failed(call_id, result)
        return {"call_id": call_id, "error": result}

    # async: spawn detached, return immediately.
    async def _bg() -> None:
        ok, result = await spawner.run(call_id, caller, task)
        if ok:
            await jobs.mark_done(call_id, result)
        else:
            await jobs.mark_failed(call_id, result)

    asyncio.create_task(_bg())
    return {"job_id": job_id, "call_id": call_id}


async def tool_query_job_status(jobs: JobTable, args: dict) -> dict:
    job_id = args.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("'job_id' is required")
    entry = await jobs.query(job_id)
    if entry is None:
        raise ValueError(f"no job {job_id!r}")
    out = {"status": entry["status"], "call_id": entry["call_id"]}
    if entry.get("result") is not None:
        out["result"] = entry["result"]
    if entry.get("error") is not None:
        out["error"] = entry["error"]
    return out


async def tool_summarize_memories(memory: Memory, spawner: Spawner) -> dict:
    new_by_caller = memory.list_new()
    if not new_by_caller:
        return {"appended": 0, "calls_processed": 0}

    summarize_md = ROLE_TEMPLATE_DIR / "summarize.md"
    if not summarize_md.is_file():
        raise RuntimeError(f"role missing summarize prompt: {summarize_md}")
    prompt = summarize_md.read_text()

    prior_global = GLOBAL_MD.read_text() if GLOBAL_MD.is_file() else ""
    chunks: list[str] = []
    new_marks: dict[str, str] = {}
    total_calls = 0
    for caller, paths in new_by_caller.items():
        for p in paths:
            chunks.append(f"# {caller} / {p.stem}\n\n{p.read_text()}")
            total_calls += 1
        new_marks[caller] = paths[-1].stem

    task_text = (
        prompt.rstrip()
        + "\n\n## Existing global.md\n\n"
        + (prior_global or "(empty)")
        + "\n\n## New per-call logs\n\n"
        + "\n\n---\n\n".join(chunks)
        + "\n\nWrite ONE append-only entry to add to global.md. "
          "Output ONLY the entry text — no commentary."
    )

    # Summarize is itself a claude -p invocation, distinct from send_job
    # but using summarize_mode (summarize.md as system prompt; no CLAUDE.md
    # symlink so role.md's "echo verbatim" instruction doesn't overpower
    # the summarize task body). The caller is the synthetic `__summarize__`
    # so its per-call log lands under memories/__summarize__/ — visible for
    # audit, and skipped by list_new() so future summarize calls don't
    # re-process their own prior outputs.
    call_id = _new_call_id()
    ok, result = await spawner.run(call_id, "__summarize__", task_text,
                                    summarize_mode=True)
    if not ok:
        raise RuntimeError(f"summarize spawn failed: {result}")
    memory.append_global(result.strip())
    memory.bump_watermarks(new_marks)
    return {"appended": 1, "calls_processed": total_calls}


# ---------------------------------------------------------------------------
# JSON-RPC / MCP wire protocol
# ---------------------------------------------------------------------------


MCP_PROTOCOL_VERSION = "2024-11-05"


def _rpc_result(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_text_result(payload: Any) -> dict:
    """MCP tool result envelope. Spec mandates a `content` array of
    typed parts; clients render `text` parts inline. We always return
    one text part whose body is the JSON-encoded payload — workers can
    parse it back if they want structure, or read it as a string."""
    text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
    return {"content": [{"type": "text", "text": text}]}


async def handle_mcp(request: web.Request) -> web.Response:
    """Single POST /mcp endpoint. Dispatches JSON-RPC method names. We do
    NOT advertise SSE streaming capability — every method returns a single
    JSON-RPC response synchronously. This is the minimum viable MCP server
    against streamable-http clients (Claude Code calls initialize +
    tools/list + tools/call; that's all we need for B.0)."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response(
            _rpc_error(None, -32700, "parse error"), status=400,
        )

    # Notifications carry no `id` and expect no response (HTTP 202).
    rid = body.get("id") if isinstance(body, dict) else None
    method = body.get("method") if isinstance(body, dict) else None
    params = body.get("params") if isinstance(body, dict) else {}
    if rid is None:
        return web.Response(status=202)

    if method == "initialize":
        return web.json_response(_rpc_result(rid, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": f"role-mcp-{ROLE}", "version": "0.1"},
        }))

    if method == "tools/list":
        return web.json_response(_rpc_result(rid, {"tools": TOOLS_SCHEMA}))

    if method == "tools/call":
        if not isinstance(params, dict):
            return web.json_response(_rpc_error(rid, -32602, "invalid params"))
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return web.json_response(_rpc_error(rid, -32602, "arguments must be an object"))

        jobs = request.app["jobs"]
        spawner = request.app["spawner"]
        memory = request.app["memory"]

        try:
            if name == "send_job":
                payload = await tool_send_job(jobs, spawner, args)
            elif name == "query_job_status":
                payload = await tool_query_job_status(jobs, args)
            elif name == "summarize_memories":
                payload = await tool_summarize_memories(memory, spawner)
            else:
                return web.json_response(_rpc_error(rid, -32601, f"unknown tool {name!r}"))
        except ValueError as e:
            return web.json_response(_rpc_result(rid, _tool_text_result({
                "error": str(e),
            }) | {"isError": True}))
        except Exception as e:  # pragma: no cover
            return web.json_response(_rpc_result(rid, _tool_text_result({
                "error": f"internal error: {e}",
            }) | {"isError": True}))

        return web.json_response(_rpc_result(rid, _tool_text_result(payload)))

    if method in ("ping", "logging/setLevel"):
        return web.json_response(_rpc_result(rid, {}))

    return web.json_response(_rpc_error(rid, -32601, f"unknown method {method!r}"))


async def handle_health(_: web.Request) -> web.Response:
    return web.json_response({"role": ROLE, "ok": True})


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def _validate_env() -> None:
    if not ROLE:
        print("error: RS_ROLE_NAME env var is required", file=sys.stderr)
        sys.exit(2)
    if not SPAWN_SH.is_file() or not os.access(SPAWN_SH, os.X_OK):
        print(f"error: SPAWN_SH not executable: {SPAWN_SH}", file=sys.stderr)
        sys.exit(2)
    if not (ROLE_TEMPLATE_DIR / "role.md").is_file():
        print(f"error: role template missing: {ROLE_TEMPLATE_DIR}/role.md",
              file=sys.stderr)
        sys.exit(2)


def build_app() -> web.Application:
    app = web.Application()
    jobs = JobTable()
    cleaned = jobs.reconcile_on_startup()
    if cleaned:
        print(f"role-mcp[{ROLE}]: marked {cleaned} orphan job(s) as failed",
              file=sys.stderr)
    app["jobs"] = jobs
    app["spawner"] = Spawner(jobs)
    app["memory"] = Memory()
    app.router.add_post("/mcp", handle_mcp)
    app.router.add_post("/mcp/", handle_mcp)
    app.router.add_get("/health", handle_health)
    return app


def main() -> None:
    _validate_env()
    for d in (JOBS_DIR, MEMORIES_DIR, CALLS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    app = build_app()
    print(f"role-mcp[{ROLE}]: listening on 0.0.0.0:{PORT}/mcp", file=sys.stderr)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None,
                handle_signals=True)


if __name__ == "__main__":
    main()
