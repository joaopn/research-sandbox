#!/usr/bin/env bash
# entrypoint.pi.sh — PI-role container boot.
#
# Expected mounts (provided by the supervisor's `pi enable` call):
#   /workspace          — per-role RW state (role.md, .claude/, sessions/, …)
#   /creds              — RO snapshot of supervisor creds (.credentials.json,
#                         optional settings.json). Parent-dir bind-mount so
#                         atomic-rename writes by the supervisor stay visible.
#
# Expected environment:
#   RS_PI_ROLE          — e.g. echo, wrangler, librarian, websearcher
#
# Startup order:
#   1. Restore home skel (volume hides image contents).
#   2. Stage creds from /creds → ~/.claude/.
#   3. Stage role.md from /opt/pi-templates/<role>/role.md if absent.
#   4. tail -f /dev/null — keep the container up; byobu sessions are
#      created on first webui tab connect.

set -euo pipefail

: "${RS_PI_ROLE:?RS_PI_ROLE must be set}"

# --- Restore home skel on first boot ---------------------------------------
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/worker-skel/. ~/
fi

# --- Stage creds (in-container claude needs OAuth) -------------------------
# The supervisor stages its current ~/.claude/.credentials.json into the
# per-role creds bind-mount source at /workspace/.pi/<role>/.creds/ on
# enable, and the inotify watcher on the supervisor re-stages on re-auth.
# We copy it into the runtime user's home so the inner claude picks it up.
# Failure is fatal — claude won't work without creds, and we'd rather fail
# loud at boot than silent in a session.
if [[ -f /creds/.credentials.json ]]; then
    mkdir -p ~/.claude
    cp /creds/.credentials.json ~/.claude/.credentials.json
    chmod 600 ~/.claude/.credentials.json
else
    echo "pi[${RS_PI_ROLE}]: missing /creds/.credentials.json" >&2
    echo "pi[${RS_PI_ROLE}]: refusing to start — supervisor staging step was skipped or supervisor is unauthenticated" >&2
    exit 2
fi
# Optional: settings (theme, verbosity). Tolerant of absence.
if [[ -f /creds/settings.json ]]; then
    cp /creds/settings.json ~/.claude/settings.json
fi

# --- Stage role.md from template if absent ---------------------------------
# Workspace-template-refresh idiom: copy on first boot only; PI's edits to
# /workspace/role.md persist across container restarts. Operator who wants
# to pick up a new template removes /workspace/role.md and re-enables.
if [[ -d "/opt/pi-templates/${RS_PI_ROLE}" ]] && [[ ! -f /workspace/role.md ]]; then
    if [[ -f "/opt/pi-templates/${RS_PI_ROLE}/role.md" ]]; then
        cp "/opt/pi-templates/${RS_PI_ROLE}/role.md" /workspace/role.md
    fi
fi

echo "pi[${RS_PI_ROLE}]: ready (workspace at /workspace, creds staged)"

# --- Keep the container alive ----------------------------------------------
# byobu sessions get created on first webui tab connect via the registered
# `command` field, not here.
exec tail -f /dev/null
