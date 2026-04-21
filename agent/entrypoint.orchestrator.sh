#!/usr/bin/env bash
# entrypoint.orchestrator.sh — Research Sandbox orchestrator container entrypoint.
#
# Expected environment variables:
#   PROJECT          — project name (used for labels / byobu title)
#   SSH_PASSWORD     — password for the `research` user's SSH
#   HOST_GID         — host user's GID, for shared bind-mount access (optional)
#   DOCKER_DIND      — set to "true" when sysbox or --privileged is providing DIND
#
# Startup order:
#   1. GID remap (for bind mounts from host).
#   2. Home-skel restore (volume hides image contents on first boot).
#   3. Stage /workspace/.claude/ and .orchestrator/ if this is a fresh volume.
#   4. dockerd (DIND) in the background, wait for socket.
#   5. sshd.
#   6. Claude Code settings (bypassPermissions).
#   7. Byobu session template.
#   8. sleep infinity (tini reaps zombies for us).

set -euo pipefail

echo "=== Orchestrator starting${PROJECT:+ for project '${PROJECT}'} ==="

# --- GID remap so host user (shared GID) can rw bind-mounted files ---
if [[ -n "${HOST_GID:-}" ]]; then
    sudo groupmod -o -g "${HOST_GID}" research 2>/dev/null || true
fi

# --- Restore home from skel on first boot (volume mount hides image contents) ---
sudo chown research:research /home/research
umask 002
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/research-skel/. ~/
fi
if ! grep -q 'umask 002' ~/.bashrc 2>/dev/null; then
    echo 'umask 002' >> ~/.bashrc
fi

# --- Workspace first-boot staging ---
# Named volumes mount root-owned by default. Claim /workspace (non-recursive) so
# the research user can create subdirs; RO bind-mounts under /workspace/shared/
# must not be chowned, so we avoid -R.
if [[ "$(stat -c %U /workspace)" != "research" ]]; then
    sudo chown research:research /workspace
fi
mkdir -p /workspace/.claude /workspace/.orchestrator/logs /workspace/plan \
         /workspace/shared /workspace/workers
# /workspace/shared/data may be a RO bind-mount; only create it if missing.
[[ -d /workspace/shared/data ]] || mkdir -p /workspace/shared/data

if [[ ! -f /workspace/.claude/CLAUDE.md ]]; then
    cp /opt/claude-templates/CLAUDE.md /workspace/.claude/CLAUDE.md
fi

# The /workspace/CLAUDE.md is what Claude Code auto-discovers when started from
# /workspace. Symlink into .claude/CLAUDE.md to keep the single source of truth.
if [[ ! -e /workspace/CLAUDE.md ]]; then
    ln -s .claude/CLAUDE.md /workspace/CLAUDE.md
fi

# --- Start dockerd (DIND) in the background ---
if [[ "${DOCKER_DIND:-}" == "true" ]] && command -v dockerd >/dev/null 2>&1; then
    echo "Starting dockerd..."
    # Clean up stale PID/socket files from a previous run (container restart).
    sudo rm -f /var/run/docker.pid /var/run/docker.sock
    sudo sh -c 'dockerd > /tmp/dockerd.log 2>&1 &'
    # Wait up to 30s for the socket.
    for _ in $(seq 1 30); do
        if docker info >/dev/null 2>&1; then
            echo "dockerd ready."
            break
        fi
        sleep 1
    done
    if ! docker info >/dev/null 2>&1; then
        echo "WARNING: dockerd did not become ready; see /tmp/dockerd.log" >&2
    fi
fi

# --- SSH ---
if [[ -n "${SSH_PASSWORD:-}" ]]; then
    echo "research:${SSH_PASSWORD}" | sudo chpasswd
fi
sudo /usr/sbin/sshd

# --- Claude Code settings (bypassPermissions). setup.sh is a template from
#     the image, copied+run once per fresh home. ---
if [[ -f /opt/claude-templates/setup.sh ]]; then
    # shellcheck source=/dev/null
    source /opt/claude-templates/setup.sh
fi

# Byobu is NOT pre-started here. `research project attach` creates the "main"
# session lazily if needed. Pre-starting would freeze a bash process that
# predates `usermod -aG docker research` (docker-ce is installed at project
# create time), so the attached shell would lack the docker group.

echo "=== Orchestrator ready ==="
echo "Workspace: /workspace"
echo "Attach:    docker exec -it <container> byobu attach -t main"
echo "SSH:       $( [[ -n "${SSH_PASSWORD:-}" ]] && echo "research@<host>:<published-port>" || echo "not configured" )"

# tini is PID 1 (ENTRYPOINT) and reaps zombies. sleep infinity keeps PID 1 alive.
exec sleep infinity
