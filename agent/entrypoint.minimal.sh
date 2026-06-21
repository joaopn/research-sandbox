#!/usr/bin/env bash
# entrypoint.minimal.sh — the `docker` containment substrate (rs-minimal leaf,
# WORKFLOW_TAXONOMY_S1.md).
#
# A single runc container: ssh + byobu + code-server, NO inner dockerd. It
# cannot spawn containers and runs no agent of its own (agents are dist-
# delivered per-project, STAGE_AGENT_DIST.md). Egress is routed through
# rs-router by the host (inject_route), defaulting locked for this substrate.
# Authority-without-agency does not apply here — there is simply nothing to
# manage and no agent to hijack.
#
# Expected env: PROJECT, SSH_PASSWORD, HOST_GID, RS_SERVICE_CODE_SERVER.

set -euo pipefail

echo "=== Minimal (docker) substrate starting${PROJECT:+ for project '${PROJECT}'} ==="

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
echo minimal > ~/.rs-role

# --- Workspace: only the orchestrator dir (project.json marker). No dind,
#     no plan/logbook/workers tree, no supervisor agent. ---
if [[ "$(stat -c %U /workspace)" != "research" ]]; then
    sudo chown research:research /workspace 2>/dev/null || true
fi
mkdir -p /workspace/.orchestrator/logs

# --- SSH ---
if [[ -n "${SSH_PASSWORD:-}" ]]; then
    echo "research:${SSH_PASSWORD}" | sudo chpasswd
fi
sudo /usr/sbin/sshd

# --- code-server (lazy-start via stub) — the Editor tab. Same stub every
#     substrate uses (all from rs-minimal-base). ---
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

echo "=== Minimal substrate ready ==="
echo "Workspace: /workspace"

# tini is PID 1 (ENTRYPOINT) and reaps zombies. sleep infinity keeps PID 1 alive.
exec sleep infinity
