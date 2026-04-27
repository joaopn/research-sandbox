#!/opt/conda/bin/python
"""rs-audit-stop — supervision audit hook.

Claude Code Stop hook. Runs every time the supervisor's claude session
is about to return control to the PI. Blocks the stop (exit 2) if any
worker currently in a terminal runtime state (waiting / done / failed)
has zero accepted cycles AND the session transcript shows no Read on
its research_log.md.

Scope: workers with a live container in the supervisor's inner docker
daemon. Registry `down` / `destroyed_*` workers have no container and
are naturally excluded.

Reads JSON on stdin (standard Claude Code hook payload):
    { "transcript_path": "/path/to/session.jsonl", ... }
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import docker
from docker.errors import NotFound

WORKSPACE = Path("/workspace")
CONTAINER_PREFIX = "rs-worker-"
LABEL_WORKER = "research.worker"

TERMINAL_STATES = frozenset({"done", "waiting", "failed"})

_READ_PATTERN = re.compile(r'"name"\s*:\s*"Read"')


def _worker_dir(name: str) -> Path:
    return WORKSPACE / "workers" / name / "work"


def _registry_path(name: str) -> Path:
    return WORKSPACE / ".workers" / f"{name}.json"


def _cycles_accepted(name: str) -> int:
    p = _registry_path(name)
    if not p.is_file():
        return 0
    try:
        return len(json.loads(p.read_text()).get("cycles", []))
    except json.JSONDecodeError:
        return 0


def _inbox_has_unread(wdir: Path) -> bool:
    inbox = wdir / "inbox"
    if not inbox.is_dir():
        return False
    return any(
        p.name.startswith("msg_") and p.suffix == ".md"
        for p in inbox.iterdir()
    )


def _resolve_state(container, wdir: Path) -> str:
    state = container.status
    if state == "exited":
        if (wdir / "DONE").exists():
            return "done"
        if (wdir / "WAITING").exists():
            return "waiting"
        return "failed"
    if state == "running":
        # Mirror rs_worker._resolve_state: a queued inbox message means the
        # worker is about to be `working`, even if WAITING is still up.
        if _inbox_has_unread(wdir):
            return "working"
        if (wdir / "WAITING").exists():
            return "waiting"
        return "working"
    return state


def _read_in_transcript(transcript: Path, worker_name: str) -> bool:
    """True if the transcript shows a Read tool call against this worker's log."""
    target = f"workers/{worker_name}/work/research_log.md"
    try:
        with transcript.open() as f:
            for line in f:
                if not line.strip():
                    continue
                if _READ_PATTERN.search(line) and target in line:
                    return True
    except OSError:
        return False
    return False


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        sys.exit(0)
    transcript = Path(transcript_path)
    if not transcript.is_file():
        sys.exit(0)

    try:
        cli = docker.from_env()
        containers = cli.containers.list(all=True, filters={"label": LABEL_WORKER})
    except Exception:
        # If docker isn't reachable (no inner dockerd yet), don't block the session.
        sys.exit(0)

    unread: list[str] = []
    for c in containers:
        try:
            c.reload()
        except NotFound:
            continue
        bare = c.name.removeprefix(CONTAINER_PREFIX) if c.name.startswith(CONTAINER_PREFIX) else c.name
        wdir = _worker_dir(bare)
        state = _resolve_state(c, wdir)
        if state not in TERMINAL_STATES:
            continue
        # Skip if the worker has ≥1 accepted cycle — first-cycle audit only.
        if _cycles_accepted(bare) > 0:
            continue
        if not _read_in_transcript(transcript, bare):
            unread.append(bare)

    if unread:
        names = ", ".join(sorted(unread))
        print(
            f"Unread research_log for worker(s) in terminal state without any "
            f"accepted cycle: {names}. "
            f"`cat /workspace/workers/<name>/work/research_log.md` for each, "
            f"then `rs-worker finalize --slug <slug>` + `rs-worker accept --slug <slug>` "
            f"(or `rs-worker message` / `rs-worker destroy + spawn`) before returning to the PI.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
