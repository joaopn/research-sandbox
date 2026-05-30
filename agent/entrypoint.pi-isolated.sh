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
#   /creds            — RO snapshot of supervisor creds (parent-dir bind-mount;
#                       atomic-rename writes stay visible).
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
#   2. Stage creds from /creds → ~/.claude/ (optional; not fatal if absent).
#   3. Clone repo to /workspace/<repo-name> (once), checkout REF, run SETUP.
#   4. tail -f /dev/null — byobu (a login shell) on first webui tab connect.

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

# Role marker for the byobu status-bar plugin (~/.byobu/bin/60_rolename).
echo "${RS_PI_ISO_NAME}" > ~/.rs-role

# --- Stage creds (optional — isolated agents boot fine un-authed) ----------
# UNLIKE entrypoint.pi.sh, missing creds are NOT fatal here. An isolated
# agent is a plain sandbox: it clones + idles at boot, and the tab is a
# login shell — claude only runs if/when the PI starts it there, and they
# can `/login` at that point. If the supervisor is authenticated, its creds
# are staged here (and pi-creds-watch.sh keeps them fresh on re-auth — this
# container carries the research.pi_role label the watcher selects on), so a
# `claude` the PI later starts is already authed. If not, we just proceed.
if [[ -f /creds/.credentials.json ]]; then
    mkdir -p ~/.claude
    cp /creds/.credentials.json ~/.claude/.credentials.json
    chmod 600 ~/.claude/.credentials.json
else
    echo "${LOG}: no /creds/.credentials.json — booting without creds; " \
         "run /login in the tab, or authenticate the supervisor and " \
         "re-sync (rs-pi sync-creds)." >&2
fi
if [[ -f /creds/settings.json ]]; then
    cp /creds/settings.json ~/.claude/settings.json
fi
# ~/.claude.json (sibling of ~/.claude/) carries oauthAccount + onboarding
# state; without it interactive claude prompts for /login on every attach.
# Staged filtered (four-key allowlist) host-side under the sentinel name.
if [[ -f /creds/home_claude.json ]]; then
    cp /creds/home_claude.json ~/.claude.json
    chmod 600 ~/.claude.json
fi

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
