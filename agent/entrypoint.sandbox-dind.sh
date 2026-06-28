#!/usr/bin/env bash
# entrypoint.sandbox-dind.sh — substrate for a --workflow sandbox-dind project: the
# sandbox flavor PLUS inner Docker (DIND) (STAGE_SANDBOX_DIND_AGENT).
#
# This boots the inner dockerd + the rs-inner bridge (via mcp-reload), ssh, and
# (when enabled) the editor. The agent (claude) and the rs-sandbox box harness are
# NOT baked into this image — they're staged post-start by the host: _stage_agent_dist
# (deploy_local=True) deploys claude into ~/.local AND populates /opt/agent-dist for
# any boxes; _stage_rs_sandbox installs /usr/local/bin/rs-sandbox (the box harness is
# a standing dind utility now — STAGE_DIND_UNIFY). So this entrypoint has NO agent-cp
# block — it mirrors the research supervisor, which also relies on the post-start
# staging. The MCP proxy tooling (mcp-reload, mcp_render_config.py) IS baked, so
# extensions get the same proxy-routed upstreams as research.
#
# Expected env: PROJECT, SSH_PASSWORD, HOST_GID, DOCKER_DIND, RS_SERVICE_CODE_SERVER.

set -euo pipefail

echo "=== Sandbox-dind substrate starting${PROJECT:+ for project '${PROJECT}'} ==="

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
echo sandbox-dind > ~/.rs-role

# --- Restore Claude creds stashed by a recreate (project update/start) ---
# The sandbox-dind supervisor now RUNS an agent (STAGE_SANDBOX_DIND_AGENT), so its
# creds must survive the sysbox recreate dance the same way the research
# supervisor's do: _recreate_supervisor mv's ~/.claude into the workspace before
# rm'ing the old container; restore it here (before dockerd / the host's post-start
# _stage_agent_dist, so a restored settings.json wins the dist's no-clobber install).
# First create boots un-authed (the PI runs `claude` + /login in the tab); this only
# fires on a recreate that had creds.
if [[ -d /workspace/.creds-stash ]]; then
    sudo rm -rf /home/research/.claude
    sudo mv /workspace/.creds-stash /home/research/.claude
    sudo chown -R research:research /home/research/.claude
    echo "restored Claude creds from /workspace/.creds-stash"
fi
if [[ -f /workspace/.creds-stash-home.json ]]; then
    sudo mv /workspace/.creds-stash-home.json /home/research/.claude.json
    sudo chown research:research /home/research/.claude.json
    sudo chmod 600 /home/research/.claude.json
    echo "restored ~/.claude.json from /workspace/.creds-stash-home.json"
fi

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

    # MCP proxy + rs-inner bridge (STAGE_DIND_UNIFY): sandbox-dind now bakes
    # mcp-reload (Dockerfile.sandbox-dind), so use it instead of the old manual
    # `docker network create rs-inner`. mcp-reload creates the rs-inner bridge (the
    # boxes join it; rs-sandbox pins them at 192.168.99.14-25), renders the proxy
    # config, and spawns the proxy when its image is staged. At FIRST boot the proxy
    # image isn't staged yet (the host stages it post-start in create()), so the
    # proxy spawn just warns; create()'s cone re-runs mcp-reload after staging.
    # Mirrors entrypoint.supervisor.sh.
    /usr/local/bin/mcp-reload || echo "WARNING: mcp-reload failed at boot" >&2
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

echo "=== Sandbox-dind ready ==="
echo "Workspace:    /workspace"
echo "Agent:        run \`claude\` in this tab (locked egress; inner Docker available)"
echo "Boxes:        rs-sandbox   (box harness staged at create)"

# tini is PID 1 (ENTRYPOINT) and reaps zombies. sleep infinity keeps PID 1 alive.
exec sleep infinity
