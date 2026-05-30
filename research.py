#!/usr/bin/env python3
"""research — CLI for Research Sandbox.

Stdlib only. Subcommand layout:

    research start                     # start shared infra (router)
    research stop                      # stop shared infra
    research project create <name> [--data PATHS] [--profile python]
                                   [--dind auto|sysbox|privileged]
                                   [--memory SIZE] [--cpus N]
                                   [--egress open|locked] [--ssh-port N]
    research project attach  <name>
    research project list
    research project status  <name>
    research project stop    <name> | --all
    research project start   <name> | --all
    research project destroy <name>
    research project ssh     <name>

State is stored in ``.env`` beside this script, plus Docker labels on the
supervisor container (``sandbox.project=<name>``). Per-project host
directory ``<PROJECTS_DIR>/<name>/workspace/`` is bind-mounted into the
supervisor's ``/workspace``; named volume ``rs-docker-<name>`` holds the
inner Docker daemon state when ``--dind privileged`` is used (sysbox keeps
it internal to the container).
"""

from __future__ import annotations

import argparse
import base64
import datetime
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

# Make cli/ helpers importable.
sys.path.insert(0, str(Path(__file__).resolve().parent / "cli"))
import mcp_registry  # noqa: E402
import pi as pi_roles  # noqa: E402
import pi_isolated  # noqa: E402
import pi_isolated_registry  # noqa: E402
import role_mcp  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent

SUPERVISOR_IMAGE = "rs-supervisor:latest"
ANALYSIS_IMAGE = "rs-analysis-base:latest"
MCP_PROXY_IMAGE = "rs-mcp-proxy:latest"
ROLE_MCP_BASE_IMAGE = "rs-role-mcp-base:latest"
PI_BASE_IMAGE = "rs-pi-base:latest"
PI_ISOLATED_IMAGE = "rs-pi-isolated:latest"
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
MCP_CONTAINER_PREFIX = "rs-mcp-"
MCP_LABEL = "research.mcp"
MCP_NAME_LABEL = "research.mcp_name"
PROBE_IMAGE = "busybox:1.36"

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


def confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip() == "yes"
    except EOFError:
        return False


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


def _build_images(force: bool) -> None:
    """Build supervisor + worker + mcp-proxy + role-mcp images. Skip
    existing ones unless --rebuild. Build order matters: rs-role-mcp-base
    FROMs rs-analysis-base, per-role images (rs-echo-mcp etc.) FROM
    rs-role-mcp-base — keep the list bottom-up so each FROM resolves to
    the just-built layer rather than a stale cached copy."""
    specs = [
        (SUPERVISOR_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.supervisor"),
        (ANALYSIS_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.analysis-base"),
        (MCP_PROXY_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.mcp-proxy"),
        (ROLE_MCP_BASE_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.role-mcp-base"),
        (PI_BASE_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.pi-base"),
        # Generic PI-isolated image — FROM rs-pi-base, adds git + the
        # clone/setup entrypoint. One image for every isolated type; no
        # per-type Dockerfile (type behavior comes from the cloned repo).
        (PI_ISOLATED_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.pi-isolated"),
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
    for role, image in sorted(pi_roles.PI_IMAGES.items()):
        dockerfile = SCRIPT_DIR / "agent" / f"Dockerfile.{role}"
        if not dockerfile.is_file():
            print(f"warning: pi image {image} has no Dockerfile at "
                  f"{dockerfile.name}; skipping (add it in the per-role stage)",
                  file=sys.stderr)
            continue
        specs.append((image, dockerfile))
    for tag, dockerfile in specs:
        if not force and run_quiet(["docker", "image", "inspect", tag]):
            print(f"image {tag} already present (use --rebuild to force)")
            continue
        print(f"building {tag}...")
        run_check([
            "docker", "build",
            "-f", str(dockerfile),
            "-t", tag,
            str(SCRIPT_DIR),
        ])


def cmd_start(args: argparse.Namespace) -> None:
    """Bring up shared infra: ensure images + start the router container."""
    _preflight()
    _build_images(force=args.rebuild)
    print("starting router...")
    compose_up = ["docker", "compose", "-f", str(SCRIPT_DIR / "docker-compose.yml"),
                  "up", "-d"]
    # When the user asks for a rebuild, force-rebuild the router image too —
    # otherwise compose reuses the cached image and edits to router/scripts/
    # or router/Dockerfile silently don't reach the running container.
    if args.rebuild:
        compose_up.append("--build")
    compose_up.append("router")
    run_check(compose_up)
    # If the router was recreated (compose --build, or simply a fresh `up`
    # after `stop`), it lost its `docker network connect` attachments to
    # every per-project network. Re-attach so subsequent `project update`
    # calls find rs-router on rs-net-<project>.
    wire_router_to_projects()
    _start_enabled_mcps()
    print("up.")


def _start_enabled_mcps() -> None:
    targets = _shared_mcps(only_enabled=True)
    for name, entry in targets:
        try:
            _spawn_shared_mcp(name, entry)
        except SystemExit:
            print(f"warning: failed to start MCP {name!r}; continuing",
                  file=sys.stderr)


def cmd_stop(_: argparse.Namespace) -> None:
    """Stop shared infra. Leaves images, volumes, and projects untouched."""
    run_check(["docker", "compose", "-f", str(SCRIPT_DIR / "docker-compose.yml"),
               "stop", "router"])
    print("stopped.")


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


def cmd_project_create(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.name
    if not project.replace("-", "").replace("_", "").isalnum():
        die("project name must be alphanumeric (plus '-' or '_')")

    container_name = container_name_for(project)
    workspace_path = workspace_path_for(project, cfg)

    if container_exists(container_name):
        die(f"project {project!r} already exists (container {container_name}). "
            f"Use destroy first.")

    # Verify prerequisites.
    if not run_quiet(["docker", "image", "inspect", SUPERVISOR_IMAGE]):
        die(f"image {SUPERVISOR_IMAGE} not found. Run `research setup` first.")
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research setup` first.")

    dind_mode = select_dind_mode(args.dind or cfg.default_dind)
    egress = args.egress or cfg.default_egress
    if egress not in ("open", "locked"):
        die(f"invalid --egress value: {egress!r} (expected open|locked)")

    # Optional --data bind-mounts (read-only inside supervisor). Comma-
    # separated host paths each land at /workspace/shared/data/<basename>/.
    # Missing paths are mkdir -p'd (typical use is empty placeholder dirs
    # that data is dropped into post-create). Basename collisions are a
    # hard error because the container destinations would clash.
    extra_mounts: list[str] = []
    data_basenames: dict[str, Path] = {}
    if args.data:
        seen_basenames = data_basenames
        for raw in args.data.split(","):
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
            if base in seen_basenames:
                die(f"--data basename collision: {base!r} appears in both "
                    f"{seen_basenames[base]} and {p}. Rename or symlink "
                    "one of the host paths so the container destinations "
                    "stay distinct.")
            seen_basenames[base] = p
            extra_mounts += ["-v", f"{p}:/workspace/shared/data/{base}:ro"]

    print(f"=== Creating project: {project} ===")
    ssh_port = args.ssh_port or find_free_port()
    ssh_pass = gen_password()

    # 1. Workspace dir (host bind-mount) + optional privileged-DIND volume.
    # setgid bit (2xxx) makes new files inherit the host user's primary GID;
    # combined with HOST_GID remap in the supervisor entrypoint, host user
    # and container's `research` user share rw access through the shared GID.
    workspace_path.mkdir(parents=True, exist_ok=True)
    os.chmod(workspace_path, 0o2770)

    # Pre-create /workspace/shared host-side so docker doesn't auto-create
    # it root-owned when `--data` bind-mounts land under it (the multi-
    # path layout mounts at /workspace/shared/data/<basename>/, which
    # causes docker to materialize the intermediate /workspace/shared
    # before the entrypoint runs; root-owned then breaks role-MCP enable
    # which mkdirs /workspace/shared/<role>/ as the research user).
    (workspace_path / "shared").mkdir(parents=True, exist_ok=True)
    if dind_mode == "privileged" and not volume_exists(docker_volume_name_for(project)):
        run_check(["docker", "volume", "create", docker_volume_name_for(project)])

    # 1b. Materialize the MCP bind-mount sources so docker doesn't auto-create
    #     directories where the supervisor expects JSON files.
    ensure_mcp_files(project, cfg)

    # 2. Per-project network + router wiring.
    network, router_ip = ensure_project_network(project, egress)

    # Webui (if running) joins the new per-project net so it can SSH to the
    # supervisor by container DNS. Idempotent + a no-op when webui is off.
    wire_webui_to_projects()

    # 3. Build docker run argv.
    # --enable tokens that match a role-MCP image key (e.g. `wrangler`)
    # are sugar for `research project role-mcp enable` post-creation;
    # tokens that match a PI image key (e.g. `pi-wrangler`) are sugar
    # for `research project pi enable`. Peel both off before computing
    # service flags so _compute_service_flags doesn't die on the
    # unknown id; defer activation to step 6d/6e below (after the
    # supervisor is up and creds are ready).
    enable_arg = getattr(args, "enable", None)
    disable_arg = getattr(args, "disable", None)
    enable_services, enable_role_mcps, enable_pi_roles, enable_pi_isolated, \
        enable_no_pi_mirror = _split_enable_tokens(enable_arg)
    disable_services, disable_pi_roles, disable_pi_isolated = \
        _split_disable_tokens(disable_arg)
    role_mcp_explicit = _parse_role_mcp_upstream(
        getattr(args, "role_mcp_upstream", None) or [],
        valid_roles=set(enable_role_mcps),
    )
    if enable_role_mcps:
        for role in enable_role_mcps:
            try:
                role_mcp.validate_role(role)
            except ValueError as e:
                die(str(e))
    for role in enable_pi_roles:
        try:
            pi_roles.validate_role(role)
        except ValueError as e:
            die(str(e))
    for role in disable_pi_roles:
        try:
            pi_roles.validate_role(role)
        except ValueError as e:
            die(str(e))
    service_flags = _compute_service_flags(
        enable_services, disable_services,
    )
    docker_args = build_supervisor_docker_args(
        container_name=container_name,
        project=project,
        network=network,
        workspace_path=workspace_path,
        ssh_port=ssh_port,
        ssh_pass=ssh_pass,
        dns_servers=cfg.sandbox_dns,
        memory=args.memory or cfg.default_memory,
        cpus=args.cpus or "",
        image=SUPERVISOR_IMAGE,
        dind_mode=dind_mode,
        inner_firewall=bool(getattr(args, "inner_firewall", False)),
        service_flags=service_flags,
    )
    # Inject --data mounts (and any future --mount) into argv just before the image name.
    if extra_mounts:
        docker_args = docker_args[:-1] + extra_mounts + [docker_args[-1]]

    # Pre-wire every registered PI-isolated type's external folder
    # (<root>/<project>/ → /external/<type>). Computed fresh from the host
    # registry so enabling a known type later is a pure inner-container op
    # (no supervisor recreate). See STAGE_PI_ISOLATED "two-hop mount".
    ext_mounts = _pi_isolated_external_mounts(project)
    if ext_mounts:
        docker_args = docker_args[:-1] + ext_mounts + [docker_args[-1]]

    # 4. Create container.
    run_check(["docker", *docker_args])

    # 5. Inject default route via router (makes egress traverse iptables).
    inject_route(container_name, router_ip)

    # 6. Wait for inner dockerd (started by the entrypoint), then push the
    #    analysis worker image and the mcp-proxy image into it.
    wait_for_inner_dockerd(container_name)
    stage_worker_image(container_name, ANALYSIS_IMAGE)
    stage_worker_image(container_name, MCP_PROXY_IMAGE)

    # 6b. The supervisor entrypoint runs mcp-reload at boot, but on first
    #     create that fires before stage_worker_image has staged the proxy
    #     image. Re-run it now so the proxy actually comes up.
    run(["docker", "exec", container_name, "/usr/local/bin/mcp-reload"],
        capture_output=True)

    # 6c. Auto-allow MCPs per --mcp.
    requested = _resolve_create_mcp_arg(args.mcp)
    granted: list[str] = []
    for mcp_name in requested:
        ok, msg = _allow_mcp_for_project(project, cfg, mcp_name, do_reload=False)
        if ok:
            granted.append(mcp_name)
        else:
            print(f"warning: skip auto-allow {mcp_name!r}: {msg}",
                  file=sys.stderr)
    if granted:
        _supervisor_mcp_reload(container_name)

    # 6d. role-mcp sugar (--enable <role>). Skipped silently if the
    #     supervisor is not yet authenticated — the user must run
    #     `claude` once and then `research project role-mcp enable …`
    #     manually, because we need creds to stage at start time. This
    #     mirrors the rs-worker creds requirement.
    role_mcps_enabled: list[str] = []
    creds_present = run(["docker", "exec", container_name, "test", "-f",
                         "/home/research/.claude/.credentials.json"],
                        capture_output=True).returncode == 0
    if enable_role_mcps:
        if not creds_present:
            print("note: --enable <role-mcp-name> requires supervisor auth; "
                  "skipping role-mcp activation. After authenticating "
                  "(see Next steps below), run:", file=sys.stderr)
            for role in enable_role_mcps:
                print(f"   research project role-mcp enable {project} {role}",
                      file=sys.stderr)
        else:
            for role in enable_role_mcps:
                upstreams_for_role = role_mcp_explicit.get(role)
                try:
                    _role_mcp_enable(
                        project, cfg, role, upstreams_for_role,
                        no_pi_mirror=enable_no_pi_mirror,
                    )
                    role_mcps_enabled.append(role)
                except SystemExit:
                    print(f"warning: failed to enable role-mcp {role!r}; "
                          "retry manually with "
                          f"`research project role-mcp enable {project} {role}`",
                          file=sys.stderr)

    # 6e. pi-role sugar (--enable pi-<role>). Same auth requirement as
    #     role-mcps — PI containers stage the supervisor's creds at start.
    pi_roles_enabled: list[str] = []
    if enable_pi_roles:
        if not creds_present:
            print("note: --enable pi-* requires supervisor auth; "
                  "skipping pi-role activation. After authenticating "
                  "(see Next steps below), run:", file=sys.stderr)
            for role in enable_pi_roles:
                print(f"   research project pi enable {project} {role}",
                      file=sys.stderr)
        else:
            for role in enable_pi_roles:
                try:
                    _pi_enable(project, cfg, role)
                    pi_roles_enabled.append(role)
                except SystemExit:
                    print(f"warning: failed to enable pi role {role!r}; "
                          "retry manually with "
                          f"`research project pi enable {project} {role}`",
                          file=sys.stderr)

    # 6f. pi-isolated sugar (--enable <type>). UNLIKE role-MCPs / baked PI
    #     roles, a PI-isolated agent does NOT require supervisor auth to come
    #     up: it's a plain sandbox — the entrypoint clones the repo, runs
    #     setup, and idles, and the tab is a login shell (not claude). The PI
    #     starts claude / authenticates / pulls skills there, in any order.
    #     So we always enable, regardless of creds_present. Each type's
    #     external folder was pre-wired into the supervisor at create (the
    #     build args above enumerate the registry), so enable is a pure
    #     inner-container start with no recreate. Creds are still staged if
    #     present (so a later `claude` skips /login) and tolerated if absent.
    pi_isolated_enabled: list[str] = []
    for name in enable_pi_isolated:
        try:
            _pi_isolated_enable(project, cfg, name)
            pi_isolated_enabled.append(name)
        except SystemExit:
            print(f"warning: failed to enable pi-isolated {name!r}; "
                  "retry manually with `research project pi-isolated "
                  f"enable {project} {name}`", file=sys.stderr)

    # 7. Report.
    inner_fw = "on" if getattr(args, "inner_firewall", False) else "off"
    mcps_line = ", ".join(granted) if granted else "(none)"
    role_mcps_line = ", ".join(role_mcps_enabled) if role_mcps_enabled else "(none)"
    pi_roles_line = ", ".join(pi_roles_enabled) if pi_roles_enabled else "(none)"
    if data_basenames:
        data_lines = "\n".join(
            f"             /workspace/shared/data/{b}/  ←  {src}"
            for b, src in sorted(data_basenames.items())
        )
        data_block = f"  Data (RO):\n{data_lines}\n"
    else:
        data_block = ""

    # Webui block — show URL + import string only when rs-webui is up.
    # Import string is the base64 the SPA's add-project modal accepts to
    # auto-fill SSH credentials; same payload as `research webui import`.
    if container_running(WEBUI_CONTAINER):
        webui_bind = read_env_value("WEBUI_BIND") or "127.0.0.1"
        webui_port = read_env_value("WEBUI_PORT") or "7777"
        import_str = _webui_import_string(project, ssh_pass)
        webui_block = (
            f"  Webui:     https://{webui_bind}:{webui_port}\n"
            f"  Import:    {import_str}\n"
        )
    else:
        webui_block = ""

    # Pending activations (role-MCPs / PI roles requested via --enable
    # but not successfully enabled). Two distinct reasons:
    #   (a) creds not present at create time → activation skipped silently
    #   (b) creds present but _role_mcp_enable / _pi_enable raised → catch
    #       block above appended to the warnings; the role didn't land.
    # The "Next steps" wording reflects the actual cause.
    pending_role_mcps = [r for r in enable_role_mcps if r not in role_mcps_enabled]
    pending_pi_roles = [r for r in enable_pi_roles if r not in pi_roles_enabled]
    pending_pi_isolated = [n for n in enable_pi_isolated
                           if n not in pi_isolated_enabled]
    pending = bool(pending_role_mcps or pending_pi_roles or pending_pi_isolated)

    steps: list[str] = []
    # Step 1 — interactive auth. Each project's supervisor is the source
    # of truth for its own credentials; there is no host-side cache and
    # no cross-project sharing. Operator runs `claude` once per project,
    # completes the device-code flow, and the supervisor holds those
    # creds for every downstream surface in this project from then on.
    steps.append(
        "  1. Authenticate Claude Code inside the supervisor (once per project):\n"
        f"     `python research.py project attach {project}`, then in byobu run\n"
        "     `claude` and complete the device-code flow in a local browser."
    )

    if pending:
        cmds: list[str] = []
        for r in pending_role_mcps:
            cmds.append(f"       python research.py project role-mcp enable {project} {r}")
        for r in pending_pi_roles:
            cmds.append(f"       python research.py project pi enable {project} {r}")
        for n in pending_pi_isolated:
            cmds.append(f"       python research.py project pi-isolated enable {project} {n}")
        # Two failure modes: "creds weren't present" vs "enable raised
        # despite creds present" — different actionable guidance.
        if not creds_present:
            reason = "(skipped at create because the supervisor wasn't authed yet):"
        else:
            reason = "(enable failed at create — see the warnings above for the cause, fix it, then re-run):"
        step_lines = [
            f"  {len(steps)+1}. Finish activating the deferred role-MCPs / PI roles",
            f"     {reason}",
            "\n".join(cmds),
        ]
        steps.append("\n".join(step_lines))
    steps.append(
        f"  {len(steps)+1}. Start working on a problem with the supervisor."
    )
    next_steps = "\n".join(steps)

    print(f"""
Project '{project}' is running.

  Container: {container_name}
  Workspace: {workspace_path}
  Network:   {network} (egress: {egress})
  DIND mode: {dind_mode}
  Inner FW:  {inner_fw}
  MCPs:      {mcps_line}
  Role-MCPs: {role_mcps_line}
  PI roles:  {pi_roles_line}
{data_block}  SSH:       research@localhost -p {ssh_port}   password: {ssh_pass}
{webui_block}
Next steps:
{next_steps}
""")


def cmd_project_attach(args: argparse.Namespace) -> None:
    container = container_name_for(args.name)
    if not container_running(container):
        die(f"project {args.name!r} is not running (use `research project start` first)")
    # Ensure a session exists (entrypoint creates one, but if byobu was closed…).
    run(["docker", "exec", container, "bash", "-lc",
         "byobu list-sessions 2>/dev/null | grep -q '^main:' || "
         "byobu new-session -d -s main -c /workspace -x 200 -y 50"],
        capture_output=True)
    os.execvp("docker", [
        "docker", "exec", "-it", container,
        "byobu", "attach", "-t", "main",
    ])


def cmd_project_list(_: argparse.Namespace) -> None:
    containers = get_supervisor_containers()
    if not containers:
        print("no projects")
        return
    w_name = max(len("PROJECT"), *(len(c["project"]) for c in containers))
    w_state = max(len("STATE"), *(len(c["state"]) for c in containers))
    header = f"{'PROJECT':<{w_name}}  {'STATE':<{w_state}}  SSH"
    print(header)
    print("-" * len(header))
    for c in sorted(containers, key=lambda x: x["project"]):
        ssh_port = get_ssh_port(c["name"]) if c["state"] == "running" else "-"
        print(f"{c['project']:<{w_name}}  {c['state']:<{w_state}}  "
              f"{'localhost:' + ssh_port if ssh_port and ssh_port != '-' else '-'}")


def cmd_project_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    container = container_name_for(args.name)
    if not container_exists(container):
        die(f"project {args.name!r} does not exist")
    state_r = run_check(["docker", "inspect", "-f", "{{.State.Status}}", container])
    state = state_r.stdout.strip()
    ssh_port = get_ssh_port(container) if state == "running" else None

    print(f"Project:   {args.name}")
    print(f"Container: {container}")
    print(f"State:     {state}")
    if ssh_port:
        print(f"SSH:       localhost:{ssh_port}")
    workspace = workspace_path_for(args.name, cfg)
    print(f"Workspace: {workspace}")

    # Inner workers (best-effort — requires the container to be running).
    if state == "running":
        r = run(["docker", "exec", container, "docker", "ps", "-a",
                 "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
                capture_output=True)
        if r.returncode == 0 and r.stdout.strip():
            print("\nWorkers (inner docker):")
            for line in r.stdout.strip().splitlines():
                print(f"  {line}")

        # Registry snapshot (Stage 1.7: per-worker JSONs under .workers/).
        reg = run(["docker", "exec", container, "sh", "-c",
                   "ls /workspace/.workers/*.json 2>/dev/null | wc -l"],
                  capture_output=True)
        if reg.returncode == 0:
            try:
                n = int(reg.stdout.strip() or "0")
                if n:
                    print(f"\nRegistry: {n} worker entry(ies) (see /workspace/.workers/)")
            except ValueError:
                pass


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


def cmd_project_stop(args: argparse.Namespace) -> None:
    _for_containers("stop", "__ALL__" if args.all else args.name)


def cmd_project_start(args: argparse.Namespace) -> None:
    """Start a stopped project. On sysbox, plain `docker start` after
    `docker stop` fails (sysbox-mgr's per-container volume bindings are
    'already exists' on second mount), so we route through
    `_recreate_supervisor`: fresh container ID, fresh bindings, workspace
    + creds + network preserved. Same shape as `project update` minus the
    image rebuild."""
    cfg = load_config()
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research start` first.")
    if args.all:
        containers = get_supervisor_containers()
    else:
        containers = [{"name": container_name_for(args.name), "project": args.name}]
    for c in containers:
        if not container_exists(c["name"]):
            print(f"skip: {c['name']} does not exist")
            continue
        if container_running(c["name"]):
            print(f"skip: {c['project']} already running")
            continue
        print(f"=== Starting project: {c['project']} ===")
        _recreate_supervisor(c["project"], cfg)
        print(f"start: {c['project']}")


def cmd_project_destroy(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.name
    container = container_name_for(project)
    if not container_exists(container):
        die(f"project {project!r} does not exist")

    project_root = project_root_for(project, cfg)
    docker_volume = docker_volume_name_for(project)
    network = project_network_for(project)

    print(f"=== Destroy: {project} ===\n")
    print("This will permanently delete:")
    print(f"  - supervisor container ({container})")
    print(f"  - workspace directory on host: {project_root}")
    print(f"      (includes .claude/ creds snapshot, plans, all worker outputs)")
    if volume_exists(docker_volume):
        print(f"  - DIND volume ({docker_volume})")
    print(f"  - per-project network ({network})")
    print()
    if not confirm("Type 'yes' to confirm: "):
        print("aborted.")
        return

    # Clean up any per-project MCP rules in the router so iptables doesn't
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
    print(f"destroyed {project}.")


def cmd_project_ssh(args: argparse.Namespace) -> None:
    container = container_name_for(args.name)
    if not container_running(container):
        die(f"project {args.name!r} is not running")
    port = get_ssh_port(container)
    if not port:
        die("could not resolve SSH port (not published?)")
    print(f"Host:     localhost")
    print(f"Port:     {port}")
    print(f"User:     research")
    print(f"Command:  ssh research@localhost -p {port}")


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
    """Copy the edited supervisor-baked files into the container. Returns
    the list of host-relative paths that were actually copied."""
    copied: list[str] = []
    for rel, dst, exe in _SUPERVISOR_FILE_MAP:
        src = SCRIPT_DIR / rel
        if not src.is_file():
            continue
        if exe:
            _docker_cp_with_mode(src, container, dst, 0o755)
        else:
            run_check(["docker", "cp", str(src), f"{container}:{dst}"])
        copied.append(rel)
    for rel, dst in _SUPERVISOR_DIR_MAP:
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
    # (_pi_isolated_external_mounts), so recovering them here would both
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
        image=SUPERVISOR_IMAGE,
        dind_mode=md["dind_mode"],
        inner_firewall=md["inner_firewall"],
        service_flags=flags,
    )
    if md["extra_mounts"]:
        docker_args = docker_args[:-1] + md["extra_mounts"] + [docker_args[-1]]
    # PI-isolated external folders are recomputed fresh from the host
    # registry (excluded from md["extra_mounts"]) so the recreate tracks
    # the current registry — newly-added types appear, removed ones drop.
    ext_mounts = _pi_isolated_external_mounts(project)
    if ext_mounts:
        docker_args = docker_args[:-1] + ext_mounts + [docker_args[-1]]
    # build_supervisor_docker_args emits ["run", "-d", ...]; convert to create.
    assert docker_args[0] == "run" and docker_args[1] == "-d"
    create_args = ["create"] + docker_args[2:]

    print(f"creating new container from {SUPERVISOR_IMAGE}...")
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
        try:
            _role_mcp_start(container, project, cfg, role,
                            force_restage=force_restage)
        except SystemExit:
            print(f"warning: failed to restart role-mcp {role!r}; "
                  f"the entry in role-mcps.json is intact, retry with "
                  f"`research project role-mcp enable {project} {role}`",
                  file=sys.stderr)

    # Same idea for PI-role containers (STAGE_BACKEND_PI P.0). The
    # pi-roles.json snapshot is the source of truth; _pi_start re-stages
    # the per-role image into the (potentially fresh) inner dockerd and
    # restarts the container. Workspace + creds-stash survive because
    # they're on the project volume.
    pi_entries = pi_roles.load_pi_roles(workspace_path)
    for role in sorted(pi_entries):
        try:
            _pi_start(container, project, cfg, role,
                      force_restage=force_restage)
        except SystemExit:
            print(f"warning: failed to restart pi role {role!r}; "
                  f"the entry in pi-roles.json is intact, retry with "
                  f"`research project pi enable {project} {role}`",
                  file=sys.stderr)

    # Same for PI-isolated agents (STAGE_PI_ISOLATED). The supervisor's
    # /external/<type> mounts were just recomputed from the registry above,
    # so every enabled agent's external folder is wired before its inner
    # container restarts.
    iso_entries = pi_isolated.load(workspace_path)
    for name in sorted(iso_entries):
        try:
            _pi_isolated_start(container, project, cfg, name,
                               force_restage=force_restage)
        except SystemExit:
            print(f"warning: failed to restart pi-isolated {name!r}; "
                  f"the entry in pi-isolated.json is intact, retry with "
                  f"`research project pi-isolated enable {project} {name}`",
                  file=sys.stderr)


def cmd_project_update(args: argparse.Namespace) -> None:
    """Push edited code into a project. Always recreates the supervisor
    via `_recreate_supervisor`; file-only mode injects edited files into
    the freshly-created container before its first start, --rebuild mode
    rebuilds all images first and the new container is created from the
    rebuilt image."""
    cfg = load_config()
    project = args.name
    container = container_name_for(project)

    if not container_exists(container):
        die(f"project {project!r} does not exist")
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research start` first.")

    print(f"=== Updating project: {project} ===")

    # Validate --enable role-mcp + pi tokens up front so a typo doesn't
    # waste a rebuild before failing.
    enable_arg = getattr(args, "enable", None)
    disable_arg = getattr(args, "disable", None)
    enable_services, enable_role_mcps, enable_pi_roles, enable_pi_isolated, \
        enable_no_pi_mirror = _split_enable_tokens(enable_arg)
    disable_services, disable_pi_roles, disable_pi_isolated = \
        _split_disable_tokens(disable_arg)
    role_mcp_explicit = _parse_role_mcp_upstream(
        getattr(args, "role_mcp_upstream", None) or [],
        valid_roles=set(enable_role_mcps),
    )
    for role in enable_role_mcps:
        try:
            role_mcp.validate_role(role)
        except ValueError as e:
            die(str(e))
    for role in enable_pi_roles:
        try:
            pi_roles.validate_role(role)
        except ValueError as e:
            die(str(e))
    for role in disable_pi_roles:
        try:
            pi_roles.validate_role(role)
        except ValueError as e:
            die(str(e))

    if args.rebuild:
        print("rebuilding images...")
        _build_images(force=True)

    hook = None
    if not args.rebuild:
        def hook(c: str) -> None:
            print(f"copying edited files into {c}...")
            for rel in _docker_cp_supervisor_files(c):
                print(f"  {rel}")

    flags_override: dict[str, bool] | None = None
    if enable_services or disable_services:
        base = _read_service_flags(container)
        flags_override = _compute_service_flags(
            enable_services, disable_services, base=base)

    # PI disables run BEFORE _recreate_supervisor: the recreate's PI
    # restart loop reads pi-roles.json, and a role removed there should
    # not come back up after recreate. The stop helper is tolerant of a
    # missing container (the recreate is about to nuke the inner dockerd
    # anyway under sysbox).
    for role in disable_pi_roles:
        try:
            _pi_disable(project, cfg, role)
        except SystemExit:
            print(f"warning: failed to disable pi role {role!r}",
                  file=sys.stderr)

    # Same ordering rationale for PI-isolated disables: drop the
    # pi-isolated.json entry before the recreate so its restart loop
    # doesn't bring the agent back up.
    for name in disable_pi_isolated:
        try:
            _pi_isolated_disable(project, cfg, name)
        except SystemExit:
            print(f"warning: failed to disable pi-isolated {name!r}",
                  file=sys.stderr)

    _recreate_supervisor(
        project, cfg,
        force_restage=args.rebuild,
        post_create_hook=hook,
        service_flags=flags_override,
    )

    if not args.keep_claude:
        print(f"refreshing /workspace/.claude/ from templates...")
        _refresh_workspace_claude_templates(container)

    # role-mcp sugar via --enable. Mirrors cmd_project_create: on a project
    # that already has the role enabled, this is idempotent — _role_mcp_enable
    # preserves the existing upstream_source + upstreams when no --upstream
    # / --auto signal is given (no silent flips on a re-run of `update`).
    for role in enable_role_mcps:
        upstreams_for_role = role_mcp_explicit.get(role)
        try:
            _role_mcp_enable(
                project, cfg, role, upstreams_for_role,
                no_pi_mirror=enable_no_pi_mirror,
            )
        except SystemExit:
            print(f"warning: failed to enable role-mcp {role!r}; retry "
                  f"with `research project role-mcp enable {project} {role}`",
                  file=sys.stderr)

    # pi-role sugar via --enable. Same pattern: idempotent on re-enable.
    for role in enable_pi_roles:
        try:
            _pi_enable(project, cfg, role)
        except SystemExit:
            print(f"warning: failed to enable pi role {role!r}; retry "
                  f"with `research project pi enable {project} {role}`",
                  file=sys.stderr)

    # pi-isolated sugar via --enable. The recreate above already enumerated
    # the registry and mounted every type's /external/<type> folder, so
    # _pi_isolated_enable here finds the mount present and just starts the
    # inner container (no second recreate). Idempotent on re-enable.
    for name in enable_pi_isolated:
        try:
            _pi_isolated_enable(project, cfg, name)
        except SystemExit:
            print(f"warning: failed to enable pi-isolated {name!r}; retry "
                  f"with `research project pi-isolated enable {project} {name}`",
                  file=sys.stderr)

    print(f"\nproject {project!r} updated.")


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


def cmd_mcp_add(args: argparse.Namespace) -> None:
    entry = _build_mcp_entry(args)
    known_roles = set(role_mcp.ROLE_IMAGES.keys())
    with mcp_registry.lock():
        try:
            data = mcp_registry.load(expand=False, known_roles=known_roles)
        except mcp_registry.RegistryError as e:
            die(str(e))
        if args.name in data["mcps"]:
            die(f"MCP {args.name!r} already registered; remove first")
        data["mcps"][args.name] = entry
        try:
            mcp_registry.save_atomic(data, known_roles=known_roles)
        except mcp_registry.RegistryError as e:
            die(str(e))
    print(f"added MCP {args.name!r} ({entry['kind']})")
    hint = f"  next: `research mcp enable {args.name}`"
    if entry["kind"] == "shared":
        hint += f" then `research mcp start {args.name}`"
    print(hint)


def cmd_mcp_describe(args: argparse.Namespace) -> None:
    new_desc = "" if args.clear else (args.text or "").strip()
    if not args.clear and not new_desc:
        die("description text is required (or pass --clear to remove)")
    with mcp_registry.lock():
        try:
            data = mcp_registry.load(expand=False)
        except mcp_registry.RegistryError as e:
            die(str(e))
        entry = data["mcps"].get(args.name)
        if entry is None:
            die(f"no MCP named {args.name!r}")
        if new_desc:
            entry["description"] = new_desc
        else:
            entry.pop("description", None)
        try:
            mcp_registry.save_atomic(data)
        except mcp_registry.RegistryError as e:
            die(str(e))
    if new_desc:
        print(f"set description for {args.name!r}")
    else:
        print(f"cleared description for {args.name!r}")


def cmd_mcp_set_roles(args: argparse.Namespace) -> None:
    """Replace the MCP's roles list. Empty CSV (``""``) clears it. Roles
    are validated against role_mcp.ROLE_IMAGES at save time, so a typo
    surfaces here rather than as a silent miss in auto-wire derivation."""
    new_roles = _parse_csv_list(args.csv)
    known_roles = set(role_mcp.ROLE_IMAGES.keys())
    unknown = [r for r in new_roles if r not in known_roles]
    if unknown:
        die(f"unknown role(s) {sorted(unknown)}; known: "
            f"{sorted(known_roles) or '(none)'}")
    with mcp_registry.lock():
        try:
            data = mcp_registry.load(expand=False, known_roles=known_roles)
        except mcp_registry.RegistryError as e:
            die(str(e))
        entry = data["mcps"].get(args.name)
        if entry is None:
            die(f"no MCP named {args.name!r}")
        if new_roles:
            entry["roles"] = new_roles
        else:
            entry.pop("roles", None)
        try:
            mcp_registry.save_atomic(data, known_roles=known_roles)
        except mcp_registry.RegistryError as e:
            die(str(e))
    if new_roles:
        print(f"set roles for {args.name!r}: {','.join(new_roles)}")
    else:
        print(f"cleared roles for {args.name!r}")


def cmd_mcp_list(args: argparse.Namespace) -> None:
    try:
        data = mcp_registry.load(expand=False)
    except mcp_registry.RegistryError as e:
        die(str(e))
    if args.json:
        out: dict = {"version": data["version"], "mcps": {}}
        for name, e in data["mcps"].items():
            row = dict(e)
            if e["kind"] == "shared":
                row["running"] = container_running(mcp_container_name_for(name))
            out["mcps"][name] = row
        print(json.dumps(out, indent=2, sort_keys=True))
        return
    rows: list[tuple[str, ...]] = []
    descs: list[str] = []
    for name, e in sorted(data["mcps"].items()):
        kind = e["kind"]
        enabled = "yes" if e.get("enabled", False) else "no"
        if kind == "external":
            target = f"{e.get('host_address', 'host.docker.internal')}:{e['host_port']}"
            status = "-"
        else:
            cname = mcp_container_name_for(name)
            target = f"{cname}:{e['port']}"
            status = "running" if container_running(cname) else "stopped"
        roles_cell = ",".join(sorted(e.get("roles") or [])) or "-"
        rows.append((name, kind, e["transport"], target, enabled, status, roles_cell))
        descs.append(e.get("description", ""))
    if not rows:
        print("(no MCPs registered)")
        return
    headers = ("NAME", "KIND", "TRANSPORT", "TARGET", "ENABLED", "STATUS", "ROLES")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r, d in zip(rows, descs):
        print(fmt.format(*r))
        if d:
            print(f"    {d}")


def cmd_mcp_remove(args: argparse.Namespace) -> None:
    in_use = projects_using_mcp(args.name)
    if in_use and not args.force:
        die(f"MCP {args.name!r} is currently allowed for projects: "
            f"{', '.join(in_use)}.\n"
            f"  Run `research project mcp deny <proj> {args.name}` for each, "
            f"or pass --force.")
    with mcp_registry.lock():
        try:
            data = mcp_registry.load(expand=False)
        except mcp_registry.RegistryError as e:
            die(str(e))
        if args.name not in data["mcps"]:
            die(f"no MCP named {args.name!r}")
        entry = data["mcps"].pop(args.name)
        try:
            mcp_registry.save_atomic(data)
        except mcp_registry.RegistryError as e:
            die(str(e))
    if entry["kind"] == "shared":
        cname = mcp_container_name_for(args.name)
        if container_exists(cname):
            run(["docker", "rm", "-f", cname], capture_output=True)
    print(f"removed MCP {args.name!r}")


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


def cmd_mcp_enable(args: argparse.Namespace) -> None:
    entry = _set_enabled(args.name, True)
    msg = f"enabled {args.name!r}"
    if entry["kind"] == "shared":
        msg += (f" (use `research mcp start {args.name}` "
                f"or `research start` to launch)")
    print(msg)


def cmd_mcp_disable(args: argparse.Namespace) -> None:
    cfg = load_config()
    affected = projects_using_mcp(args.name)
    if affected and not args.keep_projects:
        denied: list[str] = []
        for project in affected:
            ok, msg = _deny_mcp_for_project(project, cfg, args.name)
            if ok:
                denied.append(project)
            else:
                print(f"warning: deny {args.name!r} from {project!r}: {msg}",
                      file=sys.stderr)
    else:
        denied = []

    entry = _set_enabled(args.name, False)
    suffix = ""
    if entry["kind"] == "shared":
        cname = mcp_container_name_for(args.name)
        if container_running(cname):
            run_check(["docker", "stop", cname])
            suffix = f"; stopped {cname}"

    if affected:
        if args.keep_projects:
            print(f"disabled {args.name!r}{suffix}; --keep-projects: "
                  f"{len(affected)} project(s) still allow it: "
                  f"{', '.join(affected)}")
        else:
            print(f"disabled {args.name!r}{suffix}; denied from "
                  f"{len(denied)} project(s): {', '.join(denied)}")
    else:
        print(f"disabled {args.name!r}{suffix}")


# ---------------------------------------------------------------------------
# PI-isolated type registry CLI (STAGE_PI_ISOLATED)
# ---------------------------------------------------------------------------
# Host-side registry of reusable PI-isolated agent *types* (repo + root
# folder + setup). Mirrors the general MCP registry's host+project split:
# types are defined once here and referenced by name at
# `project create|update --enable <type>`. The registry ships empty — RS
# pre-bakes no types.


def projects_using_pi_isolated(name: str) -> list[str]:
    """Scan per-project pi-isolated.json snapshots for projects that enable
    this type — the gate for a safe `pi-isolated remove`."""
    cfg = load_config()
    root = Path(cfg.projects_dir).expanduser().resolve()
    if not root.is_dir():
        return []
    out: list[str] = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        entries = pi_isolated.load(workspace_path_for(p.name, cfg))
        if name in entries:
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


def cmd_pi_isolated_add(args: argparse.Namespace) -> None:
    entry: dict = {"root": args.root}
    if args.repo:
        ref = args.ref
        if ref:
            # Explicit ref: verify it resolves (unless suppressed).
            if not args.no_verify:
                _verify_pi_isolated_repo(args.repo, ref)
        else:
            # No ref: resolve the default-branch HEAD now and pin it.
            ref = _resolve_pi_isolated_ref(args.repo)
            print(f"  no --ref given; pinned {args.repo} default-branch HEAD "
                  f"to {ref}")
        entry["repo"] = args.repo
        entry["ref"] = ref
    if args.setup:
        entry["setup"] = args.setup
    if args.mount:
        entry["mount"] = args.mount
    if args.description:
        entry["description"] = args.description.strip()

    with pi_isolated_registry.lock():
        try:
            data = pi_isolated_registry.load(expand=False)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))
        if args.name in data["types"]:
            die(f"pi-isolated type {args.name!r} already registered; "
                f"remove first or use `pi-isolated set-root`/`describe`")
        data["types"][args.name] = entry
        try:
            pi_isolated_registry.save_atomic(data)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))
    print(f"added pi-isolated type {args.name!r} (root {args.root})")
    print(f"  next: `research project create <p> --enable {args.name}` "
          f"(or `project update <p> --enable {args.name}`)")


def cmd_pi_isolated_list(args: argparse.Namespace) -> None:
    try:
        data = pi_isolated_registry.load(expand=False)
    except pi_isolated_registry.RegistryError as e:
        die(str(e))
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    types = data["types"]
    if not types:
        print("(no pi-isolated types registered)")
        return
    rows: list[tuple[str, ...]] = []
    descs: list[str] = []
    for name, e in sorted(types.items()):
        repo = e.get("repo") or "-"
        ref = e.get("ref") or "-"
        rows.append((name, e["root"],
                     pi_isolated_registry.mount_for(e), repo, ref))
        descs.append(e.get("description", ""))
    headers = ("NAME", "ROOT", "MOUNT", "REPO", "REF")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r, d in zip(rows, descs):
        print(fmt.format(*r))
        if d:
            print(f"    {d}")


def cmd_pi_isolated_remove(args: argparse.Namespace) -> None:
    in_use = projects_using_pi_isolated(args.name)
    if in_use and not args.force:
        die(f"pi-isolated type {args.name!r} is enabled for projects: "
            f"{', '.join(in_use)}.\n"
            f"  Run `research project pi-isolated disable <proj> {args.name}` "
            f"for each, or pass --force.")
    with pi_isolated_registry.lock():
        try:
            data = pi_isolated_registry.load(expand=False)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))
        if args.name not in data["types"]:
            die(f"no pi-isolated type named {args.name!r}")
        data["types"].pop(args.name)
        try:
            pi_isolated_registry.save_atomic(data)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))
    print(f"removed pi-isolated type {args.name!r}")
    if in_use:
        print(f"  note: {len(in_use)} project(s) still have a stale "
              f"pi-isolated.json entry; they keep running until "
              f"`project pi-isolated disable`.")


def _pi_isolated_registry_edit(name: str, mutate) -> None:
    with pi_isolated_registry.lock():
        try:
            data = pi_isolated_registry.load(expand=False)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))
        entry = data["types"].get(name)
        if entry is None:
            die(f"no pi-isolated type named {name!r}")
        mutate(entry)
        try:
            pi_isolated_registry.save_atomic(data)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))


def cmd_pi_isolated_set_root(args: argparse.Namespace) -> None:
    _pi_isolated_registry_edit(args.name, lambda e: e.__setitem__("root", args.root))
    print(f"set root for {args.name!r}: {args.root}")
    print("  (existing projects pick up the new root on next "
          "`project update`/recreate)")


def cmd_pi_isolated_describe(args: argparse.Namespace) -> None:
    new_desc = "" if args.clear else (args.text or "").strip()
    if not args.clear and not new_desc:
        die("description text is required (or pass --clear to remove)")

    def mutate(e: dict) -> None:
        if new_desc:
            e["description"] = new_desc
        else:
            e.pop("description", None)

    _pi_isolated_registry_edit(args.name, mutate)
    print(f"{'set' if new_desc else 'cleared'} description for {args.name!r}")


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


def cmd_mcp_start(args: argparse.Namespace) -> None:
    _ensure_router_running()
    if args.name is not None:
        try:
            entry = mcp_registry.entry_for(args.name)
        except mcp_registry.RegistryError as e:
            die(str(e))
        if entry is None:
            die(f"no MCP named {args.name!r}")
        if entry["kind"] != "shared":
            die(f"`start` only applies to shared MCPs (got kind={entry['kind']!r})")
        _spawn_shared_mcp(args.name, entry)
        return
    targets = _shared_mcps(only_enabled=True)
    if not targets:
        try:
            data = mcp_registry.load()
        except mcp_registry.RegistryError as e:
            die(str(e))
        ext = [n for n, e in data["mcps"].items()
               if e.get("enabled", False) and e["kind"] == "external"]
        if ext:
            print(f"(nothing to start: {len(ext)} enabled MCP(s) are "
                  f"external and have no container lifecycle)")
        else:
            print("(no enabled MCPs)")
        return
    for name, entry in targets:
        _spawn_shared_mcp(name, entry)


def cmd_mcp_stop(args: argparse.Namespace) -> None:
    if args.name is not None:
        try:
            entry = mcp_registry.entry_for(args.name, expand=False)
        except mcp_registry.RegistryError as e:
            die(str(e))
        if entry is None:
            die(f"no MCP named {args.name!r}")
        if entry["kind"] != "shared":
            die(f"`stop` only applies to shared MCPs (got kind={entry['kind']!r})")
        targets = [(args.name, entry)]
    else:
        targets = _shared_mcps()
    stopped_any = False
    for name, _entry in targets:
        cname = mcp_container_name_for(name)
        if not container_running(cname):
            continue
        run_check(["docker", "stop", cname])
        print(f"stopped {cname}")
        stopped_any = True
    if not stopped_any:
        print("(no running shared MCPs)")


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


def cmd_mcp_test(args: argparse.Namespace) -> None:
    try:
        data = mcp_registry.load()
    except mcp_registry.RegistryError as e:
        die(str(e))
    if args.name is not None:
        entry = data["mcps"].get(args.name)
        if entry is None:
            die(f"no MCP named {args.name!r}")
        names = [args.name]
    else:
        names = sorted(data["mcps"].keys())
        if not names:
            print("(no MCPs registered)")
            return
    _ensure_router_running()
    failed = False
    for name in names:
        ok, err = _probe_mcp(name, data["mcps"][name])
        if ok:
            print(f"{name}: reachable")
        else:
            print(f"{name}: unreachable" + (f"\n  {err}" if err else ""),
                  file=sys.stderr)
            failed = True
    if failed:
        sys.exit(1)


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


def cmd_project_mcp_list(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    _require_project(project)

    allow_entries = load_project_allowlist(project, cfg)
    try:
        registry = mcp_registry.load(expand=False)
    except mcp_registry.RegistryError as e:
        die(str(e))
    reg_mcps = registry["mcps"]

    if args.json:
        out = []
        for e in allow_entries:
            name = e.get("name", "")
            r = reg_mcps.get(name, {})
            row = dict(e)
            row["enabled"] = bool(r.get("enabled", False))
            row["registered"] = name in reg_mcps
            if r.get("kind") == "shared":
                row["running"] = container_running(mcp_container_name_for(name))
            out.append(row)
        print(json.dumps(out, indent=2, sort_keys=True))
        return

    if not allow_entries:
        print(f"(project {project!r} allows no MCPs)")
        return

    rows: list[tuple[str, ...]] = []
    descs: list[str] = []
    for e in sorted(allow_entries, key=lambda x: x.get("name", "")):
        name = e.get("name", "")
        r = reg_mcps.get(name, {})
        kind = e.get("kind") or r.get("kind") or "?"
        if name not in reg_mcps:
            enabled = "missing"
        else:
            enabled = "yes" if r.get("enabled", False) else "no"
        if kind == "shared":
            running = "yes" if container_running(mcp_container_name_for(name)) else "no"
        else:
            running = "-"
        ok, _err = (False, "") if not container_running(ROUTER_CONTAINER) \
            else _probe_mcp(name, r) if name in reg_mcps else (False, "")
        reachable = "yes" if ok else "no"
        rows.append((name, kind, enabled, running, reachable))
        descs.append(e.get("description", "") or r.get("description", ""))

    headers = ("NAME", "KIND", "ENABLED", "RUNNING", "REACHABLE")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r, d in zip(rows, descs):
        print(fmt.format(*r))
        if d:
            print(f"    {d}")


def cmd_project_mcp_allow(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    container_name = _require_project(project)
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research start` first.")
    succeeded, failed = _batch_apply(project, cfg, args.mcp,
                                     _allow_mcp_for_project, "allowed")
    if succeeded:
        _supervisor_mcp_reload(container_name)
    if failed:
        sys.exit(1)


def cmd_project_mcp_deny(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    container_name = _require_project(project)
    succeeded, failed = _batch_apply(project, cfg, args.mcp,
                                     _deny_mcp_for_project, "denied")
    if succeeded:
        _supervisor_mcp_reload(container_name)
    if failed:
        sys.exit(1)


def cmd_project_mcp_sync(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    container_name = _require_project(project)
    try:
        registry = mcp_registry.load(expand=False)
    except mcp_registry.RegistryError as e:
        die(str(e))
    reg_mcps = registry["mcps"]
    enabled = {n for n, e in reg_mcps.items() if e.get("enabled", False)}
    allowed = {e.get("name") for e in load_project_allowlist(project, cfg)
               if e.get("name")}

    to_add = sorted(enabled - allowed)
    # Drop entries that are no longer enabled OR no longer in the registry.
    to_remove = sorted(n for n in allowed
                       if n not in enabled or n not in reg_mcps)

    allow_changed = bool(to_add or to_remove)

    if to_add or to_remove:
        added, _ = _batch_apply(project, cfg, to_add,
                                _allow_mcp_for_project, "allowed")
        removed, _ = _batch_apply(project, cfg, to_remove,
                                  _deny_mcp_for_project, "denied")
        if added or removed:
            _supervisor_mcp_reload(container_name)

    # Phase 3 — re-derive auto-wired role-MCP upstreams against the updated
    # allow list and restart any role-MCP whose upstream set actually
    # changed. PI mirror (if enabled) restarts in lockstep. Skip entries
    # marked `upstream_source=explicit` (operator override). Phase runs
    # AFTER phase 1+2 so _derive_auto_upstreams sees the current allow set.
    workspace_path = workspace_path_for(project, cfg)
    role_entries = role_mcp.load_role_mcps(workspace_path)
    pi_entries = pi_roles.load_pi_roles(workspace_path)
    role_changes: list[str] = []
    for role, entry in list(role_entries.items()):
        if entry.get("upstream_source") != "auto":
            continue
        old = list(entry.get("upstream_mcps") or [])
        new = _derive_auto_upstreams(role, project, cfg)
        if set(old) == set(new):
            continue
        entry["upstream_mcps"] = new
        role_mcp.save_role_mcps(workspace_path, role_entries)
        _role_mcp_start(container_name, project, cfg, role)
        added_up = sorted(set(new) - set(old))
        removed_up = sorted(set(old) - set(new))
        diff = []
        if added_up:
            diff.append("+" + ",".join(added_up))
        if removed_up:
            diff.append("-" + ",".join(removed_up))
        role_changes.append(f"restarted role-mcp {role!r} ({' '.join(diff)})")
        pi_role = f"pi-{role}"
        if pi_role in pi_entries:
            _pi_start(container_name, project, cfg, pi_role)
            role_changes.append(f"restarted pi mirror {pi_role!r} in lockstep")

    if not allow_changed and not role_changes:
        print(f"(project {project!r} already in sync with the registry)")
        return
    if role_changes:
        # mcp-proxy already routes by role-mcp name; restart doesn't
        # change the route table, but reload is cheap and re-affirms.
        _supervisor_mcp_reload(container_name)
        for line in role_changes:
            print(line)


# ---------------------------------------------------------------------------
# Per-project role-MCP lifecycle (B.0)
# ---------------------------------------------------------------------------


def _role_mcp_stage_creds(supervisor: str, role: str) -> None:
    """Snapshot the supervisor's current Claude credentials into the
    per-role daemon-state dir so the role-MCP container can stage them at
    boot. Idempotent: overwrites any previous snapshot. Errors are fatal —
    a role-MCP without creds is useless (spawned `claude -p` fails).

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
        if [ ! -f /home/research/.claude/.credentials.json ]; then
            echo "supervisor is not authenticated (no ~/.claude/.credentials.json)" >&2
            exit 2
        fi
        mkdir -p /workspace/.role-mcps/{role}/.creds
        mkdir -p /workspace/shared/{role}
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
    """Stop + remove the role-MCP container in the inner dockerd. Tolerates
    absence — caller may have already removed it via _recreate_supervisor."""
    cname = role_mcp.role_container_name(role)
    run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
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

    entries[role] = role_mcp.build_entry(
        role, chosen_upstreams, upstream_source=chosen_source,
        memory=chosen_memory, max_concurrent_calls=chosen_mcc,
    )
    role_mcp.save_role_mcps(workspace_path, entries)

    _role_mcp_start(supervisor, project, cfg, role)
    _supervisor_mcp_reload(supervisor)

    # PI mirror auto-enable: M4. Skipped if (a) operator opted out, or
    # (b) no image exists for pi-<role> (e.g. echo-mcp has no PI mirror).
    if not no_pi_mirror:
        pi_role = f"pi-{role}"
        if pi_role in pi_roles.PI_IMAGES:
            try:
                _pi_enable(project, cfg, pi_role)
            except SystemExit:
                # _pi_enable die()s on its own paths (image-stage, start
                # error). W10 can't fire — we just wrote role-mcps.json
                # above. Surface as a warning and continue; operator can
                # retry with `research project pi enable`.
                print(
                    f"warning: pi mirror {pi_role!r} failed to enable; "
                    f"retry with `research project pi enable {project} "
                    f"{pi_role}`",
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


def cmd_project_role_mcp_enable(args: argparse.Namespace) -> None:
    """Three-state upstream semantics:
      --upstream <csv>  → explicit list (survives sync)
      --upstream ""     → explicit empty (survives sync; daemon comes up
                          with no DB MCPs)
      --auto            → re-derive from registry × allow intersection,
                          mark as ``upstream_source: auto``
      (no flag)         → first enable: auto-derive; re-enable: preserve
                          existing source + upstreams (idempotent re-run,
                          no silent flips)
    --upstream and --auto are mutually exclusive (argparse-enforced)."""
    cfg = load_config()
    _require_project(args.project)
    raw_upstream = getattr(args, "upstream", None)
    force_auto = bool(getattr(args, "auto", False))
    if raw_upstream is not None:
        upstreams: list[str] | None = _parse_csv_list(raw_upstream)
    else:
        upstreams = None
    memory = getattr(args, "memory", None)
    mcc_raw = getattr(args, "max_concurrent_calls", None)
    max_concurrent_calls = int(mcc_raw) if mcc_raw is not None else None
    _role_mcp_enable(args.project, cfg, args.role, upstreams,
                     force_auto=force_auto,
                     no_pi_mirror=bool(getattr(args, "no_pi_mirror", False)),
                     memory=memory,
                     max_concurrent_calls=max_concurrent_calls)


def cmd_project_role_mcp_disable(args: argparse.Namespace) -> None:
    cfg = load_config()
    _require_project(args.project)
    _role_mcp_disable(args.project, cfg, args.role)
    print(f"role-mcp {args.role!r}: disabled")


def cmd_project_role_mcp_list(args: argparse.Namespace) -> None:
    cfg = load_config()
    supervisor = _require_project(args.project)
    workspace_path = workspace_path_for(args.project, cfg)
    entries = role_mcp.load_role_mcps(workspace_path)

    if args.json:
        out = []
        for role, e in sorted(entries.items()):
            row = dict(e)
            row["role"] = role
            row["running"] = (container_running(supervisor)
                              and _role_mcp_inner_running(supervisor, role))
            out.append(row)
        print(json.dumps(out, indent=2, sort_keys=True))
        return

    if not entries:
        print(f"(project {args.project!r} has no role-MCPs enabled)")
        return

    rows: list[tuple[str, ...]] = []
    for role, e in sorted(entries.items()):
        running = "no"
        if container_running(supervisor):
            running = "yes" if _role_mcp_inner_running(supervisor, role) else "no"
        upstreams = ",".join(e.get("upstream_mcps", []) or []) or "-"
        rows.append((role, e.get("ip", "?"), str(e.get("port", "?")),
                     running, upstreams))
    headers = ("ROLE", "IP", "PORT", "RUNNING", "UPSTREAMS")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))


def cmd_project_role_mcp_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    supervisor = _require_project(args.project)
    workspace_path = workspace_path_for(args.project, cfg)
    entries = role_mcp.load_role_mcps(workspace_path)
    entry = entries.get(args.role)
    if entry is None:
        die(f"role-mcp {args.role!r} is not enabled for project "
            f"{args.project!r}")

    state = {
        "role": args.role,
        "project": args.project,
        "entry": entry,
        "container": role_mcp.role_container_name(args.role),
        "exists": False,
        "running": False,
    }
    if container_running(supervisor):
        state["exists"] = _role_mcp_inner_exists(supervisor, args.role)
        state["running"] = _role_mcp_inner_running(supervisor, args.role)

    # Per-caller watermark + memory counts. Useful for spotting whether a
    # role-MCP has actually processed any traffic since enable. State lives
    # under .role-mcps/<role>/ (daemon-private); the publish surface at
    # shared/<role>/ is a separate concern surfaced below.
    state_dir = workspace_path / ".role-mcps" / args.role
    wm = state_dir / ".summarize-watermark"
    if wm.is_file():
        try:
            state["watermarks"] = json.loads(wm.read_text())
        except json.JSONDecodeError:
            state["watermarks"] = "(invalid json)"
    else:
        state["watermarks"] = {}
    memories = state_dir / "memories"
    if memories.is_dir():
        state["calls_by_caller"] = {
            p.name: sum(1 for _ in p.glob("*.md"))
            for p in sorted(memories.iterdir()) if p.is_dir()
        }
    else:
        state["calls_by_caller"] = {}

    # Publish-surface presence. Empty dir is normal (echo, possibly
    # websearcher); non-empty (wrangler's extracts/, librarian's refs/)
    # indicates the role is producing artifacts.
    publish_dir = workspace_path / "shared" / args.role
    state["publish_dir"] = str(publish_dir)
    state["publish_present"] = (
        publish_dir.is_dir() and any(publish_dir.iterdir())
    )

    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return

    print(f"role:        {state['role']}")
    print(f"project:     {state['project']}")
    print(f"container:   {state['container']}")
    print(f"  exists:    {state['exists']}")
    print(f"  running:   {state['running']}")
    print(f"ip:          {entry.get('ip')}")
    print(f"port:        {entry.get('port')}")
    print(f"image:       {entry.get('image')}")
    upstreams = entry.get("upstream_mcps") or []
    print(f"upstreams:   {', '.join(upstreams) or '(none)'}")
    print(f"  source:    {entry.get('upstream_source', 'explicit (legacy)')}")
    print(f"watermarks:  {json.dumps(state['watermarks'], sort_keys=True)}")
    print(f"calls:       {json.dumps(state['calls_by_caller'], sort_keys=True)}")
    print(f"publish_dir: {state['publish_dir']}"
          f" ({'has artifacts' if state['publish_present'] else 'empty'})")


# ---------------------------------------------------------------------------
# Per-project PI-role lifecycle (STAGE_BACKEND_PI P.0)
# ---------------------------------------------------------------------------


def _pi_stage_creds(supervisor: str, role: str) -> None:
    """Snapshot the supervisor's current Claude credentials into the
    per-role creds-stash dir so the PI container can stage them at boot.
    Idempotent: overwrites any previous snapshot. Errors are fatal — a PI
    container with no creds is non-functional (claude inside refuses to
    start with `Not logged in`).

    Path note: paths use the *short* role name (the ``pi-`` prefix
    stripped — e.g. ``pi-echo`` → ``echo``). The role key keeps the
    ``pi-`` prefix only because it disambiguates from worker-facing
    role-MCPs at the registry level; once inside the project volume's
    ``pi/`` directory grouping, the prefix is redundant. Creds land
    under ``.pi/<short>/.creds/`` (hidden tree, parallel to
    ``.role-mcps/<role>/``). The workspace bind-mount source at
    ``pi/<short>/`` is pre-created uid-1000-owned by the same script
    so docker's auto-create doesn't land it root-owned and break the
    worker user's writes inside the container."""
    short = pi_roles.role_short(role)
    script = f"""
        set -e
        if [ ! -f /home/research/.claude/.credentials.json ]; then
            echo "supervisor is not authenticated (no ~/.claude/.credentials.json)" >&2
            exit 2
        fi
        mkdir -p /workspace/.pi/{short}/.creds
        mkdir -p /workspace/pi/{short}
        cp /home/research/.claude/.credentials.json \
           /workspace/.pi/{short}/.creds/.credentials.json
        chmod 600 /workspace/.pi/{short}/.creds/.credentials.json
        if [ -f /home/research/.claude/settings.json ]; then
            # Strip the `hooks` key — supervisor's Stop hook calls
            # /usr/local/bin/rs-audit-stop which exists only in the
            # supervisor image; propagating it breaks every claude
            # session in the PI tab with a "command not found" Stop
            # hook error.
            jq 'del(.hooks)' /home/research/.claude/settings.json \
               > /workspace/.pi/{short}/.creds/settings.json
            chmod 600 /workspace/.pi/{short}/.creds/settings.json
        fi
        if [ -f /home/research/.claude.json ]; then
            # Filter ~/.claude.json to the PI allowlist before staging.
            # The supervisor's raw file carries `projects[<cwd>]` — per-
            # cwd prompt history, allowedTools, mcpContextUris — which
            # would leak into the PI's claude /resume UI if propagated
            # whole. Keep only the keys interactive `claude` needs to
            # skip /login. Same expression used by pi-creds-watch.sh
            # and rs-pi sync-creds at live-refresh time.
            jq '{{oauthAccount, userID, hasCompletedOnboarding, lastOnboardingVersion}} | with_entries(select(.value != null))' \
               /home/research/.claude.json \
               > /workspace/.pi/{short}/.creds/home_claude.json
            chmod 600 /workspace/.pi/{short}/.creds/home_claude.json
        fi
    """
    r = run(["docker", "exec", supervisor, "bash", "-eu", "-c", script],
            capture_output=True)
    if r.returncode != 0:
        die((r.stderr or r.stdout).strip()
            or f"failed to stage creds for pi role {role!r}")


def _pi_inner_exists(supervisor: str, role: str) -> bool:
    cname = pi_roles.role_container_name(role)
    r = run(["docker", "exec", supervisor,
             "docker", "inspect", cname], capture_output=True)
    return r.returncode == 0


def _pi_inner_running(supervisor: str, role: str) -> bool:
    cname = pi_roles.role_container_name(role)
    r = run(["docker", "exec", supervisor,
             "docker", "inspect", "-f", "{{.State.Running}}", cname],
            capture_output=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _pi_start(supervisor: str, project: str, cfg: "Config",
              role: str, *, force_restage: bool = False) -> None:
    """Run the PI container in the supervisor's inner dockerd. Idempotent:
    tears down any prior instance with the same name first so a stale
    crashed container doesn't block start. Lazy-stages the per-role image
    into the inner dockerd on first use; project create doesn't pre-stage
    PI images (most projects won't use them). Pass force_restage=True
    after a host-side image rebuild to push new content through."""
    workspace_path = workspace_path_for(project, cfg)
    entries = pi_roles.load_pi_roles(workspace_path)
    entry = entries.get(role)
    if entry is None:
        die(f"no pi-roles.json entry for role {role!r}; call enable first")

    pi_roles.validate_role(role)

    cname = pi_roles.role_container_name(role)
    run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
        capture_output=True)
    _pi_stage_creds(supervisor, role)

    image = entry.get("image") or pi_roles.PI_IMAGES[role]
    stage_worker_image(supervisor, image, force=force_restage)

    # Bind-mount layout (single-mount, RW):
    #   /workspace  ← <supervisor>/workspace/pi/<short>
    #     The PI's role workspace. role.md staged here on first boot
    #     by the entrypoint; PI's curated skills.md, sessions/, and
    #     per-role artifacts live here. NEVER includes the supervisor's
    #     /workspace/ contents — structural isolation by construction.
    #   /creds      ← <supervisor>/workspace/.pi/<short>/.creds (RO)
    #     Parent-dir bind-mount so the inotify watcher's atomic-rename
    #     writes (replacing .credentials.json) stay visible inside the
    #     container without re-mounting. The container's entrypoint
    #     copies /creds/.credentials.json into ~/.claude/ on first
    #     boot; later cred refreshes are propagated by pi-creds-watch
    #     using `docker cp` + `install` directly into the container's
    #     ~/.claude/ (the /creds RO mount is the seed, not the live
    #     view).
    # ``<short>`` is the role key with the ``pi-`` prefix stripped
    # (see ``_pi_stage_creds``'s path note).
    ip = entry["ip"]
    short = pi_roles.role_short(role)
    docker_args = [
        "docker", "exec", supervisor,
        "docker", "run", "-d",
        "--name", cname,
        "--network", INNER_NETWORK,
        "--ip", ip,
        "--restart", "unless-stopped",
        "-v", f"/workspace/pi/{short}:/workspace",
        "-v", f"/workspace/.pi/{short}/.creds:/creds:ro",
        # /etc/orchestrator is the supervisor's per-project state dir
        # (role-mcps.json, mcp-allow.json). PI containers that mirror a
        # worker-facing role-MCP (pi-wrangler, pi-librarian, …) read
        # this at entrypoint time to render their .mcp.json +
        # .tools-inventory.md from the same source the worker-facing
        # role uses. pi-echo (substrate fixture) skips that branch and
        # ignores the mount. Parent-dir bind-mount, same shape as the
        # role-MCP containers — atomic-rename writes by the host stay
        # visible (single-file-bind-mount rule).
        "-v", "/workspace/.orchestrator:/etc/orchestrator:ro",
        "-e", f"RS_PI_ROLE={short}",
        "--label", f"research.pi_role={role}",
        "--label", f"research.project={project}",
        # Project --data paths, propagated RO at the same mount points
        # the supervisor + workers see them at. Lets interactive
        # PI claude inspect project inputs without the supervisor
        # boundary punching extra holes elsewhere.
        *_data_mount_args_from_supervisor(supervisor),
        image,
    ]
    run_check(docker_args)
    print(f"pi role {role!r}: running at {ip}")


def _pi_stop(supervisor: str, role: str) -> None:
    """Stop + remove the PI container in the inner dockerd. Tolerates
    absence — caller may have already removed it via _recreate_supervisor
    or the supervisor itself may be down. Verifies removal before
    returning so the disable contract ("container is gone after this
    returns") holds even if a future docker version regresses `rm -f`."""
    cname = pi_roles.role_container_name(role)
    rm = run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
             capture_output=True)
    # `docker container inspect` (not bare `docker inspect`) — the bare
    # form falls through to image lookup when no container matches, and
    # the inner dockerd has an image tagged with the same name as the
    # container (`rs-pi-<role>:latest` vs `rs-pi-<role>` container), so
    # bare-inspect would always succeed and falsely report the container
    # as still present.
    check = run(["docker", "exec", supervisor,
                 "docker", "container", "inspect", cname],
                capture_output=True)
    if check.returncode == 0:
        rm_tail = (rm.stderr or rm.stdout or "").strip()[-200:]
        die(f"PI container {cname!r} still present after disable; "
            f"docker rm -f tail: {rm_tail!r}. Inspect "
            f"`docker exec {supervisor} docker container inspect {cname}` "
            f"manually and retry `research project pi disable`.")


def _pi_enable(project: str, cfg: "Config", role: str) -> None:
    """Write the per-project pi-roles.json entry + start the container.
    Idempotent — re-running on an already-enabled role rewrites the entry
    and restarts the container.

    PI roles whose short name matches a worker-facing role-MCP key
    (pi-wrangler ↔ wrangler, pi-librarian ↔ librarian, pi-websearcher ↔
    websearcher) MIRROR that role-MCP's upstream set — they read
    `role-mcps.json[<short>].upstream_mcps` at container start to render
    their `.mcp.json` + `.tools-inventory.md`. Such PI roles refuse to
    enable if the worker-facing role-MCP isn't enabled for the project:
    PI mode without a matching worker-facing entry would mean PI's claude
    sees an empty MCP list, which is a configuration error, not a usable
    PI tab. pi-echo (whose short name "echo" matches no role-MCP key)
    bypasses this gate — substrate-only fixture, no upstreams expected.
    """
    pi_roles.validate_role(role)
    supervisor = container_name_for(project)
    if not container_running(supervisor):
        die(f"project {project!r} is not running; bring it up first")

    workspace_path = workspace_path_for(project, cfg)

    # W10 (P.3 plan): if this PI role mirrors a worker-facing role-MCP,
    # require that role-MCP to be enabled per-project first. Failure
    # mode without this gate: pi-wrangler comes up with an empty
    # .tools-inventory.md, and the PI's claude session has no DB MCPs
    # to call — which feels like a bug in pi-wrangler but is actually
    # a missing dependency in the project's worker-facing wiring.
    short = pi_roles.role_short(role)
    if short in role_mcp.ROLE_IMAGES:
        role_mcp_entries = role_mcp.load_role_mcps(workspace_path)
        if short not in role_mcp_entries:
            die(
                f"pi role {role!r} mirrors worker-facing role-MCP "
                f"{short!r}, which is not enabled for project "
                f"{project!r}. Run `research project role-mcp enable "
                f"{project} {short} --upstream <mcp,...>` first, then "
                f"re-run this command. (PI mode reads its upstream set "
                f"from role-mcps.json so both surfaces share one source "
                f"of truth.)"
            )

    entries = pi_roles.load_pi_roles(workspace_path)
    entries[role] = pi_roles.build_entry(role)
    pi_roles.save_pi_roles(workspace_path, entries)

    _pi_start(supervisor, project, cfg, role)


def _pi_disable(project: str, cfg: "Config", role: str) -> None:
    """Stop the container, drop the pi-roles.json entry. Workspace state
    under /workspace/pi/<role>/ (role.md, skills.md, sessions/, …) and
    cred-stash under /workspace/.pi/<role>/ both survive — bind-mounts
    on the project volume are unaffected by docker rm. Re-enable picks
    up the preserved workspace."""
    supervisor = container_name_for(project)
    workspace_path = workspace_path_for(project, cfg)
    entries = pi_roles.load_pi_roles(workspace_path)
    if role not in entries:
        die(f"pi role {role!r} is not enabled for project {project!r}")
    if container_running(supervisor):
        _pi_stop(supervisor, role)
    del entries[role]
    pi_roles.save_pi_roles(workspace_path, entries)


def cmd_project_pi_enable(args: argparse.Namespace) -> None:
    cfg = load_config()
    _require_project(args.project)
    _pi_enable(args.project, cfg, args.role)


def cmd_project_pi_disable(args: argparse.Namespace) -> None:
    cfg = load_config()
    _require_project(args.project)
    _pi_disable(args.project, cfg, args.role)
    print(f"pi role {args.role!r}: disabled")


def cmd_project_pi_list(args: argparse.Namespace) -> None:
    cfg = load_config()
    supervisor = _require_project(args.project)
    workspace_path = workspace_path_for(args.project, cfg)
    entries = pi_roles.load_pi_roles(workspace_path)

    if args.json:
        out = []
        for role, e in sorted(entries.items()):
            row = dict(e)
            row["role"] = role
            row["running"] = (container_running(supervisor)
                              and _pi_inner_running(supervisor, role))
            out.append(row)
        print(json.dumps(out, indent=2, sort_keys=True))
        return

    if not entries:
        print(f"(project {args.project!r} has no pi roles enabled)")
        return

    rows: list[tuple[str, ...]] = []
    for role, e in sorted(entries.items()):
        running = "no"
        if container_running(supervisor):
            running = "yes" if _pi_inner_running(supervisor, role) else "no"
        rows.append((role, e.get("ip", "?"),
                     e.get("image", "?"), running))
    headers = ("ROLE", "IP", "IMAGE", "RUNNING")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))


def cmd_project_pi_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    supervisor = _require_project(args.project)
    workspace_path = workspace_path_for(args.project, cfg)
    entries = pi_roles.load_pi_roles(workspace_path)
    entry = entries.get(args.role)
    if entry is None:
        die(f"pi role {args.role!r} is not enabled for project "
            f"{args.project!r}")

    state = {
        "role": args.role,
        "project": args.project,
        "entry": entry,
        "container": pi_roles.role_container_name(args.role),
        "exists": False,
        "running": False,
    }
    if container_running(supervisor):
        state["exists"] = _pi_inner_exists(supervisor, args.role)
        state["running"] = _pi_inner_running(supervisor, args.role)

    role_workspace = workspace_path / "pi" / pi_roles.role_short(args.role)
    state["workspace"] = str(role_workspace)
    state["workspace_present"] = role_workspace.is_dir()
    if role_workspace.is_dir():
        state["workspace_files"] = sorted(
            p.name for p in role_workspace.iterdir() if not p.name.startswith(".")
        )
    else:
        state["workspace_files"] = []

    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return

    print(f"role:        {state['role']}")
    print(f"project:     {state['project']}")
    print(f"container:   {state['container']}")
    print(f"  exists:    {state['exists']}")
    print(f"  running:   {state['running']}")
    print(f"ip:          {entry.get('ip')}")
    print(f"image:       {entry.get('image')}")
    print(f"workspace:   {state['workspace']}"
          f" ({'present' if state['workspace_present'] else 'absent'})")
    if state['workspace_files']:
        print(f"  files:     {', '.join(state['workspace_files'])}")


def cmd_project_pi_sync_creds(args: argparse.Namespace) -> None:
    """Manual fallback for the inotify watcher: invoke the supervisor-side
    rs-pi CLI to re-stage the supervisor's current creds into every
    running PI container. The watcher is best-effort; this command is the
    explicit alternative when re-auth happened while the watcher was
    down, or to verify state."""
    supervisor = _require_project(args.project)
    r = run(["docker", "exec", supervisor, "rs-pi", "sync-creds"],
            capture_output=False)
    if r.returncode != 0:
        sys.exit(r.returncode)


# ---------------------------------------------------------------------------
# PI-isolated container lifecycle (STAGE_PI_ISOLATED)
# ---------------------------------------------------------------------------


def _pi_isolated_external_mounts(project: str) -> list[str]:
    """``-v`` args mounting each registered type's ``<root>/<project>/`` host
    folder at the supervisor's ``/external/<type>``. Computed fresh from the
    host registry at every supervisor create/recreate, so the supervisor's
    external mount set always tracks the current registry (a removed type
    drops out; a newly-added type appears on the next recreate). The per-
    project subdir is created host-side so docker doesn't auto-create it
    root-owned. ``~`` is expanded here; ``${VAR}`` was expanded by the
    registry loader."""
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


def _pi_isolated_stage_creds(supervisor: str, name: str) -> None:
    """Snapshot supervisor creds into ``.pi-isolated/<name>/.creds`` if the
    supervisor is authenticated, so a `claude` the PI later starts in the
    tab is already authed (skips /login). UNLIKE ``_pi_stage_creds``, a
    missing supervisor credential is NOT fatal here: an isolated agent is a
    plain sandbox that boots fine un-authed (it just clones + idles, and the
    tab is a login shell), and the PI can `/login` when they start claude,
    or rely on pi-creds-watch.sh propagating creds once the supervisor
    authenticates. The ``.creds`` dir is created either way so the RO
    bind-mount has a source. Four-key ~/.claude.json filter matches
    ``_pi_stage_creds``."""
    script = f"""
        set -e
        mkdir -p /workspace/.pi-isolated/{name}/.creds
        mkdir -p /workspace/pi-isolated/{name}
        if [ -f /home/research/.claude/.credentials.json ]; then
            cp /home/research/.claude/.credentials.json \
               /workspace/.pi-isolated/{name}/.creds/.credentials.json
            chmod 600 /workspace/.pi-isolated/{name}/.creds/.credentials.json
        else
            echo "pi-isolated[{name}]: supervisor unauthenticated; container "\
                 "will boot without creds (use /login in the tab, or "\
                 "authenticate the supervisor and re-sync)." >&2
        fi
        if [ -f /home/research/.claude/settings.json ]; then
            jq 'del(.hooks)' /home/research/.claude/settings.json \
               > /workspace/.pi-isolated/{name}/.creds/settings.json
            chmod 600 /workspace/.pi-isolated/{name}/.creds/settings.json
        fi
        if [ -f /home/research/.claude.json ]; then
            jq '{{oauthAccount, userID, hasCompletedOnboarding, lastOnboardingVersion}} | with_entries(select(.value != null))' \
               /home/research/.claude.json \
               > /workspace/.pi-isolated/{name}/.creds/home_claude.json
            chmod 600 /workspace/.pi-isolated/{name}/.creds/home_claude.json
        fi
    """
    r = run(["docker", "exec", supervisor, "bash", "-eu", "-c", script],
            capture_output=True)
    if r.returncode != 0:
        die((r.stderr or r.stdout).strip()
            or f"failed to stage creds for pi-isolated {name!r}")


def _pi_isolated_inner_running(supervisor: str, name: str) -> bool:
    cname = pi_isolated.container_name(name)
    r = run(["docker", "exec", supervisor,
             "docker", "inspect", "-f", "{{.State.Running}}", cname],
            capture_output=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _pi_isolated_inner_exists(supervisor: str, name: str) -> bool:
    cname = pi_isolated.container_name(name)
    r = run(["docker", "exec", supervisor,
             "docker", "container", "inspect", cname], capture_output=True)
    return r.returncode == 0


def _pi_isolated_start(supervisor: str, project: str, cfg: "Config",
                       name: str, *, force_restage: bool = False) -> None:
    """Run the PI-isolated container in the supervisor's inner dockerd.
    Idempotent: tears down any prior same-named container first. Requires
    the supervisor to already mount ``/external/<name>`` (the enable path
    guarantees this by recreating when absent) — the external folder is
    bind-mounted from there into the container at the type's configured
    mount."""
    workspace_path = workspace_path_for(project, cfg)
    entries = pi_isolated.load(workspace_path)
    entry = entries.get(name)
    if entry is None:
        die(f"no pi-isolated.json entry for {name!r}; call enable first")

    cname = pi_isolated.container_name(name)
    run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
        capture_output=True)
    _pi_isolated_stage_creds(supervisor, name)
    stage_worker_image(supervisor, PI_ISOLATED_IMAGE, force=force_restage)

    mount = entry.get("mount") or pi_isolated_registry.DEFAULT_MOUNT
    docker_args = [
        "docker", "exec", supervisor,
        "docker", "run", "-d",
        "--name", cname,
        "--network", INNER_NETWORK,
        "--ip", entry["ip"],
        "--restart", "unless-stopped",
        # Pi tree (RW) — the container's /workspace root. Structurally
        # isolated from the supervisor's /workspace by construction (separate
        # bind source).
        "-v", f"/workspace/pi-isolated/{name}:/workspace",
        # Cred seed (RO parent-dir mount; pi-creds-watch.sh refreshes live).
        "-v", f"/workspace/.pi-isolated/{name}/.creds:/creds:ro",
        # External host folder (<root>/<project>/) at the configured mount —
        # the PI's own content folder (e.g. a vault), kept clean. The repo is
        # cloned to /workspace/<repo-name>, NOT here. RS_PI_ISO_MOUNT (below)
        # exports this path so a SETUP command can point the harness at it.
        "-v", f"/external/{name}:{mount}",
        "-e", f"RS_PI_ISO_NAME={name}",
        "-e", f"RS_PI_ISO_REPO={entry.get('repo') or ''}",
        "-e", f"RS_PI_ISO_REF={entry.get('ref') or ''}",
        "-e", f"RS_PI_ISO_SETUP={entry.get('setup') or ''}",
        "-e", f"RS_PI_ISO_MOUNT={mount}",
        # research.pi_role label is the cred-propagation opt-in marker
        # (pi-creds-watch.sh + rs-pi sync-creds filter on this label key,
        # not a name glob), so isolated containers get cred fan-out free.
        "--label", f"research.pi_role=iso-{name}",
        "--label", f"research.pi_isolated={name}",
        "--label", f"research.project={project}",
        *_data_mount_args_from_supervisor(supervisor),
        PI_ISOLATED_IMAGE,
    ]
    run_check(docker_args)
    print(f"pi-isolated {name!r}: running at {entry['ip']}")


def _pi_isolated_stop(supervisor: str, name: str) -> None:
    """Stop + remove the inner container. Tolerates absence. Verifies removal
    with `docker container inspect` (not bare inspect, which falls through to
    the same-named image)."""
    cname = pi_isolated.container_name(name)
    rm = run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
             capture_output=True)
    check = run(["docker", "exec", supervisor,
                 "docker", "container", "inspect", cname], capture_output=True)
    if check.returncode == 0:
        rm_tail = (rm.stderr or rm.stdout or "").strip()[-200:]
        die(f"pi-isolated container {cname!r} still present after disable; "
            f"docker rm -f tail: {rm_tail!r}.")


def _pi_isolated_enable(project: str, cfg: "Config", name: str) -> None:
    """Snapshot the registry type into pi-isolated.json + start the
    container. Idempotent. Recreates the supervisor first if it doesn't yet
    mount ``/external/<name>`` (type registered after the project's last
    create/recreate) — the recreate re-enumerates the registry, adds the
    mount, and its restart loop brings up this agent. Otherwise starts the
    inner container directly (no recreate)."""
    supervisor = container_name_for(project)
    if not container_running(supervisor):
        die(f"project {project!r} is not running; bring it up first")

    type_entry = pi_isolated_registry.entry_for(name, expand=True)
    if type_entry is None:
        die(f"no pi-isolated type named {name!r} in the host registry. "
            f"Register it first: `research pi-isolated add {name} "
            f"--root <host-dir> [--repo <url> --ref <sha>]`")

    workspace_path = workspace_path_for(project, cfg)
    entries = pi_isolated.load(workspace_path)
    try:
        ip = pi_isolated.allocate_ip(entries, name)
    except ValueError as e:
        die(str(e))
    entries[name] = pi_isolated.build_entry(name, type_entry, ip)
    pi_isolated.save(workspace_path, entries)

    if not _supervisor_has_external_mount(supervisor, name):
        print(f"pi-isolated {name!r}: supervisor not yet mounting "
              f"/external/{name}; recreating supervisor to wire the external "
              f"folder (creds + workspace survive)...")
        _recreate_supervisor(project, cfg)
        return  # recreate's restart loop starts the container

    _pi_isolated_start(supervisor, project, cfg, name)


def _pi_isolated_disable(project: str, cfg: "Config", name: str) -> None:
    """Stop the container, drop the pi-isolated.json entry. The external
    host folder (<root>/<project>/, which holds the cloned repo), the pi
    tree workspace, and the creds stash all survive. The supervisor's
    /external/<name> mount stays (recomputed from the registry at each
    recreate); harmless when the type is still registered."""
    supervisor = container_name_for(project)
    workspace_path = workspace_path_for(project, cfg)
    entries = pi_isolated.load(workspace_path)
    if name not in entries:
        die(f"pi-isolated {name!r} is not enabled for project {project!r}")
    if container_running(supervisor):
        _pi_isolated_stop(supervisor, name)
    del entries[name]
    pi_isolated.save(workspace_path, entries)


def cmd_project_pi_isolated_enable(args: argparse.Namespace) -> None:
    cfg = load_config()
    _require_project(args.project)
    _pi_isolated_enable(args.project, cfg, args.name)


def cmd_project_pi_isolated_disable(args: argparse.Namespace) -> None:
    cfg = load_config()
    _require_project(args.project)
    _pi_isolated_disable(args.project, cfg, args.name)
    print(f"pi-isolated {args.name!r}: disabled")


def cmd_project_pi_isolated_list(args: argparse.Namespace) -> None:
    cfg = load_config()
    supervisor = _require_project(args.project)
    entries = pi_isolated.load(workspace_path_for(args.project, cfg))
    if args.json:
        out = []
        for name, e in sorted(entries.items()):
            row = dict(e)
            row["name"] = name
            row["running"] = (container_running(supervisor)
                              and _pi_isolated_inner_running(supervisor, name))
            out.append(row)
        print(json.dumps(out, indent=2, sort_keys=True))
        return
    if not entries:
        print(f"(project {args.project!r} has no pi-isolated agents enabled)")
        return
    rows: list[tuple[str, ...]] = []
    for name, e in sorted(entries.items()):
        running = "no"
        if container_running(supervisor):
            running = "yes" if _pi_isolated_inner_running(supervisor, name) else "no"
        rows.append((name, e.get("ip", "?"), e.get("mount", "?"),
                     e.get("repo") or "-", running))
    headers = ("NAME", "IP", "MOUNT", "REPO", "RUNNING")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))


def cmd_project_pi_isolated_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    supervisor = _require_project(args.project)
    workspace_path = workspace_path_for(args.project, cfg)
    entries = pi_isolated.load(workspace_path)
    entry = entries.get(args.name)
    if entry is None:
        die(f"pi-isolated {args.name!r} is not enabled for project "
            f"{args.project!r}")
    state = {
        "name": args.name,
        "project": args.project,
        "entry": entry,
        "container": pi_isolated.container_name(args.name),
        "exists": False,
        "running": False,
        "supervisor_external_mount": False,
    }
    if container_running(supervisor):
        state["exists"] = _pi_isolated_inner_exists(supervisor, args.name)
        state["running"] = _pi_isolated_inner_running(supervisor, args.name)
        state["supervisor_external_mount"] = _supervisor_has_external_mount(
            supervisor, args.name)
    ws = workspace_path / "pi-isolated" / args.name
    state["workspace"] = str(ws)
    state["workspace_present"] = ws.is_dir()
    # The repo is cloned visibly into the pi tree root as
    # /workspace/<repo-name> (container view) = <ws>/<repo-name> (host view).
    repo = entry.get("repo")
    clone_dir = None
    if repo:
        repo_name = repo.rstrip("/").rsplit("/", 1)[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        clone_dir = ws / repo_name
    state["clone_dir"] = str(clone_dir) if clone_dir else None
    state["repo_cloned"] = bool(clone_dir and (clone_dir / ".git").is_dir())

    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return
    print(f"name:        {state['name']}")
    print(f"project:     {state['project']}")
    print(f"container:   {state['container']}")
    print(f"  exists:    {state['exists']}")
    print(f"  running:   {state['running']}")
    print(f"ip:          {entry.get('ip')}")
    print(f"repo:        {entry.get('repo') or '(none)'}")
    print(f"ref:         {entry.get('ref') or '(none)'}")
    print(f"mount:       {entry.get('mount')}")
    print(f"root:        {entry.get('root')}")
    print(f"external mount on supervisor: {state['supervisor_external_mount']}")
    print(f"workspace:   {state['workspace']}"
          f" ({'present' if state['workspace_present'] else 'absent'})")
    print(f"clone dir:   {state['clone_dir']}")
    print(f"  repo cloned: {state['repo_cloned']}")


def _registered_pi_isolated_types() -> set[str]:
    """Names in the host PI-isolated registry, or empty on load failure (a
    malformed registry shouldn't break `project create`; the dedicated
    `pi-isolated` subcommands surface the error)."""
    try:
        return set(pi_isolated_registry.load(expand=False)["types"])
    except pi_isolated_registry.RegistryError:
        return set()


def _split_enable_tokens(
    enable_arg: str | None,
) -> tuple[str | None, list[str], list[str], list[str], bool]:
    """Split ``--enable`` value into (service_csv, role_mcp_roles,
    pi_roles, pi_isolated_types, no_pi_mirror).

    Tokens are matched against four sets in order, all using their
    canonical names (no prefix sugar):
      - a key in ``role_mcp.ROLE_IMAGES`` (e.g. ``wrangler``,
        ``echo-mcp``) peels into the role-mcp list,
      - a key in ``pi_roles.PI_IMAGES`` (e.g. ``pi-wrangler``,
        ``pi-echo``) peels into the pi list,
      - a name in the host PI-isolated registry peels into the
        pi-isolated list,
      - the bare sentinel ``no-pi-mirror`` suppresses PI-mirror auto-
        enable for every role-mcp token in the same ``--enable`` value
        (it applies to all roles, not per-role — per-role granularity is
        available via ``research project role-mcp enable --no-pi-mirror``),
      - anything else stays for ``_compute_service_flags`` to interpret
        as a service id.

    A name that lives in more than one of these surfaces (role-MCP / PI /
    PI-isolated / service) is a configuration error in this codebase, not
    an operator problem; the registry tables are the source of truth and
    authors/operators must keep names disjoint. The parser doesn't try to
    disambiguate.

    Empty service set returns None to keep the default (all services
    enabled / inherit prior flags) intact."""
    if not enable_arg:
        return None, [], [], [], False
    iso_types = _registered_pi_isolated_types()
    services: list[str] = []
    role_mcps: list[str] = []
    pi: list[str] = []
    pi_isolated_types: list[str] = []
    no_pi_mirror = False
    for tok in enable_arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok == "no-pi-mirror":
            no_pi_mirror = True
        elif tok in role_mcp.ROLE_IMAGES:
            role_mcps.append(tok)
        elif tok in pi_roles.PI_IMAGES:
            pi.append(tok)
        elif tok in iso_types:
            pi_isolated_types.append(tok)
        else:
            services.append(tok)
    svc_csv = ",".join(services) if services else None
    return svc_csv, role_mcps, pi, pi_isolated_types, no_pi_mirror


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


def _split_disable_tokens(
    disable_arg: str | None,
) -> tuple[str | None, list[str], list[str]]:
    """Mirror of `_split_enable_tokens` for the disable side: (service_csv,
    pi_roles, pi_isolated_types). Tokens of the form ``pi-<role>`` peel off
    into the pi list; tokens in the host PI-isolated registry peel into the
    pi-isolated list; everything else stays for `_compute_service_flags`.
    role-mcps are not disabled via ``--disable`` (they have their own
    ``role-mcp disable`` subcommand), so no role-mcp branch here."""
    if not disable_arg:
        return None, [], []
    iso_types = _registered_pi_isolated_types()
    services: list[str] = []
    pi: list[str] = []
    pi_isolated_types: list[str] = []
    for tok in disable_arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("pi-"):
            pi.append(tok)
        elif tok in iso_types:
            pi_isolated_types.append(tok)
        else:
            services.append(tok)
    svc_csv = ",".join(services) if services else None
    return svc_csv, pi, pi_isolated_types


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


def cmd_webui_cert_tailscale() -> None:
    """Stage a Tailscale-issued Let's Encrypt cert into rs-webui-tls,
    replacing the auto-generated self-signed cert. Browser ServiceWorker
    registration (markdown preview, notebook output rendering, Data
    Wrangler webview, …) requires a publicly-trusted cert; Tailscale
    serves this for free on tailnet names with zero per-device CA
    install."""
    if shutil.which("tailscale") is None:
        die("tailscale CLI not on PATH. Install Tailscale first: "
            "https://tailscale.com/download")
    fqdn = _detect_tailscale_fqdn()
    if not fqdn:
        die("could not detect tailnet FQDN. Verify with `tailscale status` "
            "that the daemon is running and the host is registered.")

    print(f"requesting Tailscale cert for {fqdn}...")
    with tempfile.TemporaryDirectory() as td:
        cert_path = Path(td) / "cert.pem"
        key_path = Path(td) / "key.pem"
        r = run([
            "tailscale", "cert",
            "--cert-file", str(cert_path),
            "--key-file", str(key_path),
            fqdn,
        ], capture_output=True)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()
            low = err.lower()
            if "https" in low and ("not enabled" in low or "not configured" in low):
                die("HTTPS is not enabled for your tailnet. Toggle it on at "
                    "https://login.tailscale.com/admin/dns then re-run.\n"
                    f"  tailscale: {err}")
            if "operator" in low or "permission" in low or "denied" in low:
                die("`tailscale cert` requires sudo unless you're the "
                    "configured tailscaled operator. One-time fix:\n"
                    "  sudo tailscale up --operator=$USER\n"
                    f"(raw error: {err})")
            die(f"tailscale cert failed: {err}")
        cert_pem = cert_path.read_bytes()
        key_pem = key_path.read_bytes()

    _stage_webui_cert(cert_pem, key_pem, provider=f"tailscale:{fqdn}")
    print(f"staged Tailscale cert for {fqdn} into rs-webui-tls")

    if container_running(WEBUI_CONTAINER):
        print("recreating webui to pick up new cert...")
        _webui_recreate_in_place()

    bind = read_env_value("WEBUI_BIND") or "127.0.0.1"
    port = read_env_value("WEBUI_PORT") or "7777"
    print()
    if bind in ("127.0.0.1", "::1", "localhost"):
        print(f"  note: WEBUI_BIND={bind} only accepts loopback connections")
        print(f"  to reach via tailnet, run:")
        print(f"    research webui start --bind 0.0.0.0")
        print(f"  then open https://{fqdn}:{port}/")
    elif bind == "0.0.0.0":
        print(f"  open https://{fqdn}:{port}/ — cert is publicly trusted")
    else:
        print(f"  webui bound to {bind}; open https://{fqdn}:{port}/ to use "
              "the trusted cert")
        print(f"  (connections via {bind} directly will see a name-mismatch "
              f"warning — the cert covers {fqdn} only)")


def cmd_webui(args: argparse.Namespace) -> None:
    """Manage the optional webui container (start | stop | status | import |
    cert-tailscale)."""
    action = args.webui_action
    if action == "cert-tailscale":
        cmd_webui_cert_tailscale()
        return
    port = read_env_value("WEBUI_PORT") or "7777"
    bind = read_env_value("WEBUI_BIND") or "127.0.0.1"

    if action == "import":
        target = getattr(args, "project", None)
        if target:
            container = container_name_for(target)
            if not container_exists(container):
                die(f"project {target!r} does not exist")
            ssh_pass = _supervisor_ssh_pass(container)
            if not ssh_pass:
                die(f"could not read SSH password from {container}")
            print(_webui_import_string(target, ssh_pass))
            return
        rows: list[tuple[str, str]] = []
        for c in get_supervisor_containers():
            sp = _supervisor_ssh_pass(c["name"])
            if sp:
                rows.append((c["project"], _webui_import_string(c["project"], sp)))
        if not rows:
            print("no projects with SSH info available.")
            return
        name_w = max(len(p) for p, _ in rows)
        for p, s in rows:
            print(f"{p.ljust(name_w)}  {s}")
        return

    if action == "start":
        new_bind = getattr(args, "bind", None)
        new_port = getattr(args, "port", None)
        rebuild = getattr(args, "rebuild", False)
        recreate = False
        if new_bind and new_bind != bind:
            update_env_key("WEBUI_BIND", new_bind)
            bind = new_bind
            recreate = True
        if new_port and new_port != port:
            update_env_key("WEBUI_PORT", new_port)
            port = new_port
            recreate = True
        os.environ["WEBUI_BIND"] = bind
        os.environ["WEBUI_PORT"] = port
        if (recreate or rebuild) and container_exists(WEBUI_CONTAINER):
            reason = "bind/port changed" if recreate else "rebuild requested"
            print(f"{reason}; recreating webui...")
            run(["docker", "rm", "-f", WEBUI_CONTAINER], capture_output=True)

        if not rebuild and container_running(WEBUI_CONTAINER):
            wire_webui_to_projects()
            print(f"webui already running at https://{bind}:{port}")
            return
        if container_exists(WEBUI_CONTAINER):
            run(["docker", "rm", "-f", WEBUI_CONTAINER], capture_output=True)
        if rebuild or not run_quiet(["docker", "image", "inspect", WEBUI_IMAGE]):
            print("building webui image...")
            docker_compose("--profile", "webui", "build", "webui")
        print(f"starting webui (bind {bind}:{port})...")
        docker_compose("--profile", "webui", "up", "-d", "webui")
        wire_webui_to_projects()
        print(f"webui:  https://{bind}:{port}")
        print(f"  (self-signed cert — your browser will warn on first visit; click through)")
        return

    if action == "stop":
        if not container_exists(WEBUI_CONTAINER):
            print("webui not running.")
            return
        print("stopping webui...")
        run(["docker", "rm", "-f", WEBUI_CONTAINER], capture_output=True)
        print("webui stopped.")
        return

    if action == "status":
        if container_running(WEBUI_CONTAINER):
            print(f"webui: running")
            print(f"  https://{bind}:{port}")
        elif container_exists(WEBUI_CONTAINER):
            print("webui: stopped (container exists)")
        else:
            print(f"webui: not running  (configured bind {bind}:{port})")
        return


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="research", description="Research Sandbox CLI")
    sub = p.add_subparsers(dest="command", required=True)

    st = sub.add_parser("start", help="start shared infra (router); build images if missing")
    st.add_argument("--rebuild", action="store_true",
                    help="force-rebuild supervisor + worker images even if they exist")
    st.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="stop shared infra (router)")
    sp.set_defaults(func=cmd_stop)

    proj = sub.add_parser("project", help="per-project operations")
    proj_sub = proj.add_subparsers(dest="subcommand", required=True)

    c = proj_sub.add_parser("create", help="create a new project")
    c.add_argument("name")
    c.add_argument("--data", metavar="PATHS",
                   help="comma-separated host paths, each mounted RO at "
                        "/workspace/shared/data/<basename>/ (e.g. "
                        "`--data /home/me/raw,/srv/parsed` lands as "
                        "/workspace/shared/data/raw and .../parsed). "
                        "Missing paths are mkdir -p'd. Basename collisions "
                        "across the list are a hard error.")
    c.add_argument("--profile", default="python", help="supervisor image profile")
    c.add_argument("--dind", choices=["auto", "sysbox", "privileged"],
                   help="DIND mode (default: auto — sysbox if available, else privileged)")
    c.add_argument("--memory", help="memory limit (e.g. 8g)")
    c.add_argument("--cpus", help="cpu limit (e.g. 4)")
    c.add_argument("--egress", choices=["open", "locked"],
                   help="router egress policy (default from .env, usually open)")
    c.add_argument("--ssh-port", type=int, help="explicit SSH host port")
    c.add_argument("--inner-firewall", action="store_true",
                   help="enable defense-in-depth iptables ACL on the supervisor's "
                        "rs-inner bridge (workers can only reach mcp-proxy + DNS)")
    c.add_argument("--enable", metavar="IDS",
                   help=f"comma-separated tokens to force-enable. Tokens "
                        f"are matched by name against three registries: "
                        f"services ({','.join(KNOWN_SERVICES)}), role-MCPs "
                        f"({','.join(sorted(role_mcp.ROLE_IMAGES))}), and "
                        f"PI roles ({','.join(sorted(pi_roles.PI_IMAGES))}). "
                        f"Role-MCP tokens are sugar for `research project "
                        f"role-mcp enable`; PI-role tokens are sugar for "
                        f"`research project pi enable`. The bare sentinel "
                        f"`no-pi-mirror` in this list suppresses the "
                        f"matching `pi-<role>` auto-enable for every "
                        f"role-MCP token.")
    c.add_argument("--role-mcp-upstream", metavar="ROLE=CSV",
                   action="append",
                   help="repeatable: pin an explicit upstream list for a "
                        "role-mcp activated via `--enable <role>` "
                        "(e.g. `--role-mcp-upstream wrangler=postgres-mcp"
                        ",mongo-mcp`). Without this flag, the role-mcp "
                        "auto-derives upstreams from the registry × allow "
                        "intersection. Empty CSV (e.g. `wrangler=`) means "
                        "explicit no-upstreams.")
    c.add_argument("--disable", metavar="IDS",
                   help="comma-separated service ids to disable "
                        "(default: all known services enabled; supervisor "
                        "is always-on and cannot be disabled). Also "
                        "accepts pi-<role> tokens for `research project "
                        "pi disable`")
    c.add_argument("--mcp", metavar="NAMES", default="all-enabled",
                   help="MCPs to auto-allow at create time: 'all-enabled' "
                        "(default — every currently-enabled MCP), 'none', or a "
                        "comma-separated list of registry names. Per-MCP failures "
                        "(disabled, container not running, host unreachable) print "
                        "a warning and skip; the project still comes up. Add or "
                        "remove later with `research project mcp allow|deny|sync`.")
    c.set_defaults(func=cmd_project_create)

    a = proj_sub.add_parser("attach", help="docker exec + byobu attach")
    a.add_argument("name")
    a.set_defaults(func=cmd_project_attach)

    ls = proj_sub.add_parser("list", help="list all projects")
    ls.set_defaults(func=cmd_project_list)

    st = proj_sub.add_parser("status", help="show project status")
    st.add_argument("name")
    st.set_defaults(func=cmd_project_status)

    for op in ("stop", "start"):
        sp = proj_sub.add_parser(op, help=f"{op} the supervisor")
        g = sp.add_mutually_exclusive_group(required=True)
        g.add_argument("name", nargs="?")
        g.add_argument("--all", action="store_true")
        sp.set_defaults(func=(cmd_project_stop if op == "stop" else cmd_project_start))

    d = proj_sub.add_parser("destroy", help="remove container + volume + network")
    d.add_argument("name")
    d.set_defaults(func=cmd_project_destroy)

    u = proj_sub.add_parser(
        "update",
        help="push edited code into a project; preserves workspace + creds",
        description="Recreates the supervisor container (rm + create + start; "
                    "the only safe shape on sysbox), preserving workspace, "
                    "creds, network, SSH port, env, mounts, memory/CPU limits. "
                    "Without --rebuild, edited supervisor-baked files (cli/, "
                    "container/supervisor/*, container/analysis/CLAUDE.md.template, "
                    "agent/entrypoint.supervisor.sh) are docker-cp'd into the "
                    "freshly-created container before its first start. With "
                    "--rebuild, all three images are rebuilt first and the "
                    "new container comes from the rebuilt image. After the "
                    "recreate, /workspace/.claude/ template copies (CLAUDE.md, "
                    "slash commands, logbook templates) are refreshed from "
                    "/opt/claude-templates/* unless --keep-claude is given.",
    )
    u.add_argument("name")
    u.add_argument("--rebuild", action="store_true",
                   help="rebuild images first (required for Dockerfile / "
                        "mcp-proxy / worker-image changes)")
    u.add_argument("--keep-claude", action="store_true",
                   help="preserve the project's /workspace/.claude/* files "
                        "instead of overwriting them with the latest "
                        "templates (default: refresh, so role-doc and "
                        "slash-command edits propagate)")
    u.add_argument("--enable", metavar="IDS",
                   help=f"comma-separated tokens to enable. Matched by "
                        f"name against services ({','.join(KNOWN_SERVICES)}), "
                        f"role-MCPs ({','.join(sorted(role_mcp.ROLE_IMAGES))}), "
                        f"and PI roles ({','.join(sorted(pi_roles.PI_IMAGES))}). "
                        f"Bare sentinel `no-pi-mirror` suppresses the "
                        f"matching `pi-<role>` auto-enable for every "
                        f"role-MCP token.")
    u.add_argument("--role-mcp-upstream", metavar="ROLE=CSV",
                   action="append",
                   help="repeatable: pin an explicit upstream list for a "
                        "role-mcp activated via `--enable <role>` "
                        "(see `project create --help` for full semantics).")
    u.add_argument("--disable", metavar="IDS",
                   help="comma-separated service ids to disable "
                        "(supervisor is always-on and cannot be "
                        "disabled). Also accepts pi-<role> tokens to "
                        "disable a PI role container (workspace "
                        "preserved).")
    u.set_defaults(func=cmd_project_update)

    sh = proj_sub.add_parser("ssh", help="print SSH connection info")
    sh.add_argument("name")
    sh.set_defaults(func=cmd_project_ssh)

    pm = proj_sub.add_parser("mcp",
                             help="per-project MCP allowlist (list / allow / deny / sync)")
    pm_sub = pm.add_subparsers(dest="mcp_action", required=True)

    pml = pm_sub.add_parser("list",
                            help="show every MCP allowed for the project, "
                                 "with kind / enabled / running / reachable / description")
    pml.add_argument("project")
    pml.add_argument("--json", action="store_true")
    pml.set_defaults(func=cmd_project_mcp_list)

    pma = pm_sub.add_parser("allow",
                            help="grant the project access to one or more registered MCPs "
                                 "(per-MCP failures warn + skip; supervisor reload runs once)")
    pma.add_argument("project")
    pma.add_argument("mcp", nargs="+")
    pma.set_defaults(func=cmd_project_mcp_allow)

    pmd = pm_sub.add_parser("deny",
                            help="revoke the project's access to one or more allowed MCPs")
    pmd.add_argument("project")
    pmd.add_argument("mcp", nargs="+")
    pmd.set_defaults(func=cmd_project_mcp_deny)

    pms = pm_sub.add_parser("sync",
                            help="reconcile the project's allowlist against the registry: "
                                 "add every newly-enabled MCP, remove any that are no longer "
                                 "enabled or have been deregistered")
    pms.add_argument("project")
    pms.set_defaults(func=cmd_project_mcp_sync)

    # ----- Role-MCP lifecycle (B.0) --------------------------------------
    rm = proj_sub.add_parser(
        "role-mcp",
        help="per-project role-MCP lifecycle "
             "(enable / disable / list / status). Role-MCPs are project-"
             "internal orchestration containers, distinct from the general "
             "MCP registry — they route through the same mcp-proxy but "
             "live in role-mcps.json, not mcp-allow.json.",
    )
    rm_sub = rm.add_subparsers(dest="role_action", required=True)

    rme = rm_sub.add_parser("enable",
                            help="bring up a role-MCP container in the "
                                 "supervisor's inner dockerd")
    rme.add_argument("project")
    rme.add_argument("role",
                     help="role name (e.g. echo-mcp). Must be one "
                          "research.py knows about — see cli/role_mcp.py "
                          "ROLE_IPS / ROLE_IMAGES.")
    rme_src = rme.add_mutually_exclusive_group()
    rme_src.add_argument("--upstream",
                         help="comma-separated upstream MCP names the role-"
                              "worker's spawned `claude -p` will see (must "
                              "already be allowed via `project mcp allow`). "
                              "Pins the entry as `upstream_source=explicit` "
                              "so `project mcp sync` leaves it alone. Pass "
                              "an empty string ('') for an explicit no-"
                              "upstreams entry.")
    rme_src.add_argument("--auto", action="store_true",
                         help="(re-)derive the upstream set from the "
                              "registry × allow intersection (MCPs with "
                              "matching `roles`) and mark the entry as "
                              "`upstream_source=auto` so `project mcp sync` "
                              "keeps it current. Use this to flip a pinned "
                              "entry back to auto-derive.")
    rme.add_argument("--no-pi-mirror", action="store_true",
                     help="suppress the matching PI mirror's auto-enable. "
                          "Default: when a `pi-<role>` image exists, the "
                          "matching PI-role container is enabled in lockstep.")
    rme.add_argument("--memory",
                     help="per-role-MCP container memory cap (docker syntax, "
                          "e.g. 4g). Persists in role-mcps.json and survives "
                          "_recreate_supervisor. Default from "
                          "DEFAULT_ROLE_MCP_MEMORY in .env (2g if unset).")
    rme.add_argument("--max-concurrent-calls", type=int,
                     help="daemon-side cap on in-flight send_job calls; "
                          "beyond this the daemon returns an MCP tool "
                          "error with structured concurrency_limit payload "
                          "immediately (no spawn). 0 disables the cap. "
                          "Default from DEFAULT_ROLE_MCP_MAX_CONCURRENT_CALLS "
                          "in .env (3 if unset).")
    rme.set_defaults(func=cmd_project_role_mcp_enable)

    rmd = rm_sub.add_parser("disable",
                            help="stop + remove the role-MCP container "
                                 "(workspace state under shared/<role> "
                                 "survives)")
    rmd.add_argument("project")
    rmd.add_argument("role")
    rmd.set_defaults(func=cmd_project_role_mcp_disable)

    rml = rm_sub.add_parser("list",
                            help="show every role-MCP enabled for the "
                                 "project, with running state + upstreams")
    rml.add_argument("project")
    rml.add_argument("--json", action="store_true")
    rml.set_defaults(func=cmd_project_role_mcp_list)

    rms = rm_sub.add_parser("status",
                            help="deep-print one role-MCP's container "
                                 "state, watermarks, and per-caller call "
                                 "counts")
    rms.add_argument("project")
    rms.add_argument("role")
    rms.add_argument("--json", action="store_true")
    rms.set_defaults(func=cmd_project_role_mcp_status)

    # ----- PI-role lifecycle (STAGE_BACKEND_PI P.0) ----------------------
    pi = proj_sub.add_parser(
        "pi",
        help="per-project PI-role container lifecycle "
             "(enable / disable / list / status / sync-creds). PI roles "
             "are interactive per-project containers accessed via webui "
             "tabs (or via the Supervisor tab + `rs-pi` CLI); they "
             "live in pi-roles.json, distinct from role-mcps.json.",
    )
    pi_sub = pi.add_subparsers(dest="pi_action", required=True)

    pie = pi_sub.add_parser("enable",
                            help="bring up a PI role container in the "
                                 "supervisor's inner dockerd")
    pie.add_argument("project")
    pie.add_argument("role",
                     help="pi role name (e.g. pi-echo). Must be one "
                          "research.py knows about — see cli/pi.py "
                          "PI_IPS / PI_IMAGES.")
    pie.set_defaults(func=cmd_project_pi_enable)

    pid = pi_sub.add_parser("disable",
                            help="stop + remove the PI role container "
                                 "(workspace under pi/<role>/ survives)")
    pid.add_argument("project")
    pid.add_argument("role")
    pid.set_defaults(func=cmd_project_pi_disable)

    pil = pi_sub.add_parser("list",
                            help="show every PI role enabled for the "
                                 "project, with running state")
    pil.add_argument("project")
    pil.add_argument("--json", action="store_true")
    pil.set_defaults(func=cmd_project_pi_list)

    pis = pi_sub.add_parser("status",
                            help="deep-print one PI role's container "
                                 "state + workspace presence")
    pis.add_argument("project")
    pis.add_argument("role")
    pis.add_argument("--json", action="store_true")
    pis.set_defaults(func=cmd_project_pi_status)

    pisc = pi_sub.add_parser("sync-creds",
                             help="manually re-propagate supervisor "
                                  "creds into every running PI role "
                                  "container (fallback for the inotify "
                                  "watcher)")
    pisc.add_argument("project")
    pisc.set_defaults(func=cmd_project_pi_sync_creds)

    # ----- per-project PI-isolated lifecycle (STAGE_PI_ISOLATED) --------
    piso = proj_sub.add_parser(
        "pi-isolated",
        help="per-project PI-isolated agent lifecycle "
             "(enable / disable / list / status). Isolated agents clone a "
             "skill repo defined in the host `pi-isolated` registry and mount "
             "<root>/<project>/ from the host; they live in pi-isolated.json, "
             "distinct from pi-roles.json and role-mcps.json.",
    )
    piso_sub = piso.add_subparsers(dest="pi_isolated_action", required=True)

    pie2 = piso_sub.add_parser("enable",
                               help="snapshot a registered type into the "
                                    "project + start its container (recreates "
                                    "the supervisor if the external folder "
                                    "isn't mounted yet)")
    pie2.add_argument("project")
    pie2.add_argument("name", help="pi-isolated type name (see "
                                   "`research pi-isolated list`)")
    pie2.set_defaults(func=cmd_project_pi_isolated_enable)

    pid2 = piso_sub.add_parser("disable",
                               help="stop + remove the container (the external "
                                    "folder with the cloned repo, and the pi "
                                    "workspace, survive)")
    pid2.add_argument("project")
    pid2.add_argument("name")
    pid2.set_defaults(func=cmd_project_pi_isolated_disable)

    pil2 = piso_sub.add_parser("list",
                               help="show every pi-isolated agent enabled for "
                                    "the project, with running state")
    pil2.add_argument("project")
    pil2.add_argument("--json", action="store_true")
    pil2.set_defaults(func=cmd_project_pi_isolated_list)

    pis2 = piso_sub.add_parser("status",
                               help="deep-print one agent's container state + "
                                    "workspace/clone presence")
    pis2.add_argument("project")
    pis2.add_argument("name")
    pis2.add_argument("--json", action="store_true")
    pis2.set_defaults(func=cmd_project_pi_isolated_status)

    # ----- MCP registry (Stage 2.1) -------------------------------------
    mcp = sub.add_parser("mcp", help="MCP registry operations")
    mcp_sub = mcp.add_subparsers(dest="subcommand", required=True)

    a = mcp_sub.add_parser("add", help="register an MCP in the host registry")
    a.add_argument("name")
    a.add_argument("--kind", choices=("external", "shared"), required=True)
    a.add_argument("--transport", choices=("http", "sse"), default="http")
    a.add_argument("--host", metavar="HOST:PORT",
                   help="(external) HOST:PORT the supervisor reaches the MCP at "
                        "(use host.docker.internal:<port> for a service on the docker host)")
    a.add_argument("--header", action="append", default=[],
                   metavar="K=V",
                   help="(external) HTTP header to inject (repeatable)")
    a.add_argument("--image",
                   help="(shared) docker image of the MCP server")
    a.add_argument("--port", type=int,
                   help="(shared) port the MCP listens on inside its container")
    a.add_argument("--env", action="append", default=[],
                   metavar="K=V",
                   help="(shared) env var passed to the MCP container (repeatable)")
    a.add_argument("--path", default=mcp_registry.DEFAULT_PATH,
                   help=f"upstream URL path the MCP listens on "
                        f"(default {mcp_registry.DEFAULT_PATH!r}, the SDK convention)")
    a.add_argument("--description",
                   help="PI-authored project-level intent for what this MCP "
                        "gives access to (e.g. 'parsed event aggregates'); "
                        "propagates into per-project allowlists and into each "
                        "worker's CLAUDE.md when granted")
    a.add_argument("--roles", metavar="CSV",
                   help=f"comma-separated role-MCP affinities — declares "
                        f"that this MCP serves the named role-MCP(s) as an "
                        f"upstream. Used by `--enable <role>` (sugar for "
                        f"`project role-mcp enable`) and `project mcp sync` "
                        f"to auto-wire the per-project upstream set. Known "
                        f"roles: "
                        f"{','.join(sorted(role_mcp.ROLE_IMAGES)) or '(none)'}")
    a.set_defaults(func=cmd_mcp_add)

    md_d = mcp_sub.add_parser("describe",
                              help="set or clear an MCP's description")
    md_d.add_argument("name")
    g = md_d.add_mutually_exclusive_group(required=True)
    g.add_argument("text", nargs="?", help="new description text")
    g.add_argument("--clear", action="store_true",
                   help="remove the existing description")
    md_d.set_defaults(func=cmd_mcp_describe)

    sr = mcp_sub.add_parser("set-roles",
                            help="replace an MCP's role-MCP affinities "
                                 "(comma-separated; empty CSV clears)")
    sr.add_argument("name")
    sr.add_argument("csv", help="comma-separated role names, or empty string "
                                "to clear")
    sr.set_defaults(func=cmd_mcp_set_roles)

    ml = mcp_sub.add_parser("list", help="list registered MCPs")
    ml.add_argument("--json", action="store_true")
    ml.set_defaults(func=cmd_mcp_list)

    mr = mcp_sub.add_parser("remove", help="remove MCP from registry (and stop its container)")
    mr.add_argument("name")
    mr.add_argument("--force", action="store_true",
                    help="remove even if projects currently allow it (Stage 2.2 state)")
    mr.set_defaults(func=cmd_mcp_remove)

    me = mcp_sub.add_parser("enable",
                            help="mark an MCP as enabled (required for `project mcp allow`; "
                                 "shared MCPs auto-start on `research start`)")
    me.add_argument("name")
    me.set_defaults(func=cmd_mcp_enable)

    md = mcp_sub.add_parser("disable",
                            help="clear the enabled flag, deny from every project that "
                                 "currently allows it, and (if shared and running) stop "
                                 "the container")
    md.add_argument("name")
    md.add_argument("--keep-projects", action="store_true",
                    help="leave per-project allowlists untouched; project workers "
                         "keep the stale wiring until you `project mcp deny` or "
                         "`project mcp sync` them yourself")
    md.set_defaults(func=cmd_mcp_disable)

    msp = mcp_sub.add_parser("start", help="(shared) start MCP containers (defaults to all enabled)")
    msp.add_argument("name", nargs="?",
                     help="MCP name; omit to start every enabled shared MCP")
    msp.set_defaults(func=cmd_mcp_start)

    mst = mcp_sub.add_parser("stop", help="(shared) stop MCP containers (defaults to all running)")
    mst.add_argument("name", nargs="?",
                     help="MCP name; omit to stop every running shared MCP")
    mst.set_defaults(func=cmd_mcp_stop)

    mts = mcp_sub.add_parser("test", help="probe reachability of MCPs (defaults to every registered MCP)")
    mts.add_argument("name", nargs="?",
                     help="MCP name; omit to test every registered MCP")
    mts.set_defaults(func=cmd_mcp_test)

    # ----- PI-isolated type registry (STAGE_PI_ISOLATED) ----------------
    piso_reg = sub.add_parser(
        "pi-isolated",
        help="host registry of PI-isolated agent types (reusable repo + "
             "root-folder definitions enabled per-project with "
             "`project create|update --enable <type>`)")
    piso_reg_sub = piso_reg.add_subparsers(dest="subcommand", required=True)

    pa = piso_reg_sub.add_parser("add", help="register a PI-isolated type")
    pa.add_argument("name")
    pa.add_argument("--root", required=True, metavar="HOST_DIR",
                    help="host folder for this type; the per-project subdir "
                         "<root>/<project>/ is the RW external mount. May use "
                         "~ and ${VAR}.")
    pa.add_argument("--repo", help="git URL cloned into the container at "
                                   "enable time (omit for a pure-folder agent)")
    pa.add_argument("--ref", help="commit/tag to check out. Optional: if "
                                  "omitted, the repo's default-branch HEAD is "
                                  "resolved and pinned now (still no drift — "
                                  "you just don't supply the SHA). Requires "
                                  "git on the host when omitted.")
    pa.add_argument("--setup", help="shell command run in the clone dir after "
                                    "checkout (e.g. 'bash setup.sh'); runs on "
                                    "every boot, must be idempotent")
    pa.add_argument("--mount", metavar="CONTAINER_PATH",
                    help=f"absolute container path the external folder lands "
                         f"at (default {pi_isolated_registry.DEFAULT_MOUNT!r}; "
                         f"keep under /workspace/)")
    pa.add_argument("--description",
                    help="operator note surfaced in `pi-isolated list`")
    pa.add_argument("--no-verify", action="store_true",
                    help="skip the `git ls-remote` repo/ref check at add time")
    pa.set_defaults(func=cmd_pi_isolated_add)

    pl = piso_reg_sub.add_parser("list", help="list registered PI-isolated types")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_pi_isolated_list)

    pr = piso_reg_sub.add_parser("remove", help="remove a type from the registry")
    pr.add_argument("name")
    pr.add_argument("--force", action="store_true",
                    help="remove even if projects currently enable it")
    pr.set_defaults(func=cmd_pi_isolated_remove)

    psr = piso_reg_sub.add_parser("set-root", help="change a type's host root folder")
    psr.add_argument("name")
    psr.add_argument("root", metavar="HOST_DIR")
    psr.set_defaults(func=cmd_pi_isolated_set_root)

    pds = piso_reg_sub.add_parser("describe", help="set or clear a type's description")
    pds.add_argument("name")
    pdg = pds.add_mutually_exclusive_group(required=True)
    pdg.add_argument("text", nargs="?", help="new description text")
    pdg.add_argument("--clear", action="store_true", help="remove the description")
    pds.set_defaults(func=cmd_pi_isolated_describe)

    # ----- webui (browser SSH multiplexer) ------------------------------
    wu = sub.add_parser("webui", help="manage the optional browser UI container")
    wu_sub = wu.add_subparsers(dest="webui_action", required=True)

    wus = wu_sub.add_parser("start",
                            help="start the webui (builds image if missing)")
    wus.add_argument("--bind",
                     help="host IP to bind to (default 127.0.0.1; set to 0.0.0.0 "
                          "to expose on LAN; updates .env, regenerates TLS cert SAN)")
    wus.add_argument("--port",
                     help="host port to bind to (default 7777; updates .env). "
                          "Container always listens on 7777 internally")
    wus.add_argument("--rebuild", action="store_true",
                     help="rebuild the webui image and recreate the container "
                          "(use after editing webui/server.py, app.js, etc.)")
    wus.set_defaults(func=cmd_webui)

    wup = wu_sub.add_parser("stop", help="remove the webui container")
    wup.set_defaults(func=cmd_webui)

    wut = wu_sub.add_parser("status", help="show webui status")
    wut.set_defaults(func=cmd_webui)

    wui = wu_sub.add_parser(
        "import",
        help="print the base64 import string the SPA's add-project form auto-fills from",
    )
    wui.add_argument("project", nargs="?",
                     help="project name; omit to list every project that has SSH info")
    wui.set_defaults(func=cmd_webui)

    wct = wu_sub.add_parser(
        "cert-tailscale",
        help="replace the self-signed cert with a Tailscale-issued "
             "Let's Encrypt cert (fixes browser ServiceWorker / webview)",
        description="Auto-detects the host's tailnet FQDN, runs "
                    "`tailscale cert <fqdn>`, stages the resulting "
                    "cert+key into the rs-webui-tls volume, and recreates "
                    "the webui container so the new cert takes effect. "
                    "Requires HTTPS enabled in your tailnet's admin UI "
                    "(login.tailscale.com/admin/dns) and either sudo or "
                    "`sudo tailscale up --operator=$USER` set once.",
    )
    wct.set_defaults(func=cmd_webui)

    return p


def main() -> None:
    load_config()
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
