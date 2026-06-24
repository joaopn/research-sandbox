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
# Deploy each enabled agent dist into our OWN writable ~/.local (STAGE_MULTI_AGENT).
# create() RO-mounts one copy-source per enabled agent at /opt/agent-dist/<agent>,
# so the mounts ARE the enabled set — loop over the mounted subdirs (empty set => no
# mount => /opt/agent-dist absent => this no-ops, a lean box). Guard on the per-agent
# LAUNCHER'S ABSENCE, NOT ~/.bashrc/first-boot: rs-minimal's /home/research is
# image-resident (not a volume), so ~/.bashrc always exists and the first-boot block
# above never fires here. The per-agent absence guard deploys each exactly once — a
# later docker-start restart finds the launchers present and skips, so an autoupdater
# bump is never clobbered. The mounts are inert RO copy-sources; the box runs from
# its own copies.
if [[ -d /opt/agent-dist ]]; then
    mkdir -p ~/.local
    for agent_src in /opt/agent-dist/*/; do
        [[ -d "$agent_src" ]] || continue          # no match => the glob stays literal
        agent_name="$(basename "$agent_src")"
        if [[ ! -e ~/.local/bin/"$agent_name" ]]; then
            cp -a "$agent_src". ~/.local/
        fi
    done
fi
if ! grep -q 'umask 002' ~/.bashrc 2>/dev/null; then
    echo 'umask 002' >> ~/.bashrc
fi
# ~/.local/bin on PATH (where the agent launcher lands) — unconditional +
# idempotent (self-heals), like the umask line; rs-minimal-base doesn't export it.
if ! grep -q '\.local/bin' ~/.bashrc 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
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

# --- code-server editor (dist) — STAGE_EDITOR_DIST. NO-OP in slice 1: the system
#     bake above is present (! -e /usr/bin/code-server is false) so this is skipped
#     and the baked block serves the editor. Present to pre-stage slice 2's flip:
#     slice 2 deletes BOTH the Dockerfile bake AND the baked launch block above (or
#     the orphaned block would try to run the now-deleted /opt/code-server-tools
#     stub), after which this dist block activates — the same shared dist deploy
#     script the interactive leaves use. The docker box stages no /opt/editor-dist,
#     so the populated-mount guard double-skips it there.
if [[ "${RS_SERVICE_CODE_SERVER:-enabled}" == "enabled" ]] \
   && [[ -e /opt/editor-dist/.local/bin/code-server ]] \
   && [[ ! -e /usr/bin/code-server ]]; then
    bash /opt/editor-dist/tools/code-server-deploy.sh || true
fi

echo "=== Minimal substrate ready ==="
echo "Workspace: /workspace"

# tini is PID 1 (ENTRYPOINT) and reaps zombies. sleep infinity keeps PID 1 alive.
exec sleep infinity
