#!/usr/bin/env bash
# entrypoint.sandbox-dind.sh — Management substrate for a --workflow sandbox-dind project
# (STAGE_SANDBOX_PROJECT.md).
#
# Agent-less by construction: this image (rs-sandbox-dind) contains no claude, no
# rs-worker, no mcp-proxy/firewall, no research-supervisor templates. It hosts
# isolated boxes (via rs-sandbox in the inner dockerd) and the Editor; the PI
# manages boxes from the non-agent Management tab. Authority-without-agency is
# enforced by what's NOT in the image, not by a runtime branch.
#
# Expected env: PROJECT, SSH_PASSWORD, HOST_GID, DOCKER_DIND, RS_SERVICE_CODE_SERVER.

set -euo pipefail

echo "=== Management substrate starting${PROJECT:+ for project '${PROJECT}'} ==="

# --- GID remap so host user (shared GID) can rw bind-mounted files ---
if [[ -n "${HOST_GID:-}" ]]; then
    sudo groupmod -o -g "${HOST_GID}" research 2>/dev/null || true
fi

# --- Restore home from skel on first boot (volume hides image contents) ---
sudo chown research:research /home/research
umask 002
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/research-skel/. ~/
fi
if ! grep -q 'umask 002' ~/.bashrc 2>/dev/null; then
    echo 'umask 002' >> ~/.bashrc
fi
echo management > ~/.rs-role

# --- Workspace: only the orchestrator dir (extensions.json + project.json). No
#     plan/logbook/workers tree — there are no workers and no supervisor agent.
if [[ "$(stat -c %U /workspace)" != "research" ]]; then
    sudo chown research:research /workspace 2>/dev/null || true
fi
mkdir -p /workspace/.orchestrator/logs

# --- Start dockerd (DIND) in the background (hosts the inner boxes) ---
if [[ "${DOCKER_DIND:-}" == "true" ]] && command -v dockerd >/dev/null 2>&1; then
    echo "Starting dockerd..."
    sudo rm -f /var/run/docker.pid /var/run/docker.sock
    sudo sh -c 'dockerd > /tmp/dockerd.log 2>&1 &'
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

    # The boxes join the rs-inner bridge (rs-sandbox pins them at
    # 192.168.99.14-25). In a research supervisor mcp-reload creates this
    # network; Management has no proxy/mcp-reload, so create it here. Same
    # subnet as the supervisor's rs-inner (keep in lockstep with mcp-reload.sh).
    docker network inspect rs-inner >/dev/null 2>&1 \
        || docker network create --subnet 192.168.99.0/24 rs-inner >/dev/null \
        || echo "WARNING: failed to create rs-inner network" >&2
fi

# --- SSH ---
if [[ -n "${SSH_PASSWORD:-}" ]]; then
    echo "research:${SSH_PASSWORD}" | sudo chpasswd
fi
sudo /usr/sbin/sshd

# --- code-server editor (dist) — STAGE_EDITOR_DIST. The bake is gone (slice 2);
#     the editor (artifact-management surface) is a host-cached dist. Like the
#     research supervisor, this block is effectively a no-op at first boot (the
#     entrypoint runs BEFORE the post-start `_stage_editor_dist`, so
#     /opt/editor-dist is empty here) — the management box's OWN editor is brought
#     up by that staging's deploy_local. Kept for parity + a populated-mount boot;
#     coexistence + populated-mount guards keep it safe.
if [[ "${RS_SERVICE_CODE_SERVER:-enabled}" == "enabled" ]] \
   && [[ -e /opt/editor-dist/.local/bin/code-server ]] \
   && [[ ! -e /usr/bin/code-server ]]; then
    bash /opt/editor-dist/tools/code-server-deploy.sh || true
fi

echo "=== Management ready ==="
echo "Workspace:    /workspace"
echo "Manage boxes: rs-sandbox   (Management tab, or \`research project attach\`)"

# tini is PID 1 (ENTRYPOINT) and reaps zombies. sleep infinity keeps PID 1 alive.
exec sleep infinity
