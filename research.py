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
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
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


def stage_worker_image(container: str, image: str) -> None:
    """Push the host-built image into the supervisor's inner Docker daemon."""
    if not run_quiet(["docker", "image", "inspect", image]):
        die(f"host image {image} not found; run `research setup`.")
    # Skip if already present inside.
    r = run(["docker", "exec", container, "docker", "image", "inspect", image],
            capture_output=True)
    if r.returncode == 0:
        return
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
    run(["docker", "network", "disconnect", network, ROUTER_CONTAINER],
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
    print("up.")


def cmd_stop(_: argparse.Namespace) -> None:
    """Stop shared infra. Leaves images, volumes, and projects untouched."""
    run_check(["docker", "compose", "-f", str(SCRIPT_DIR / "docker-compose.yml"),
               "stop", "router"])
    print("stopped.")


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

    # 3. Build docker run argv.
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

    # 7. Report.
    print(f"""
Project '{project}' is running.

  Container: {container_name}
  Workspace: {workspace_path}
  Network:   {network} (egress: {egress})
  DIND mode: {dind_mode}
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
    target = "__ALL__" if args.all else args.name
    if target == "__ALL__":
        containers = get_supervisor_containers()
    else:
        containers = [{"name": container_name_for(args.name), "project": args.name}]
    for c in containers:
        if not container_exists(c["name"]):
            print(f"skip: {c['name']} does not exist")
            continue
        run_check(["docker", "start", c["name"]])
        # Re-inject default route — docker forgets it on start.
        network = project_network_for(c["project"])
        if network_exists(network):
            try:
                router_ip = get_router_ip(network)
                inject_route(c["name"], router_ip)
            except SystemExit:
                # Non-fatal: container starts, but egress may not traverse the router.
                print(f"warning: could not reinject route for {c['name']}", file=sys.stderr)
        print(f"start: {c['name']}")


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
# Argument parsing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCP registry CLI (Stage 2.1)
# ---------------------------------------------------------------------------


def mcp_container_name_for(name: str) -> str:
    return f"{MCP_CONTAINER_PREFIX}{name}"


def projects_using_mcp(mcp_name: str) -> list[str]:
    """Scan per-project allowlists for projects that allow this MCP."""
    cfg = load_config()
    root = Path(cfg.projects_dir).expanduser().resolve()
    if not root.is_dir():
        return []
    out: list[str] = []
    for p in sorted(root.iterdir()):
        allow_file = p / ".mcp-allow.json"
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
    entry written at `mcp-allow` time."""
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
    if not ip:
        die(f"could not resolve {cname}'s IP on {ROUTER_NETWORK}")
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


def _build_mcp_entry(args: argparse.Namespace) -> dict:
    entry: dict = {"kind": args.kind, "transport": args.transport}
    if args.kind == "external":
        if args.host_port is None:
            die("--host-port is required for --kind external")
        entry["host_port"] = args.host_port
        if args.host_address != "host.docker.internal":
            entry["host_address"] = args.host_address
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
    if entry["kind"] == "shared":
        _ensure_router_running()
        try:
            resolved = mcp_registry.entry_for(args.name)  # expanded
        except mcp_registry.RegistryError as e:
            die(f"failed to resolve env vars for {args.name!r}: {e}\n"
                f"(registry entry saved; set the variable then run "
                f"`research mcp spawn {args.name}`)")
        assert resolved is not None
        _spawn_shared_mcp(args.name, resolved)
    print(f"added MCP {args.name!r} ({entry['kind']})")


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
    for name, e in sorted(data["mcps"].items()):
        kind = e["kind"]
        if kind == "external":
            target = f"{e.get('host_address', 'host.docker.internal')}:{e['host_port']}"
            status = "-"
        else:
            cname = mcp_container_name_for(name)
            target = f"{cname}:{e['port']}"
            status = "running" if container_running(cname) else "stopped"
        rows.append((name, kind, e["transport"], target, status))
    if not rows:
        print("(no MCPs registered)")
        return
    headers = ("NAME", "KIND", "TRANSPORT", "TARGET", "STATUS")
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))


def cmd_mcp_remove(args: argparse.Namespace) -> None:
    in_use = projects_using_mcp(args.name)
    if in_use and not args.force:
        die(f"MCP {args.name!r} is currently allowed for projects: "
            f"{', '.join(in_use)}.\n"
            f"  Run `research project mcp-deny <proj> {args.name}` for each, "
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


def cmd_mcp_spawn(args: argparse.Namespace) -> None:
    try:
        entry = mcp_registry.entry_for(args.name)
    except mcp_registry.RegistryError as e:
        die(str(e))
    if entry is None:
        die(f"no MCP named {args.name!r}")
    if entry["kind"] != "shared":
        die(f"`spawn` only applies to shared MCPs (got kind={entry['kind']!r})")
    _ensure_router_running()
    _spawn_shared_mcp(args.name, entry)


def cmd_mcp_stop(args: argparse.Namespace) -> None:
    try:
        entry = mcp_registry.entry_for(args.name, expand=False)
    except mcp_registry.RegistryError as e:
        die(str(e))
    if entry is None:
        die(f"no MCP named {args.name!r}")
    if entry["kind"] != "shared":
        die(f"`stop` only applies to shared MCPs (got kind={entry['kind']!r})")
    cname = mcp_container_name_for(args.name)
    if not container_running(cname):
        print(f"{cname} not running")
        return
    run_check(["docker", "stop", cname])
    print(f"stopped {cname}")


def cmd_mcp_test(args: argparse.Namespace) -> None:
    try:
        entry = mcp_registry.entry_for(args.name)
    except mcp_registry.RegistryError as e:
        die(str(e))
    if entry is None:
        die(f"no MCP named {args.name!r}")
    _ensure_router_running()
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
        cname = mcp_container_name_for(args.name)
        if not container_running(cname):
            die(f"shared MCP container {cname} not running "
                f"(try: research mcp spawn {args.name})")
        cmd = [
            "docker", "run", "--rm",
            "--network", ROUTER_NETWORK,
            PROBE_IMAGE,
            "nc", "-z", "-w", "5", cname, str(entry["port"]),
        ]
    r = run(cmd, capture_output=True)
    if r.returncode == 0:
        print(f"{args.name}: reachable")
        return
    msg = (r.stderr or r.stdout).strip()
    print(f"{args.name}: unreachable" + (f"\n{msg}" if msg else ""), file=sys.stderr)
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


def cmd_project_mcp_allow(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    mcp_name = args.mcp

    container_name = container_name_for(project)
    if not container_exists(container_name):
        die(f"project {project!r} does not exist")
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research start` first.")

    try:
        entry = mcp_registry.entry_for(mcp_name)
    except mcp_registry.RegistryError as e:
        die(str(e))
    if entry is None:
        die(f"no MCP named {mcp_name!r}; register with `research mcp add` first")

    if entry["kind"] == "external":
        host_addr = entry.get("host_address", "host.docker.internal")
        ip = resolve_host_gateway() if host_addr == "host.docker.internal" else host_addr
        port = entry["host_port"]
    else:  # shared
        cname = mcp_container_name_for(mcp_name)
        if not container_running(cname):
            die(f"shared MCP container {cname} not running; "
                f"run `research mcp spawn {mcp_name}` first")
        ip = mcp_container_ip(mcp_name)
        port = entry["port"]

    network = project_network_for(project)
    subnet = get_network_subnet(network)
    run_check(["docker", "exec", ROUTER_CONTAINER,
               "/scripts/mcp-allow.sh", subnet, ip, str(port)])

    allowlist = load_project_allowlist(project, cfg)
    allowlist = [e for e in allowlist if e.get("name") != mcp_name]
    new_entry = {
        "name": mcp_name,
        "kind": entry["kind"],
        "transport": entry.get("transport", "http"),
        "ip": ip,
        "port": port,
    }
    if entry.get("headers"):
        new_entry["headers"] = entry["headers"]
    allowlist.append(new_entry)
    save_project_allowlist(project, cfg, allowlist)

    _supervisor_mcp_reload(container_name)
    print(f"allowed {mcp_name!r} for project {project!r} -> {ip}:{port}")


def cmd_project_mcp_deny(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    mcp_name = args.mcp

    container_name = container_name_for(project)
    if not container_exists(container_name):
        die(f"project {project!r} does not exist")

    allowlist = load_project_allowlist(project, cfg)
    target = next((e for e in allowlist if e.get("name") == mcp_name), None)
    if target is None:
        die(f"{mcp_name!r} is not currently allowed for project {project!r}")

    network = project_network_for(project)
    if network_exists(network) and container_running(ROUTER_CONTAINER):
        subnet = get_network_subnet(network)
        run(["docker", "exec", ROUTER_CONTAINER,
             "/scripts/mcp-deny.sh", subnet,
             str(target.get("ip", "")), str(target.get("port", ""))],
            capture_output=True)

    allowlist = [e for e in allowlist if e.get("name") != mcp_name]
    save_project_allowlist(project, cfg, allowlist)

    _supervisor_mcp_reload(container_name)
    print(f"denied {mcp_name!r} for project {project!r}")


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

    sh = proj_sub.add_parser("ssh", help="print SSH connection info")
    sh.add_argument("name")
    sh.set_defaults(func=cmd_project_ssh)

    pma = proj_sub.add_parser("mcp-allow",
                              help="grant a project access to a registered MCP")
    pma.add_argument("project")
    pma.add_argument("mcp")
    pma.set_defaults(func=cmd_project_mcp_allow)

    pmd = proj_sub.add_parser("mcp-deny",
                              help="revoke a project's access to a registered MCP")
    pmd.add_argument("project")
    pmd.add_argument("mcp")
    pmd.set_defaults(func=cmd_project_mcp_deny)

    # ----- MCP registry (Stage 2.1) -------------------------------------
    mcp = sub.add_parser("mcp", help="MCP registry operations")
    mcp_sub = mcp.add_subparsers(dest="subcommand", required=True)

    a = mcp_sub.add_parser("add", help="register an MCP and (for shared) start its container")
    a.add_argument("name")
    a.add_argument("--kind", choices=("external", "shared"), required=True)
    a.add_argument("--transport", choices=("http", "sse"), default="http")
    a.add_argument("--host-port", type=int,
                   help="(external) port on the host the supervisor will reach via host.docker.internal")
    a.add_argument("--host-address", default="host.docker.internal",
                   help="(external) override host_address (default: host.docker.internal)")
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
    a.set_defaults(func=cmd_mcp_add)

    ml = mcp_sub.add_parser("list", help="list registered MCPs")
    ml.add_argument("--json", action="store_true")
    ml.set_defaults(func=cmd_mcp_list)

    mr = mcp_sub.add_parser("remove", help="remove MCP from registry (and stop its container)")
    mr.add_argument("name")
    mr.add_argument("--force", action="store_true",
                    help="remove even if projects currently allow it (Stage 2.2 state)")
    mr.set_defaults(func=cmd_mcp_remove)

    msp = mcp_sub.add_parser("spawn", help="(shared) start the MCP container")
    msp.add_argument("name")
    msp.set_defaults(func=cmd_mcp_spawn)

    mst = mcp_sub.add_parser("stop", help="(shared) stop the MCP container without removing the registry entry")
    mst.add_argument("name")
    mst.set_defaults(func=cmd_mcp_stop)

    mts = mcp_sub.add_parser("test", help="probe reachability of an MCP")
    mts.add_argument("name")
    mts.set_defaults(func=cmd_mcp_test)

    return p


def main() -> None:
    load_config()
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
