#!/opt/conda/bin/python
"""rs-worker — worker lifecycle, inside the orchestrator.

Mirrors research.py's shape but operates one level in: against the
orchestrator's inner Docker daemon, on a per-project basis.

State: workers are identified entirely by docker labels + filesystem
structure. No separate registry file.

  - container name:  research-worker-<name>
  - workdir:         /workspace/workers/<name>/work/
  - labels:          research.worker=1
                     research.worker_type=analysis
                     research.data_mounts=<comma-joined>
                     research.created_at=<iso8601>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import docker
from docker.errors import APIError, ImageNotFound, NotFound

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKSPACE = Path(os.environ.get("RS_WORKSPACE", "/workspace"))
CONTAINER_PREFIX = "research-worker-"
DEFAULT_IMAGE = "research-analysis-base:latest"
# Claude Code CLI writes OAuth creds to a HIDDEN file (leading dot).
ORCH_CREDS = Path.home() / ".claude" / ".credentials.json"
ORCH_SETTINGS = Path.home() / ".claude" / "settings.json"
WORKER_CLAUDE_MD_TEMPLATE = Path("/opt/claude-templates/worker.CLAUDE.md.template")

LABEL_WORKER = "research.worker"
LABEL_TYPE = "research.worker_type"
LABEL_MOUNTS = "research.data_mounts"
LABEL_CREATED = "research.created_at"
LABEL_INTERACTIVE = "research.interactive"


def container_name(name: str) -> str:
    return f"{CONTAINER_PREFIX}{name}"


def worker_dir(name: str) -> Path:
    return WORKSPACE / "workers" / name / "work"


def _client() -> docker.DockerClient:
    return docker.from_env()


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"rs-worker: {msg}", file=sys.stderr)
    sys.exit(code)


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str, sort_keys=True)
    sys.stdout.write("\n")


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _valid_name(name: str) -> bool:
    return bool(name) and name.replace("-", "").replace("_", "").isalnum()


def _get_container(name: str):
    try:
        return _client().containers.get(container_name(name))
    except NotFound:
        die(f"no such worker: {name!r}")


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


def cmd_spawn(args: argparse.Namespace) -> None:
    if not _valid_name(args.name):
        die(f"name must be alphanumeric (plus '-' or '_'): {args.name!r}")

    if not ORCH_CREDS.is_file():
        die(
            "orchestrator is not authenticated. Run `claude` once (via the "
            "VSCode CC extension or `claude` in byobu) to complete OAuth, then "
            "retry."
        )

    # Resolve task text.
    if args.task_file:
        task_text = sys.stdin.read() if args.task_file == "-" else Path(args.task_file).read_text()
    else:
        task_text = args.task

    cli = _client()
    cname = container_name(args.name)
    try:
        cli.containers.get(cname)
        die(f"worker {args.name!r} already exists. Destroy it first.")
    except NotFound:
        pass

    image = args.image or DEFAULT_IMAGE
    try:
        cli.images.get(image)
    except ImageNotFound:
        die(
            f"image {image!r} is not available in the orchestrator's inner "
            f"Docker daemon. Re-stage via `research project destroy <proj> && "
            f"research project create <proj> …`, or rebuild with `research setup`."
        )

    if args.interactive:
        die("--interactive mode is reserved for a later stage; not implemented yet")

    # --- stage workdir ---
    wdir = worker_dir(args.name)
    wdir.mkdir(parents=True, exist_ok=True)
    for sub in ("inbox", "outbox", "outputs", ".claude"):
        (wdir / sub).mkdir(exist_ok=True)

    (wdir / "task.md").write_text(task_text.rstrip() + "\n")

    if WORKER_CLAUDE_MD_TEMPLATE.is_file():
        (wdir / "CLAUDE.md").write_text(WORKER_CLAUDE_MD_TEMPLATE.read_text())
    else:
        (wdir / "CLAUDE.md").write_text(
            "# Analysis Worker\n\nTask in /workspace/task.md. Deliverables in "
            "/workspace/outputs/. Maintain /workspace/research_log.md. Touch "
            "/workspace/DONE when finished.\n"
        )

    if not (wdir / "research_log.md").exists():
        (wdir / "research_log.md").write_text(
            f"# Research log — {args.name}\n\n"
            "(Worker will populate this during the run.)\n"
        )

    # Creds + settings snapshot. Mirror the CLI's hidden-file naming inside
    # the worker's staging dir so the worker entrypoint copies them into
    # place with the same name.
    shutil.copy2(ORCH_CREDS, wdir / ".claude" / ".credentials.json")
    os.chmod(wdir / ".claude" / ".credentials.json", 0o600)
    if ORCH_SETTINGS.is_file():
        shutil.copy2(ORCH_SETTINGS, wdir / ".claude" / "settings.json")

    # Remove stale sentinels from a previous run under the same name.
    for sentinel in ("DONE", "WAITING"):
        p = wdir / sentinel
        if p.exists():
            p.unlink()

    # --- mounts ---
    mounts = [
        docker.types.Mount(
            target="/workspace", source=str(wdir), type="bind", read_only=False
        )
    ]
    for src in args.data_mount:
        mounts.append(
            docker.types.Mount(target=src, source=src, type="bind", read_only=True)
        )

    # --- env ---
    env = {"PYTHONUNBUFFERED": "1"}
    for kv in args.env:
        k, _, v = kv.partition("=")
        if not k:
            die(f"invalid --env value: {kv!r} (expected K=V)")
        env[k] = v

    # --- labels ---
    labels = {
        LABEL_WORKER: "1",
        LABEL_TYPE: "analysis",
        LABEL_MOUNTS: ",".join(args.data_mount),
        LABEL_CREATED: _iso_now(),
    }
    if args.interactive:
        labels[LABEL_INTERACTIVE] = "1"

    # --- run ---
    try:
        container = cli.containers.run(
            image,
            name=cname,
            detach=True,
            mounts=mounts,
            environment=env,
            labels=labels,
        )
    except APIError as e:
        # Roll back the workdir on failure so spawn is idempotent-ish.
        shutil.rmtree(wdir.parent, ignore_errors=True)
        die(f"docker run failed: {e}")

    _print_json({
        "name": args.name,
        "container": cname,
        "container_id": container.id,
        "image": image,
        "data_mounts": args.data_mount,
        "interactive": bool(args.interactive),
        "created_at": labels[LABEL_CREATED],
    })


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def cmd_list(_args: argparse.Namespace) -> None:
    result = []
    for c in _client().containers.list(all=True, filters={"label": LABEL_WORKER}):
        bare = c.name.removeprefix(CONTAINER_PREFIX) if c.name.startswith(CONTAINER_PREFIX) else c.name
        dm = c.labels.get(LABEL_MOUNTS, "")
        result.append({
            "name": bare,
            "container": c.name,
            "state": c.status,
            "worker_type": c.labels.get(LABEL_TYPE, ""),
            "data_mounts": [s for s in dm.split(",") if s],
            "created_at": c.labels.get(LABEL_CREATED, ""),
            "image": c.image.tags[0] if c.image.tags else c.image.id[:12],
        })
    _print_json(sorted(result, key=lambda x: x["name"]))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> None:
    c = _get_container(args.name)
    wdir = worker_dir(args.name)

    inbox = sorted(p.name for p in (wdir / "inbox").glob("*")) if (wdir / "inbox").is_dir() else []
    outputs = (
        sorted(str(p.relative_to(wdir)) for p in (wdir / "outputs").rglob("*") if p.is_file())
        if (wdir / "outputs").is_dir() else []
    )

    log_tail: list[str] = []
    log_source: str | None = None
    for logname in ("log.jsonl", "terminal.log"):
        p = wdir / logname
        if p.is_file():
            log_source = logname
            try:
                with p.open() as f:
                    log_tail = f.readlines()[-args.log_lines:]
            except OSError:
                pass
            break

    state = c.status
    if state == "exited":
        if (wdir / "DONE").exists():
            state = "done"
        elif (wdir / "WAITING").exists():
            state = "waiting"
        else:
            state = "failed"

    _print_json({
        "name": args.name,
        "container": c.name,
        "state": state,
        "exit_code": c.attrs.get("State", {}).get("ExitCode"),
        "done_sentinel": (wdir / "DONE").exists(),
        "waiting_sentinel": (wdir / "WAITING").exists(),
        "inbox_unread": inbox,
        "outputs": outputs,
        "log_source": log_source,
        "log_tail": log_tail,
    })


# ---------------------------------------------------------------------------
# message
# ---------------------------------------------------------------------------


def cmd_message(args: argparse.Namespace) -> None:
    c = _get_container(args.name)
    text = args.text.rstrip() + "\n"

    if args.send_keys:
        if c.labels.get(LABEL_INTERACTIVE) != "1":
            die(
                f"--send-keys only works on interactive workers, and {args.name!r} "
                f"is headless. Drop --send-keys to use the inbox instead."
            )
        subprocess.run(
            ["docker", "exec", c.name, "byobu", "send-keys", "-t", "worker:0",
             args.text, "Enter"],
            check=True,
        )
        _print_json({"name": args.name, "delivered_via": "send-keys"})
        return

    wdir = worker_dir(args.name)
    inbox = wdir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    msg = inbox / f"msg_{int(time.time())}.md"
    msg.write_text(text)
    _print_json({
        "name": args.name,
        "delivered_via": "inbox",
        "path": str(msg.relative_to(wdir)),
    })


# ---------------------------------------------------------------------------
# stop / start
# ---------------------------------------------------------------------------


def cmd_stop(args: argparse.Namespace) -> None:
    _get_container(args.name).stop(timeout=10)
    _print_json({"name": args.name, "state": "stopped"})


def cmd_start(args: argparse.Namespace) -> None:
    _get_container(args.name).start()
    _print_json({"name": args.name, "state": "running"})


# ---------------------------------------------------------------------------
# destroy
# ---------------------------------------------------------------------------


def cmd_destroy(args: argparse.Namespace) -> None:
    c = _get_container(args.name)
    wdir = worker_dir(args.name)

    if not args.yes:
        outputs = list((wdir / "outputs").rglob("*")) if (wdir / "outputs").is_dir() else []
        msg = [
            f"Worker {args.name!r}:",
            f"  container: {c.name} ({c.status})",
            f"  workdir:   {wdir.parent}",
        ]
        if outputs:
            msg.append(f"  outputs:   {sum(1 for p in outputs if p.is_file())} file(s) (will be deleted)")
        msg.append("Pass --yes to confirm destruction.")
        die("\n".join(msg))

    try:
        c.remove(force=True)
    except NotFound:
        pass
    shutil.rmtree(wdir.parent, ignore_errors=True)
    _print_json({"name": args.name, "destroyed": True})


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------


def cmd_attach(args: argparse.Namespace) -> None:
    c = _get_container(args.name)
    session = "worker" if c.labels.get(LABEL_INTERACTIVE) == "1" else "main"
    cmd = ["docker", "exec", "-it", c.name, "byobu", "attach", "-t", session]
    if args.print:
        print(" ".join(cmd))
        return
    os.execvp("docker", cmd)


# ---------------------------------------------------------------------------
# tail
# ---------------------------------------------------------------------------


def cmd_tail(args: argparse.Namespace) -> None:
    _get_container(args.name)  # existence check
    wdir = worker_dir(args.name)
    for logname in ("log.jsonl", "terminal.log"):
        p = wdir / logname
        if p.is_file():
            tail_args = ["tail", "-n", str(args.lines), str(p)]
            if args.follow:
                tail_args.insert(1, "-F")
            os.execvp("tail", tail_args)
    die(f"no log file found for worker {args.name!r}")


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rs-worker",
        description="Worker lifecycle inside the orchestrator.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn", help="stage a worker workdir and start its container")
    sp.add_argument("name")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--task", help="task text (literal)")
    g.add_argument("--task-file", help="path to task.md, or '-' for stdin")
    sp.add_argument("--image", default=DEFAULT_IMAGE, help=f"worker image (default: {DEFAULT_IMAGE})")
    sp.add_argument("--data-mount", action="append", default=[],
                    help="absolute path to bind-mount RO into the worker; repeatable")
    sp.add_argument("--env", action="append", default=[], help="K=V; repeatable")
    sp.add_argument("--interactive", action="store_true",
                    help="(reserved) run claude interactively in byobu instead of headless")
    sp.set_defaults(func=cmd_spawn)

    sl = sub.add_parser("list", help="list all workers (JSON)")
    sl.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="container + filesystem + log blob (JSON)")
    st.add_argument("name")
    st.add_argument("--log-lines", type=int, default=20)
    st.set_defaults(func=cmd_status)

    sm = sub.add_parser("message", help="send a message to a worker")
    sm.add_argument("name")
    sm.add_argument("text")
    sm.add_argument("--send-keys", action="store_true",
                    help="inject into byobu pane (interactive workers only)")
    sm.set_defaults(func=cmd_message)

    for op, fn in (("stop", cmd_stop), ("start", cmd_start)):
        o = sub.add_parser(op, help=f"docker {op} the worker")
        o.add_argument("name")
        o.set_defaults(func=fn)

    sd = sub.add_parser("destroy", help="rm -f container + delete workdir")
    sd.add_argument("name")
    sd.add_argument("--yes", action="store_true", help="confirm destruction")
    sd.set_defaults(func=cmd_destroy)

    sa = sub.add_parser("attach", help="exec into the worker's byobu session")
    sa.add_argument("name")
    sa.add_argument("--print", action="store_true",
                    help="print the command instead of exec'ing")
    sa.set_defaults(func=cmd_attach)

    slg = sub.add_parser("tail", help="tail the worker's log file")
    slg.add_argument("name")
    slg.add_argument("-n", "--lines", type=int, default=20)
    slg.add_argument("-f", "--follow", action="store_true")
    slg.set_defaults(func=cmd_tail)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
