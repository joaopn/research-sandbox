#!/opt/conda/bin/python
"""rs-worker — worker lifecycle, inside the supervisor.

Mirrors research.py's shape but operates one level in: against the
supervisor's inner Docker daemon, on a per-project basis.

State model (Stage 1.7): each worker has a persistent registry entry at
    /workspace/.workers/<name>.json
describing lifecycle state (live | down | destroyed_pre_accept |
destroyed_post_accept) plus the list of accepted cycles. Runtime state
(waiting/working/done/failed) is derived on demand from docker + the
WAITING / DONE sentinels in the bind-mount. Names are project-permanent:
a destroyed worker's name is tombstoned and can never be reused.
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
CONTAINER_PREFIX = "rs-worker-"
DEFAULT_IMAGE = "rs-analysis-base:latest"
# Claude Code CLI writes OAuth creds to a HIDDEN file (leading dot).
ORCH_CREDS = Path.home() / ".claude" / ".credentials.json"
ORCH_SETTINGS = Path.home() / ".claude" / "settings.json"
WORKER_CLAUDE_MD_TEMPLATE = Path("/opt/claude-templates/worker.CLAUDE.md.template")

LABEL_WORKER = "research.worker"
LABEL_TYPE = "research.worker_type"
LABEL_MOUNTS = "research.data_mounts"
LABEL_CREATED = "research.created_at"
LABEL_INTERACTIVE = "research.interactive"
LABEL_MCPS = "research.mcps"

INNER_NETWORK = "rs-inner"
ALLOWLIST_PATH = Path("/workspace/.orchestrator/mcp-allow.json")
# Sibling registry for role-MCPs (echo-mcp, wrangler, librarian, ...). Workers
# reach role-MCPs through the same proxy URL pattern as external MCPs
# (mcp-proxy:8888/<name>/mcp); the distinction is purely management-surface.
# `_write_mcp_config` merges both files into one by_name lookup, matching the
# proxy config renderer's merge policy.
ROLE_MCPS_PATH = Path("/workspace/.orchestrator/role-mcps.json")

# Plan / accept / finalize contract.
PLAN_SECTIONS = ("Question", "Inputs", "Deliverables", "Verification", "MCPs")
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

# Registry lifecycle states.
REG_LIVE = "live"
REG_DOWN = "down"
REG_DESTROYED_PRE = "destroyed_pre_accept"
REG_DESTROYED_POST = "destroyed_post_accept"
REG_DESTROYED = frozenset({REG_DESTROYED_PRE, REG_DESTROYED_POST})

# Slug format: kebab-case, 2-80 chars, no leading/trailing dash.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def container_name(name: str) -> str:
    return f"{CONTAINER_PREFIX}{name}"


def worker_dir(name: str) -> Path:
    return WORKSPACE / "workers" / name / "work"


def staging_link(name: str) -> Path:
    return WORKSPACE / "staging" / name


def results_dir(name: str) -> Path:
    return WORKSPACE / "results" / name


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


def _valid_slug(slug: str) -> bool:
    return bool(slug) and bool(_SLUG_RE.match(slug)) and len(slug) <= 80


def _get_container(name: str):
    try:
        return _client().containers.get(container_name(name))
    except NotFound:
        die(f"no such worker: {name!r}")


def _skeleton_research_log(name: str) -> str:
    return f"# Research log — {name}\n\n(Worker will populate this during the run.)\n"


def _inbox_has_unread(wdir: Path) -> bool:
    inbox = wdir / "inbox"
    if not inbox.is_dir():
        return False
    return any(
        p.name.startswith("msg_") and p.suffix == ".md"
        for p in inbox.iterdir()
    )


def _resolve_state(container, wdir: Path) -> str:
    """Map docker container.status + sentinel files to a runtime state.

    Returns one of: running, created, restarting, paused, working, waiting,
    done, failed. ("working" = running without a WAITING sentinel yet.)
    """
    state = container.status
    if state == "exited":
        if (wdir / "DONE").exists():
            return "done"
        if (wdir / "WAITING").exists():
            return "waiting"
        return "failed"
    if state == "running":
        # A pending inbox message means work is in flight (or imminent), even
        # if WAITING is still up from the prior cycle. Without this, `wait`
        # called immediately after `message` can return prematurely.
        if _inbox_has_unread(wdir):
            return "working"
        if (wdir / "WAITING").exists():
            return "waiting"
        return "working"
    return state


def _validate_plan(text: str) -> list[str]:
    """Return list of missing required section names (empty = plan is well-formed)."""
    return [
        s for s in PLAN_SECTIONS
        if not re.search(rf"^##\s+{re.escape(s)}\b", text, re.MULTILINE)
    ]


def _plan_summary(text: str) -> str:
    """Snapshot the first non-empty line under ## Question."""
    m = re.search(r"^##\s+Question\b[^\n]*\n(.*?)(?=^##\s|\Z)", text,
                  re.MULTILINE | re.DOTALL)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        s = line.strip()
        if s:
            return s
    return ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def registry_path(name: str) -> Path:
    return WORKSPACE / ".workers" / f"{name}.json"


def registry_load(name: str) -> dict | None:
    p = registry_path(name)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        die(f"registry entry for {name!r} is corrupt JSON: {p}")


def registry_write_atomic(name: str, entry: dict) -> None:
    """Atomic write via tmp + rename."""
    p = registry_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


def registry_all() -> dict[str, dict]:
    d = WORKSPACE / ".workers"
    if not d.is_dir():
        return {}
    out: dict[str, dict] = {}
    for p in sorted(d.glob("*.json")):
        try:
            out[p.stem] = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# spawn — fresh OR implicit respawn of a `down` worker
# ---------------------------------------------------------------------------


def _parse_mcps_arg(raw: str) -> list[str]:
    return [m.strip() for m in (raw or "").split(",") if m.strip()]


def _load_allowlist_by_name() -> dict[str, dict]:
    """Best-effort read of the per-project allowlist into a name→entry dict.
    Returns ``{}`` if the file is missing or malformed; callers that need to
    fail hard (e.g. validating ``--mcps``) should check separately."""
    if not ALLOWLIST_PATH.is_file():
        return {}
    try:
        entries = json.loads(ALLOWLIST_PATH.read_text())
    except json.JSONDecodeError:
        return {}
    if not isinstance(entries, list):
        return {}
    out: dict[str, dict] = {}
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("name"), str):
            out[e["name"]] = e
    return out


def _load_role_mcps_by_name() -> dict[str, dict]:
    """Best-effort read of the per-project role-MCP registry, shaped to feed
    `_write_mcp_config`'s by_name lookup directly. Entries are intentionally
    minimal — role-MCPs don't carry the external-MCP fields (host_port,
    headers, etc.) and the URL rendering only needs `transport` and `path`,
    both of which fall back to their MCP-server-contract defaults
    (`http` and `/mcp`). An empty dict per entry suffices and keeps the
    surface stable if future role-MCP fields appear in role-mcps.json
    without rs_worker needing to learn them."""
    if not ROLE_MCPS_PATH.is_file():
        return {}
    try:
        data = json.loads(ROLE_MCPS_PATH.read_text())
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for name, entry in data.items():
        if isinstance(name, str) and isinstance(entry, dict):
            out[name] = {}
    return out


def _write_mcp_config(wdir: Path, requested: list[str]) -> tuple[str, list[tuple[str, str]]]:
    """Validate ``requested`` against the per-project allowlist + role-MCP
    registry, write the worker's .mcp.json (each entry pointed at the
    supervisor's mcp-proxy on rs-inner), and return ``(label, granted)``
    where ``granted`` is a list of ``(name, description)`` pairs used to
    render the worker CLAUDE.md block. With no requested MCPs we leave
    any pre-existing .mcp.json alone (a respawn may have one already) and
    surface its current contents in ``granted`` so the rendered block stays
    in sync with the actual wiring.

    The two registries (external in mcp-allow.json, role in role-mcps.json)
    are merged into one by_name lookup with role-MCPs overlaying — this
    mirrors `mcp_render_config.py`'s merge policy so the worker's view of
    "what MCPs exist" matches the proxy's view of "what URLs route". From
    the worker's POV both are reachable at mcp-proxy:8888/<name>/mcp."""
    if not requested:
        existing = wdir / ".mcp.json"
        if not existing.is_file():
            return "", []
        try:
            cfg = json.loads(existing.read_text())
        except json.JSONDecodeError:
            return "", []
        names = sorted((cfg.get("mcpServers") or {}).keys())
        if not names:
            return "", []
        # role-MCPs don't carry descriptions in v1 — overlay returns
        # empty-dict entries, so `.get("description", "")` yields "" for
        # them and the CLAUDE.md block renders bare bullets.
        by_name = _load_allowlist_by_name()
        by_name.update(_load_role_mcps_by_name())
        granted = [(n, by_name.get(n, {}).get("description", "")) for n in names]
        return ",".join(names), granted

    if not ALLOWLIST_PATH.is_file():
        die(f"--mcps requested but {ALLOWLIST_PATH} missing; "
            "this project predates Stage 2.2 — destroy and recreate it.")

    try:
        allow_entries = json.loads(ALLOWLIST_PATH.read_text())
    except json.JSONDecodeError as e:
        die(f"{ALLOWLIST_PATH} invalid JSON: {e}")
    if not isinstance(allow_entries, list):
        die(f"{ALLOWLIST_PATH} must be a JSON array")

    by_name: dict[str, dict] = {}
    for e in allow_entries:
        if isinstance(e, dict) and isinstance(e.get("name"), str):
            by_name[e["name"]] = e
    role_by_name = _load_role_mcps_by_name()
    by_name.update(role_by_name)  # role-MCPs win on name collision

    unknown = [n for n in requested if n not in by_name]
    if unknown:
        ext_names = sorted(n for n in by_name if n not in role_by_name)
        role_names = sorted(role_by_name)
        die(
            f"these MCPs are not granted to this project: {', '.join(unknown)}\n"
            f"  external (mcp-allow.json):  {', '.join(ext_names) or '(none)'}\n"
            f"  role-MCPs (role-mcps.json): {', '.join(role_names) or '(none)'}\n"
            f"  fix: `research project mcp allow <project> <mcp>` for external MCPs,\n"
            f"       `research project role-mcp enable <project> <role>` for role-MCPs.\n"
            f"       Both run on the host."
        )

    cfg = {
        "mcpServers": {
            name: {
                "type": by_name[name].get("transport", "http"),
                "url": f"http://mcp-proxy:8888/{name}{by_name[name].get('path', '/mcp')}",
            }
            for name in requested
        }
    }
    (wdir / ".mcp.json").write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    granted = [(n, by_name[n].get("description", "")) for n in requested]
    return ",".join(requested), granted


def _append_worker_mcp_block(wdir: Path, granted: list[tuple[str, str]]) -> None:
    """Append the rendered MCP block to the worker's CLAUDE.md. No-op when
    no MCPs are granted. Names without a description render as bare bullets;
    descriptions land verbatim after an em-dash."""
    if not granted:
        return
    bullets = []
    for name, desc in granted:
        bullets.append(f"- **{name}** — {desc}" if desc else f"- **{name}**")
    block = (
        "\n## MCP servers granted to this worker\n\n"
        + "\n".join(bullets)
        + "\n\nThe PI's project-level intent for each MCP is shown above. "
        "Your per-cycle rationale (what to use them for in *this* cycle) "
        "is in `/workspace/task.md` under `## MCPs`. If a tool you need "
        "isn't listed here, ask the supervisor — don't try to import "
        "other MCPs.\n"
    )
    claude_md = wdir / "CLAUDE.md"
    claude_md.write_text(claude_md.read_text() + block)


def _stage_workdir(name: str, plan_text: str, fresh: bool) -> Path:
    """Stage workdir for (fresh or respawned) worker. Returns workdir path."""
    wdir = worker_dir(name)
    wdir.mkdir(parents=True, exist_ok=True)
    for sub in ("inbox", "outputs", "scratch", ".claude"):
        (wdir / sub).mkdir(exist_ok=True)

    # Current-session brief, rewritten every spawn.
    (wdir / "task.md").write_text(plan_text.rstrip() + "\n")

    # Worker CLAUDE.md — always rewrite so template edits land on respawn.
    if WORKER_CLAUDE_MD_TEMPLATE.is_file():
        (wdir / "CLAUDE.md").write_text(WORKER_CLAUDE_MD_TEMPLATE.read_text())
    else:
        (wdir / "CLAUDE.md").write_text(
            "# Analysis Worker\n\nTask in /workspace/task.md. Per-cycle "
            "deliverables in /workspace/outputs/<slug>/. Maintain "
            "/workspace/research_log.md. Touch /workspace/DONE on shutdown.\n"
        )

    # research_log.md + skeleton reference:
    #   - fresh: seed both
    #   - respawn: leave the accumulated research_log alone; refresh the
    #     skeleton reference so a future accept check compares against it
    skeleton = _skeleton_research_log(name)
    (wdir / ".claude" / "skeleton_research_log.md").write_text(skeleton)
    if fresh and not (wdir / "research_log.md").exists():
        (wdir / "research_log.md").write_text(skeleton)

    # Creds + settings snapshot: always refresh (supervisor may have re-auth'd).
    shutil.copy2(ORCH_CREDS, wdir / ".claude" / ".credentials.json")
    os.chmod(wdir / ".claude" / ".credentials.json", 0o600)
    if ORCH_SETTINGS.is_file():
        shutil.copy2(ORCH_SETTINGS, wdir / ".claude" / "settings.json")

    # Clear stale runtime sentinels. Entrypoint does this too (belt+braces).
    for sentinel in ("DONE", "WAITING"):
        p = wdir / sentinel
        if p.exists():
            p.unlink()

    return wdir


def cmd_spawn(args: argparse.Namespace) -> None:
    if not _valid_name(args.name):
        die(f"name must be alphanumeric (plus '-' or '_'): {args.name!r}")

    if not ORCH_CREDS.is_file():
        die(
            "supervisor is not authenticated. Run `claude` once (via the "
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

    # --- consult registry ---
    existing = registry_load(args.name)
    cli = _client()
    cname = container_name(args.name)

    if existing:
        state = existing.get("state")
        if state == REG_LIVE:
            die(
                f"worker {args.name!r} is already live (registry). Use "
                f"`rs-worker message` to iterate, or `rs-worker destroy` first."
            )
        if state in REG_DESTROYED:
            cycles = len(existing.get("cycles", []))
            created = existing.get("created_at", "?")
            summary = existing.get("plan_summary", "") or "(no summary)"
            die(
                f"name {args.name!r} is reserved in this project.\n"
                f"  created_at: {created}\n"
                f"  state:      {state}\n"
                f"  cycles:     {cycles} accepted\n"
                f"  summary:    {summary}\n"
                f"Worker names are project-permanent. Pick a new name "
                f"(e.g. {args.name}_v2) and retry."
            )
        if state != REG_DOWN:
            die(f"unexpected registry state {state!r} for worker {args.name!r}")

    fresh = existing is None

    # Container should not already exist at this point (down → no container).
    try:
        cli.containers.get(cname)
        die(
            f"stale container {cname!r} exists but registry says "
            f"{'(no entry)' if fresh else existing.get('state')!r}. "
            f"Investigate with `docker ps -a --filter name={cname}`."
        )
    except NotFound:
        pass

    image = args.image or DEFAULT_IMAGE
    try:
        cli.images.get(image)
    except ImageNotFound:
        die(
            f"image {image!r} is not available in the supervisor's inner "
            f"Docker daemon. Re-stage via `research project destroy <proj> && "
            f"research project create <proj> …`, or rebuild with `research setup`."
        )

    if args.interactive:
        die("--interactive mode is reserved for a later stage; not implemented yet")

    # --- stage workdir + canonical plan ---
    wdir = _stage_workdir(args.name, plan_text, fresh=fresh)

    canonical_plan = WORKSPACE / "plan" / f"{args.name}.md"
    canonical_plan.parent.mkdir(parents=True, exist_ok=True)
    canonical_plan.write_text(plan_text.rstrip() + "\n")

    # --- mcp wiring: validate requested names against the per-project
    #     allowlist, write <wdir>/.mcp.json before container start, and
    #     append the per-worker MCP block to the staged CLAUDE.md so the
    #     worker can see what each granted MCP is for ---
    requested_mcps = _parse_mcps_arg(getattr(args, "mcps", "") or "")
    mcps_label, granted_mcps = _write_mcp_config(wdir, requested_mcps)
    _append_worker_mcp_block(wdir, granted_mcps)

    # --- write/update registry BEFORE docker run so a crash leaves a discoverable state ---
    now = _iso_now()
    if fresh:
        entry = {
            "name": args.name,
            "created_at": now,
            "state": REG_LIVE,
            "plan_summary": _plan_summary(plan_text),
            "cycles": [],
            "last_spawn_at": now,
            "last_down_at": None,
            "destroyed_at": None,
        }
    else:
        entry = dict(existing or {})
        entry["state"] = REG_LIVE
        entry["last_spawn_at"] = now
    registry_write_atomic(args.name, entry)

    # --- mounts ---
    mounts = [
        docker.types.Mount(
            target="/workspace", source=str(wdir), type="bind", read_only=False
        ),
        # Project shared/ is RO-mounted into every worker by default.
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
        LABEL_CREATED: now,
    }
    if args.interactive:
        labels[LABEL_INTERACTIVE] = "1"
    if mcps_label:
        labels[LABEL_MCPS] = mcps_label

    # --- run ---
    try:
        container = cli.containers.run(
            image,
            name=cname,
            detach=True,
            mounts=mounts,
            environment=env,
            labels=labels,
            network=INNER_NETWORK,
        )
    except APIError as e:
        # Rollback so spawn is re-invokable. Fresh: drop workdir + registry.
        # Respawn: restore prior registry (state=down). Workdir is already
        # the preserved bind-mount; don't touch it.
        if fresh:
            shutil.rmtree(wdir.parent, ignore_errors=True)
            try:
                registry_path(args.name).unlink()
            except FileNotFoundError:
                pass
        else:
            registry_write_atomic(args.name, existing)  # type: ignore[arg-type]
        die(f"docker run failed: {e}")

    # Promote: a draft proposal at plan/draft/<name>.md has now been promoted
    # to the canonical plan/<name>.md by the write above. Remove the draft so
    # `ls plan/draft/` only shows un-spawned proposals.
    draft_plan = WORKSPACE / "plan" / "draft" / f"{args.name}.md"
    if draft_plan.is_file():
        try:
            draft_plan.unlink()
        except OSError:
            pass

    _print_json({
        "name": args.name,
        "container": cname,
        "container_id": container.id,
        "image": image,
        "plan": str(canonical_plan),
        "data_mounts": args.data_mount,
        "interactive": bool(args.interactive),
        "state": REG_LIVE,
        "respawned": not fresh,
        "cycles_accepted": len(entry["cycles"]),
        "created_at": entry["created_at"],
        "last_spawn_at": entry["last_spawn_at"],
    })


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _live_row(c, name: str) -> dict:
    dm = c.labels.get(LABEL_MOUNTS, "")
    wdir = worker_dir(name)
    reg = registry_load(name) or {}
    return {
        "name": name,
        "container": c.name,
        "state": _resolve_state(c, wdir),
        "registry_state": reg.get("state"),
        "cycles_accepted": len(reg.get("cycles", [])),
        "staging": staging_link(name).is_symlink(),
        "worker_type": c.labels.get(LABEL_TYPE, ""),
        "data_mounts": [s for s in dm.split(",") if s],
        "created_at": c.labels.get(LABEL_CREATED, ""),
        "image": c.image.tags[0] if c.image.tags else c.image.id[:12],
    }


def _registry_row(name: str, reg: dict) -> dict:
    return {
        "name": name,
        "container": None,
        "state": None,
        "registry_state": reg.get("state"),
        "cycles_accepted": len(reg.get("cycles", [])),
        "staging": staging_link(name).is_symlink(),
        "worker_type": "analysis",
        "data_mounts": [],
        "created_at": reg.get("created_at", ""),
        "image": None,
        "last_spawn_at": reg.get("last_spawn_at"),
        "last_down_at": reg.get("last_down_at"),
        "destroyed_at": reg.get("destroyed_at"),
    }


def cmd_list(args: argparse.Namespace) -> None:
    live_by_name: dict[str, dict] = {}
    for c in _client().containers.list(all=True, filters={"label": LABEL_WORKER}):
        bare = c.name.removeprefix(CONTAINER_PREFIX) if c.name.startswith(CONTAINER_PREFIX) else c.name
        live_by_name[bare] = _live_row(c, bare)

    if not args.all:
        _print_json(sorted(live_by_name.values(), key=lambda x: x["name"]))
        return

    # --all: union live containers + every registry entry.
    rows: dict[str, dict] = dict(live_by_name)
    for name, reg in registry_all().items():
        if name in rows:
            # Container present — still surface registry extras.
            rows[name]["last_spawn_at"] = reg.get("last_spawn_at")
            rows[name]["last_down_at"] = reg.get("last_down_at")
            rows[name]["destroyed_at"] = reg.get("destroyed_at")
            continue
        rows[name] = _registry_row(name, reg)

    _print_json(sorted(rows.values(), key=lambda x: x["name"]))


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def cmd_history(_args: argparse.Namespace) -> None:
    rows = []
    for name, reg in registry_all().items():
        rows.append({
            "name": name,
            "state": reg.get("state"),
            "created_at": reg.get("created_at"),
            "last_spawn_at": reg.get("last_spawn_at"),
            "last_down_at": reg.get("last_down_at"),
            "destroyed_at": reg.get("destroyed_at"),
            "cycles_accepted": len(reg.get("cycles", [])),
            "plan_summary": reg.get("plan_summary", ""),
        })
    rows.sort(key=lambda r: r.get("created_at") or "")
    _print_json(rows)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> None:
    reg = registry_load(args.name)
    wdir = worker_dir(args.name)

    try:
        c = _client().containers.get(container_name(args.name))
    except NotFound:
        c = None

    inbox = sorted(p.name for p in (wdir / "inbox").glob("*")) if (wdir / "inbox").is_dir() else []
    outputs = (
        sorted(str(p.relative_to(wdir)) for p in (wdir / "outputs").rglob("*") if p.is_file())
        if (wdir / "outputs").is_dir() else []
    )
    output_slugs = (
        sorted(p.name for p in (wdir / "outputs").iterdir() if p.is_dir())
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

    state = _resolve_state(c, wdir) if c else None
    _print_json({
        "name": args.name,
        "container": c.name if c else None,
        "state": state,
        "registry_state": (reg or {}).get("state"),
        "cycles": (reg or {}).get("cycles", []),
        "plan_summary": (reg or {}).get("plan_summary", ""),
        "created_at": (reg or {}).get("created_at"),
        "last_spawn_at": (reg or {}).get("last_spawn_at"),
        "last_down_at": (reg or {}).get("last_down_at"),
        "destroyed_at": (reg or {}).get("destroyed_at"),
        "staging": staging_link(args.name).is_symlink(),
        "exit_code": (c.attrs.get("State", {}).get("ExitCode") if c else None),
        "done_sentinel": (wdir / "DONE").exists(),
        "waiting_sentinel": (wdir / "WAITING").exists(),
        "inbox_unread": inbox,
        "output_slugs": output_slugs,
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
    # Guard against same-second collisions.
    while msg.exists():
        time.sleep(0.01)
        msg = inbox / f"msg_{int(time.time())}.md"
    msg.write_text(text)
    _print_json({
        "name": args.name,
        "delivered_via": "inbox",
        "path": str(msg.relative_to(wdir)),
    })


# ---------------------------------------------------------------------------
# shutdown — graceful stop + rm + registry down (used by /log)
# ---------------------------------------------------------------------------


def cmd_shutdown(args: argparse.Namespace) -> None:
    c = _get_container(args.name)
    reg = registry_load(args.name)
    if not reg:
        die(f"no registry entry for {args.name!r}; cannot shut down cleanly")
    if reg.get("state") != REG_LIVE:
        die(f"worker {args.name!r} registry state is {reg.get('state')!r}, not 'live'")

    # SIGTERM → entrypoint trap → DONE → exit 0. Grace: spec default 10s.
    try:
        c.stop(timeout=args.timeout)
    except APIError as e:
        die(f"docker stop failed: {e}")
    try:
        c.remove(force=False)
    except APIError as e:
        die(f"docker rm failed: {e}")

    now = _iso_now()
    reg["state"] = REG_DOWN
    reg["last_down_at"] = now
    registry_write_atomic(args.name, reg)

    _print_json({
        "name": args.name,
        "state": REG_DOWN,
        "last_down_at": now,
    })


# ---------------------------------------------------------------------------
# destroy — explicit, rare; tombstones the name
# ---------------------------------------------------------------------------


def cmd_destroy(args: argparse.Namespace) -> None:
    reg = registry_load(args.name)
    if not reg:
        # Also allow destroying a bare container whose registry entry never
        # got written (e.g. partial crash during first spawn). Preserve the
        # existing behavior: only refuse if the name simply isn't there.
        try:
            c = _client().containers.get(container_name(args.name))
        except NotFound:
            die(f"no such worker: {args.name!r}")
    else:
        try:
            c = _client().containers.get(container_name(args.name))
        except NotFound:
            c = None

    wdir = worker_dir(args.name)

    if not args.yes:
        msg = [f"Worker {args.name!r}:"]
        if c is not None:
            msg.append(f"  container: {c.name} ({c.status})")
        msg.append(f"  workdir:   {wdir.parent} (will be wiped)")
        if reg:
            msg.append(f"  cycles accepted: {len(reg.get('cycles', []))}")
        msg.append(f"  results/:  preserved at {results_dir(args.name)}")
        msg.append(f"  registry:  tombstoned; name {args.name!r} will be reserved")
        msg.append("Pass --yes to confirm destruction.")
        die("\n".join(msg))

    # Graceful stop if a container is still around.
    if c is not None:
        try:
            c.stop(timeout=10)
        except APIError:
            pass
        try:
            c.remove(force=True)
        except (APIError, NotFound):
            pass

    # Remove staging symlink.
    sl = staging_link(args.name)
    if sl.is_symlink() or sl.exists():
        try:
            sl.unlink()
        except OSError:
            pass

    # Wipe the bind-mount worker dir (not results/, not registry).
    shutil.rmtree(wdir.parent, ignore_errors=True)

    # Archive the plan.
    plan_file = WORKSPACE / "plan" / f"{args.name}.md"
    archived_to: str | None = None
    if plan_file.is_file():
        archive_dir = WORKSPACE / "plan" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        dst = archive_dir / f"{args.name}.md"
        shutil.move(str(plan_file), str(dst))
        archived_to = str(dst)

    # Tombstone the registry.
    had_cycles = bool((reg or {}).get("cycles"))
    if reg is None:
        # No prior entry — write a minimal tombstone so future spawns are
        # refused with a reserved-name error.
        now = _iso_now()
        reg = {
            "name": args.name,
            "created_at": now,
            "state": REG_DESTROYED_PRE,
            "plan_summary": "",
            "cycles": [],
            "last_spawn_at": now,
            "last_down_at": None,
            "destroyed_at": now,
        }
    else:
        reg["state"] = REG_DESTROYED_POST if had_cycles else REG_DESTROYED_PRE
        reg["destroyed_at"] = _iso_now()
    registry_write_atomic(args.name, reg)

    _print_json({
        "name": args.name,
        "destroyed": True,
        "state": reg["state"],
        "plan_archived_to": archived_to,
        "results_preserved_at": str(results_dir(args.name)) if results_dir(args.name).is_dir() else None,
    })


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
    """Return {name -> {state, exit_code}} for each requested worker."""
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
# finalize — stage a cycle's deliverable dir for PI review
# ---------------------------------------------------------------------------


def _is_denied(path: Path) -> bool:
    if any(part in OUTPUT_DENYLIST_DIRS for part in path.parts):
        return True
    return path.suffix.lower() in OUTPUT_DENYLIST_SUFFIXES


def _is_whitelisted(path: Path) -> bool:
    return path.suffix.lower() in OUTPUT_WHITELIST_SUFFIXES


def cmd_finalize(args: argparse.Namespace) -> None:
    if not _valid_slug(args.slug):
        die(f"invalid slug {args.slug!r}; use kebab-case (lowercase, '-' separators)")

    _get_container(args.name)  # existence check
    wdir = worker_dir(args.name)
    slug_dir = wdir / "outputs" / args.slug
    if not slug_dir.is_dir():
        die(
            f"outputs/{args.slug}/ does not exist for worker {args.name!r}. "
            f"Did the worker place its deliverable there?"
        )

    sl = staging_link(args.name)
    sl.parent.mkdir(parents=True, exist_ok=True)
    if sl.is_symlink() or sl.exists():
        die(
            f"staging/{args.name} already exists (pointing at "
            f"{os.readlink(sl) if sl.is_symlink() else '?'}). "
            f"Run `rs-worker unstage {args.name}` first, or `rs-worker accept` "
            f"if the pending cycle is good."
        )

    # Relative symlink so it's portable across renames of PROJECTS_DIR.
    target = Path("..") / "workers" / args.name / "work" / "outputs" / args.slug
    os.symlink(target, sl)

    _print_json({
        "name": args.name,
        "slug": args.slug,
        "staging": str(sl),
        "target": str(target),
    })


# ---------------------------------------------------------------------------
# unstage — clear the staging symlink (PI rejected; iterate with a new slug)
# ---------------------------------------------------------------------------


def cmd_unstage(args: argparse.Namespace) -> None:
    sl = staging_link(args.name)
    if not (sl.is_symlink() or sl.exists()):
        die(f"no staging symlink for worker {args.name!r}")
    try:
        target = os.readlink(sl) if sl.is_symlink() else None
    except OSError:
        target = None
    sl.unlink()
    _print_json({
        "name": args.name,
        "unstaged": True,
        "was_pointing_at": target,
    })


# ---------------------------------------------------------------------------
# accept — shape-check outputs/<slug>/, copy to results/, update registry
# ---------------------------------------------------------------------------


def _accept_checks(wdir: Path, container, slug_dir: Path) -> list[str]:
    """Return list of failure messages for a slug's deliverable dir."""
    failures: list[str] = []

    state = _resolve_state(container, wdir)
    if state not in ACCEPTED_STATES:
        failures.append(
            f"state is {state!r}; must be 'done' or 'waiting' (run `rs-worker wait` "
            "first, or investigate a failed run)"
        )
        return failures

    if not slug_dir.is_dir() or not any(p.is_file() for p in slug_dir.rglob("*")):
        failures.append(f"outputs/{slug_dir.name}/ is empty")

    rl = wdir / "research_log.md"
    skel = wdir / ".claude" / "skeleton_research_log.md"
    if not rl.is_file():
        failures.append("research_log.md is missing")
    elif skel.is_file() and rl.read_bytes() == skel.read_bytes():
        failures.append("research_log.md is unchanged from the skeleton")

    if slug_dir.is_dir():
        has_whitelisted = any(
            _is_whitelisted(p) for p in slug_dir.rglob("*") if p.is_file()
        )
        if not has_whitelisted:
            failures.append(
                f"no files in outputs/{slug_dir.name}/ match the deliverable whitelist "
                f"({', '.join(OUTPUT_WHITELIST_SUFFIXES)})"
            )
        denied = sorted(
            str(p.relative_to(slug_dir))
            for p in slug_dir.rglob("*") if p.is_file() and _is_denied(p)
        )
        denied_dirs = sorted(
            str(p.relative_to(slug_dir))
            for p in slug_dir.rglob("*") if p.is_dir() and p.name in OUTPUT_DENYLIST_DIRS
        )
        flagged = denied + denied_dirs
        if flagged:
            failures.append(
                f"outputs/{slug_dir.name}/ contains denied files/dirs: "
                + ", ".join(flagged)
            )

    return failures


def cmd_accept(args: argparse.Namespace) -> None:
    if not _valid_slug(args.slug):
        die(f"invalid slug {args.slug!r}; use kebab-case (lowercase, '-' separators)")

    c = _get_container(args.name)
    wdir = worker_dir(args.name)
    reg = registry_load(args.name)
    if not reg:
        die(f"no registry entry for {args.name!r}; spawn may have failed")
    if reg.get("state") != REG_LIVE:
        die(f"worker {args.name!r} registry state is {reg.get('state')!r}, not 'live'")

    # Staging precondition.
    sl = staging_link(args.name)
    if not sl.is_symlink():
        die(
            f"no staging symlink at staging/{args.name}. Run `rs-worker finalize "
            f"{args.name} --slug {args.slug}` first."
        )
    expected = Path("..") / "workers" / args.name / "work" / "outputs" / args.slug
    actual = Path(os.readlink(sl))
    if actual != expected:
        die(
            f"staging/{args.name} points at {actual!s}, not at outputs/{args.slug}/. "
            f"Unstage and re-finalize with the correct slug."
        )

    # Slug-uniqueness.
    existing_slugs = {cyc.get("slug") for cyc in reg.get("cycles", [])}
    if args.slug in existing_slugs:
        die(
            f"slug {args.slug!r} is already an accepted cycle for {args.name!r}. "
            f"Pick a slug that describes this cycle's unique facet."
        )

    slug_dir = wdir / "outputs" / args.slug
    failures: list[str] = []
    if not args.waived:
        failures = _accept_checks(wdir, c, slug_dir)
        if failures:
            for msg in failures:
                print(f"rs-worker: accept check failed: {msg}", file=sys.stderr)
            sys.exit(1)

    # Plan must exist before any state mutation: every accepted cycle gets a
    # snapshot alongside its deliverable so plans aren't lost on respawn.
    canonical_plan = WORKSPACE / "plan" / f"{args.name}.md"
    if not canonical_plan.is_file():
        die(
            f"canonical plan {canonical_plan} is missing; cannot snapshot it "
            f"into the cycle bundle. Restore the plan or rewrite it before "
            f"retrying accept."
        )

    # Reserve plan.md at the cycle root for the snapshot.
    if (slug_dir / "plan.md").is_file():
        die(
            f"outputs/{args.slug}/plan.md is reserved for the harness-written "
            f"plan snapshot; rename the worker's file (e.g. cycle_plan.md) and "
            f"retry."
        )

    # Copy deliverable into results/<name>/<NNN>_<slug>/.
    ordinal = len(reg.get("cycles", [])) + 1
    result_subdir = results_dir(args.name) / f"{ordinal:03d}_{args.slug}"
    result_subdir.parent.mkdir(parents=True, exist_ok=True)
    if result_subdir.exists():
        # Should not happen (ordinal/slug pair unique by construction); still fail loud.
        die(f"results path already exists: {result_subdir}")
    shutil.copytree(slug_dir, result_subdir)
    shutil.copy2(canonical_plan, result_subdir / "plan.md")

    # Drop the staging symlink.
    sl.unlink()

    # Append the cycle to the registry.
    now = _iso_now()
    cycle = {"ordinal": ordinal, "slug": args.slug, "accepted_at": now}
    if args.waived:
        cycle["waived"] = args.waived
    reg.setdefault("cycles", []).append(cycle)
    registry_write_atomic(args.name, reg)

    _print_json({
        "name": args.name,
        "accepted": True,
        "cycle": ordinal,
        "ordinal": ordinal,
        "slug": args.slug,
        "waived": args.waived,
        "accepted_at": now,
        "promoted_to": str(result_subdir),
    })


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rs-worker",
        description="Worker lifecycle inside the supervisor.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn",
                        help="stage a worker workdir and start its container; "
                             "implicit-respawn on a `down` name")
    sp.add_argument("name")
    sp.add_argument("--plan", required=True,
                    help="path to a plan file with required top-level sections: "
                         "## Question, ## Inputs, ## Deliverables, ## Verification, ## MCPs")
    sp.add_argument("--image", default=DEFAULT_IMAGE, help=f"worker image (default: {DEFAULT_IMAGE})")
    sp.add_argument("--data-mount", action="append", default=[],
                    help="absolute path to bind-mount RO into the worker; repeatable")
    sp.add_argument("--env", action="append", default=[], help="K=V; repeatable")
    sp.add_argument("--mcps", default="",
                    help="comma-separated MCP names from `research mcp list`; "
                         "each must already be granted via "
                         "`research project mcp allow <project> <mcp>`")
    sp.add_argument("--interactive", action="store_true",
                    help="(reserved) run claude interactively in byobu instead of headless")
    sp.set_defaults(func=cmd_spawn)

    sl = sub.add_parser("list",
                        help="list workers (default: live only; --all includes "
                             "down and destroyed tombstones)")
    sl.add_argument("--all", action="store_true",
                    help="include `down` and `destroyed_*` workers from the registry")
    sl.set_defaults(func=cmd_list)

    sh = sub.add_parser("history",
                        help="dump every registry entry sorted by created_at")
    sh.set_defaults(func=cmd_history)

    st = sub.add_parser("status",
                        help="container + filesystem + registry + log blob (JSON)")
    st.add_argument("name")
    st.add_argument("--log-lines", type=int, default=20)
    st.set_defaults(func=cmd_status)

    sm = sub.add_parser("message", help="send a message to a worker")
    sm.add_argument("name")
    sm.add_argument("text")
    sm.add_argument("--send-keys", action="store_true",
                    help="inject into byobu pane (interactive workers only)")
    sm.set_defaults(func=cmd_message)

    sdwn = sub.add_parser("shutdown",
                          help="graceful docker stop + rm; registry state → down; "
                               "bind-mount preserved")
    sdwn.add_argument("name")
    sdwn.add_argument("--timeout", type=int, default=10,
                      help="seconds to wait for SIGTERM trap before docker kills (default: 10)")
    sdwn.set_defaults(func=cmd_shutdown)

    sd = sub.add_parser("destroy",
                        help="rm -f container + wipe workdir + tombstone registry; "
                             "results/ and plan archive preserved")
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
                        help="stage outputs/<slug>/ for PI review by creating "
                             "staging/<name> -> outputs/<slug>/")
    sf.add_argument("name")
    sf.add_argument("--slug", required=True, help="kebab-case slug of the cycle to stage")
    sf.set_defaults(func=cmd_finalize)

    sus = sub.add_parser("unstage",
                         help="remove staging/<name> symlink (PI rejected; will iterate)")
    sus.add_argument("name")
    sus.set_defaults(func=cmd_unstage)

    sac = sub.add_parser("accept",
                         help="promote staged outputs/<slug>/ to results/<name>/<NNN>_<slug>/ "
                              "after shape checks pass; append the cycle to the registry")
    sac.add_argument("name")
    sac.add_argument("--slug", required=True, help="kebab-case slug matching the staged cycle")
    sac.add_argument("--waived", default=None, metavar="REASON",
                     help="skip shape checks and accept with an explicit logged reason")
    sac.set_defaults(func=cmd_accept)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
