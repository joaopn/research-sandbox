#!/usr/bin/env bash
# entrypoint.supervisor.sh — Research Sandbox supervisor container entrypoint.
#
# Expected environment variables:
#   PROJECT          — project name (used for labels / byobu title)
#   SSH_PASSWORD     — password for the `research` user's SSH
#   HOST_GID         — host user's GID, for shared bind-mount access (optional)
#   DOCKER_DIND      — set to "true" when sysbox or --privileged is providing DIND
#
# Per-service env vars (RS_SERVICE_<ID>):
#   `research project create|update --enable/--disable <ids>` flips both
#   `research.service.<id>` labels (read by the webui) and `RS_SERVICE_<ID>`
#   env vars (read here) in lockstep. Conditional service start blocks
#   wrap themselves in:
#       if [[ "${RS_SERVICE_FOO:-enabled}" == "enabled" ]]; then
#           ...start foo...
#       fi
#   xterm (sshd + byobu) is implicit always-on — it's the substrate for
#   `research project ssh` — and is NOT gated by an env var. W1 ships no
#   conditional blocks; the pattern lands when the first toggleable
#   service (jupyter) arrives in W2.
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

echo "=== Supervisor starting${PROJECT:+ for project '${PROJECT}'} ==="

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

# --- Restore Claude credentials stashed by `research project update --rebuild`
#     (which `mv`s ~research/.claude into the workspace bind-mount before
#     destroying the old container, so the creds survive the swap without
#     ever touching the host outside the project's own workspace dir). ---
if [[ -d /workspace/.creds-stash ]]; then
    sudo rm -rf /home/research/.claude
    sudo mv /workspace/.creds-stash /home/research/.claude
    sudo chown -R research:research /home/research/.claude
    echo "restored Claude creds from /workspace/.creds-stash"
fi

# --- Workspace first-boot staging ---
# Under host bind-mount, /workspace is owned by the host user. Under sysbox's
# user namespace the chown may fail (host uid outside the ns-uid range); under
# named volumes it lands root-owned and must be reclaimed. Either way: try
# once, ignore failure, rely on HOST_GID + setgid bit for shared access.
if [[ "$(stat -c %U /workspace)" != "research" ]]; then
    sudo chown research:research /workspace 2>/dev/null || true
fi
mkdir -p /workspace/.claude /workspace/.orchestrator/logs \
         /workspace/plan /workspace/plan/draft /workspace/plan/archive \
         /workspace/logbook/supervisor /workspace/logbook/pi \
         /workspace/shared /workspace/workers \
         /workspace/.workers /workspace/staging /workspace/results
# /workspace/shared/data may be a RO bind-mount; only create it if missing.
[[ -d /workspace/shared/data ]] || mkdir -p /workspace/shared/data

if [[ ! -f /workspace/.claude/CLAUDE.md ]]; then
    cp /opt/claude-templates/CLAUDE.md /workspace/.claude/CLAUDE.md
fi

# Slash commands (project-level). The supervisor's `/log` lives here.
mkdir -p /workspace/.claude/commands
for src in /opt/claude-templates/commands/*.md; do
    [[ -f "$src" ]] || continue
    dst="/workspace/.claude/commands/$(basename "$src")"
    [[ -f "$dst" ]] || cp "$src" "$dst"
done

# Two-stream logbook templates, referenced by /workspace/.claude/commands/log.md.
for tmpl in logbook_supervisor_template.md logbook_pi_template.md; do
    src="/opt/claude-templates/$tmpl"
    dst="/workspace/.claude/$tmpl"
    [[ -f "$src" && ! -f "$dst" ]] && cp "$src" "$dst" || true
done

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

# --- MCP proxy: rs-inner bridge + proxy container in the inner dockerd ----
# Workers spawned by `rs-worker spawn --mcps ...` join rs-inner and resolve
# the proxy by DNS as `mcp-proxy:8888`. mcp-reload is idempotent: it renders
# config, ensures rs-inner exists, and either SIGHUPs a running proxy or
# spawns one. Same script `research project mcp-allow` invokes via
# `docker exec` after a per-project allowlist mutation.
/usr/local/bin/mcp-reload || echo "WARNING: mcp-reload failed at boot" >&2

# --- Optional inner-netns firewall (Stage 2.3, defense-in-depth) ----------
# Restricts rs-inner egress to mcp-proxy + Docker embedded DNS. Off by default
# until we've dogfooded the proxy path; opt-in via `research project create
# --inner-firewall` (sets RS_INNER_FIREWALL=1 on the supervisor).
if [[ "${RS_INNER_FIREWALL:-0}" == "1" ]]; then
    /usr/local/bin/rs-inner-firewall || \
        echo "WARNING: inner-firewall failed to apply" >&2
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

echo "=== Supervisor ready ==="
echo "Workspace: /workspace"
echo "Attach:    docker exec -it <container> byobu attach -t main"
echo "SSH:       $( [[ -n "${SSH_PASSWORD:-}" ]] && echo "research@<host>:<published-port>" || echo "not configured" )"

# tini is PID 1 (ENTRYPOINT) and reaps zombies. sleep infinity keeps PID 1 alive.
exec sleep infinity
