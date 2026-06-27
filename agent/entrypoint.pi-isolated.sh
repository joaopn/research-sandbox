#!/usr/bin/env bash
# entrypoint.pi-isolated.sh — PI-isolated agent container boot.
#
# Generic: clones an operator-chosen skill repo, runs its setup, goes idle.
# RS has NO knowledge of the harness's conventions (vault layout, ingest
# commands, lint rules). Everything past "the repo is cloned and set up" is
# the harness's own concern, invoked by the PI in the byobu tab.
#
# Expected mounts (provided by the supervisor's enable call):
#   /workspace        — per-agent RW state (the pi tree, the container's
#                       /workspace root). The repo is cloned VISIBLY here as
#                       /workspace/<repo-name>. On the project volume, so the
#                       clone persists across recreate. Structurally isolated
#                       from the supervisor's /workspace.
#   $RS_PI_ISO_MOUNT  — the external host folder (<root>/<project>/), a subpath
#                       of /workspace (default /workspace/external). This is
#                       the PI's OWN content folder — the repo is NOT cloned
#                       here (that would pollute it). Exported so a SETUP
#                       command can point the harness at it (e.g. as a vault).
#
# Auth: PI-owned and boots un-authed — no creds staged here. The PI runs
# `/login` in the tab, or the operator pushes the supervisor's creds in via
# `rs-pi sync-creds`. bypassPermissions config is baked into rs-pi-isolated.
#
# Expected environment:
#   RS_PI_ISO_NAME   — type name (e.g. obsidian-wiki); used for log prefixes.
#   RS_PI_ISO_REPO   — git URL to clone, or empty for a pure-folder agent.
#   RS_PI_ISO_REF    — commit/tag to check out (set when REPO is set).
#   RS_PI_ISO_SETUP  — shell command run in the clone dir after checkout.
#   RS_PI_ISO_MOUNT  — container path of the external (content) folder.
#
# Startup order:
#   1. Restore home skel (volume hides image contents).
#   2. Clone repo to /workspace/<repo-name> (once), checkout REF, run SETUP.
#   3. tail -f /dev/null — byobu (a login shell) on first webui tab connect.

set -euo pipefail

: "${RS_PI_ISO_NAME:?RS_PI_ISO_NAME must be set}"
REPO="${RS_PI_ISO_REPO:-}"
REF="${RS_PI_ISO_REF:-}"
SETUP="${RS_PI_ISO_SETUP:-}"
LOG="pi-iso[${RS_PI_ISO_NAME}]"

# --- Restore home skel on first boot ---------------------------------------
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/worker-skel/. ~/
fi

# --- Deploy the agent (claude) from the supervisor-staged dist -------------
# Own writable ~/.local (no bake; STAGE_AGENT_DIST slice 2). Absence-guarded so a
# restart preserves an autoupdater bump; PI's interactive `claude` finds it via
# ~/.local/bin on PATH (self-healed below).
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
echo "${RS_PI_ISO_NAME}" > ~/.rs-role

# No artifact-contract surface: rebased onto rs-ext-base (lane-3), an isolated
# agent is FULLY PRIVATE — no published//internal/ dirs, no manifest verb, no
# Stop-hook gate. Its outputs stay in its own tree; nothing publishes back to the
# research supervisor.

# --- Auth: PI-owned, no staging --------------------------------------------
# Isolated agents boot un-authed. The tab is a login shell — claude only runs
# if/when the PI starts it there, and they `/login` at that point (or the
# operator pushes the supervisor's creds in via `rs-pi sync-creds`, which
# targets this container by its research.pi_role label). settings.json
# (hook-free bypassPermissions) is baked into rs-pi-isolated, so claude
# doesn't prompt.

# --- Clone / checkout / setup the harness repo -----------------------------
# Clone VISIBLY into the workspace root as /workspace/<repo-name> — NOT inside
# the mounted content folder ($RS_PI_ISO_MOUNT, which stays clean for the PI's
# own files), and NOT hidden. The pi tree (/workspace) is on the project
# volume, so the clone persists across recreate. REF is pinned — no silent
# upstream drift. SETUP runs on EVERY boot (the container's ~ resets on
# recreate, so any symlink-into-~/.claude/skills must be re-established) in the
# clone dir; it's expected idempotent + cheap — a harness whose setup is
# heavy/slow should graduate to a baked PI role (PLAN/STAGE_PI_ISOLATED.md Q3).
if [[ -n "${REPO}" ]]; then
    REPO_DIR="/workspace/$(basename "${REPO%.git}")"
    if [[ ! -d "${REPO_DIR}/.git" ]]; then
        echo "${LOG}: cloning ${REPO} → ${REPO_DIR}"
        rm -rf "${REPO_DIR}"
        git clone "${REPO}" "${REPO_DIR}"
    fi
    if [[ -n "${REF}" ]]; then
        echo "${LOG}: checkout ${REF}"
        git -C "${REPO_DIR}" fetch --depth 1 origin "${REF}" 2>/dev/null || true
        git -C "${REPO_DIR}" checkout --quiet "${REF}"
    fi
    if [[ -n "${SETUP}" ]]; then
        echo "${LOG}: running setup: ${SETUP}"
        ( cd "${REPO_DIR}" && bash -lc "${SETUP}" )
    fi
elif [[ -n "${SETUP}" ]]; then
    echo "${LOG}: running setup (no repo): ${SETUP}"
    ( cd /workspace && bash -lc "${SETUP}" )
fi

echo "${LOG}: ready (workspace at /workspace)"

# --- Keep the container alive ----------------------------------------------
# byobu sessions get created on first webui tab connect via the registered
# `command` field (a login shell — the PI starts claude themselves), not here.
exec tail -f /dev/null
