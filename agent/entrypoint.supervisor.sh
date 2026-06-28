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
#   `supervisor` (sshd + byobu, formerly named `xterm`) is implicit
#   always-on — it's the substrate for `research project ssh` — and is
#   NOT gated by an env var. The first toggleable service is code-
#   server; conditional `if [[ "${RS_SERVICE_CODE_SERVER:-enabled}" ==
#   "enabled" ]]` block lives further down in this file.
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

# Role marker for the byobu status-bar plugin (~/.byobu/bin/60_rolename).
# Written every boot to survive image swaps; SSH login sessions strip the
# container's env, so a file is the lowest-common-denominator carrier.
echo supervisor > ~/.rs-role

# --- Restore Claude credentials stashed by `research project update --rebuild`
#     (which `mv`s ~research/.claude into the workspace bind-mount before
#     destroying the old container, so the creds survive the swap without
#     ever touching the host outside the project's own workspace dir).
#     The stash is purely internal to the recreate/restart machinery —
#     it's NOT populated by any host-side flow; each project's credentials
#     are owned by its own supervisor and never cross project boundaries.
#
#     Two stash points, restored independently:
#       /workspace/.creds-stash/        → ~/.claude/  (dir contents)
#       /workspace/.creds-stash-home.json → ~/.claude.json  (sibling file)
#
#     `.claude.json` is a $HOME-root dotfile, NOT under .claude/. It
#     carries the `oauthAccount` claim; without it, interactive claude
#     treats the user as logged-out even when .credentials.json is
#     valid.  ---
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
# /workspace/shared/data is the parent dir for `--data PATHS` bind-mounts
# (one RO mount per host path at /workspace/shared/data/<basename>/).
# Docker auto-creates it root-owned when any --data mount lands; we mkdir
# it here only when no --data was passed, so the worker bind-mount of the
# whole shared/ tree doesn't trip over a missing path.
[[ -d /workspace/shared/data ]] || mkdir -p /workspace/shared/data
# Same reclaim as /workspace above: `--data` bind-mounts force docker to
# auto-create /workspace/shared root-owned BEFORE the entrypoint runs,
# which breaks role-MCP enable (it mkdirs /workspace/shared/<role>/ as
# the research user). Idempotent: if research.py's host-side pre-create
# already won the race (newer projects), this is a no-op.
if [[ "$(stat -c %U /workspace/shared)" != "research" ]]; then
    sudo chown research:research /workspace/shared 2>/dev/null || true
fi

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

# --- Claude Code settings (bypassPermissions + the rs-audit-stop Stop hook).
#     setup.sh is a template from the image, copied+run once per fresh home.
#     MUST run BEFORE dockerd starts: the host blocks on dockerd-ready before it
#     stages the agent dist, whose no-clobber settings install would otherwise
#     race this write and could drop the hook (STAGE_AGENT_DIST_SETTINGS S1-D2).
#     Placement window: after the creds-stash restore (so a restored settings.json
#     wins), before dockerd — do NOT hoist above the skel restore (it would
#     pre-create ~/.bashrc and skip the home restore). ---
if [[ -f /opt/claude-templates/setup.sh ]]; then
    # shellcheck source=/dev/null
    source /opt/claude-templates/setup.sh
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

# PI containers are PI-owned (STAGE_PI_AUTH_OWNERSHIP): they boot un-authed
# and the PI authenticates in-tab (/login), or the operator pushes the
# supervisor's creds in via `rs-pi sync-creds`. There is no automatic
# supervisor→PI credential propagation — no watcher to launch here.

# --- SSH ---
if [[ -n "${SSH_PASSWORD:-}" ]]; then
    echo "research:${SSH_PASSWORD}" | sudo chpasswd
fi
sudo /usr/sbin/sshd

# --- code-server editor (dist) — STAGE_EDITOR_DIST. The minimal-lineage bake is
#     gone (slice 2); the editor is a host-cached dist. For the SUPERVISOR this
#     block is effectively a no-op at first boot: the entrypoint runs at
#     container-start, BEFORE the post-start `_stage_editor_dist`, so
#     /opt/editor-dist is empty here. The supervisor's OWN editor is brought up by
#     that staging's deploy_local (a post-start `docker exec` of this same deploy
#     script). The block stays for parity with the interactive leaves and to cover
#     a boot where the mount is already populated. Coexistence + populated-mount
#     guards keep it safe (no system bake exists now → the `! -e` guard passes).
if [[ "${RS_SERVICE_CODE_SERVER:-enabled}" == "enabled" ]] \
   && [[ -e /opt/editor-dist/.local/bin/code-server ]] \
   && [[ ! -e /usr/bin/code-server ]]; then
    bash /opt/editor-dist/tools/code-server-deploy.sh || true
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
