#!/usr/bin/env bash
# entrypoint.sandbox-box.sh — disposable sandbox box boot (STAGE_SANDBOX_PROJECT).
#
# A blank, isolated box for running un-vetted code. Deliberately minimal and
# clean: NO artifact-contract (no published/ or internal/ dirs, no manifest
# verb, no Stop-hook gate, no publish overlay), NO repo clone, NO credentials.
# Just restore the home skel, set the byobu role marker, ensure a hook-free
# bypassPermissions settings.json, and idle. The PI drives the box from the
# webui tab / `project attach`; if they want an LLM in the box they run
# `claude` and `/login` (boxes are auth-free by design).
#
# Environment:
#   RS_SANDBOX_NAME — box name (e.g. box-1); used for the role marker + logs.

set -euo pipefail

: "${RS_SANDBOX_NAME:?RS_SANDBOX_NAME must be set}"

# Restore home from skel on first boot (the volume mount hides image contents).
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/worker-skel/. ~/
fi

# Deploy the agent (claude) from the management-supervisor-staged dist into our
# OWN writable ~/.local (no bake; STAGE_AGENT_DIST slice 2). The box is auth-free
# (run `claude` + /login inside), so the binary must be present even though no
# creds are. Absence-guarded so a restart preserves an autoupdater bump; the
# in-box `claude` finds it via ~/.local/bin on PATH (self-healed below).
if [[ -d /opt/agent-dist && ! -e ~/.local/bin/claude ]]; then
    mkdir -p ~/.local
    cp -a /opt/agent-dist/. ~/.local/
fi
if ! grep -q '\.local/bin' ~/.bashrc 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
fi

# --- code-server editor (dist) — STAGE_EDITOR_DIST. Deploy + lazy-launch from
#     /opt/editor-dist when the project enabled the editor (RS_SERVICE_CODE_SERVER
#     forwarded from its flag), the mount is populated, and no system bake exists
#     (the coexistence guard). The deploy/launch logic is ONE shared script in the
#     dist (no per-entrypoint duplication). Non-fatal: the editor is optional.
if [[ "${RS_SERVICE_CODE_SERVER:-disabled}" == "enabled" ]] \
   && [[ -e /opt/editor-dist/.local/bin/code-server ]] \
   && [[ ! -e /usr/bin/code-server ]]; then
    bash /opt/editor-dist/tools/code-server-deploy.sh || true
fi

# Role marker for the byobu status-bar plugin (~/.byobu/bin/60_rolename).
echo "${RS_SANDBOX_NAME}" > ~/.rs-role

# bypassPermissions so an in-box `claude` doesn't prompt (the container is the
# security boundary). Crucially NO `hooks` key — the artifact-contract gate is
# deliberately absent here, unlike rs-pi-base. analysis-base ships no
# settings.json, so this write is the only source.
mkdir -p ~/.claude
if [[ ! -f ~/.claude/settings.json ]]; then
    printf '%s\n' '{"permissions": {"defaultMode": "bypassPermissions"}, "theme": "dark"}' \
        > ~/.claude/settings.json
fi

# Browser variant (rs-sandbox-box-browser) bakes the playwright MCP server
# declaration at /opt/sandbox-box/extra-mcps.json. Copy it to /workspace/.mcp.json
# so the box's claude (launched from /workspace) auto-discovers the browser
# tools. Plain boxes don't have the file — the step is skipped. Don't clobber a
# PI-edited .mcp.json.
if [[ -f /opt/sandbox-box/extra-mcps.json && ! -f /workspace/.mcp.json ]]; then
    cp /opt/sandbox-box/extra-mcps.json /workspace/.mcp.json
    echo "sandbox-box[${RS_SANDBOX_NAME}]: wired playwright MCP into /workspace/.mcp.json"
fi

echo "sandbox-box[${RS_SANDBOX_NAME}]: ready (workspace at /workspace)"

# Idle. byobu sessions are created on first webui-tab / attach connect.
exec tail -f /dev/null
