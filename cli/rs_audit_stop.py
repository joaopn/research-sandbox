#!/opt/conda/bin/python
"""rs-audit-stop — supervision audit hook.

Claude Code Stop hook. Runs every time the supervisor's claude session
is about to return control to the PI. Blocks the stop (exit 2) if any
terminated-but-unaccepted worker's research_log.md has not been Read in
this session's transcript.

Reads JSON on stdin (standard Claude Code hook payload):
    { "transcript_path": "/path/to/session.jsonl", ... }

Scope: all terminated-unaccepted workers in the supervisor's inner
docker daemon, not just this session's spawns. Strictly stricter than
session-scoped and avoids propagating session_id through container labels.
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


def _resolve_state(container, wdir: Path) -> str:
    state = container.status
    if state == "exited":
        if (wdir / "DONE").exists():
            return "done"
        if (wdir / "WAITING").exists():
            return "waiting"
        return "failed"
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
    # Parse the hook payload. Be permissive: any parse error = no-op.
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

    # Enumerate workers via the supervisor's inner docker daemon.
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
        if (wdir / ".accepted.json").is_file():
            continue
        if not _read_in_transcript(transcript, bare):
            unread.append(bare)

    if unread:
        names = ", ".join(sorted(unread))
        print(
            f"Unread research_log for terminated worker(s): {names}. "
            f"`cat /workspace/workers/<name>/work/research_log.md` for each, "
            f"then `rs-worker finalize` + `rs-worker accept` (or `rs-worker message` / "
            f"`rs-worker destroy + spawn` to iterate) before returning to the PI.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
