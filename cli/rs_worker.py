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
import re
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

# Plan / accept / finalize contract.
PLAN_SECTIONS = ("Question", "Inputs", "Deliverables", "Verification")
OUTPUT_WHITELIST_SUFFIXES = (
    ".ipynb", ".py", ".sh", ".sql",
    ".csv", ".parquet", ".feather",
    ".png", ".jpg", ".svg", ".pdf",
    ".md",
)
OUTPUT_DENYLIST_SUFFIXES = (".pyc", ".tmp")
OUTPUT_DENYLIST_DIRS = ("__pycache__", ".ipynb_checkpoints")

TERMINAL_STATES = frozenset({"done", "waiting", "failed"})
ACCEPTED_STATES = frozenset({"done", "waiting"})
POLL_INTERVAL_SEC = 2.0
DEFAULT_WAIT_TIMEOUT = 540


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


def _skeleton_research_log(name: str) -> str:
    return f"# Research log — {name}\n\n(Worker will populate this during the run.)\n"


def _resolve_state(container, wdir: Path) -> str:
    """Map docker container.status + sentinel files to a worker state.

    Returns one of: running, created, restarting, paused, done, waiting, failed.
    """
    state = container.status
    if state == "exited":
        if (wdir / "DONE").exists():
            return "done"
        if (wdir / "WAITING").exists():
            return "waiting"
        return "failed"
    return state


def _is_accepted(wdir: Path) -> bool:
    return (wdir / ".accepted.json").is_file()


def _validate_plan(text: str) -> list[str]:
    """Return list of missing required section names (empty = plan is well-formed)."""
    return [
        s for s in PLAN_SECTIONS
        if not re.search(rf"^##\s+{re.escape(s)}\b", text, re.MULTILINE)
    ]


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

    # --- resolve + validate plan ---
    plan_src = Path(args.plan).expanduser()
    if not plan_src.is_file():
        die(f"plan file not found: {plan_src}")
    plan_text = plan_src.read_text()
    missing = _validate_plan(plan_text)
    if missing:
        die(
            "plan is missing required top-level section(s): "
            + ", ".join(f"## {s}" for s in missing)
            + f" (in {plan_src})"
        )

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
    for sub in ("inbox", "outbox", "outputs", "scratch", ".claude"):
        (wdir / sub).mkdir(exist_ok=True)

    # Worker task is the plan, verbatim.
    (wdir / "task.md").write_text(plan_text.rstrip() + "\n")

    # Canonical plan copy, durable beyond worker lifetime.
    canonical_plan = WORKSPACE / "plan" / f"{args.name}.md"
    canonical_plan.parent.mkdir(parents=True, exist_ok=True)
    canonical_plan.write_text(plan_text.rstrip() + "\n")

    if WORKER_CLAUDE_MD_TEMPLATE.is_file():
        (wdir / "CLAUDE.md").write_text(WORKER_CLAUDE_MD_TEMPLATE.read_text())
    else:
        (wdir / "CLAUDE.md").write_text(
            "# Analysis Worker\n\nTask in /workspace/task.md. Deliverables in "
            "/workspace/outputs/. Maintain /workspace/research_log.md. Touch "
            "/workspace/DONE when finished.\n"
        )

    # Write skeleton to both research_log.md (for the worker) and .claude/
    # (a hidden reference copy the accept gate compares against).
    skeleton = _skeleton_research_log(args.name)
    if not (wdir / "research_log.md").exists():
        (wdir / "research_log.md").write_text(skeleton)
    (wdir / ".claude" / "skeleton_research_log.md").write_text(skeleton)

    # Creds + settings snapshot. Mirror the CLI's hidden-file naming inside
    # the worker's staging dir so the worker entrypoint copies them into
    # place with the same name.
    shutil.copy2(ORCH_CREDS, wdir / ".claude" / ".credentials.json")
    os.chmod(wdir / ".claude" / ".credentials.json", 0o600)
    if ORCH_SETTINGS.is_file():
        shutil.copy2(ORCH_SETTINGS, wdir / ".claude" / "settings.json")

    # Remove stale sentinels from a previous run under the same name.
    for sentinel in ("DONE", "WAITING", ".accepted.json"):
        p = wdir / sentinel
        if p.exists():
            p.unlink()

    # --- mounts ---
    mounts = [
        docker.types.Mount(
            target="/workspace", source=str(wdir), type="bind", read_only=False
        ),
        # Project shared/ is RO-mounted into every worker by default. This is
        # the project's canonical input-data location (populated by --data-dir
        # at project-create time) and every data-using worker needs it. Making
        # it implicit eliminates the "supervisor forgot --data-mount" class of
        # silent failure. --data-mount remains for paths outside /workspace/shared/.
        docker.types.Mount(
            target="/workspace/shared", source=str(WORKSPACE / "shared"),
            type="bind", read_only=True,
        ),
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
        "plan": str(canonical_plan),
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
        wdir = worker_dir(bare)
        result.append({
            "name": bare,
            "container": c.name,
            "state": _resolve_state(c, wdir),
            "accepted": _is_accepted(wdir),
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

    state = _resolve_state(c, wdir)

    _print_json({
        "name": args.name,
        "container": c.name,
        "state": state,
        "accepted": _is_accepted(wdir),
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
# wait
# ---------------------------------------------------------------------------


_WAIT_TERMINAL = TERMINAL_STATES | frozenset({"missing"})


def _snapshot(names: list[str]) -> dict[str, dict]:
    """Return {name -> {state, exit_code}} for each requested worker.

    Missing workers (destroyed / never existed) get state='missing', exit_code=None.
    """
    cli = _client()
    out: dict[str, dict] = {}
    for name in names:
        try:
            c = cli.containers.get(container_name(name))
        except NotFound:
            out[name] = {"state": "missing", "exit_code": None}
            continue
        c.reload()
        wdir = worker_dir(name)
        out[name] = {
            "state": _resolve_state(c, wdir),
            "exit_code": c.attrs.get("State", {}).get("ExitCode"),
        }
    return out


def cmd_wait(args: argparse.Namespace) -> None:
    for n in args.name:
        if not _valid_name(n):
            die(f"invalid worker name: {n!r}")

    # Pre-flight: every named worker must exist right now.
    snap = _snapshot(args.name)
    missing = [n for n, s in snap.items() if s["state"] == "missing"]
    if missing:
        die(f"no such worker(s): {', '.join(missing)}")

    deadline = time.monotonic() + args.timeout
    while True:
        snap = _snapshot(args.name)
        terminal = [n for n in args.name if snap[n]["state"] in _WAIT_TERMINAL]
        if args.all:
            if len(terminal) == len(args.name):
                results = [
                    {"name": n, "state": snap[n]["state"],
                     "exit_code": snap[n]["exit_code"]}
                    for n in args.name
                ]
                _print_json(results)
                any_failed = any(
                    snap[n]["state"] in ("failed", "missing") for n in args.name
                )
                sys.exit(1 if any_failed else 0)
        else:
            if terminal:
                first = terminal[0]
                _print_json({
                    "name": first,
                    "state": snap[first]["state"],
                    "exit_code": snap[first]["exit_code"],
                })
                sys.exit(0)

        if time.monotonic() >= deadline:
            in_flight = [n for n in args.name if snap[n]["state"] not in _WAIT_TERMINAL]
            _print_json({"timeout": True, "in_flight": in_flight})
            sys.exit(3)

        time.sleep(POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def _is_denied(path: Path) -> bool:
    if any(part in OUTPUT_DENYLIST_DIRS for part in path.parts):
        return True
    return path.suffix.lower() in OUTPUT_DENYLIST_SUFFIXES


def _is_whitelisted(path: Path) -> bool:
    return path.suffix.lower() in OUTPUT_WHITELIST_SUFFIXES


def cmd_finalize(args: argparse.Namespace) -> None:
    _get_container(args.name)  # existence check
    wdir = worker_dir(args.name)
    outputs = wdir / "outputs"
    scratch = wdir / "scratch"
    if not outputs.is_dir():
        die(f"no outputs/ directory for worker {args.name!r}")

    moved: list[dict] = []
    removed: list[str] = []
    kept = 0

    # Walk outputs/ top-down so we can prune deny-dirs early.
    for root, dirs, files in os.walk(outputs, topdown=True):
        root_p = Path(root)
        # Remove deny-listed subdirectories in place.
        for d in list(dirs):
            if d in OUTPUT_DENYLIST_DIRS:
                target = root_p / d
                rel = target.relative_to(outputs)
                removed.append(str(rel))
                if not args.dry_run:
                    shutil.rmtree(target, ignore_errors=True)
                dirs.remove(d)

        for f in files:
            src = root_p / f
            rel = src.relative_to(outputs)
            if _is_denied(src):
                removed.append(str(rel))
                if not args.dry_run:
                    src.unlink(missing_ok=True)
                continue
            if _is_whitelisted(src):
                kept += 1
                continue
            dst = scratch / rel
            moved.append({"from": f"outputs/{rel}", "to": f"scratch/{rel}"})
            if not args.dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))

    _print_json({
        "name": args.name,
        "dry_run": bool(args.dry_run),
        "moved": moved,
        "removed": removed,
        "kept": kept,
    })


# ---------------------------------------------------------------------------
# accept
# ---------------------------------------------------------------------------


def _accept_checks(wdir: Path, container) -> list[str]:
    """Return list of failure messages (empty = all checks pass)."""
    failures: list[str] = []

    state = _resolve_state(container, wdir)
    if state not in ACCEPTED_STATES:
        failures.append(
            f"state is {state!r}; must be 'done' or 'waiting' (run `rs-worker wait` "
            "first, or investigate a failed run)"
        )
        # Everything below assumes the worker had a chance to write outputs.
        return failures

    outputs = wdir / "outputs"
    if not outputs.is_dir() or not any(p.is_file() for p in outputs.rglob("*")):
        failures.append("outputs/ is empty")

    rl = wdir / "research_log.md"
    skel = wdir / ".claude" / "skeleton_research_log.md"
    if not rl.is_file():
        failures.append("research_log.md is missing")
    elif skel.is_file() and rl.read_bytes() == skel.read_bytes():
        failures.append("research_log.md is unchanged from the skeleton")

    if outputs.is_dir():
        # Whitelist: at least one output must match.
        has_whitelisted = any(
            _is_whitelisted(p) for p in outputs.rglob("*") if p.is_file()
        )
        if not has_whitelisted:
            failures.append(
                "no files in outputs/ match the deliverable whitelist "
                f"({', '.join(OUTPUT_WHITELIST_SUFFIXES)})"
            )
        # Denylist: no output may match.
        denied = sorted(
            str(p.relative_to(outputs))
            for p in outputs.rglob("*") if p.is_file() and _is_denied(p)
        )
        if denied:
            failures.append(f"outputs/ contains denied files: {', '.join(denied)}")

    return failures


def cmd_accept(args: argparse.Namespace) -> None:
    c = _get_container(args.name)
    wdir = worker_dir(args.name)

    if _is_accepted(wdir):
        die(f"worker {args.name!r} is already accepted")

    failures: list[str] = []
    if not args.waived:
        failures = _accept_checks(wdir, c)
        if failures:
            for msg in failures:
                print(f"rs-worker: accept check failed: {msg}", file=sys.stderr)
            sys.exit(1)

    sentinel = wdir / ".accepted.json"
    sentinel.write_text(json.dumps({
        "name": args.name,
        "accepted_at": _iso_now(),
        "waived": args.waived,
    }, indent=2, sort_keys=True) + "\n")

    # Archive the plan: move /workspace/plan/<name>.md → plan/archive/<name>.md.
    # Keeps plan/ as the active briefs list; archive/ is provenance.
    archive_dir = WORKSPACE / "plan" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    plan_file = WORKSPACE / "plan" / f"{args.name}.md"
    archived_to: str | None = None
    if plan_file.is_file():
        dst = archive_dir / f"{args.name}.md"
        shutil.move(str(plan_file), str(dst))
        archived_to = str(dst)

    _print_json({
        "name": args.name,
        "accepted": True,
        "waived": args.waived,
        "accepted_at": json.loads(sentinel.read_text())["accepted_at"],
        "plan_archived_to": archived_to,
    })


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
    sp.add_argument("--plan", required=True,
                    help="path to a plan file with required top-level sections: "
                         "## Question, ## Inputs, ## Deliverables, ## Verification")
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

    sw = sub.add_parser("wait",
                        help="block until one (or all, with --all) named worker(s) "
                             "reach a terminal state")
    sw.add_argument("name", nargs="+")
    sw.add_argument("--all", action="store_true",
                    help="wait until every named worker is terminal (default: any)")
    sw.add_argument("--timeout", type=int, default=DEFAULT_WAIT_TIMEOUT,
                    help=f"hard upper bound in seconds (default: {DEFAULT_WAIT_TIMEOUT}, "
                         "under Claude Code's Bash tool timeout)")
    sw.set_defaults(func=cmd_wait)

    sf = sub.add_parser("finalize",
                        help="move non-deliverable files out of outputs/ into scratch/; "
                             "prune denied files (__pycache__, .ipynb_checkpoints, *.pyc)")
    sf.add_argument("name")
    sf.add_argument("--dry-run", action="store_true",
                    help="print actions without mutating the workdir")
    sf.set_defaults(func=cmd_finalize)

    sac = sub.add_parser("accept",
                         help="mark a worker accepted after shape checks pass "
                              "(terminal state, non-empty outputs/ with whitelisted "
                              "files, research_log.md past skeleton)")
    sac.add_argument("name")
    sac.add_argument("--waived", default=None, metavar="REASON",
                     help="skip shape checks and accept with an explicit logged reason")
    sac.set_defaults(func=cmd_accept)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
