#!/usr/bin/env bash
# entrypoint.management.sh — Management substrate for a --type sandbox project
# (STAGE_SANDBOX_PROJECT.md).
#
# Agent-less by construction: this image (rs-management) contains no claude, no
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

# --- Workspace: only the orchestrator dir (sandbox.json + project.json). No
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

# --- code-server (lazy-start via stub) — the Editor tab, artifact-management
#     surface. Same stub the research supervisor uses (both from the base).
if [[ "${RS_SERVICE_CODE_SERVER:-enabled}" == "enabled" ]]; then
    CS_USER_DIR=/workspace/.local/share/code-server
    CS_EXT_DIR="${CS_USER_DIR}/extensions"
    mkdir -p "${CS_USER_DIR}/User" "${CS_EXT_DIR}"

    if [[ ! -f "${CS_USER_DIR}/User/settings.json" ]] && \
       [[ -f /opt/code-server-templates/User/settings.json ]]; then
        cp /opt/code-server-templates/User/settings.json \
           "${CS_USER_DIR}/User/settings.json"
    fi

    if [[ -d /opt/code-server-templates/extensions ]]; then
        for vsix in /opt/code-server-templates/extensions/*.vsix; do
            [[ -f "$vsix" ]] || continue
            base=$(basename "$vsix" .vsix)
            shopt -s nullglob
            existing=( "${CS_EXT_DIR}/"*"${base}"* )
            shopt -u nullglob
            if (( ${#existing[@]} > 0 )); then
                continue
            fi
            echo "installing code-server extension: ${base}"
            code-server \
                --install-extension "$vsix" \
                --extensions-dir "${CS_EXT_DIR}" \
                --user-data-dir "${CS_USER_DIR}" \
                || echo "WARNING: failed to install ${base}" >&2
        done
    fi

    : "${CODE_SERVER_STUB_PORT:=8443}"
    : "${CODE_SERVER_UPSTREAM_PORT:=8444}"
    : "${CODE_SERVER_IDLE_SECONDS:=1800}"
    export CODE_SERVER_STUB_PORT CODE_SERVER_UPSTREAM_PORT \
           CODE_SERVER_IDLE_SECONDS
    nohup /opt/code-server-tools/code-server-stub.py \
        > /tmp/code-server-stub.log 2>&1 &
    echo "code-server stub launched on :${CODE_SERVER_STUB_PORT}; "\
"upstream :${CODE_SERVER_UPSTREAM_PORT}; idle reap ${CODE_SERVER_IDLE_SECONDS}s"
fi

# --- code-server editor (dist) — STAGE_EDITOR_DIST. NO-OP in slice 1: the system
#     bake above is present (! -e /usr/bin/code-server is false) so this is skipped
#     and the baked block serves the editor. Present to pre-stage slice 2's flip:
#     slice 2 deletes BOTH the Dockerfile bake AND the baked launch block above (or
#     the orphaned block would try to run the now-deleted /opt/code-server-tools
#     stub), after which this dist block activates — the same shared dist deploy
#     script the interactive leaves use. Guard is the populated-mount check.
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
