"""rscore — the host-side project-lifecycle core.

Stdlib-only. One implementation of the lifecycle verbs (create / destroy /
start / stop / update / list / status), shared by every front-end so they can
never drift apart. Today the only front-end is the terminal CLI (research.py);
a browser-driven one will call the same verbs later.

The shape each verb follows:

    request kwargs ─▶ <Req>.from_kwargs(**kw)   # the one validation point
                          │  (name rule, string→enum, csv/list→tuple)
                          ▼
                     verb(req) ─▶ <Result> dataclass
                          │
              ┌───────────┴────────────┐
        terminal: format text     browser: serialise JSON

Dependency direction is one-way: callers import rscore; rscore imports nothing
of theirs. The lifecycle substrate (Config + constants + the docker/network
helpers below the verbs) lives in this module so the verbs are self-contained;
research.py imports it all back via `from rscore import *` so its non-lifecycle
cmd_* keep resolving the same helpers.

Two failure channels, deliberately distinct:

  • ValidationError — bad input (name, enum value). Raised by from_kwargs
    BEFORE any side effect, so a front-end can reject cleanly with no partial
    state. The terminal maps it to its error-print + exit; the browser to a
    400-style reply.
  • SystemExit — a failure mid-execution (missing image, a component that
    won't start). Raised by the shared die() deep in the helper cone and left
    as SystemExit so it propagates and aborts the verb (fail-explicit). The
    terminal lets it exit the process; the browser wraps it into an error
    reply. Component failures are NOT swallowed — see create()'s notes.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import enum
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

# rscore lives in cli/; make sibling helper modules importable even when
# imported directly (e.g. by the broker), not only via research.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import defaults  # noqa: E402
import mcp_registry  # noqa: E402
import pi_isolated_registry  # noqa: E402
import role_mcp  # noqa: E402
import sandbox  # noqa: E402


# ---------------------------------------------------------------------------
# Enums — closed vocabularies (one per CLI choices=[...] today). str-mixin so a
# member compares/round-trips to its string value with no adapter code.
# ---------------------------------------------------------------------------


class ProjectType(str, enum.Enum):
    RESEARCH = "research"
    SANDBOX = "sandbox"


class Egress(str, enum.Enum):
    OPEN = "open"
    LOCKED = "locked"


class DindMode(str, enum.Enum):
    AUTO = "auto"
    SYSBOX = "sysbox"
    PRIVILEGED = "privileged"


# ---------------------------------------------------------------------------
# Input-validation failure channel.
# ---------------------------------------------------------------------------


class ValidationError(ValueError):
    """Bad request input. Always raised before any side effect."""


# ---------------------------------------------------------------------------
# Validators + coercion (the choke point's building blocks).
# ---------------------------------------------------------------------------

# Project name: ASCII letters/digits, plus '-'/'_' after the first character,
# and must start with a letter or digit. Stricter than a bare alnum check on
# purpose — unicode names and leading '-' are downstream Docker footguns (and a
# leading '-' can be read as a flag), with no use case here.
_PROJECT_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def valid_project_name(name: str) -> bool:
    return isinstance(name, str) and bool(_PROJECT_NAME_RE.match(name))


def _require_name(name: Any) -> str:
    if not valid_project_name(name):
        raise ValidationError(
            "project name must be ASCII letters/digits, may contain '-' or "
            "'_', and must start with a letter or digit")
    return name


def _as_enum(cls: type, value: Any, *, field_name: str, default: Any) -> Any:
    """Coerce a string/enum into ``cls``. ``None`` ⇒ ``default`` (which may be
    ``None`` to mean 'resolve from config at run time', as egress/dind do). An
    unknown value is a ValidationError listing the valid choices."""
    if value is None:
        return default
    if isinstance(value, cls):
        return value
    try:
        return cls(value)
    except ValueError:
        choices = ", ".join(m.value for m in cls)
        raise ValidationError(
            f"invalid {field_name} value: {value!r} (expected {choices})")


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Normalise a list-like field from EITHER a comma-string (terminal) OR a
    list (browser) into a tuple of stripped, non-empty tokens. ``None`` ⇒
    ``()``. One choke point serves both front-ends unchanged."""
    if value is None:
        return ()
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, Sequence):
        parts = list(value)
    else:
        raise ValidationError(
            f"expected a string or list, got {type(value).__name__}")
    return tuple(p.strip() for p in parts if isinstance(p, str) and p.strip())


# ---------------------------------------------------------------------------
# Request objects — the verb vocabulary. Frozen + built only via from_kwargs
# (the validation point); never instantiate raw from untrusted input.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateRequest:
    name: str
    type: ProjectType = ProjectType.RESEARCH
    egress: Egress | None = None          # None ⇒ config/flavor default at run time
    dind: DindMode | None = None          # None ⇒ cfg.default_dind at run time
    profile: str = "python"               # accepted for CLI parity; currently unused
    data: tuple[str, ...] = ()
    memory: str = ""                      # "" ⇒ cfg.default_memory
    cpus: str = ""
    ssh_port: int | None = None
    inner_firewall: bool = False
    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()
    role_mcp_upstream: tuple[str, ...] = ()
    mcp: str = "all-enabled"

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "CreateRequest":
        ssh_port = kw.get("ssh_port")
        if ssh_port is not None and not isinstance(ssh_port, int):
            raise ValidationError("ssh_port must be an integer")
        return cls(
            name=_require_name(kw.get("name")),
            type=_as_enum(ProjectType, kw.get("type"),
                          field_name="--type", default=ProjectType.RESEARCH),
            egress=_as_enum(Egress, kw.get("egress"),
                            field_name="--egress", default=None),
            dind=_as_enum(DindMode, kw.get("dind"),
                          field_name="--dind", default=None),
            profile=kw.get("profile") or "python",
            data=_as_tuple(kw.get("data")),
            memory=kw.get("memory") or "",
            cpus=kw.get("cpus") or "",
            ssh_port=ssh_port,
            inner_firewall=bool(kw.get("inner_firewall", False)),
            enable=_as_tuple(kw.get("enable")),
            disable=_as_tuple(kw.get("disable")),
            role_mcp_upstream=_as_tuple(kw.get("role_mcp_upstream")),
            mcp=kw.get("mcp") if kw.get("mcp") is not None else "all-enabled",
        )


@dataclass(frozen=True)
class DestroyRequest:
    name: str
    # Confirmation is a FRONT-END concern (terminal prompt; the browser's own
    # gate). The verb just destroys — no prompting in rscore.

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "DestroyRequest":
        return cls(name=_require_name(kw.get("name")))


@dataclass(frozen=True)
class StatusRequest:
    name: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "StatusRequest":
        return cls(name=_require_name(kw.get("name")))


@dataclass(frozen=True)
class StartStopRequest:
    """stop/start take a single project OR --all (exactly one)."""
    name: str | None = None
    all: bool = False

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "StartStopRequest":
        name = kw.get("name")
        all_ = bool(kw.get("all", False))
        if all_ == (name is not None):
            raise ValidationError(
                "specify exactly one of: project name, or --all")
        return cls(name=_require_name(name) if name is not None else None,
                   all=all_)


@dataclass(frozen=True)
class UpdateRequest:
    name: str
    rebuild: bool = False
    keep_claude: bool = False
    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()
    role_mcp_upstream: tuple[str, ...] = ()

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "UpdateRequest":
        return cls(
            name=_require_name(kw.get("name")),
            rebuild=bool(kw.get("rebuild", False)),
            keep_claude=bool(kw.get("keep_claude", False)),
            enable=_as_tuple(kw.get("enable")),
            disable=_as_tuple(kw.get("disable")),
            role_mcp_upstream=_as_tuple(kw.get("role_mcp_upstream")),
        )


# ---------------------------------------------------------------------------
# Result objects — what each verb returns. The terminal formats these into its
# text; the browser serialises them to JSON. The SSH password rides back in the
# result (in memory), never written to disk.
# ---------------------------------------------------------------------------


@dataclass
class CreateResult:
    project: str
    container: str
    workspace: str
    network: str
    egress: str
    dind_mode: str
    inner_firewall: bool
    project_type: str
    ssh_port: int
    ssh_password: str                       # in-memory only
    data_mounts: dict[str, str] = field(default_factory=dict)   # basename → host src
    mcps: list[str] = field(default_factory=list)               # granted (best-effort)
    workers: list[str] = field(default_factory=list)            # enabled (all-or-abort)
    sandboxes: list[str] = field(default_factory=list)          # enabled (all-or-abort)


@dataclass
class ProjectSummary:
    project: str
    state: str
    ssh: str | None                         # "localhost:<port>" or None


@dataclass
class ProjectStatus:
    project: str
    container: str
    state: str
    workspace: str
    ssh_port: str | None
    inner_workers: list[str] = field(default_factory=list)   # "name\tstatus\timage" lines
    registry_count: int = 0


@dataclass
class ActionResult:
    """One per container for stop/start, incl. skips."""
    name: str
    project: str | None
    action: str                             # "stop" | "start"
    outcome: str                            # "ok" | "skip:absent" | "skip:already"


@dataclass
class UpdateResult:
    project: str
    rebuilt: bool
    refreshed_claude: bool
    workers_enabled: list[str] = field(default_factory=list)
    sandboxes_enabled: list[str] = field(default_factory=list)
    workers_disabled: list[str] = field(default_factory=list)
    sandboxes_disabled: list[str] = field(default_factory=list)


# ===========================================================================
# Verbs
# ===========================================================================


def create(req: CreateRequest, cfg: "Config" | None = None) -> CreateResult:  # type: ignore[name-defined]
    """Create a project. ``req`` is already validated (no name re-check).

    Failure policy:
      • Missing prerequisites / bad inputs → die() (SystemExit), aborts.
      • A requested worker or sandbox that won't enable → aborts the whole
        create (fail-explicit; the partial project is left standing so it can
        be inspected or destroyed and retried).
      • External MCP allow-listing is best-effort: a single MCP that can't be
        granted prints a warning and creation continues (MCPs are external and
        can be transient).

    Progress lines print to stdout for now; a structured progress feed for the
    browser is an additive front-end concern for later, not a redesign.
    """
    if cfg is None:
        cfg = load_config()
    project = req.name

    container_name = container_name_for(project)
    workspace_path = workspace_path_for(project, cfg)

    if container_exists(container_name):
        die(f"project {project!r} already exists (container {container_name}). "
            f"Use destroy first.")

    project_type = (PROJECT_TYPE_SANDBOX
                    if req.type is ProjectType.SANDBOX
                    else PROJECT_TYPE_RESEARCH)
    substrate_image = (MANAGEMENT_IMAGE if project_type == PROJECT_TYPE_SANDBOX
                       else SUPERVISOR_IMAGE)

    # Verify prerequisites.
    if not run_quiet(["docker", "image", "inspect", substrate_image]):
        die(f"image {substrate_image} not found. Run `research setup` first.")
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research setup` first.")

    dind_mode = select_dind_mode(
        (req.dind.value if req.dind is not None else None) or cfg.default_dind)

    # Egress. Sandbox flavor defaults to `locked`; research to cfg default.
    egress = (req.egress.value if req.egress is not None else None) or (
        "locked" if project_type == PROJECT_TYPE_SANDBOX else cfg.default_egress)
    if egress not in ("open", "locked"):
        die(f"invalid --egress value: {egress!r} (expected open|locked)")

    # Optional --data bind-mounts (RO inside supervisor), each at
    # /workspace/shared/data/<basename>/. Missing paths are mkdir -p'd;
    # basename collisions are a hard error.
    extra_mounts: list[str] = []
    data_basenames: dict[str, Path] = {}
    for raw in req.data:
        raw = raw.strip()
        if not raw:
            continue
        p = Path(raw).expanduser().resolve()
        if p.exists() and not p.is_dir():
            die(f"--data path exists but is not a directory: {p}")
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            print(f"created data directory: {p}")
        base = p.name
        if not base:
            die(f"--data path has no basename (refusing to mount root): {p}")
        if base in data_basenames:
            die(f"--data basename collision: {base!r} appears in both "
                f"{data_basenames[base]} and {p}. Rename or symlink "
                "one of the host paths so the container destinations "
                "stay distinct.")
        data_basenames[base] = p
        extra_mounts += ["-v", f"{p}:/workspace/shared/data/{base}:ro"]

    print(f"=== Creating project: {project} ===")
    ssh_port = req.ssh_port or find_free_port()
    ssh_pass = gen_password()

    # 1. Workspace dir (host bind-mount) + optional privileged-DIND volume.
    workspace_path.mkdir(parents=True, exist_ok=True)
    os.chmod(workspace_path, 0o2770)
    (workspace_path / "shared").mkdir(parents=True, exist_ok=True)
    if dind_mode == "privileged" and not volume_exists(docker_volume_name_for(project)):
        run_check(["docker", "volume", "create", docker_volume_name_for(project)])

    # 1b. Materialize the MCP bind-mount sources (files, not dirs).
    ensure_mcp_files(project, cfg)

    # 1c. Project-flavor marker on the volume (survives a supervisor recreate).
    orch_dir = workspace_path / ".orchestrator"
    orch_dir.mkdir(parents=True, exist_ok=True)
    (orch_dir / "project.json").write_text(
        json.dumps({"type": project_type}, indent=2) + "\n")

    # 2. Per-project network + router wiring.
    network, router_ip = ensure_project_network(project, egress)
    wire_webui_to_projects()

    # 3. Build docker run argv. Peel --enable/--disable tokens into the three
    #    registries before computing service flags.
    enable_services, enable_workers, enable_sandboxes, \
        enable_no_sandbox_mirror = _split_enable_tokens(",".join(req.enable))
    disable_services, disable_workers, disable_sandboxes = \
        _split_disable_tokens(",".join(req.disable))
    _dis_w, _dis_s = set(disable_workers), set(disable_sandboxes)
    if project_type == PROJECT_TYPE_SANDBOX:
        for w in enable_workers:
            print(f"note: --enable {w!r}: sandbox projects have no worker layer; "
                  f"ignoring (for a browser box use `rs-sandbox create --browser`)",
                  file=sys.stderr)
        enable_workers = []
        enable_sandboxes = [s for s in enable_sandboxes if s not in _dis_s]
    else:
        enable_workers = [w for w in _ordered_union(defaults.enabled("worker"), enable_workers)
                          if w not in _dis_w]
        enable_sandboxes = [s for s in _ordered_union(defaults.enabled("sandbox"), enable_sandboxes)
                            if s not in _dis_s]
    _known_sb = sandbox.known_type_names()
    for s in list(enable_sandboxes):
        if s not in _known_sb:
            print(f"note: default sandbox {s!r} is no longer a known type; "
                  f"skipping its auto-enable", file=sys.stderr)
            enable_sandboxes.remove(s)
    role_mcp_explicit = _parse_role_mcp_upstream(
        list(req.role_mcp_upstream), valid_roles=set(enable_workers))
    for role in enable_workers:
        try:
            role_mcp.validate_role(role)
        except ValueError as e:
            die(str(e))
    service_flags = _compute_service_flags(enable_services, disable_services)
    docker_args = build_supervisor_docker_args(
        container_name=container_name,
        project=project,
        network=network,
        workspace_path=workspace_path,
        ssh_port=ssh_port,
        ssh_pass=ssh_pass,
        dns_servers=cfg.sandbox_dns,
        memory=req.memory or cfg.default_memory,
        cpus=req.cpus or "",
        image=substrate_image,
        dind_mode=dind_mode,
        inner_firewall=req.inner_firewall,
        project_type=project_type,
        service_flags=service_flags,
    )
    if extra_mounts:
        docker_args = docker_args[:-1] + extra_mounts + [docker_args[-1]]
    ext_mounts = _sandbox_external_mounts(project)
    if ext_mounts:
        docker_args = docker_args[:-1] + ext_mounts + [docker_args[-1]]

    # 4. Create container.
    run_check(["docker", *docker_args])

    # 5. Inject default route via router (egress traverses iptables).
    inject_route(container_name, router_ip)

    # 6. Wait for inner dockerd, then stage the inner images.
    wait_for_inner_dockerd(container_name)
    if project_type == PROJECT_TYPE_SANDBOX:
        stage_worker_image(container_name, SANDBOX_BOX_IMAGE)
        stage_worker_image(container_name, SANDBOX_BOX_BROWSER_IMAGE)
    else:
        stage_worker_image(container_name, ANALYSIS_IMAGE)
    stage_worker_image(container_name, MCP_PROXY_IMAGE)

    # 6b. Re-run mcp-reload now that the proxy image is staged.
    run(["docker", "exec", container_name, "/usr/local/bin/mcp-reload"],
        capture_output=True)

    # 6c. Auto-allow MCPs per --mcp. BEST-EFFORT: external MCPs can be
    #     transient, so a single failure warns and creation continues.
    requested = _resolve_create_mcp_arg(req.mcp)
    granted: list[str] = []
    for mcp_name in requested:
        ok, msg = _allow_mcp_for_project(project, cfg, mcp_name, do_reload=False)
        if ok:
            granted.append(mcp_name)
        else:
            print(f"warning: skip auto-allow {mcp_name!r}: {msg}", file=sys.stderr)
    if granted:
        _supervisor_mcp_reload(container_name)

    # 6d. worker sugar (--enable <worker>). FAIL-EXPLICIT: a worker that won't
    #     enable aborts the create (no swallow). Enabling a worker auto-enables
    #     its baked sandbox mirror unless `no-sandbox-mirror` was given.
    workers_enabled: list[str] = []
    for role in enable_workers:
        _role_mcp_enable(project, cfg, role, role_mcp_explicit.get(role),
                         no_pi_mirror=enable_no_sandbox_mirror)
        workers_enabled.append(role)

    # 6e. sandbox sugar (--enable <sandbox>). FAIL-EXPLICIT, same as workers.
    #     A twin already brought up as a worker's mirror is skipped here.
    sandboxes_enabled: list[str] = []
    for name in enable_sandboxes:
        if name in workers_enabled:
            continue
        _sandbox_enable(project, cfg, name)
        sandboxes_enabled.append(name)

    # 7. Return result (the front-end formats the report from this).
    return CreateResult(
        project=project,
        container=container_name,
        workspace=str(workspace_path),
        network=network,
        egress=egress,
        dind_mode=dind_mode,
        inner_firewall=req.inner_firewall,
        project_type=project_type,
        ssh_port=int(ssh_port),
        ssh_password=ssh_pass,
        data_mounts={b: str(src) for b, src in data_basenames.items()},
        mcps=granted,
        workers=workers_enabled,
        sandboxes=sandboxes_enabled,
    )


def destroy(req: DestroyRequest, cfg: "Config" | None = None) -> None:  # type: ignore[name-defined]
    """Tear a project down: container + workspace dir + DIND volume + network,
    plus its router MCP-allow rules. Confirmation is the front-end's job — this
    verb just destroys. die()s if the project doesn't exist."""
    if cfg is None:
        cfg = load_config()
    project = req.name
    container = container_name_for(project)
    if not container_exists(container):
        die(f"project {project!r} does not exist")

    project_root = project_root_for(project, cfg)
    docker_volume = docker_volume_name_for(project)
    network = project_network_for(project)

    # Clean up per-project MCP rules in the router so iptables doesn't
    # accumulate orphan ACCEPTs after the project network is gone.
    if network_exists(network) and container_running(ROUTER_CONTAINER):
        try:
            subnet = get_network_subnet(network)
        except SystemExit:
            subnet = ""
        for ent in load_project_allowlist(project, cfg):
            ip = ent.get("ip")
            port = ent.get("port")
            if subnet and isinstance(ip, str) and isinstance(port, int):
                run(["docker", "exec", ROUTER_CONTAINER,
                     "/scripts/mcp-deny.sh", subnet, ip, str(port)],
                    capture_output=True)

    run(["docker", "rm", "-f", container], capture_output=True)
    if project_root.exists():
        shutil.rmtree(project_root, ignore_errors=True)
    if volume_exists(docker_volume):
        run(["docker", "volume", "rm", docker_volume], capture_output=True)
    remove_project_network(project)


def start(req: StartStopRequest, cfg: "Config" | None = None) -> list[ActionResult]:  # type: ignore[name-defined]
    """Start a stopped project (or all). On sysbox a plain `docker start` after
    `docker stop` fails, so we route through _recreate_supervisor: fresh
    container ID + bindings, workspace/creds/network preserved. Fail-explicit:
    a recreate that dies aborts (no swallow)."""
    if cfg is None:
        cfg = load_config()
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research start` first.")
    if req.all:
        containers = get_supervisor_containers()
    else:
        containers = [{"name": container_name_for(req.name), "project": req.name}]
    results: list[ActionResult] = []
    for c in containers:
        if not container_exists(c["name"]):
            results.append(ActionResult(c["name"], c.get("project"), "start", "skip:absent"))
            continue
        if container_running(c["name"]):
            results.append(ActionResult(c["name"], c.get("project"), "start", "skip:already"))
            continue
        print(f"=== Starting project: {c['project']} ===")
        _recreate_supervisor(c["project"], cfg)
        results.append(ActionResult(c["name"], c["project"], "start", "ok"))
    return results


def stop(req: StartStopRequest, cfg: "Config" | None = None) -> list[ActionResult]:  # type: ignore[name-defined]
    """Stop a project (or all). Fail-explicit: a `docker stop` that fails dies."""
    if req.all:
        names = [c["name"] for c in get_supervisor_containers()]
    else:
        names = [container_name_for(req.name)]
    results: list[ActionResult] = []
    for name in names:
        if not container_exists(name):
            results.append(ActionResult(name, None, "stop", "skip:absent"))
            continue
        run_check(["docker", "stop", name])
        results.append(ActionResult(name, None, "stop", "ok"))
    return results


def list_projects(cfg: "Config" | None = None) -> list[ProjectSummary]:  # type: ignore[name-defined]
    """Every supervisor container with its state + SSH endpoint (if running)."""
    out: list[ProjectSummary] = []
    for c in get_supervisor_containers():
        ssh = None
        if c["state"] == "running":
            port = get_ssh_port(c["name"])
            ssh = f"localhost:{port}" if port else None
        out.append(ProjectSummary(project=c["project"], state=c["state"], ssh=ssh))
    return out


def status(req: StatusRequest, cfg: "Config" | None = None) -> ProjectStatus:  # type: ignore[name-defined]
    """Project state + workspace + (if running) inner-worker lines and the
    .workers/ registry count. die()s if the project doesn't exist."""
    if cfg is None:
        cfg = load_config()
    container = container_name_for(req.name)
    if not container_exists(container):
        die(f"project {req.name!r} does not exist")
    state = run_check(["docker", "inspect", "-f", "{{.State.Status}}", container]).stdout.strip()
    ssh_port = get_ssh_port(container) if state == "running" else None
    workspace = workspace_path_for(req.name, cfg)

    inner: list[str] = []
    reg_count = 0
    if state == "running":
        r = run(["docker", "exec", container, "docker", "ps", "-a",
                 "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
                capture_output=True)
        if r.returncode == 0 and r.stdout.strip():
            inner = r.stdout.strip().splitlines()
        reg = run(["docker", "exec", container, "sh", "-c",
                   "ls /workspace/.workers/*.json 2>/dev/null | wc -l"],
                  capture_output=True)
        if reg.returncode == 0:
            try:
                reg_count = int(reg.stdout.strip() or "0")
            except ValueError:
                reg_count = 0
    return ProjectStatus(
        project=req.name, container=container, state=state,
        workspace=str(workspace), ssh_port=ssh_port,
        inner_workers=inner, registry_count=reg_count)


def update(req: UpdateRequest, cfg: "Config" | None = None) -> UpdateResult:  # type: ignore[name-defined]
    """Push edited code into a running project. Always recreates the supervisor
    (the only safe shape on sysbox): file-only mode docker-cp's edited files
    into the fresh container before first start; --rebuild rebuilds images
    first. Fail-explicit: a worker/sandbox enable or disable that dies aborts.
    Defaults are NOT re-folded (create-time only)."""
    if cfg is None:
        cfg = load_config()
    project = req.name
    container = container_name_for(project)
    if not container_exists(container):
        die(f"project {project!r} does not exist")
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research start` first.")

    print(f"=== Updating project: {project} ===")

    # Validate --enable worker tokens up front so a typo doesn't waste a rebuild.
    enable_services, enable_workers, enable_sandboxes, \
        enable_no_sandbox_mirror = _split_enable_tokens(",".join(req.enable))
    disable_services, disable_workers, disable_sandboxes = \
        _split_disable_tokens(",".join(req.disable))
    role_mcp_explicit = _parse_role_mcp_upstream(
        list(req.role_mcp_upstream), valid_roles=set(enable_workers))
    for role in enable_workers:
        try:
            role_mcp.validate_role(role)
        except ValueError as e:
            die(str(e))

    if req.rebuild:
        print("rebuilding images...")
        _build_images(force=True)

    hook = None
    if not req.rebuild:
        def hook(c: str) -> None:
            print(f"copying edited files into {c}...")
            for rel in _docker_cp_supervisor_files(c):
                print(f"  {rel}")

    flags_override: dict[str, bool] | None = None
    if enable_services or disable_services:
        base = _read_service_flags(container)
        flags_override = _compute_service_flags(
            enable_services, disable_services, base=base)

    # Disables run BEFORE the recreate (the recreate's restart loops read
    # role-mcps.json / sandbox.json; a removed entry must not come back up).
    # Fail-explicit: a disable that dies aborts.
    workers_disabled: list[str] = []
    sandboxes_disabled: list[str] = []
    for role in disable_workers:
        _role_mcp_disable(project, cfg, role)
        workers_disabled.append(role)
    for name in disable_sandboxes:
        _sandbox_disable(project, cfg, name)
        sandboxes_disabled.append(name)

    _recreate_supervisor(
        project, cfg,
        force_restage=req.rebuild,
        post_create_hook=hook,
        service_flags=flags_override,
    )

    # Sandbox projects have no /workspace/.claude/ by design — skip the refresh.
    refreshed = False
    if not req.keep_claude and _container_project_type(container) != PROJECT_TYPE_SANDBOX:
        print("refreshing /workspace/.claude/ from templates...")
        _refresh_workspace_claude_templates(container)
        refreshed = True

    # worker + sandbox enables (idempotent on re-run). Fail-explicit.
    workers_enabled: list[str] = []
    sandboxes_enabled: list[str] = []
    for role in enable_workers:
        _role_mcp_enable(project, cfg, role, role_mcp_explicit.get(role),
                         no_pi_mirror=enable_no_sandbox_mirror)
        workers_enabled.append(role)
    for name in enable_sandboxes:
        _sandbox_enable(project, cfg, name)
        sandboxes_enabled.append(name)

    return UpdateResult(
        project=project, rebuilt=req.rebuild, refreshed_claude=refreshed,
        workers_enabled=workers_enabled, sandboxes_enabled=sandboxes_enabled,
        workers_disabled=workers_disabled, sandboxes_disabled=sandboxes_disabled)


# ===========================================================================
# Relocated substrate — moved verbatim out of research.py. These lifecycle
# helpers + Config + constants back the verbs above; research.py imports them
# back via `from rscore import *`.
# ===========================================================================


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (rscore lives in cli/)

# Shared per-project substrate (DIND + ssh + byobu + code-server). Both
# per-project container images FROM it (STAGE_SANDBOX_PROJECT.md image split).
SUBSTRATE_BASE_IMAGE = "rs-substrate-base:latest"
SUPERVISOR_IMAGE = "rs-supervisor:latest"          # research flavor (agent leaf)
MANAGEMENT_IMAGE = "rs-management:latest"           # --type sandbox flavor (agent-less)
ANALYSIS_IMAGE = "rs-analysis-base:latest"
MCP_PROXY_IMAGE = "rs-mcp-proxy:latest"
ROLE_MCP_BASE_IMAGE = "rs-role-mcp-base:latest"
PI_BASE_IMAGE = "rs-pi-base:latest"
PI_ISOLATED_IMAGE = "rs-pi-isolated:latest"
# Disposable box image for the agent-less sandbox-project flavor
# (STAGE_SANDBOX_PROJECT.md). FROM rs-analysis-base (NOT rs-pi-base) — clean,
# no artifact-contract baggage. Staged into a sandbox project's inner dockerd
# in place of the analysis worker.
SANDBOX_BOX_IMAGE = "rs-sandbox-box:latest"
# Browser variant — FROM rs-sandbox-box + @playwright/mcp + Chromium, wired
# into the box's claude as a stdio MCP (the playwright bundle lifted from the
# websearcher image, WITHOUT its role.md harness). Opt-in per box via
# `rs-sandbox create --browser`.
SANDBOX_BOX_BROWSER_IMAGE = "rs-sandbox-box-browser:latest"
INNER_NETWORK = "rs-inner"
WEBUI_IMAGE = "rs-webui:latest"
WEBUI_CONTAINER = "rs-webui"
ROUTER_CONTAINER = "rs-router"
ROUTER_NETWORK = "rs-sandbox"
CONTAINER_PREFIX = "rs-project-"
DOCKER_VOLUME_PREFIX = "rs-docker-"
PROJECT_NETWORK_PREFIX = "rs-net-"
PROJECT_LABEL = "research.project"
DIND_MODE_LABEL = "research.dind"
# Project flavor: "research" (default — supervisor agent + workers) or
# "sandbox" (agent-less collection of isolated boxes, STAGE_SANDBOX_PROJECT.md).
# Mirrored into .orchestrator/project.json so the webui (which reads only the
# project volume, no docker socket) and the in-supervisor rs-sandbox CLI can
# branch on it.
PROJECT_TYPE_LABEL = "research.project_type"
PROJECT_TYPE_RESEARCH = "research"
PROJECT_TYPE_SANDBOX = "sandbox"
MCP_CONTAINER_PREFIX = "rs-mcp-"
MCP_LABEL = "research.mcp"
MCP_NAME_LABEL = "research.mcp_name"
PROBE_IMAGE = "busybox:1.36"

# Image version pins live in a visible root-level manifest, not scattered as
# Dockerfile ARG defaults. `_build_images` threads them as `docker build
# --build-arg`; see versions.env for the workflow + per-pin caveats.
VERSIONS_FILE = SCRIPT_DIR / "versions.env"

# Upstream datasource per pin, consumed by `research images outdated`. Kept here
# rather than annotated into versions.env so the manifest stays a clean
# KEY=VALUE file. Two ecosystems are stdlib-awkward to query honestly — the
# docker-ce static repo (HTML dir listing) and the VS Code Marketplace gallery
# (POST query API) — so they're marked "manual" with a URL instead of a faked
# check. A pin present in versions.env but absent here prints "no source"; a
# source whose key isn't pinned is skipped. Keep in sync when adding a pin.
VERSION_SOURCES: dict[str, dict[str, str]] = {
    "CODE_SERVER_VERSION": {
        "kind": "github-releases",
        "repo": "coder/code-server",
        # The tag is the code-server version; the extension-compat gate is the
        # *bundled* VS Code version, which this check does NOT resolve — open
        # the release to confirm before bumping for an engines.vscode reason.
        "note": "verify bundled VS Code in the release notes before bumping",
    },
    "PLAYWRIGHT_MCP_VERSION": {"kind": "npm", "pkg": "@playwright/mcp"},
    "PYYAML_VERSION": {"kind": "pypi", "pkg": "PyYAML"},
    "AIOHTTP_VERSION": {"kind": "pypi", "pkg": "aiohttp"},
    "DOCKER_VERSION": {
        "kind": "manual",
        "url": "https://download.docker.com/linux/static/stable/x86_64/",
    },
    "DATA_WRANGLER_VERSION": {
        "kind": "manual",
        "url": "https://marketplace.visualstudio.com/items"
               "?itemName=ms-toolsai.datawrangler",
    },
}

# Per-supervisor service registry. KNOWN_SERVICES lists every kind the webui
# might render; --enable / --disable on `project create|update` flips
# `research.service.<id>` labels and `RS_SERVICE_<ID>` env vars in lockstep.
# ALWAYS_ON_SERVICES can't be disabled — `supervisor` (the SSH + byobu
# substrate, formerly `xterm`) is what `research project ssh` rides on;
# disabling it would brick the project. New service kinds extend both
# lists in the same commit that ships the entrypoint conditional and the
# registry entry.
KNOWN_SERVICES: list[str] = ["supervisor", "code-server"]
ALWAYS_ON_SERVICES: set[str] = {"supervisor"}
SERVICE_LABEL_PREFIX = "research.service."

# In-supervisor ports for code-server's lazy-start stub. The stub listens on
# CODE_SERVER_STUB_PORT (the port the webui reverse-proxy hits via container
# DNS) and spawns code-server on CODE_SERVER_UPSTREAM_PORT, which never
# leaves 127.0.0.1. Ports are constants — supervisors are single-tenant
# inside their own network namespace, no contention possible.
CODE_SERVER_STUB_PORT = 8443
CODE_SERVER_UPSTREAM_PORT = 8444

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, **kw)


def run_check(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    r = run(cmd, capture_output=True, **kw)
    if r.returncode != 0:
        die(f"command failed: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r


def run_quiet(cmd: list[str]) -> bool:
    return run(cmd, capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# .env state
# ---------------------------------------------------------------------------


class Config:
    def __init__(self) -> None:
        self.projects_dir: str = (
            os.environ.get("PROJECTS_DIR") or str(SCRIPT_DIR / "container_volumes")
        )
        self.sandbox_dns: list[str] = [
            s.strip()
            for s in os.environ.get("SANDBOX_DNS", "9.9.9.9,149.112.112.112").split(",")
            if s.strip()
        ]
        self.default_profile: str = os.environ.get("DEFAULT_PROFILE", "python")
        self.default_memory: str = os.environ.get("DEFAULT_MEMORY", "")
        # Per-role-MCP container memory cap. Blast-radius backstop: if a
        # runaway claude -p / Chromium / DB-MCP child triggers OOM, the
        # killer takes the role container, not the supervisor. Default 2g:
        # at 1g, a single browser-bearing call (Chromium ~400MB resident +
        # daemon ~500MB + renderer/GPU subprocesses) leaves no headroom
        # and risks OOM on real loads. At 4g, value is wasteful for non-
        # browser roles (wrangler peaks <500MB) but harmless. Pair with
        # default_role_mcp_max_concurrent_calls — bumping memory should
        # bump concurrency proportionally. Override per .env or per-role
        # at enable: `--memory 4g`.
        self.default_role_mcp_memory: str = os.environ.get(
            "DEFAULT_ROLE_MCP_MEMORY", "2g")
        # Per-role-MCP daemon-side concurrency cap. send_job calls beyond
        # this return an MCP tool error with structured payload
        # {reason: "concurrency_limit", ...} immediately — no spawn, no
        # Chromium / DB connection wasted on a refused call. Default 3:
        # each browser-bearing concurrent call is ~400MB resident; 3 fits
        # the 2g default_role_mcp_memory comfortably with daemon overhead.
        # Non-browser roles (wrangler, echo-mcp) effectively uncapped in
        # practice — their per-call footprint is tiny. Set to 0 to disable
        # the cap entirely. Override per .env or per-role at enable:
        # `--max-concurrent-calls 6`.
        self.default_role_mcp_max_concurrent_calls: int = int(
            os.environ.get("DEFAULT_ROLE_MCP_MAX_CONCURRENT_CALLS", "3"))
        self.default_dind: str = os.environ.get("DEFAULT_DIND", "auto")
        self.default_egress: str = os.environ.get("DEFAULT_EGRESS", "open")


def load_config() -> Config:
    env = SCRIPT_DIR / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            if k and k not in os.environ:
                os.environ[k] = v
    return Config()


def read_env_value(key: str) -> str:
    """Read a single key from .env (commented lines ignored)."""
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if "=" in stripped:
            k, _, v = stripped.partition("=")
            if k.strip() == key:
                return v.strip()
    return ""


def update_env_key(key: str, value: str) -> None:
    """Set or append KEY=VALUE in .env. Replaces a commented `# KEY=` line
    in place if present, so .env stays diffable across edits."""
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        env_file.write_text(f"{key}={value}\n")
        return
    lines = env_file.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_file.write_text("\n".join(lines) + "\n")


def docker_compose(*compose_args: str) -> None:
    run_check([
        "docker", "compose",
        "-f", str(SCRIPT_DIR / "docker-compose.yml"),
        *compose_args,
    ])


# ---------------------------------------------------------------------------
# Port / password generation
# ---------------------------------------------------------------------------


def gen_password() -> str:
    return secrets.token_urlsafe(16)


def find_free_port(base: int = 2240) -> int:
    # 2240–3239 avoids ADS's 2222–3221 range so the two can coexist on one host.
    for port in range(base, base + 1000):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", port))
                return port
        except OSError:
            continue
    die(f"could not find a free port starting at {base}")


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def container_running(name: str) -> bool:
    r = run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True)
    return r.stdout.strip() == "true"


def container_exists(name: str) -> bool:
    return run_quiet(["docker", "inspect", name])


def volume_exists(name: str) -> bool:
    return run_quiet(["docker", "volume", "inspect", name])


def network_exists(name: str) -> bool:
    return run_quiet(["docker", "network", "inspect", name])


def sysbox_available() -> bool:
    r = run(["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True)
    return '"sysbox-runc"' in r.stdout


def select_dind_mode(mode: str) -> str:
    if mode == "auto":
        if sysbox_available():
            return "sysbox"
        print(
            "note: sysbox-runc not found; falling back to --privileged DIND "
            "(weaker isolation; see README).",
            file=sys.stderr,
        )
        return "privileged"
    if mode not in ("sysbox", "privileged"):
        die(f"invalid --dind value: {mode!r} (expected auto|sysbox|privileged)")
    if mode == "sysbox" and not sysbox_available():
        die(
            "sysbox-runc is not available on this host. "
            "Install it or rerun with --dind privileged."
        )
    return mode


def get_supervisor_containers() -> list[dict]:
    fmt = "{{.Names}}\t{{.State}}\t{{.Label \"" + PROJECT_LABEL + "\"}}"
    r = run_check([
        "docker", "ps", "-a",
        "--filter", f"label={PROJECT_LABEL}",
        "--format", fmt,
    ])
    containers = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2]:
            containers.append({"name": parts[0], "state": parts[1], "project": parts[2]})
    return containers


def container_name_for(project: str) -> str:
    return f"{CONTAINER_PREFIX}{project}"


def docker_volume_name_for(project: str) -> str:
    return f"{DOCKER_VOLUME_PREFIX}{project}"


def project_root_for(project: str, cfg: "Config") -> Path:
    return Path(cfg.projects_dir).expanduser().resolve() / project


def workspace_path_for(project: str, cfg: "Config") -> Path:
    return project_root_for(project, cfg) / "workspace"


def project_network_for(project: str) -> str:
    return f"{PROJECT_NETWORK_PREFIX}{project}"


def wait_for_inner_dockerd(container: str, timeout: int = 60) -> None:
    import time

    deadline = time.time() + timeout
    print("waiting for inner dockerd...")
    while time.time() < deadline:
        r = run(["docker", "exec", container, "docker", "info"], capture_output=True)
        if r.returncode == 0:
            return
        time.sleep(1)
    die(f"inner dockerd did not become ready within {timeout}s "
        f"(check `docker logs {container}` and `docker exec {container} sudo cat /tmp/dockerd.log`)")


def stage_worker_image(container: str, image: str, force: bool = False) -> None:
    """Push the host-built image into the supervisor's inner Docker daemon.

    With ``force=True`` the inner daemon's existing copy of the tag (if any)
    is removed first, so the load brings in the rebuilt content rather than
    being a no-op when the tag points at a stale image (the case when
    `research project update --rebuild` re-stages after a host rebuild)."""
    if not run_quiet(["docker", "image", "inspect", image]):
        die(f"host image {image} not found; run `research setup`.")
    # Skip if already present inside, unless --force.
    present = run(["docker", "exec", container, "docker", "image", "inspect", image],
                  capture_output=True).returncode == 0
    if present and not force:
        return
    if present:
        run(["docker", "exec", container, "docker", "image", "rm", "-f", image],
            capture_output=True)
    print(f"staging {image} into the supervisor (this can take a minute)...")
    save = subprocess.Popen(
        ["docker", "save", image],
        stdout=subprocess.PIPE,
    )
    assert save.stdout is not None
    load = subprocess.Popen(
        ["docker", "exec", "-i", container, "docker", "load"],
        stdin=save.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    save.stdout.close()
    save.wait()
    load_out, _ = load.communicate()
    if load.returncode != 0:
        die(f"failed to stage {image}:\n{load_out.decode(errors='replace')}")


def get_ssh_port(container: str) -> str | None:
    r = run(
        ["docker", "inspect", "-f",
         "{{range $k, $v := .NetworkSettings.Ports}}{{if eq $k \"22/tcp\"}}"
         "{{(index $v 0).HostPort}}{{end}}{{end}}",
         container],
        capture_output=True,
    )
    return r.stdout.strip() or None


# ---------------------------------------------------------------------------
# Networking (router + per-project networks)
# ---------------------------------------------------------------------------


def get_router_ip(network: str) -> str:
    r = run_check([
        "docker", "inspect", ROUTER_CONTAINER,
        "-f", '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}',
    ])
    ip = r.stdout.strip()
    if not ip:
        die(f"{ROUTER_CONTAINER} is not attached to {network}")
    return ip


def get_network_subnet(network: str) -> str:
    r = run_check([
        "docker", "network", "inspect", network,
        "-f", "{{(index .IPAM.Config 0).Subnet}}",
    ])
    return r.stdout.strip()


def inject_route(container: str, router_ip: str) -> None:
    # `replace` (vs `add`) handles both first-boot (no default route) and
    # non-internal networks (Docker has already set a default route to the
    # bridge gateway; we overwrite it to point at the router for egress
    # enforcement).
    run_check([
        "docker", "run", "--rm", "--privileged",
        "--network", f"container:{container}",
        "alpine:3.20", "ip", "route", "replace", "default", "via", router_ip,
    ])


def apply_firewall_rules(network: str, mode: str) -> None:
    subnet = get_network_subnet(network)
    run_check([
        "docker", "exec", ROUTER_CONTAINER,
        "/scripts/apply-rules.sh", subnet, mode,
    ])


def remove_firewall_rules(network: str) -> None:
    if not network_exists(network):
        return
    subnet = get_network_subnet(network)
    run(["docker", "exec", ROUTER_CONTAINER, "/scripts/remove-rules.sh", subnet],
        capture_output=True)


def ensure_project_network(project: str, mode: str) -> tuple[str, str]:
    """Create per-project bridge network; connect router; apply firewall rules.

    Note: NOT ``--internal``. Docker 29 silently drops ``-p`` port publishing on
    internal networks, which breaks SSH. Egress enforcement is provided by the
    router's iptables FORWARD rules (keyed on source subnet); we inject a
    default route via the router after container start so all egress traverses
    those rules.
    """
    network = project_network_for(project)
    if not network_exists(network):
        run_check(["docker", "network", "create", network])
    run(["docker", "network", "connect", network, ROUTER_CONTAINER],
        capture_output=True)
    router_ip = get_router_ip(network)
    apply_firewall_rules(network, mode)
    return network, router_ip


def remove_project_network(project: str) -> None:
    network = project_network_for(project)
    remove_firewall_rules(network)
    # Disconnect every container research.py knows might be attached. The
    # webui (if running) was wired in by `wire_webui_to_projects()` at
    # create time; without an explicit disconnect, `network rm` fails with
    # "endpoints remain". Both calls are idempotent — they exit non-zero
    # silently when the container isn't on this network.
    for svc in (ROUTER_CONTAINER, WEBUI_CONTAINER):
        run(["docker", "network", "disconnect", network, svc],
            capture_output=True)
    run(["docker", "network", "rm", network], capture_output=True)


# ---------------------------------------------------------------------------
# docker run argv builder
# ---------------------------------------------------------------------------


def build_supervisor_docker_args(
    *,
    container_name: str,
    project: str,
    network: str,
    workspace_path: Path,
    ssh_port: int,
    ssh_pass: str,
    dns_servers: list[str],
    memory: str,
    cpus: str,
    image: str,
    dind_mode: str,
    inner_firewall: bool = False,
    project_type: str = PROJECT_TYPE_RESEARCH,
    service_flags: dict[str, bool] | None = None,
) -> list[str]:
    args = [
        "run", "-d",
        "--name", container_name,
        "--hostname", project,
        "--network", network,
        "--add-host", "host.docker.internal:host-gateway",
        "-v", f"{workspace_path}:/workspace",
        "-p", f"{ssh_port}:22",
        "-e", f"PROJECT={project}",
        "-e", f"SSH_PASSWORD={ssh_pass}",
        "-e", f"HOST_GID={os.getgid()}",
        "-e", "DOCKER_DIND=true",
        "--label", f"{PROJECT_LABEL}={project}",
        "--label", f"{DIND_MODE_LABEL}={dind_mode}",
        # Flavor marker for the host (_container_project_type / recreate
        # metadata) and the webui. The per-flavor *image* (selected by the
        # caller's `image=`) is what actually differs at runtime; the entrypoints
        # no longer branch on a flavor env.
        "--label", f"{PROJECT_TYPE_LABEL}={project_type}",
    ]
    if inner_firewall:
        args += ["-e", "RS_INNER_FIREWALL=1"]
    # Per-service flags: webui reads the labels (outside-the-container truth);
    # entrypoint reads the env vars (inside-the-container truth). Both must
    # land on the same container in lockstep.
    flags = service_flags if service_flags is not None else {sid: True for sid in KNOWN_SERVICES}
    for sid in sorted(flags):
        ena = "enabled" if flags[sid] else "disabled"
        args += ["--label", f"{SERVICE_LABEL_PREFIX}{sid}={ena}"]
        args += ["-e", f"RS_SERVICE_{sid.upper()}={ena}"]
    # code-server lazy-reap idle window. Optional — entrypoint defaults to
    # 1800s (30 min) when unset; .env can override per-host. Survives
    # _recreate_supervisor by being re-passed from the host's env on every
    # create, which is what we want (a host-side tweak should propagate to
    # the next project lifecycle, not require per-project state).
    idle = os.environ.get("CODE_SERVER_IDLE_SECONDS")
    if idle:
        args += ["-e", f"CODE_SERVER_IDLE_SECONDS={idle}"]
    for s in dns_servers:
        args += ["--dns", s]

    if dind_mode == "sysbox":
        args += ["--runtime=sysbox-runc", "--pids-limit=4096"]
    elif dind_mode == "privileged":
        args += ["--privileged", "--pids-limit=4096",
                 "-v", f"{docker_volume_name_for(project)}:/var/lib/docker"]

    if memory:
        args += [f"--memory={memory}"]
    if cpus:
        args += [f"--cpus={cpus}"]
    args.append(image)
    return args


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _preflight() -> None:
    """Sanity checks + idempotent one-time bootstrap (.env, images)."""
    if not shutil.which("docker"):
        die("docker not found on PATH. Install Docker Engine first.")
    r = run(["docker", "info"], capture_output=True)
    if r.returncode != 0:
        die("docker daemon is not reachable. Start it and try again.")

    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        example = SCRIPT_DIR / ".env.example"
        if example.exists():
            env_path.write_text(example.read_text())
            print(f"created {env_path.name} (copied from .env.example)")
        else:
            env_path.write_text("")
            print(f"created empty {env_path.name}")


def load_versions() -> dict[str, str]:
    """Parse the root-level versions.env (KEY=VALUE, `#`-comment lines) into a
    dict of image-version pins. Mirrors load_config()'s .env parsing — stdlib
    only, no quote/inline-comment handling (pins are bare tokens). Missing file
    yields {} so every Dockerfile ARG default still applies."""
    pins: dict[str, str] = {}
    if not VERSIONS_FILE.exists():
        return pins
    for line in VERSIONS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k:
            pins[k] = v
    return pins


def _build_images(force: bool) -> None:
    """Build supervisor + worker + mcp-proxy + role-mcp images. Skip
    existing ones unless --rebuild. Build order matters: rs-role-mcp-base
    FROMs rs-analysis-base, per-role images (rs-echo-mcp etc.) FROM
    rs-role-mcp-base — keep the list bottom-up so each FROM resolves to
    the just-built layer rather than a stale cached copy."""
    specs = [
        # Shared substrate base MUST build before the two leaf images that
        # FROM it (rs-supervisor, rs-management) — keep it first.
        (SUBSTRATE_BASE_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.substrate-base"),
        (SUPERVISOR_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.supervisor"),
        (MANAGEMENT_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.management"),
        (ANALYSIS_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.analysis-base"),
        (MCP_PROXY_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.mcp-proxy"),
        (ROLE_MCP_BASE_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.role-mcp-base"),
        (PI_BASE_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.pi-base"),
        # Generic PI-isolated image — FROM rs-pi-base, adds git + the
        # clone/setup entrypoint. One image for every isolated type; no
        # per-type Dockerfile (type behavior comes from the cloned repo).
        (PI_ISOLATED_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.pi-isolated"),
        # Disposable sandbox-project box image — FROM rs-analysis-base, clean
        # of the PI artifact-contract (STAGE_SANDBOX_PROJECT.md). The browser
        # variant FROMs it, so it must build first (bottom-up).
        (SANDBOX_BOX_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.sandbox-box"),
        (SANDBOX_BOX_BROWSER_IMAGE,
         SCRIPT_DIR / "agent" / "Dockerfile.sandbox-box-browser"),
    ]
    for role, image in sorted(role_mcp.ROLE_IMAGES.items()):
        dockerfile = SCRIPT_DIR / "agent" / f"Dockerfile.{role}"
        if not dockerfile.is_file():
            print(f"warning: role-mcp image {image} has no Dockerfile at "
                  f"{dockerfile.name}; skipping (add it in the per-role stage)",
                  file=sys.stderr)
            continue
        specs.append((image, dockerfile))
    # PI per-role images (rs-pi-echo, rs-pi-wrangler, …) FROM rs-pi-base.
    # Build order is bottom-up so each FROM resolves to the freshly-built
    # layer, same discipline as role-mcp-base.
    for role, image in sorted(sandbox.BAKED_IMAGES.items()):
        dockerfile = SCRIPT_DIR / "agent" / f"Dockerfile.pi-{role}"
        if not dockerfile.is_file():
            print(f"warning: sandbox image {image} has no Dockerfile at "
                  f"{dockerfile.name}; skipping (add it in the per-role stage)",
                  file=sys.stderr)
            continue
        specs.append((image, dockerfile))
    pins = load_versions()
    for tag, dockerfile in specs:
        if not force and run_quiet(["docker", "image", "inspect", tag]):
            print(f"image {tag} already present (use --rebuild to force)")
            continue
        # Pass only the pins this Dockerfile declares an `ARG` for, so docker
        # doesn't warn about unconsumed build-args. The ARG default in the
        # Dockerfile stays the fallback for standalone `docker build` outside
        # this CLI; here the manifest value wins.
        text = dockerfile.read_text()
        build_args: list[str] = []
        for key, value in pins.items():
            if f"ARG {key}" in text:
                build_args += ["--build-arg", f"{key}={value}"]
        print(f"building {tag}...")
        run_check([
            "docker", "build",
            "-f", str(dockerfile),
            "-t", tag,
            *build_args,
            str(SCRIPT_DIR),
        ])


def _http_json(url: str, timeout: float = 10.0) -> dict:
    """GET a JSON document with the stdlib. A User-Agent is required by the
    GitHub API (it 403s anonymous requests without one) and harmless elsewhere."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "research-sandbox/images-outdated"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _latest_version(source: dict[str, str]) -> str:
    """Resolve the latest published version for a non-manual datasource. Raises
    on network/parse failure; the caller renders that as 'unreachable'."""
    kind = source["kind"]
    if kind == "github-releases":
        data = _http_json(
            f"https://api.github.com/repos/{source['repo']}/releases/latest")
        return str(data["tag_name"]).lstrip("v")
    if kind == "npm":
        # %2F-encode the scope so the scoped-package GET resolves cleanly.
        pkg = source["pkg"].replace("/", "%2F")
        data = _http_json(f"https://registry.npmjs.org/{pkg}")
        return str(data["dist-tags"]["latest"])
    if kind == "pypi":
        data = _http_json(f"https://pypi.org/pypi/{source['pkg']}/json")
        return str(data["info"]["version"])
    raise ValueError(f"unknown datasource kind: {kind}")


def _start_enabled_mcps() -> None:
    targets = _shared_mcps(only_enabled=True)
    for name, entry in targets:
        try:
            _spawn_shared_mcp(name, entry)
        except SystemExit:
            print(f"warning: failed to start MCP {name!r}; continuing",
                  file=sys.stderr)


def _resolve_create_mcp_arg(value: str | None) -> list[str]:
    """Resolve ``project create --mcp`` into a list of registry names. The
    helper validates membership only — enabled-state and reachability are
    checked per-MCP by ``_allow_mcp_for_project`` so a single misbehaving
    entry can be skipped without aborting the create."""
    v = (value or "all-enabled").strip()
    try:
        data = mcp_registry.load(expand=False)
    except mcp_registry.RegistryError as e:
        die(str(e))
    if v == "all-enabled":
        return sorted(n for n, e in data["mcps"].items()
                      if e.get("enabled", False))
    if v == "none":
        return []
    names = [n.strip() for n in v.split(",") if n.strip()]
    unknown = [n for n in names if n not in data["mcps"]]
    if unknown:
        die(f"--mcp: unknown MCP name(s): {', '.join(unknown)}")
    return names


def _for_containers(op: str, target: str | None) -> None:
    if target == "__ALL__":
        containers = [c["name"] for c in get_supervisor_containers()]
    elif target:
        containers = [container_name_for(target)]
    else:
        die("must specify a project name or --all")
    if not containers:
        print("no projects to act on")
        return
    for name in containers:
        if not container_exists(name):
            print(f"skip: {name} does not exist")
            continue
        run_check(["docker", op, name])
        print(f"{op}: {name}")


# ---------------------------------------------------------------------------
# project update — push edited code into a running project
# ---------------------------------------------------------------------------


# Files baked into the supervisor image at build time. `update` (no --rebuild)
# `docker cp`s each into the stopped supervisor; the next start runs the new
# code. Pairs are (host-relative source, container target, executable bit).
_SUPERVISOR_FILE_MAP: list[tuple[str, str, bool]] = [
    ("cli/rs_worker.py",                                "/usr/local/bin/rs-worker",                          True),
    ("cli/rs_audit_stop.py",                            "/usr/local/bin/rs-audit-stop",                      True),
    ("container/supervisor/mcp_render_config.py",       "/opt/mcp-proxy-tools/mcp_render_config.py",         False),
    ("container/supervisor/mcp-reload.sh",              "/usr/local/bin/mcp-reload",                         True),
    ("container/supervisor/inner-firewall.sh",          "/usr/local/bin/rs-inner-firewall",                  True),
    ("container/supervisor/CLAUDE.md",                  "/opt/claude-templates/CLAUDE.md",                   False),
    ("container/supervisor/setup.sh",                   "/opt/claude-templates/setup.sh",                    True),
    ("container/supervisor/logbook_supervisor_template.md", "/opt/claude-templates/logbook_supervisor_template.md", False),
    ("container/supervisor/logbook_pi_template.md",     "/opt/claude-templates/logbook_pi_template.md",      False),
    ("container/supervisor/code-server-stub.py",        "/opt/code-server-tools/code-server-stub.py",        True),
    ("container/supervisor/code-server-settings.json",  "/opt/code-server-templates/User/settings.json",     False),
    ("container/analysis/CLAUDE.md.template",           "/opt/claude-templates/worker.CLAUDE.md.template",   False),
    ("agent/entrypoint.supervisor.sh",                  "/entrypoint.sh",                                    True),
]

_SUPERVISOR_DIR_MAP: list[tuple[str, str]] = [
    ("container/supervisor/commands", "/opt/claude-templates/commands"),
]

# The rs-management leaf has none of the supervisor's agent files — its
# file-only-update surface is just rs-sandbox + its own entrypoint. (code-server
# / byobu live in the shared base, so changes there are a base rebuild, not a
# file-copy.)
_MANAGEMENT_FILE_MAP: list[tuple[str, str, bool]] = [
    ("cli/rs_sandbox.py",              "/usr/local/bin/rs-sandbox", True),
    ("agent/entrypoint.management.sh", "/entrypoint.sh",            True),
]


def _docker_cp_with_mode(src: Path, container: str, dst: str, mode: int) -> None:
    """`docker cp` a file with an explicit mode bit, via a tempdir staging
    step (the source files in the working tree may be 0664; baked scripts
    need 0755). Tempdir is cleaned up on return; never persists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        staged = Path(tmpdir) / src.name
        shutil.copy2(src, staged)
        os.chmod(staged, mode)
        run_check(["docker", "cp", str(staged), f"{container}:{dst}"])


def _docker_cp_supervisor_files(container: str) -> list[str]:
    """Copy the edited per-project-substrate files into the container (file-only
    `project update`). Flavor-aware: a sandbox project's rs-management container
    has none of the supervisor's agent paths, so it gets the management map.
    Returns the list of host-relative paths that were actually copied."""
    is_sandbox = _container_project_type(container) == PROJECT_TYPE_SANDBOX
    file_map = _MANAGEMENT_FILE_MAP if is_sandbox else _SUPERVISOR_FILE_MAP
    dir_map: list[tuple[str, str]] = [] if is_sandbox else _SUPERVISOR_DIR_MAP
    copied: list[str] = []
    for rel, dst, exe in file_map:
        src = SCRIPT_DIR / rel
        if not src.is_file():
            continue
        if exe:
            _docker_cp_with_mode(src, container, dst, 0o755)
        else:
            run_check(["docker", "cp", str(src), f"{container}:{dst}"])
        copied.append(rel)
    for rel, dst in dir_map:
        src = SCRIPT_DIR / rel
        if not src.is_dir():
            continue
        # Trailing /. on src copies the contents into the existing target dir
        # (instead of nesting). Doesn't delete files removed from the source.
        run_check(["docker", "cp", f"{src}/.", f"{container}:{dst}/"])
        copied.append(rel + "/")
    return copied


def _read_supervisor_metadata(container: str) -> dict:
    """Inspect the existing supervisor; return the params needed to recreate
    it with `build_supervisor_docker_args` (used by `update --rebuild`)."""
    r = run_check(["docker", "inspect", container])
    data = json.loads(r.stdout)[0]

    # Published SSH host port.
    port_bindings = (data.get("HostConfig") or {}).get("PortBindings") or {}
    ssh_b = port_bindings.get("22/tcp") or []
    ssh_port = int(ssh_b[0]["HostPort"]) if ssh_b else 0

    # Env: SSH_PASSWORD, RS_INNER_FIREWALL.
    env: dict[str, str] = {}
    for entry in (data.get("Config") or {}).get("Env") or []:
        if "=" in entry:
            k, v = entry.split("=", 1)
            env[k] = v

    labels = (data.get("Config") or {}).get("Labels") or {}

    # Bind-mounts other than /workspace and the privileged-DIND volume —
    # i.e. user-supplied --data paths under /workspace/shared/data/<basename>/.
    # /external/* (PI-isolated external folders) are deliberately EXCLUDED:
    # they're recomputed fresh from the host registry on every recreate
    # (_sandbox_external_mounts), so recovering them here would both
    # double-add and pin stale roots after a registry edit.
    extra_mounts: list[str] = []
    for m in data.get("Mounts") or []:
        dst_in = m.get("Destination", "")
        if dst_in in ("/workspace", "/var/lib/docker"):
            continue
        if dst_in.startswith("/external/"):
            continue
        if m.get("Type") != "bind":
            continue
        ro = ":ro" if m.get("RW") is False else ""
        extra_mounts += ["-v", f"{m['Source']}:{dst_in}{ro}"]

    hc = data.get("HostConfig") or {}
    mem_bytes = hc.get("Memory") or 0
    if mem_bytes and mem_bytes % (1024 ** 3) == 0:
        memory = f"{mem_bytes // (1024 ** 3)}g"
    elif mem_bytes:
        memory = str(mem_bytes)
    else:
        memory = ""
    nano_cpus = hc.get("NanoCpus") or 0
    cpus = f"{nano_cpus / 1e9:g}" if nano_cpus else ""

    service_flags: dict[str, bool] = {}
    for sid in KNOWN_SERVICES:
        v = labels.get(f"{SERVICE_LABEL_PREFIX}{sid}")
        # Missing label (legacy projects) defaults to enabled, which matches
        # the on-create default. ALWAYS_ON_SERVICES are forced True regardless.
        service_flags[sid] = (v != "disabled")
    for sid in ALWAYS_ON_SERVICES:
        service_flags[sid] = True

    return {
        "ssh_port": ssh_port,
        "ssh_pass": env.get("SSH_PASSWORD", ""),
        "dind_mode": labels.get(DIND_MODE_LABEL, "privileged"),
        "inner_firewall": env.get("RS_INNER_FIREWALL") == "1",
        "project_type": labels.get(PROJECT_TYPE_LABEL, PROJECT_TYPE_RESEARCH),
        "memory": memory,
        "cpus": cpus,
        "extra_mounts": extra_mounts,
        "service_flags": service_flags,
    }


def _stash_creds_for_rebuild(
    container: str, was_running: bool, workspace_path: Path
) -> None:
    """Move Claude auth state into the workspace bind-mount so it survives
    container destruction. Two pieces:
      - ~research/.claude/        → /workspace/.creds-stash/
      - ~research/.claude.json    → /workspace/.creds-stash-home.json

    The second piece is what makes interactive `claude` skip the /login
    prompt after the recreate (it carries `oauthAccount`); without it,
    the operator re-OAuths every `project update --rebuild`.

    Two restore paths inside this function depending on container state:
    - Running supervisor: `docker exec mv` — atomic, never leaves the
      container's filesystem until the bind-mount writeback hits the
      host workspace dir.
    - Stopped supervisor: `docker cp` — the container is about to be
      destroyed so this is functionally a move; creds never touch /tmp.

    Idempotent: skips any stash points that already exist from a prior
    failed update. The entrypoint will move them back at next start."""
    host_stash = workspace_path / ".creds-stash"
    host_home_stash = workspace_path / ".creds-stash-home.json"
    if was_running:
        if not host_stash.exists():
            run(["docker", "exec", container, "sh", "-c",
                 "if [ -d /home/research/.claude ] && "
                 "[ ! -d /workspace/.creds-stash ]; then "
                 "mv /home/research/.claude /workspace/.creds-stash; "
                 "fi"],
                capture_output=True)
        if not host_home_stash.exists():
            run(["docker", "exec", container, "sh", "-c",
                 "if [ -f /home/research/.claude.json ] && "
                 "[ ! -f /workspace/.creds-stash-home.json ]; then "
                 "mv /home/research/.claude.json "
                 "/workspace/.creds-stash-home.json; "
                 "fi"],
                capture_output=True)
    else:
        # docker cp on a stopped container works for files in its filesystem.
        if not host_stash.exists():
            run(["docker", "cp",
                 f"{container}:/home/research/.claude",
                 str(host_stash)],
                capture_output=True)
        if not host_home_stash.exists():
            run(["docker", "cp",
                 f"{container}:/home/research/.claude.json",
                 str(host_home_stash)],
                capture_output=True)


def _recreate_supervisor(
    project: str,
    cfg: "Config",
    *,
    force_restage: bool = False,
    post_create_hook=None,
    service_flags: dict[str, bool] | None = None,
) -> None:
    """Stash creds → stop+rm → create new container from SUPERVISOR_IMAGE
    → optional post-create hook → start → re-inject route → re-stage inner
    images → respawn mcp-proxy. The only safe shape on sysbox: stop+start
    of the same container ID hits sysbox-mgr's `volume dir for container
    <id> already exists` bug. A fresh container ID gets fresh bindings.

    Workspace, network, SSH port, env, mounts, memory/CPU limits all
    survive because they're recovered from the existing container's
    metadata before rm. Creds move through /workspace/.creds-stash, never
    via /tmp.

    The post-create hook runs against the not-yet-started container —
    used by `cmd_project_update` to docker-cp edited files in before the
    entrypoint reads them."""
    container = container_name_for(project)
    was_running = container_running(container)
    workspace_path = workspace_path_for(project, cfg)

    _stash_creds_for_rebuild(container, was_running, workspace_path)
    md = _read_supervisor_metadata(container)

    if was_running:
        print(f"stopping {container}...")
        run_check(["docker", "stop", container])
    print(f"removing old container {container}...")
    run_check(["docker", "rm", container])

    network = project_network_for(project)
    flags = service_flags if service_flags is not None else md["service_flags"]
    md_ptype = md.get("project_type", PROJECT_TYPE_RESEARCH)
    substrate_image = (MANAGEMENT_IMAGE if md_ptype == PROJECT_TYPE_SANDBOX
                       else SUPERVISOR_IMAGE)
    docker_args = build_supervisor_docker_args(
        container_name=container,
        project=project,
        network=network,
        workspace_path=workspace_path,
        ssh_port=md["ssh_port"],
        ssh_pass=md["ssh_pass"],
        dns_servers=cfg.sandbox_dns,
        memory=md["memory"],
        cpus=md["cpus"],
        image=substrate_image,
        dind_mode=md["dind_mode"],
        inner_firewall=md["inner_firewall"],
        project_type=md_ptype,
        service_flags=flags,
    )
    if md["extra_mounts"]:
        docker_args = docker_args[:-1] + md["extra_mounts"] + [docker_args[-1]]
    # PI-isolated external folders are recomputed fresh from the host
    # registry (excluded from md["extra_mounts"]) so the recreate tracks
    # the current registry — newly-added types appear, removed ones drop.
    ext_mounts = _sandbox_external_mounts(project)
    if ext_mounts:
        docker_args = docker_args[:-1] + ext_mounts + [docker_args[-1]]
    # build_supervisor_docker_args emits ["run", "-d", ...]; convert to create.
    assert docker_args[0] == "run" and docker_args[1] == "-d"
    create_args = ["create"] + docker_args[2:]

    print(f"creating new container from {substrate_image}...")
    run_check(["docker", *create_args])

    if post_create_hook is not None:
        post_create_hook(container)

    print(f"starting {container}...")
    run_check(["docker", "start", container])

    router_ip = get_router_ip(network)
    inject_route(container, router_ip)

    # Inner-dockerd state. In sysbox mode /var/lib/docker is fresh, so
    # worker + proxy + role-mcp images need staging. Privileged DIND has a
    # named volume that survives rm; stage_worker_image is a no-op there
    # unless `force_restage` (i.e. images were rebuilt on the host).
    wait_for_inner_dockerd(container)
    run(["docker", "exec", container, "docker", "rm", "-f", "mcp-proxy"],
        capture_output=True)
    # Sandbox flavor stages the blank box image (no analysis workers); see
    # cmd_project_create's matching branch.
    if md.get("project_type") == PROJECT_TYPE_SANDBOX:
        stage_worker_image(container, SANDBOX_BOX_IMAGE, force=force_restage)
        stage_worker_image(container, SANDBOX_BOX_BROWSER_IMAGE, force=force_restage)
    else:
        stage_worker_image(container, ANALYSIS_IMAGE, force=force_restage)
    stage_worker_image(container, MCP_PROXY_IMAGE, force=force_restage)

    run(["docker", "exec", container, "/usr/local/bin/mcp-reload"],
        capture_output=True)

    # Bring previously-enabled role-MCPs back up. The inner dockerd is
    # fresh under sysbox; each role-MCP container is gone and must be
    # re-created from the role-mcps.json snapshot. _role_mcp_start lazy-
    # stages each per-role image into the inner dockerd before running
    # it, with force_restage threaded through so a host-side rebuild
    # propagates inward.
    workspace_path = workspace_path_for(project, cfg)
    role_entries = role_mcp.load_role_mcps(workspace_path)
    for role in sorted(role_entries):
        if role_entries[role].get("stopped"):
            # Deliberately parked via `project worker stop` — do NOT
            # auto-restart on recreate. Park survives the supervisor swap
            # (mirrors the worker `down` model); explicit `worker start`
            # brings it back.
            continue
        try:
            _role_mcp_start(container, project, cfg, role,
                            force_restage=force_restage)
        except SystemExit:
            print(f"warning: failed to restart role-mcp {role!r}; "
                  f"the entry in role-mcps.json is intact, retry with "
                  f"`research project role-mcp enable {project} {role}`",
                  file=sys.stderr)

    # Same idea for sandbox containers (baked PI roles + BYO isolated agents,
    # STAGE_CLI_TAXONOMY). The sandbox.json snapshot is the source of truth;
    # _sandbox_start re-stages the image into the (potentially fresh) inner
    # dockerd and restarts the container. For BYO entries the supervisor's
    # /external/<type> mounts were just recomputed from the registry above, so
    # every agent's external folder is wired before its container restarts.
    # Workspace state survives because it's on the project volume.
    sandbox_entries = sandbox.load(workspace_path)
    for name in sorted(sandbox_entries):
        # kind="sandbox" boxes (the agent-less flavor) are owned by the
        # in-supervisor rs-sandbox CLI, not the host baked/byo start path —
        # _sandbox_start only knows baked/byo and would wrongly treat a box as
        # byo (external mount + repo env). Delegate the restart to rs-sandbox
        # so the docker-run logic lives in one place.
        if sandbox_entries[name].get("kind") == sandbox.SANDBOX_KIND:
            r = run(["docker", "exec", container, "rs-sandbox", "restart", name],
                    capture_output=True)
            if r.returncode != 0:
                print(f"warning: failed to restart sandbox box {name!r}: "
                      f"{(r.stderr or r.stdout).strip()}", file=sys.stderr)
            continue
        try:
            _sandbox_start(container, project, cfg, name,
                           force_restage=force_restage)
        except SystemExit:
            print(f"warning: failed to restart sandbox {name!r}; "
                  f"the entry in sandbox.json is intact, retry with "
                  f"`research project sandbox enable {project} {name}`",
                  file=sys.stderr)


def _container_project_type(container: str) -> str:
    """Project flavor from the supervisor's research.project_type label
    (defaults to "research" for legacy containers without it)."""
    r = run(["docker", "inspect", "-f",
             f'{{{{index .Config.Labels "{PROJECT_TYPE_LABEL}"}}}}', container],
            capture_output=True)
    t = r.stdout.strip() if r.returncode == 0 else ""
    return t or PROJECT_TYPE_RESEARCH


def _refresh_workspace_claude_templates(container: str) -> None:
    """Overwrite /workspace/.claude/{CLAUDE.md, logbook_*_template.md,
    commands/} from /opt/claude-templates/. The entrypoint's first-boot
    `if-not-present` guard means existing projects never see template
    edits otherwise — this closes that loop. Slash-commands dir is
    rebuilt from scratch (so removed slash commands actually disappear),
    not merged."""
    run_check(["docker", "exec", container, "sh", "-eu", "-c", r"""
        cp -f /opt/claude-templates/CLAUDE.md /workspace/.claude/CLAUDE.md
        cp -f /opt/claude-templates/logbook_supervisor_template.md \
              /workspace/.claude/logbook_supervisor_template.md
        cp -f /opt/claude-templates/logbook_pi_template.md \
              /workspace/.claude/logbook_pi_template.md
        rm -rf /workspace/.claude/commands
        cp -a /opt/claude-templates/commands /workspace/.claude/commands
    """])


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCP registry CLI (Stage 2.1)
# ---------------------------------------------------------------------------


def mcp_container_name_for(name: str) -> str:
    return f"{MCP_CONTAINER_PREFIX}{name}"


def projects_using_mcp(mcp_name: str) -> list[str]:
    """Scan per-project allowlists for projects that allow this MCP. The
    allowlist lives at ``<workspace>/.orchestrator/mcp-allow.json`` (see
    project_allowlist_path) — looking at the project root directly would
    silently miss every project."""
    cfg = load_config()
    root = Path(cfg.projects_dir).expanduser().resolve()
    if not root.is_dir():
        return []
    out: list[str] = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        allow_file = project_allowlist_path(p.name, cfg)
        if not allow_file.is_file():
            continue
        try:
            data = json.loads(allow_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, list):
            continue
        for e in data:
            if isinstance(e, dict) and e.get("name") == mcp_name:
                out.append(p.name)
                break
    return out


def project_allowlist_path(project: str, cfg: "Config") -> Path:
    """Per-project MCP allowlist. Lives INSIDE the workspace (which is the
    only thing the supervisor bind-mounts) so atomic-rename writes by
    research.py are visible to the supervisor immediately. A single-file
    bind-mount would pin the original inode and silently make replacements
    invisible to the container."""
    return workspace_path_for(project, cfg) / ".orchestrator" / "mcp-allow.json"


def ensure_mcp_files(project: str, cfg: "Config") -> None:
    """Initialize the host-side mcp registry (if missing) and an empty
    per-project allowlist. The registry is host-only state; the supervisor
    never reads it directly — every datum it needs lives in the allowlist
    entry written at `project mcp allow` time."""
    if not mcp_registry.REGISTRY_PATH.is_file():
        mcp_registry.REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        mcp_registry.save_atomic(mcp_registry.empty())
    allow = project_allowlist_path(project, cfg)
    if not allow.is_file():
        allow.parent.mkdir(parents=True, exist_ok=True)
        allow.write_text("[]\n")


def resolve_host_gateway() -> str:
    """Numeric IP for `host.docker.internal` from a container on the host's
    docker daemon. Used to translate `external` MCP destinations to numeric
    IPs in per-project allowlists, so the supervisor's inner-daemon proxy
    can reach them via the existing rs-router path."""
    r = run_check([
        "docker", "run", "--rm",
        "--add-host=host.docker.internal:host-gateway",
        "alpine:3.20", "getent", "hosts", "host.docker.internal",
    ])
    out = r.stdout.strip()
    if not out:
        die("could not resolve host.docker.internal via host-gateway")
    return out.split()[0]


def mcp_container_ip(name: str) -> str:
    cname = mcp_container_name_for(name)
    r = run_check([
        "docker", "inspect", cname, "-f",
        '{{(index .NetworkSettings.Networks "' + ROUTER_NETWORK + '").IPAddress}}',
    ])
    ip = r.stdout.strip()
    # A crash-looping container reports "running" between restarts but has an
    # empty IPAddress; docker's template renders that as the literal string
    # "invalid IP" rather than empty — non-empty would silently propagate to
    # iptables. Parse with ipaddress to catch both.
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        die(f"could not resolve {cname}'s IP on {ROUTER_NETWORK} "
            f"(docker inspect returned {ip!r}); the container may be "
            f"crash-looping. Check `docker logs {cname}`.")
    return ip


def load_project_allowlist(project: str, cfg: "Config") -> list[dict]:
    p = project_allowlist_path(project, cfg)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        die(f"allowlist {p} is invalid JSON: {e}")
    if not isinstance(data, list):
        die(f"allowlist {p} must be a JSON array")
    return [e for e in data if isinstance(e, dict)]


def save_project_allowlist(project: str, cfg: "Config", entries: list[dict]) -> None:
    p = project_allowlist_path(project, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


def _parse_kv(items: list[str], flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            die(f"{flag} entry must be K=V, got {item!r}")
        k, _, v = item.partition("=")
        if not k:
            die(f"{flag} entry has empty key: {item!r}")
        out[k] = v
    return out


def _parse_host_arg(s: str) -> tuple[str, int]:
    host, sep, port_s = s.rpartition(":")
    if not sep or not host or not port_s:
        die(f"--host must be HOST:PORT, got {s!r}")
    try:
        port = int(port_s)
    except ValueError:
        die(f"--host port must be an integer, got {port_s!r}")
    return host, port


def _build_mcp_entry(args: argparse.Namespace) -> dict:
    entry: dict = {"kind": args.kind, "transport": args.transport}
    if args.path and args.path != mcp_registry.DEFAULT_PATH:
        entry["path"] = args.path
    if args.kind == "external":
        if args.host is None:
            die("--host is required for --kind external")
        host_addr, host_port = _parse_host_arg(args.host)
        entry["host_address"] = host_addr
        entry["host_port"] = host_port
        if args.header:
            entry["headers"] = _parse_kv(args.header, "--header")
    else:  # shared
        if not args.image:
            die("--image is required for --kind shared")
        if args.port is None:
            die("--port is required for --kind shared")
        entry["image"] = args.image
        entry["port"] = args.port
        if args.env:
            entry["env"] = _parse_kv(args.env, "--env")
    desc = (getattr(args, "description", None) or "").strip()
    if desc:
        entry["description"] = desc
    roles = _parse_csv_list(getattr(args, "roles", None))
    if roles:
        entry["roles"] = roles
    return entry


def _ensure_router_running() -> None:
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research start` first.")


def _spawn_shared_mcp(name: str, entry: dict) -> None:
    """Run the shared MCP container on rs-sandbox. Idempotent: skips if running."""
    cname = mcp_container_name_for(name)
    if container_running(cname):
        return
    if container_exists(cname):
        run(["docker", "rm", "-f", cname], capture_output=True)
    if not run_quiet(["docker", "image", "inspect", entry["image"]]):
        print(f"pulling {entry['image']}...")
        run_check(["docker", "pull", entry["image"]])
    cmd = [
        "docker", "run", "-d",
        "--name", cname,
        "--network", ROUTER_NETWORK,
        "--restart", "unless-stopped",
        "--label", f"{MCP_LABEL}=1",
        "--label", f"{MCP_NAME_LABEL}={name}",
    ]
    for k, v in entry.get("env", {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [entry["image"]]
    run_check(cmd)
    print(f"started {cname}")


def _set_enabled(name: str, value: bool) -> dict:
    with mcp_registry.lock():
        try:
            data = mcp_registry.load(expand=False)
        except mcp_registry.RegistryError as e:
            die(str(e))
        entry = data["mcps"].get(name)
        if entry is None:
            die(f"no MCP named {name!r}")
        entry["enabled"] = value
        try:
            mcp_registry.save_atomic(data)
        except mcp_registry.RegistryError as e:
            die(str(e))
    return entry


# ---------------------------------------------------------------------------
# PI-isolated type registry CLI (STAGE_PI_ISOLATED)
# ---------------------------------------------------------------------------
# Host-side registry of reusable PI-isolated agent *types* (repo + root
# folder + setup). Mirrors the general MCP registry's host+project split:
# types are defined once here and referenced by name at
# `project create|update --enable <type>`. The registry ships empty — RS
# pre-bakes no types.


def projects_using_sandbox_type(name: str) -> list[str]:
    """Scan per-project sandbox.json snapshots for projects that enable this
    BYO type — the gate for a safe `sandbox remove`. A BYO sandbox entry is
    keyed by its type name, so membership is the test."""
    cfg = load_config()
    root = Path(cfg.projects_dir).expanduser().resolve()
    if not root.is_dir():
        return []
    out: list[str] = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        entries = sandbox.load(workspace_path_for(p.name, cfg))
        e = entries.get(name)
        if isinstance(e, dict) and e.get("kind") == "byo":
            out.append(p.name)
    return out


def _verify_pi_isolated_repo(repo: str, ref: str) -> None:
    """Best-effort `git ls-remote` check that repo+ref resolve, so a typo
    surfaces at `add` time rather than inside the supervisor at first
    enable (bad failure-distance — STAGE_PI_ISOLATED Q6). Skipped with a
    warning if git isn't on the host PATH; the operator can pass
    --no-verify to skip deliberately."""
    if not shutil.which("git"):
        print("warning: git not on PATH; skipping repo/ref verification "
              "(pass --no-verify to silence)", file=sys.stderr)
        return
    r = run(["git", "ls-remote", "--exit-code", repo, ref],
            capture_output=True)
    if r.returncode != 0:
        die(f"could not resolve ref {ref!r} in {repo!r} via git ls-remote "
            f"(pass --no-verify to skip this check):\n"
            f"  {(r.stderr or r.stdout).strip()}")


def _resolve_pi_isolated_ref(repo: str) -> str:
    """Resolve the repo's default-branch HEAD to a concrete commit SHA so a
    `--ref`-less `add` still pins (no silent upstream drift — the invariant
    holds, the operator just doesn't have to look the SHA up). Requires git
    on the host: pinning is non-negotiable, so if we can't resolve we fail
    rather than store an unpinned entry."""
    if not shutil.which("git"):
        die("git not on PATH: cannot resolve the latest commit to pin "
            "(--ref omitted). Install git, or pass --ref <sha> explicitly.")
    r = run(["git", "ls-remote", repo, "HEAD"], capture_output=True)
    if r.returncode != 0 or not r.stdout.split():
        die(f"could not resolve default-branch HEAD of {repo!r} via "
            f"git ls-remote:\n  {(r.stderr or r.stdout).strip()}")
    return r.stdout.split()[0]


def _reject_mirror_sandbox_default(name: str) -> None:
    """A baked mirror sandbox (wrangler/websearcher) has no independent default
    — its enablement follows its worker twin. Point the operator there."""
    if sandbox.is_baked(name) and sandbox.mirror_of(name):
        die(f"sandbox {name!r} mirrors the worker service of the same name — "
            f"its default follows the worker, not a separate flag. Use "
            f"`research worker enable {name}` / `disable {name}` instead "
            f"(that auto-enables/disables this sandbox in lockstep).")


def _sandbox_registry_edit(name: str, mutate) -> None:
    with pi_isolated_registry.lock():
        try:
            data = pi_isolated_registry.load(expand=False)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))
        entry = data["types"].get(name)
        if entry is None:
            die(f"no sandbox type named {name!r}")
        mutate(entry)
        try:
            pi_isolated_registry.save_atomic(data)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))


def _shared_mcps(only_enabled: bool = False) -> list[tuple[str, dict]]:
    try:
        data = mcp_registry.load()
    except mcp_registry.RegistryError as e:
        die(str(e))
    out = []
    for name, entry in sorted(data["mcps"].items()):
        if entry["kind"] != "shared":
            continue
        if only_enabled and not entry.get("enabled", False):
            continue
        out.append((name, entry))
    return out


def _probe_mcp(name: str, entry: dict) -> tuple[bool, str]:
    if entry["kind"] == "external":
        host = entry.get("host_address", "host.docker.internal")
        port = entry["host_port"]
        cmd = [
            "docker", "run", "--rm",
            "--network", ROUTER_NETWORK,
            "--add-host", "host.docker.internal:host-gateway",
            PROBE_IMAGE,
            "nc", "-z", "-w", "5", host, str(port),
        ]
    else:
        cname = mcp_container_name_for(name)
        if not container_running(cname):
            return False, (f"shared MCP container {cname} not running "
                           f"(try: research mcp start {name})")
        cmd = [
            "docker", "run", "--rm",
            "--network", ROUTER_NETWORK,
            PROBE_IMAGE,
            "nc", "-z", "-w", "5", cname, str(entry["port"]),
        ]
    r = run(cmd, capture_output=True)
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout).strip()


def _supervisor_mcp_reload(container_name: str) -> None:
    """Re-render the supervisor's proxy config and SIGHUP the proxy."""
    if not container_running(container_name):
        return
    r = run(["docker", "exec", container_name, "/usr/local/bin/mcp-reload"],
            capture_output=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        print(f"warning: mcp-reload in {container_name} failed: {msg}",
              file=sys.stderr)


def _allow_mcp_for_project(project: str, cfg: "Config", mcp_name: str,
                           *, do_reload: bool = True) -> tuple[bool, str]:
    """Open the router hole, append (or replace) the per-project allowlist
    entry, and optionally reload the supervisor's mcp-proxy. Returns
    ``(ok, message)`` so callers can iterate batches without aborting.
    The caller owns project-existence checks; this helper validates
    router + registry + (shared) container state."""
    container_name = container_name_for(project)

    if not container_running(ROUTER_CONTAINER):
        return False, f"{ROUTER_CONTAINER} is not running"

    try:
        entry = mcp_registry.entry_for(mcp_name)
    except mcp_registry.RegistryError as e:
        return False, str(e)
    if entry is None:
        return False, f"no MCP named {mcp_name!r}"
    if not entry.get("enabled", False):
        return False, f"MCP {mcp_name!r} is not enabled"

    if entry["kind"] == "external":
        host_addr = entry.get("host_address", "host.docker.internal")
        ip = resolve_host_gateway() if host_addr == "host.docker.internal" else host_addr
        port = entry["host_port"]
    else:  # shared
        cname = mcp_container_name_for(mcp_name)
        if not container_running(cname):
            return False, f"shared MCP container {cname} not running"
        ip = mcp_container_ip(mcp_name)
        port = entry["port"]

    network = project_network_for(project)
    subnet = get_network_subnet(network)
    r = run(["docker", "exec", ROUTER_CONTAINER,
             "/scripts/mcp-allow.sh", subnet, ip, str(port)],
            capture_output=True)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or "mcp-allow.sh failed"

    allowlist = load_project_allowlist(project, cfg)
    allowlist = [e for e in allowlist if e.get("name") != mcp_name]
    new_entry = {
        "name": mcp_name,
        "kind": entry["kind"],
        "transport": entry.get("transport", "http"),
        "ip": ip,
        "port": port,
        "path": entry.get("path", mcp_registry.DEFAULT_PATH),
    }
    if entry.get("headers"):
        new_entry["headers"] = entry["headers"]
    if entry.get("description"):
        new_entry["description"] = entry["description"]
    allowlist.append(new_entry)
    save_project_allowlist(project, cfg, allowlist)

    if do_reload:
        _supervisor_mcp_reload(container_name)
    return True, f"-> {ip}:{port}"


def _deny_mcp_for_project(project: str, cfg: "Config", mcp_name: str,
                          *, do_reload: bool = True) -> tuple[bool, str]:
    """Close the router hole, drop the entry from the per-project
    allowlist, optionally reload the supervisor's mcp-proxy. Returns
    ``(ok, message)``. Tolerates a torn-down project network / stopped
    router (skips the firewall step in that case)."""
    container_name = container_name_for(project)

    allowlist = load_project_allowlist(project, cfg)
    target = next((e for e in allowlist if e.get("name") == mcp_name), None)
    if target is None:
        return False, f"{mcp_name!r} is not currently allowed"

    network = project_network_for(project)
    if network_exists(network) and container_running(ROUTER_CONTAINER):
        subnet = get_network_subnet(network)
        run(["docker", "exec", ROUTER_CONTAINER,
             "/scripts/mcp-deny.sh", subnet,
             str(target.get("ip", "")), str(target.get("port", ""))],
            capture_output=True)

    allowlist = [e for e in allowlist if e.get("name") != mcp_name]
    save_project_allowlist(project, cfg, allowlist)

    if do_reload:
        _supervisor_mcp_reload(container_name)
    return True, ""


def _require_project(project: str) -> str:
    container_name = container_name_for(project)
    if not container_exists(container_name):
        die(f"project {project!r} does not exist")
    return container_name


def _batch_apply(project: str, cfg: "Config", names: list[str],
                 helper, action: str) -> tuple[list[str], list[str]]:
    """Run ``helper`` (allow or deny) for each name with reload deferred.
    Returns ``(succeeded, failed)`` lists. Caller is responsible for the
    one-shot reload at the end."""
    succeeded: list[str] = []
    failed: list[str] = []
    for name in names:
        ok, msg = helper(project, cfg, name, do_reload=False)
        if ok:
            succeeded.append(name)
            print(f"{action} {name!r}{(' ' + msg) if msg else ''}")
        else:
            failed.append(name)
            print(f"warning: {action} {name!r} skipped: {msg}",
                  file=sys.stderr)
    return succeeded, failed


# ---------------------------------------------------------------------------
# Per-project role-MCP lifecycle (B.0)
# ---------------------------------------------------------------------------


def _role_mcp_stage_creds(supervisor: str, role: str) -> None:
    """Snapshot the supervisor's current Claude credentials into the
    per-role daemon-state dir so the role-MCP container can stage them at
    boot. Idempotent: overwrites any previous snapshot.

    Tolerant of an un-authed supervisor: if the supervisor has no
    `.credentials.json` yet, this stages nothing and returns cleanly — it
    does NOT fail. Enablement is independent of auth (the worker-side twin
    of the PI auth-ownership model): the daemon boots idle and the
    supervisor's claude copies its own creds in later via
    `rs-role-mcp sync-creds`, prompted by the `needs_credentials` send_job
    envelope. The `mkdir -p` below is kept unconditionally — it is the
    load-bearing root-owned-bind-source guard, needed whether or not creds
    exist.

    Path note: creds land under .role-mcps/<role>/.creds/ (the daemon-state
    location), NOT under shared/<role>/ which is reserved for the role's
    public publish surface. Mixing them would expose creds to any future
    cross-role RO consumer of shared/<role>/."""
    # mkdir -p does double duty: stages creds, AND pre-creates the publish
    # source dir (/workspace/shared/{role}) and daemon-state dir
    # (/workspace/.role-mcps/{role}) with the supervisor user's ownership
    # (uid 1000) BEFORE docker tries to bind-mount them. If we don't,
    # docker auto-creates missing bind-mount sources as root-owned 755,
    # and the role-MCP container's worker user (also uid 1000 but
    # different namespace) can't write to them. Failure mode is
    # silent until the daemon tries its first write.
    #
    # Role-MCPs only spawn headless `claude -p`, which works from
    # `.credentials.json` alone — no `~/.claude.json` propagation here.
    script = f"""
        set -e
        mkdir -p /workspace/.role-mcps/{role}/.creds
        mkdir -p /workspace/shared/{role}
        if [ ! -f /home/research/.claude/.credentials.json ]; then
            echo "supervisor not yet authenticated; role-mcp {role} will boot idle (run rs-role-mcp sync-creds after /login)" >&2
            exit 0
        fi
        cp /home/research/.claude/.credentials.json \
           /workspace/.role-mcps/{role}/.creds/.credentials.json
        chmod 600 /workspace/.role-mcps/{role}/.creds/.credentials.json
        if [ -f /home/research/.claude/settings.json ]; then
            # Strip the `hooks` key — the supervisor's Stop hook calls
            # /usr/local/bin/rs-audit-stop, which is baked into the
            # supervisor image only. Propagating it would break every
            # claude session in the role-MCP container with a "command
            # not found" error on every Stop event.
            jq 'del(.hooks)' /home/research/.claude/settings.json \
               > /workspace/.role-mcps/{role}/.creds/settings.json
            chmod 600 /workspace/.role-mcps/{role}/.creds/settings.json
        fi
    """
    r = run(["docker", "exec", supervisor, "bash", "-eu", "-c", script],
            capture_output=True)
    if r.returncode != 0:
        die((r.stderr or r.stdout).strip()
            or f"failed to stage creds for role-mcp {role!r}")


def _role_mcp_migrate_state(supervisor: str, role: str) -> None:
    """One-shot move of daemon-state subdirs from the B.0 layout
    (/workspace/shared/<role>/{jobs,memories,...}) to the B.3 layout
    (/workspace/.role-mcps/<role>/{jobs,memories,...}).

    Idempotent: only moves entries when the source exists and the
    destination doesn't. Safe to call on every role-mcp start; on
    already-migrated workspaces every check short-circuits.

    The publish surface at /workspace/shared/<role>/ is preserved (the
    non-daemon-state files there, if any, stay put — they're the role's
    public artifact dir going forward). Daemon-state names are explicit
    (no glob) so we don't accidentally sweep a future publish artifact
    a user dropped in there."""
    daemon_state_names = [
        "jobs", "memories", ".calls", ".creds",
        "global.md", ".summarize-watermark",
    ]
    moves = " ".join(daemon_state_names)
    script = f"""
        set -e
        src=/workspace/shared/{role}
        dst=/workspace/.role-mcps/{role}
        mkdir -p "$dst"
        for name in {moves}; do
            if [ -e "$src/$name" ] && [ ! -e "$dst/$name" ]; then
                mv "$src/$name" "$dst/$name"
                echo "migrated $src/$name -> $dst/$name" >&2
            fi
        done
    """
    run(["docker", "exec", supervisor, "bash", "-eu", "-c", script],
        capture_output=True)


def _role_mcp_inner_exists(supervisor: str, role: str) -> bool:
    cname = role_mcp.role_container_name(role)
    r = run(["docker", "exec", supervisor,
             "docker", "inspect", cname], capture_output=True)
    return r.returncode == 0


def _role_mcp_inner_running(supervisor: str, role: str) -> bool:
    cname = role_mcp.role_container_name(role)
    r = run(["docker", "exec", supervisor,
             "docker", "inspect", "-f", "{{.State.Running}}", cname],
            capture_output=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _data_mount_args_from_supervisor(supervisor: str) -> list[str]:
    """Harvest `--data` bind-mounts from the supervisor and return docker
    `-v` args that propagate them RO into an inner container at the same
    paths. Role-MCPs and PI containers gain visibility into the project's
    `/workspace/shared/data/<basename>/` dirs that workers already see via
    their RO mount of `<workspace>/shared/`.

    Symmetric exposure: every inner container sees every `--data` path
    the operator passed at `project create`. Per-role narrower visibility
    would need a new flag (deferred — `--data` stays project-level).

    The destination path inside the supervisor is itself a valid path on
    the supervisor's filesystem (it's a bind-mount from the host); the
    inner dockerd can bind that same path into a child container with
    no further translation. We RO-pin it regardless of the supervisor's
    own mount mode (operator may have writable `--data` in the future;
    inner containers stay RO for the security posture)."""
    r = run(["docker", "inspect", supervisor, "--format", "{{json .Mounts}}"],
            capture_output=True)
    if r.returncode != 0:
        return []
    try:
        mounts = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    args: list[str] = []
    for m in mounts:
        if m.get("Type") != "bind":
            continue
        dst = m.get("Destination") or ""
        if dst.startswith("/workspace/shared/data/"):
            args += ["-v", f"{dst}:{dst}:ro"]
    return args


def _role_mcp_start(supervisor: str, project: str, cfg: "Config",
                    role: str, *, force_restage: bool = False) -> None:
    """Run the role-MCP container in the supervisor's inner dockerd.
    Idempotent: tears down any prior instance with the same name first so
    a stale crashed container doesn't block start. Lazy-stages the
    per-role image into the inner dockerd on first use; project create
    doesn't pre-stage role-MCP images (most projects won't use them).
    Pass force_restage=True after a host-side image rebuild to push the
    new content through."""
    workspace_path = workspace_path_for(project, cfg)
    entries = role_mcp.load_role_mcps(workspace_path)
    entry = entries.get(role)
    if entry is None:
        die(f"no role-mcps.json entry for role {role!r}; call enable first")

    role_mcp.validate_role(role)

    cname = role_mcp.role_container_name(role)
    # rm any prior container BEFORE migrating state, so the move can't race
    # a running daemon that's still writing into /workspace/shared/<role>/.
    run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
        capture_output=True)
    _role_mcp_migrate_state(supervisor, role)
    _role_mcp_stage_creds(supervisor, role)

    image = entry.get("image") or role_mcp.ROLE_IMAGES[role]
    stage_worker_image(supervisor, image, force=force_restage)

    # Bind-mount layout:
    #   /workspace                  ← <supervisor>/workspace/.role-mcps/<role>
    #     RW. Daemon-private state: jobs/, memories/, global.md, .calls/,
    #     .creds/, .summarize-watermark, .tools-inventory.md. Hidden under
    #     a leading-dot dir on the project volume so casual `ls /shared/`
    #     doesn't surface internals.
    #   /workspace/published        ← <supervisor>/workspace/shared/<role>
    #     RW from this role-MCP. The role's PUBLIC artifact surface —
    #     intended to be cross-role-RO-consumable later. Wrangler writes
    #     extracts/<topic>/<slug>.{parquet,sql,metadata.json} here;
    #     librarian (B.2) will write refs/<topic>/; echo and (likely)
    #     websearcher leave it empty.
    #   /etc/orchestrator           ← <supervisor>/workspace/.orchestrator (RO)
    #     Parent-dir bind-mount so atomic-rename writes by the host stay
    #     visible (single-file-bind-mount rule); entrypoint reads
    #     role-mcps.json and mcp-allow.json from here.
    ip = entry["ip"]
    # Substrate (B.1-substrate) resource flags + concurrency env. Persisted
    # in role-mcps.json so they survive _recreate_supervisor without
    # re-consulting Config (operator's enable-time intent is captured).
    memory = entry.get("memory") or cfg.default_role_mcp_memory
    mcc = entry.get("max_concurrent_calls")
    if mcc is None:
        mcc = cfg.default_role_mcp_max_concurrent_calls
    docker_args = [
        "docker", "exec", supervisor,
        "docker", "run", "-d",
        "--name", cname,
        "--network", INNER_NETWORK,
        "--ip", ip,
        "--restart", "unless-stopped",
        # tini at PID 1 reaps zombies. Belt-and-suspenders with per-MCP
        # `dumb-init` wrappers in image-baked extras (B.1) — if a wrapped
        # stdio MCP dies uncleanly, grandchildren reparent to container
        # PID 1 and tini reaps them.
        "--init",
        # Blast-radius backstop. OOM-killer takes the role container,
        # not the supervisor. See Config.default_role_mcp_memory comment
        # for the size reasoning.
        f"--memory={memory}",
        "-v", f"/workspace/.role-mcps/{role}:/workspace",
        "-v", f"/workspace/shared/{role}:/workspace/published",
        "-v", "/workspace/.orchestrator:/etc/orchestrator:ro",
        "-e", f"RS_ROLE_NAME={role}",
        "-e", f"RS_ROLE_MCP_PORT={role_mcp.ROLE_MCP_PORT}",
        # Daemon reads this to enforce the cap on send_job. 0 = uncapped.
        "-e", f"RS_ROLE_MAX_CONCURRENT_CALLS={int(mcc)}",
        "--label", f"research.role_mcp={role}",
        "--label", f"research.project={project}",
        # Project --data paths, propagated RO at the same mount points
        # the supervisor + workers see them at.
        *_data_mount_args_from_supervisor(supervisor),
        image,
    ]
    run_check(docker_args)
    print(f"role-mcp {role!r}: running at {ip}:{role_mcp.ROLE_MCP_PORT}")


def _role_mcp_stop(supervisor: str, role: str) -> None:
    """Hard stop + remove the role-MCP container (`docker rm -f` = SIGKILL,
    no grace, no drain). This is `disable`'s teardown — it pairs with
    dropping the role-mcps.json entry, so abruptness is acceptable: the role
    is leaving the project. For a *graceful park that keeps the entry*, use
    `_role_mcp_park` instead. Tolerates absence — caller may have already
    removed it via _recreate_supervisor."""
    cname = role_mcp.role_container_name(role)
    run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
        capture_output=True)


def _role_mcp_in_flight(workspace_path: Path, role: str) -> list[dict]:
    """Read-only, host-side count of in-flight send_job calls, straight off
    the project volume (`<workspace>/.role-mcps/<role>/jobs/*.json` — the same
    files the daemon writes; see daemon.py JobStore). A job still in status
    `running` is a live `claude -p` call (or a daemon-restart orphan the
    daemon would reap on its next boot; from the host the file state is the
    conservative signal — we'd rather refuse a stop than kill a real call).
    Returns the running entries so the caller can name them in a refusal.
    Pure read — no docker, no mutation, safe to call as a gate."""
    jobs_dir = workspace_path / ".role-mcps" / role / "jobs"
    if not jobs_dir.is_dir():
        return []
    running: list[dict] = []
    for p in sorted(jobs_dir.glob("*.json")):
        try:
            entry = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(entry, dict) and entry.get("status") == "running":
            running.append(entry)
    return running


def _role_mcp_park(supervisor: str, role: str) -> None:
    """Graceful stop of the role-MCP container WITHOUT removing it and
    WITHOUT touching role-mcps.json — the role stays registered, just parked.
    Uses `docker stop` (SIGTERM + grace), not the `rm -f` (SIGKILL) that
    `disable` uses. The daemon has no SIGTERM drain handler, so gracefulness
    here comes from *quiescence*, not from the signal: callers gate on
    `_role_mcp_in_flight` == 0 first, so the daemon is idle (no `claude -p`
    child, no memory write mid-flight) when the signal lands and a
    handler-less SIGTERM terminates a process with nothing to tear. The
    container is left `exited` (name preserved, visible in `docker ps -a`);
    `_role_mcp_start` rm's and replaces it on unpark, and
    `_recreate_supervisor` skips parked entries entirely. The
    `--restart unless-stopped` policy honors a manual stop, so the parked
    container won't auto-restart within the supervisor's lifetime."""
    cname = role_mcp.role_container_name(role)
    run(["docker", "exec", supervisor, "docker", "stop", cname],
        capture_output=True)


def _derive_auto_upstreams(role: str, project: str, cfg: "Config") -> list[str]:
    """Auto-wired upstream set for ``role``: every registered MCP whose
    ``roles`` field lists ``role`` AND that is currently allowed for the
    project. Sorted alphabetically so role-mcps.json diffs across sync
    runs are minimal."""
    try:
        registry = mcp_registry.load(expand=False)
    except mcp_registry.RegistryError as e:
        die(str(e))
    allowed = {e.get("name") for e in load_project_allowlist(project, cfg)
               if e.get("name")}
    return sorted(
        name for name, entry in registry["mcps"].items()
        if role in (entry.get("roles") or [])
        and name in allowed
    )


def _role_mcp_enable(project: str, cfg: "Config", role: str,
                     upstreams: list[str] | None,
                     *, force_auto: bool = False,
                     no_pi_mirror: bool = False,
                     memory: str | None = None,
                     max_concurrent_calls: int | None = None) -> None:
    """Validate + write the per-project role-mcps.json entry + start the
    container + reload the supervisor's mcp-proxy so its config includes
    the role-MCP route. Also auto-enables the matching PI mirror
    (``pi-<role>``) unless ``no_pi_mirror`` is set or no such image
    exists. Idempotent.

    Upstream-source state machine:
      - ``upstreams=list, force_auto=False``: explicit pin. Survives sync.
      - ``upstreams=None, force_auto=True``: re-derive from registry × allow,
        write ``upstream_source=auto``. The re-mark path.
      - ``upstreams=None, force_auto=False``:
          - if no existing entry: first-time enable — auto-derive, write
            ``upstream_source=auto``. Empty result emits the M8 warning.
          - if an entry exists: idempotent re-run — preserve current
            ``upstream_source`` and ``upstream_mcps``. No silent flips."""
    role_mcp.validate_role(role)
    supervisor = container_name_for(project)
    if not container_running(supervisor):
        die(f"project {project!r} is not running; bring it up first")

    workspace_path = workspace_path_for(project, cfg)
    entries = role_mcp.load_role_mcps(workspace_path)
    existing = entries.get(role)

    if upstreams is not None:
        chosen_upstreams = list(upstreams)
        chosen_source = "explicit"
    elif force_auto or existing is None:
        chosen_upstreams = _derive_auto_upstreams(role, project, cfg)
        chosen_source = "auto"
        if not chosen_upstreams:
            print(
                f"warning: no registered MCPs claim role {role!r}; "
                f"role-mcp {role!r} starting with empty inventory. "
                f"Add a registry entry with `research mcp add ... "
                f"--roles {role}` (then `research project mcp sync "
                f"{project}`), or pin explicit upstreams with "
                f"`research project role-mcp enable {project} {role} "
                f"--upstream <csv>`.",
                file=sys.stderr,
            )
    else:
        # Preserve-on-reenable: idempotent re-run, no silent flips.
        chosen_upstreams = list(existing.get("upstream_mcps") or [])
        chosen_source = existing.get("upstream_source") or "explicit"

    allow_entries = load_project_allowlist(project, cfg)
    try:
        role_mcp.validate_upstreams(chosen_upstreams, allow_entries)
    except ValueError as e:
        die(str(e))

    # Resource caps: explicit flag > existing entry > cfg default. The
    # entry always carries a concrete value so _recreate_supervisor and
    # `role-mcp status` reads don't need access to Config — the persisted
    # state is the source of truth. A bump to DEFAULT_ROLE_MCP_* only
    # affects NEW enables; existing entries keep their captured values
    # until disable+enable (predictable across recreates).
    if memory is not None:
        chosen_memory = memory
    elif existing is not None and existing.get("memory"):
        chosen_memory = str(existing["memory"])
    else:
        chosen_memory = cfg.default_role_mcp_memory

    if max_concurrent_calls is not None:
        chosen_mcc = max_concurrent_calls
    elif existing is not None and existing.get("max_concurrent_calls") is not None:
        chosen_mcc = int(existing["max_concurrent_calls"])
    else:
        chosen_mcc = cfg.default_role_mcp_max_concurrent_calls

    new_entry = role_mcp.build_entry(
        role, chosen_upstreams, upstream_source=chosen_source,
        memory=chosen_memory, max_concurrent_calls=chosen_mcc,
    )
    # Preserve parked state across re-enable / sync re-derive: a worker the
    # operator deliberately stopped (`project worker stop`) stays parked
    # until an explicit `project worker start`. build_entry always renders a
    # running entry, so without this carry-forward a `project mcp sync`
    # re-derive would silently resurrect a stopped worker.
    parked = bool(existing is not None and existing.get("stopped"))
    if parked:
        new_entry["stopped"] = True
    entries[role] = new_entry
    role_mcp.save_role_mcps(workspace_path, entries)

    if parked:
        print(
            f"worker {role!r}: entry updated but left parked — start it with "
            f"`research project worker start {project} {role}`",
            file=sys.stderr,
        )
    else:
        _role_mcp_start(supervisor, project, cfg, role)
        _supervisor_mcp_reload(supervisor)

    # Sandbox mirror auto-enable (M4). Skipped if (a) operator opted out
    # (no_pi_mirror — the flag kept its internal name), or (b) no baked
    # sandbox shares this worker's name (e.g. echo-mcp has no mirror; only
    # wrangler / websearcher do). The mirror's name IS the worker's name.
    if not no_pi_mirror and sandbox.is_baked(role):
        try:
            _sandbox_enable(project, cfg, role)
        except SystemExit:
            # _sandbox_enable die()s on its own paths (image-stage, start
            # error). W10 can't fire — we just wrote role-mcps.json above.
            # Surface as a warning; operator can retry with sandbox enable.
            print(
                f"warning: sandbox mirror {role!r} failed to enable; "
                f"retry with `research project sandbox enable {project} "
                f"{role}`",
                file=sys.stderr,
            )


def _role_mcp_disable(project: str, cfg: "Config", role: str) -> None:
    """Stop the container, drop the role-mcps.json entry, reload the
    proxy. Workspace state under /workspace/.role-mcps/<role>/ (daemon
    state: jobs, memories, global.md, creds) and under
    /workspace/shared/<role>/ (publish surface) both survive — the
    bind-mounts are on the project volume and unaffected by docker rm."""
    supervisor = container_name_for(project)
    workspace_path = workspace_path_for(project, cfg)
    entries = role_mcp.load_role_mcps(workspace_path)
    if role not in entries:
        die(f"role-mcp {role!r} is not enabled for project {project!r}")
    if container_running(supervisor):
        _role_mcp_stop(supervisor, role)
    del entries[role]
    role_mcp.save_role_mcps(workspace_path, entries)
    if container_running(supervisor):
        _supervisor_mcp_reload(supervisor)


def _parse_csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [t.strip() for t in value.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Per-project PI-role lifecycle (STAGE_BACKEND_PI P.0)
# ---------------------------------------------------------------------------


def _sandbox_ensure_workspace(supervisor: str, name: str, kind: str) -> None:
    """Pre-create the sandbox's workspace bind-mount source dir as the
    uid-1000 supervisor user, so dockerd's auto-create on ``docker run -v``
    doesn't land it root-owned and lock out the container's worker user.

    No credentials are staged — sandboxes are PI-owned: they boot un-authed
    and the PI authenticates in-tab (``/login``) or pulls the supervisor's
    creds via the manual ``rs-pi sync-creds`` bridge. bypassPermissions
    config is baked into rs-pi-base. Source path differs by kind (baked:
    ``pi/<name>``; byo: ``pi-isolated/<name>``) — see sandbox.workspace_subdir."""
    sub = sandbox.workspace_subdir(name, kind)
    r = run(["docker", "exec", supervisor, "bash", "-eu", "-c",
             f"mkdir -p /workspace/{sub}"], capture_output=True)
    if r.returncode != 0:
        die((r.stderr or r.stdout).strip()
            or f"failed to ensure workspace dir for sandbox {name!r}")


def _sandbox_inner_exists(supervisor: str, name: str, kind: str) -> bool:
    # `docker container inspect` (not bare inspect, which falls through to the
    # same-named image — the inner dockerd tags per-role images with the
    # container's unqualified name).
    cname = sandbox.container_name(name, kind)
    r = run(["docker", "exec", supervisor,
             "docker", "container", "inspect", cname], capture_output=True)
    return r.returncode == 0


def _sandbox_inner_running(supervisor: str, name: str, kind: str) -> bool:
    cname = sandbox.container_name(name, kind)
    r = run(["docker", "exec", supervisor,
             "docker", "inspect", "-f", "{{.State.Running}}", cname],
            capture_output=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _inner_container_states(supervisor: str) -> dict[str, str]:
    """Map inner-container name → docker state (running/exited/created/dead/…)
    via a single ``docker ps -a``. Empty dict when the supervisor is down, so
    callers fall back to config-only listings (the comprehensive-list rule:
    every subcontainer visible, supervisor up *or* down)."""
    if not container_running(supervisor):
        return {}
    r = run(["docker", "exec", supervisor, "docker", "ps", "-a",
             "--format", "{{.Names}}\t{{.State}}"], capture_output=True)
    if r.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if "\t" in line:
            n, _, s = line.partition("\t")
            out[n.strip()] = s.strip()
    return out


def _sandbox_start(supervisor: str, project: str, cfg: "Config",
                   name: str, *, force_restage: bool = False) -> None:
    """Run a sandbox container in the supervisor's inner dockerd. Idempotent:
    tears down any prior same-named container first. Branches on the entry's
    ``kind`` — baked roles stage a per-role image + mirror MCP wiring; BYO
    agents stage the generic image + clone an external repo. Lazy-stages the
    image; pass force_restage after a host-side rebuild."""
    workspace_path = workspace_path_for(project, cfg)
    entries = sandbox.load(workspace_path)
    entry = entries.get(name)
    if entry is None:
        die(f"no sandbox.json entry for {name!r}; call enable first")
    kind = entry.get("kind")

    cname = sandbox.container_name(name, kind)
    run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
        capture_output=True)
    _sandbox_ensure_workspace(supervisor, name, kind)
    ip = entry["ip"]

    if kind == "baked":
        image = entry.get("image") or sandbox.BAKED_IMAGES[name]
        stage_worker_image(supervisor, image, force=force_restage)
        # Single RW workspace mount + RO orchestrator mount. The latter lets a
        # mirror role render .mcp.json + .tools-inventory.md at entrypoint time
        # from the same role-mcps.json the worker service uses (pi-echo-style
        # roles with no worker twin simply ignore it). No /creds mount —
        # PI-owned; bypassPermissions baked into rs-pi-base.
        docker_args = [
            "docker", "exec", supervisor,
            "docker", "run", "-d",
            "--name", cname,
            "--network", INNER_NETWORK,
            "--ip", ip,
            "--restart", "unless-stopped",
            "-v", f"/workspace/pi/{name}:/workspace",
            "-v", "/workspace/.orchestrator:/etc/orchestrator:ro",
            "-e", f"RS_PI_ROLE={name}",
            "--label", f"research.pi_role={sandbox.pi_role_label(name, kind)}",
            "--label", f"research.project={project}",
            # Project --data paths, propagated RO at the same mount points the
            # supervisor + workers see them at.
            *_data_mount_args_from_supervisor(supervisor),
            image,
        ]
        run_check(docker_args)
        print(f"sandbox {name!r} (baked): running at {ip}")
    else:  # byo
        stage_worker_image(supervisor, PI_ISOLATED_IMAGE, force=force_restage)
        mount = entry.get("mount") or pi_isolated_registry.DEFAULT_MOUNT
        # RW workspace (repo cloned to /workspace/<repo> by the entrypoint) +
        # the external host folder at the configured mount. No /creds mount.
        docker_args = [
            "docker", "exec", supervisor,
            "docker", "run", "-d",
            "--name", cname,
            "--network", INNER_NETWORK,
            "--ip", ip,
            "--restart", "unless-stopped",
            "-v", f"/workspace/pi-isolated/{name}:/workspace",
            "-v", f"/external/{name}:{mount}",
            "-e", f"RS_PI_ISO_NAME={name}",
            "-e", f"RS_PI_ISO_REPO={entry.get('repo') or ''}",
            "-e", f"RS_PI_ISO_REF={entry.get('ref') or ''}",
            "-e", f"RS_PI_ISO_SETUP={entry.get('setup') or ''}",
            "-e", f"RS_PI_ISO_MOUNT={mount}",
            "--label", f"research.pi_role={sandbox.pi_role_label(name, kind)}",
            "--label", f"research.pi_isolated={name}",
            "--label", f"research.project={project}",
            *_data_mount_args_from_supervisor(supervisor),
            PI_ISOLATED_IMAGE,
        ]
        run_check(docker_args)
        print(f"sandbox {name!r} (byo): running at {ip}")


def _sandbox_stop(supervisor: str, name: str, kind: str) -> None:
    """Stop + remove the sandbox container in the inner dockerd. Tolerates
    absence. Verifies removal with `docker container inspect` (not bare
    inspect, which falls through to the same-named image)."""
    cname = sandbox.container_name(name, kind)
    rm = run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
             capture_output=True)
    check = run(["docker", "exec", supervisor,
                 "docker", "container", "inspect", cname],
                capture_output=True)
    if check.returncode == 0:
        rm_tail = (rm.stderr or rm.stdout or "").strip()[-200:]
        die(f"sandbox container {cname!r} still present after disable; "
            f"docker rm -f tail: {rm_tail!r}. Inspect "
            f"`docker exec {supervisor} docker container inspect {cname}` "
            f"manually and retry `research project sandbox disable`.")


def _sandbox_enable(project: str, cfg: "Config", name: str) -> None:
    """Resolve the sandbox kind (baked role vs BYO registry type), write the
    per-project sandbox.json entry, and start the container. Idempotent.

    Baked roles that mirror a worker service (same short name — wrangler,
    websearcher) refuse to enable unless that worker service is enabled
    first (the W10 gate: a mirror with no worker twin renders an empty MCP
    list). ``echo`` has no worker twin and bypasses the gate. BYO agents
    recreate the supervisor first if it doesn't yet mount /external/<name>."""
    supervisor = container_name_for(project)
    if not container_running(supervisor):
        die(f"project {project!r} is not running; bring it up first")
    workspace_path = workspace_path_for(project, cfg)
    entries = sandbox.load(workspace_path)

    if sandbox.is_baked(name):
        mirror = sandbox.mirror_of(name)
        if mirror is not None:
            worker_entries = role_mcp.load_role_mcps(workspace_path)
            if mirror not in worker_entries:
                die(f"sandbox {name!r} mirrors worker service {mirror!r}, "
                    f"which is not enabled for project {project!r}. Run "
                    f"`research project worker enable {project} {mirror} "
                    f"--upstream <mcp,...>` first, then re-run this command. "
                    f"(Sandbox mode reads its upstream set from the worker "
                    f"service so both surfaces share one source of truth.)")
        entries[name] = sandbox.build_baked_entry(name)
        sandbox.save(workspace_path, entries)
        _sandbox_start(supervisor, project, cfg, name)
        return

    # BYO: look up the host registry type, allocate an IP, snapshot the entry.
    type_entry = pi_isolated_registry.entry_for(name, expand=True)
    if type_entry is None:
        die(f"no sandbox named {name!r} (not a baked role, not in the host "
            f"BYO registry). Register a BYO type first: `research sandbox add "
            f"{name} --root <host-dir> [--repo <url> --ref <sha>]`")
    try:
        ip = sandbox.allocate_byo_ip(entries, name)
    except ValueError as e:
        die(str(e))
    entries[name] = sandbox.build_byo_entry(name, type_entry, ip)
    sandbox.save(workspace_path, entries)

    if not _supervisor_has_external_mount(supervisor, name):
        print(f"sandbox {name!r}: supervisor not yet mounting /external/{name}; "
              f"recreating supervisor to wire the external folder (creds + "
              f"workspace survive)...")
        _recreate_supervisor(project, cfg)
        return  # recreate's restart loop starts the container
    _sandbox_start(supervisor, project, cfg, name)


def _sandbox_disable(project: str, cfg: "Config", name: str) -> None:
    """Stop the container, drop the sandbox.json entry. Workspace state (and,
    for BYO, the external host folder with the cloned repo) survives — they're
    on the project volume / host root, unaffected by docker rm."""
    supervisor = container_name_for(project)
    workspace_path = workspace_path_for(project, cfg)
    entries = sandbox.load(workspace_path)
    if name not in entries:
        die(f"sandbox {name!r} is not enabled for project {project!r}")
    kind = entries[name].get("kind")
    if container_running(supervisor):
        _sandbox_stop(supervisor, name, kind)
    del entries[name]
    sandbox.save(workspace_path, entries)


# ---------------------------------------------------------------------------
# Sandbox external-folder mounts (BYO sandboxes only — baked roles have none)
# ---------------------------------------------------------------------------


def _sandbox_external_mounts(project: str) -> list[str]:
    """``-v`` args mounting each registered BYO type's ``<root>/<project>/``
    host folder at the supervisor's ``/external/<type>``. Computed fresh from
    the host BYO registry at every supervisor create/recreate, so the mount
    set always tracks the current registry (a removed type drops out; a newly-
    added type appears on the next recreate). The per-project subdir is created
    host-side so docker doesn't auto-create it root-owned. ``~`` is expanded
    here; ``${VAR}`` was expanded by the registry loader."""
    try:
        data = pi_isolated_registry.load(expand=True)
    except pi_isolated_registry.RegistryError as e:
        die(str(e))
    mounts: list[str] = []
    for name, entry in sorted(data["types"].items()):
        root = Path(entry["root"]).expanduser()
        host_dir = root / project
        host_dir.mkdir(parents=True, exist_ok=True)
        mounts += ["-v", f"{host_dir}:/external/{name}"]
    return mounts


def _supervisor_has_external_mount(supervisor: str, name: str) -> bool:
    """True if the supervisor container currently bind-mounts
    ``/external/<name>``. Drives the enable path's decide-to-recreate: a
    type registered after the supervisor's last create/recreate isn't
    mounted yet, so enable must recreate (re-enumerating the registry)
    before it can start the inner container against that source path."""
    r = run(["docker", "inspect", "-f",
             "{{range .Mounts}}{{.Destination}}\n{{end}}", supervisor],
            capture_output=True)
    if r.returncode != 0:
        return False
    return f"/external/{name}" in r.stdout.split("\n")


def _registered_sandbox_byo_types() -> set[str]:
    """BYO sandbox type names in the host registry, or empty on load failure
    (a malformed registry shouldn't break `project create`; the dedicated
    `sandbox` subcommands surface the error). Baked role names are constants
    (sandbox.baked_names()), not in this set."""
    try:
        return set(pi_isolated_registry.load(expand=False)["types"])
    except pi_isolated_registry.RegistryError:
        return set()


def _split_enable_tokens(
    enable_arg: str | None,
) -> tuple[str | None, list[str], list[str], bool]:
    """Split ``--enable`` value into (service_csv, worker_roles, sandboxes,
    no_sandbox_mirror).

    Tokens are matched against the registries in order, by canonical name:
      - the bare sentinel ``no-sandbox-mirror`` suppresses the sandbox-mirror
        auto-enable for every worker token in the same ``--enable`` value,
      - a key in ``role_mcp.ROLE_IMAGES`` (e.g. ``wrangler``, ``websearcher``,
        ``echo-mcp``) peels into the worker list. A worker whose name also
        names a baked sandbox (wrangler / websearcher) auto-enables that
        sandbox mirror downstream — so a bare twin name means "enable both",
      - a name resolving to a sandbox type (baked ``echo`` or a BYO registry
        type) that is NOT also a worker peels into the sandbox list,
      - anything else stays for ``_compute_service_flags`` as a service id.

    Worker-first ordering resolves the twin overlap (``wrangler`` is both a
    worker and a baked sandbox): the bare name enables the worker (+ mirror),
    while a sandbox-only enable goes through ``project sandbox enable``.

    Empty service set returns None to keep the default intact."""
    if not enable_arg:
        return None, [], [], False
    sandbox_types = sandbox.known_type_names()
    services: list[str] = []
    workers: list[str] = []
    sandboxes: list[str] = []
    no_sandbox_mirror = False
    for tok in enable_arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok == "no-sandbox-mirror":
            no_sandbox_mirror = True
        elif tok in role_mcp.ROLE_IMAGES:
            workers.append(tok)
        elif tok in sandbox_types:
            sandboxes.append(tok)
        else:
            services.append(tok)
    svc_csv = ",".join(services) if services else None
    return svc_csv, workers, sandboxes, no_sandbox_mirror


def _parse_role_mcp_upstream(
    raw: list[str], *, valid_roles: set[str],
) -> dict[str, list[str]]:
    """Parse repeated ``--role-mcp-upstream <role>=<csv>`` flags into
    ``{role: [mcp_name, ...]}``. Each role must appear in the same
    ``--enable`` set as a role-MCP token, so a typo or stray role-mcp
    surfaces here rather than as an orphan upstream override.

    An entry with empty CSV (``--role-mcp-upstream wrangler=``) means
    'explicit empty' — daemon comes up with no upstreams — distinct from
    the absence of the flag entirely (which falls through to auto-derive)."""
    out: dict[str, list[str]] = {}
    for item in raw:
        if "=" not in item:
            die(f"--role-mcp-upstream value must be 'role=csv', got {item!r}")
        role, _, csv = item.partition("=")
        role = role.strip()
        if not role:
            die(f"--role-mcp-upstream missing role name in {item!r}")
        if role not in valid_roles:
            die(f"--role-mcp-upstream {role!r} not in --enable role-mcp "
                f"set {sorted(valid_roles)}")
        out[role] = _parse_csv_list(csv)
    return out


def _ordered_union(*lists: list[str]) -> list[str]:
    """Concatenate lists preserving first-seen order, dropping duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for x in lst:
            if x not in seen:
                seen.add(x)
                out.append(x)
    return out


def _split_disable_tokens(
    disable_arg: str | None,
) -> tuple[str | None, list[str], list[str]]:
    """Mirror of `_split_enable_tokens` for the disable side: (service_csv,
    workers, sandboxes). Same worker-first resolution order so a token that
    names both a worker service and a baked sandbox (wrangler / websearcher)
    disables the worker (whose mirror then isn't enabled either). `--disable`
    overrules the default-enable set at `project create`, and disables an
    enabled worker/sandbox at `project update`."""
    if not disable_arg:
        return None, [], []
    sandbox_types = sandbox.known_type_names()
    services: list[str] = []
    workers: list[str] = []
    sandboxes: list[str] = []
    for tok in disable_arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok in role_mcp.ROLE_IMAGES:
            workers.append(tok)
        elif tok in sandbox_types:
            sandboxes.append(tok)
        else:
            services.append(tok)
    svc_csv = ",".join(services) if services else None
    return svc_csv, workers, sandboxes


# ---------------------------------------------------------------------------
# Per-supervisor service flags
# ---------------------------------------------------------------------------


def _parse_service_list(s: str | None) -> set[str]:
    if not s:
        return set()
    return {tok for tok in (t.strip() for t in s.split(",")) if tok}


def _compute_service_flags(
    enable_arg: str | None,
    disable_arg: str | None,
    base: dict[str, bool] | None = None,
) -> dict[str, bool]:
    """Resolve per-service enabled flags from --enable / --disable args.

    Default = every known service enabled (or `base` if updating, so missing
    flags inherit the supervisor's prior choices). `supervisor` (the
    SSH + byobu substrate) is always-on and cannot be disabled. Unknown
    service ids are a hard error."""
    enable = _parse_service_list(enable_arg)
    disable = _parse_service_list(disable_arg)
    unknown = (enable | disable) - set(KNOWN_SERVICES)
    if unknown:
        die(f"unknown service id(s): {sorted(unknown)} "
            f"(known: {KNOWN_SERVICES})")
    bad = disable & ALWAYS_ON_SERVICES
    if bad:
        die(f"cannot disable always-on service(s): {sorted(bad)}")

    flags: dict[str, bool] = dict(base) if base else {}
    for sid in KNOWN_SERVICES:
        flags.setdefault(sid, True)
    for sid in disable:
        flags[sid] = False
    for sid in enable:
        flags[sid] = True
    for sid in ALWAYS_ON_SERVICES:
        flags[sid] = True
    return flags


def _read_service_flags(container: str) -> dict[str, bool]:
    """Recover per-service flags from a supervisor's existing labels.
    Missing labels (legacy projects) default to enabled. Used by
    `_recreate_supervisor` so a bare `project update` preserves prior
    --enable/--disable choices."""
    if not container_exists(container):
        return {sid: True for sid in KNOWN_SERVICES}
    r = run(["docker", "inspect", container, "-f",
             "{{json .Config.Labels}}"], capture_output=True)
    try:
        labels = json.loads(r.stdout) or {}
    except json.JSONDecodeError:
        labels = {}
    out: dict[str, bool] = {}
    for sid in KNOWN_SERVICES:
        v = labels.get(f"{SERVICE_LABEL_PREFIX}{sid}")
        out[sid] = (v != "disabled")  # missing or "enabled" => True
    for sid in ALWAYS_ON_SERVICES:
        out[sid] = True
    return out


# ---------------------------------------------------------------------------
# webui (browser SSH multiplexer + service-aware proxy host)
# ---------------------------------------------------------------------------


def _supervisor_ssh_pass(container: str) -> str | None:
    """Read SSH_PASSWORD from a supervisor container's env. Returns None
    when the container is missing or the password isn't published."""
    if not container_exists(container):
        return None
    r = run(["docker", "inspect", container, "-f",
             "{{range .Config.Env}}{{println .}}{{end}}"],
            capture_output=True)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("SSH_PASSWORD="):
            return line.split("=", 1)[1]
    return None


def _webui_import_string(project: str, ssh_pass: str) -> str:
    """Build the base64 import string for the webui SPA. Webui reaches the
    supervisor via container DNS on the per-project network (rs-webui is
    `docker network connect`'d to every rs-net-<proj>), not via the
    published host SSH port."""
    return base64.b64encode(json.dumps({
        "name": project,
        "host": container_name_for(project),
        "port": 22,
        "username": "research",
        "password": ssh_pass,
    }).encode()).decode()


def wire_webui_to_projects() -> None:
    """Connect rs-webui to every existing per-project network. Idempotent;
    no-op when the webui isn't running. Called at webui start and after
    every `project create` so the webui sees fresh projects without a
    restart."""
    if not container_exists(WEBUI_CONTAINER):
        return
    r = run(["docker", "network", "ls",
             "--filter", f"name=^{PROJECT_NETWORK_PREFIX}",
             "--format", "{{.Name}}"],
            capture_output=True)
    for net in r.stdout.strip().splitlines():
        if net:
            run(["docker", "network", "connect", net, WEBUI_CONTAINER],
                capture_output=True)


def wire_router_to_projects() -> None:
    """Connect rs-router to every existing per-project network. Idempotent;
    no-op when the router isn't running.

    `cmd_project_create` is the original wire-er (via ensure_project_network).
    This re-wirer exists for the case where the router container was rebuilt
    or recreated — compose's `up -d --build router` does `rm` + `run`, which
    drops every `docker network connect` to rs-net-<project> that prior
    creates set up. Without this, the next `project update` against an
    existing project dies at `get_router_ip` because the recreated rs-router
    isn't attached to that project's network.

    iptables state on the router IS recovered on its own: the router's
    entrypoint replays `/etc/sandbox/rules/*` on startup, and that directory
    lives on the named volume `rs-router-rules` which survives `docker rm`.
    So this helper only handles the network-attachment side of the recreate;
    the firewall side is self-healing."""
    if not container_running(ROUTER_CONTAINER):
        return
    r = run(["docker", "network", "ls",
             "--filter", f"name=^{PROJECT_NETWORK_PREFIX}",
             "--format", "{{.Name}}"],
            capture_output=True)
    for net in r.stdout.strip().splitlines():
        if net:
            run(["docker", "network", "connect", net, ROUTER_CONTAINER],
                capture_output=True)


def _detect_tailscale_fqdn() -> str | None:
    """Read the host's tailnet FQDN from `tailscale status --json`. Returns
    None if tailscale isn't installed, the daemon isn't running, or the
    host hasn't joined a tailnet."""
    if shutil.which("tailscale") is None:
        return None
    r = run(["tailscale", "status", "--json"], capture_output=True)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    fqdn = (data.get("Self") or {}).get("DNSName", "")
    return fqdn.rstrip(".") or None


def _webui_tls_volume() -> str:
    """Resolve the docker volume name actually mounted at /app/tls in
    rs-webui. Falls back to the literal name pinned in docker-compose.yml
    when rs-webui isn't running yet. This avoids a previous bug where the
    helper wrote to a bare `rs-webui-tls` volume while compose mounted a
    project-prefixed one (`research-sandbox_rs-webui-tls`)."""
    if container_exists(WEBUI_CONTAINER):
        r = run(["docker", "inspect", WEBUI_CONTAINER, "-f",
                 "{{range .Mounts}}{{if eq .Destination \"/app/tls\"}}"
                 "{{.Name}}{{end}}{{end}}"], capture_output=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return "rs-webui-tls"


def _stage_webui_cert(cert_pem: bytes, key_pem: bytes,
                      provider: str) -> None:
    """Write cert+key+`.custom` marker into the webui's TLS volume. The
    marker tells the in-container ensure_tls() to skip its auto-regenerate-
    self-signed path so the user-provided cert sticks even when WEBUI_BIND
    doesn't appear in the cert's SAN."""
    volume = _webui_tls_volume()
    if not run_quiet(["docker", "volume", "inspect", volume]):
        run_check(["docker", "volume", "create", volume])
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / "cert.pem").write_bytes(cert_pem)
        (td_path / "key.pem").write_bytes(key_pem)
        (td_path / ".custom").write_text(f"{provider}\n")
        # chown to UID 1000 — the in-container webui user. Without this
        # the busybox-written files are root-owned, and any regen attempt
        # (e.g. server.py falling back to self-signed when the marker is
        # absent) hits Permission denied on the cert.pem write and the
        # container restart-loops. chmod 644/600 then matches what the
        # auto-generated cert flow uses.
        run_check([
            "docker", "run", "--rm",
            "-v", f"{volume}:/tls",
            "-v", f"{td_path}:/src:ro",
            "busybox", "sh", "-c",
            "cp /src/cert.pem /tls/cert.pem && "
            "cp /src/key.pem  /tls/key.pem && "
            "cp /src/.custom  /tls/.custom && "
            "chown 1000:1000 /tls/cert.pem /tls/key.pem /tls/.custom && "
            "chmod 644 /tls/cert.pem /tls/.custom && "
            "chmod 600 /tls/key.pem"
        ])


def _webui_recreate_in_place() -> None:
    """Tear down + bring up the webui container so it re-reads the TLS
    volume on next start. No image rebuild; preserves WEBUI_BIND/PORT."""
    bind = read_env_value("WEBUI_BIND") or "127.0.0.1"
    port = read_env_value("WEBUI_PORT") or "7777"
    os.environ["WEBUI_BIND"] = bind
    os.environ["WEBUI_PORT"] = port
    if container_exists(WEBUI_CONTAINER):
        run(["docker", "rm", "-f", WEBUI_CONTAINER], capture_output=True)
    docker_compose("--profile", "webui", "up", "-d", "webui")
    wire_webui_to_projects()


# Re-export every module-level name (incl. _underscore helpers) so
# research.py's existing cmd_* call sites resolve via `from rscore import *`.
__all__ = [_n for _n in list(globals()) if not _n.startswith('__')]
