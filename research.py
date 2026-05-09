#!/usr/bin/env python3
"""research — CLI for Research Sandbox.

Stdlib only. Subcommand layout:

    research start                     # start shared infra (router)
    research stop                      # stop shared infra
    research project create <name> [--data-dir PATH] [--profile python]
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
SUPERVISOR_IMAGE = "rs-supervisor:latest"
ANALYSIS_IMAGE = "rs-analysis-base:latest"
MCP_PROXY_IMAGE = "rs-mcp-proxy:latest"
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
# ALWAYS_ON_SERVICES can't be disabled — xterm is the substrate for
# `research project ssh`. New service kinds extend both lists in the same
# commit that ships the entrypoint conditional and the registry entry.
KNOWN_SERVICES: list[str] = ["xterm", "code-server"]
ALWAYS_ON_SERVICES: set[str] = {"xterm"}
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
    """Build supervisor + worker + mcp-proxy images. Skip existing ones unless --rebuild."""
    specs = [
        (SUPERVISOR_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.supervisor"),
        (ANALYSIS_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.analysis-base"),
        (MCP_PROXY_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.mcp-proxy"),
    ]
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

    # Optional data-dir bind-mount (read-only inside supervisor). Created
    # if missing, like `mkdir -p` — typical use is an empty dir for a fresh
    # project that data will be dropped into afterward.
    extra_mounts: list[str] = []
    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser().resolve()
        if data_dir.exists() and not data_dir.is_dir():
            die(f"--data-dir exists but is not a directory: {data_dir}")
        if not data_dir.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
            print(f"created data directory: {data_dir}")
        extra_mounts += ["-v", f"{data_dir}:/workspace/shared/data:ro"]

    print(f"=== Creating project: {project} ===")
    ssh_port = args.ssh_port or find_free_port()
    ssh_pass = gen_password()

    # 1. Workspace dir (host bind-mount) + optional privileged-DIND volume.
    # setgid bit (2xxx) makes new files inherit the host user's primary GID;
    # combined with HOST_GID remap in the supervisor entrypoint, host user
    # and container's `research` user share rw access through the shared GID.
    workspace_path.mkdir(parents=True, exist_ok=True)
    os.chmod(workspace_path, 0o2770)
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
    service_flags = _compute_service_flags(
        getattr(args, "enable", None), getattr(args, "disable", None),
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
    # Inject data-dir (and any future --mount) into argv just before the image name.
    if extra_mounts:
        docker_args = docker_args[:-1] + extra_mounts + [docker_args[-1]]

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

    # 7. Report.
    inner_fw = "on" if getattr(args, "inner_firewall", False) else "off"
    mcps_line = ", ".join(granted) if granted else "(none)"
    print(f"""
Project '{project}' is running.

  Container: {container_name}
  Workspace: {workspace_path}
  Network:   {network} (egress: {egress})
  DIND mode: {dind_mode}
  Inner FW:  {inner_fw}
  MCPs:      {mcps_line}
  SSH:       research@localhost -p {ssh_port}   password: {ssh_pass}

Next steps:
  1. Authenticate Claude Code inside the supervisor (once per project).
     Either:
       (a) Connect via VSCode Remote-SSH (localhost:{ssh_port}, user 'research',
           password as above), then click the Claude Code extension — it will
           trigger OAuth via a port-forwarded localhost callback.
       (b) `research project attach {project}`, then in byobu run `claude` and
           complete the device-code flow in a local browser.
  2. Once authenticated, ask the supervisor to spawn workers.
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
    # i.e. user-supplied --data-dir paths.
    extra_mounts: list[str] = []
    for m in data.get("Mounts") or []:
        dst_in = m.get("Destination", "")
        if dst_in in ("/workspace", "/var/lib/docker"):
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
    """Move ~research/.claude into /workspace/.creds-stash so the bind-mount
    preserves it across container removal. Two paths:

    - Running supervisor: `docker exec mv` (no copy, atomic, never leaves
      the container's filesystem until the bind-mount writeback hits the
      host workspace dir).
    - Stopped supervisor: `docker cp` from the stopped container directly
      into the host's workspace bind-mount path. The container is about to
      be destroyed, so this is functionally a move; creds never touch /tmp.

    Idempotent: if the stash already exists from a prior failed update,
    skips. The entrypoint will move it back at next start either way."""
    host_stash = workspace_path / ".creds-stash"
    if host_stash.exists():
        return
    if was_running:
        run(["docker", "exec", container, "sh", "-c",
             "if [ -d /home/research/.claude ] && "
             "[ ! -d /workspace/.creds-stash ]; then "
             "mv /home/research/.claude /workspace/.creds-stash; "
             "fi"],
            capture_output=True)
    else:
        # docker cp on a stopped container works for files in its filesystem.
        run(["docker", "cp",
             f"{container}:/home/research/.claude",
             str(host_stash)],
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
    # worker + proxy images need staging. Privileged DIND has a named
    # volume that survives rm; stage_worker_image is a no-op there
    # unless `force_restage` (i.e. images were rebuilt on the host).
    wait_for_inner_dockerd(container)
    run(["docker", "exec", container, "docker", "rm", "-f", "mcp-proxy"],
        capture_output=True)
    stage_worker_image(container, ANALYSIS_IMAGE, force=force_restage)
    stage_worker_image(container, MCP_PROXY_IMAGE, force=force_restage)
    run(["docker", "exec", container, "/usr/local/bin/mcp-reload"],
        capture_output=True)


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
    if getattr(args, "enable", None) or getattr(args, "disable", None):
        base = _read_service_flags(container)
        flags_override = _compute_service_flags(args.enable, args.disable, base=base)

    _recreate_supervisor(
        project, cfg,
        force_restage=args.rebuild,
        post_create_hook=hook,
        service_flags=flags_override,
    )

    if not args.keep_claude:
        print(f"refreshing /workspace/.claude/ from templates...")
        _refresh_workspace_claude_templates(container)

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
    with mcp_registry.lock():
        try:
            data = mcp_registry.load(expand=False)
        except mcp_registry.RegistryError as e:
            die(str(e))
        if args.name in data["mcps"]:
            die(f"MCP {args.name!r} already registered; remove first")
        data["mcps"][args.name] = entry
        try:
            mcp_registry.save_atomic(data)
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
        rows.append((name, kind, e["transport"], target, enabled, status))
        descs.append(e.get("description", ""))
    if not rows:
        print("(no MCPs registered)")
        return
    headers = ("NAME", "KIND", "TRANSPORT", "TARGET", "ENABLED", "STATUS")
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

    if not to_add and not to_remove:
        print(f"(project {project!r} already in sync with the registry)")
        return

    added, _ = _batch_apply(project, cfg, to_add,
                            _allow_mcp_for_project, "allowed")
    removed, _ = _batch_apply(project, cfg, to_remove,
                              _deny_mcp_for_project, "denied")
    if added or removed:
        _supervisor_mcp_reload(container_name)


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
    flags inherit the supervisor's prior choices). xterm is always-on and
    cannot be disabled. Unknown service ids are a hard error."""
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
    c.add_argument("--data-dir", help="host path mounted RO at /workspace/shared/data")
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
                   help=f"comma-separated service ids to force-enable "
                        f"(known: {','.join(KNOWN_SERVICES)})")
    c.add_argument("--disable", metavar="IDS",
                   help="comma-separated service ids to disable "
                        "(default: all known services enabled; xterm is "
                        "always-on and cannot be disabled)")
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
                   help=f"comma-separated service ids to enable "
                        f"(known: {','.join(KNOWN_SERVICES)})")
    u.add_argument("--disable", metavar="IDS",
                   help="comma-separated service ids to disable "
                        "(xterm is always-on and cannot be disabled)")
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
    a.set_defaults(func=cmd_mcp_add)

    md_d = mcp_sub.add_parser("describe",
                              help="set or clear an MCP's description")
    md_d.add_argument("name")
    g = md_d.add_mutually_exclusive_group(required=True)
    g.add_argument("text", nargs="?", help="new description text")
    g.add_argument("--clear", action="store_true",
                   help="remove the existing description")
    md_d.set_defaults(func=cmd_mcp_describe)

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
