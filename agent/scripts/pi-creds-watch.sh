#!/usr/bin/env bash
# pi-creds-watch.sh — propagate supervisor cred writes into PI containers.
#
# Spawned from entrypoint.supervisor.sh's first-boot block. Watches the
# supervisor's ~/.claude/ directory for atomic-rename writes to
# .credentials.json (the canonical pattern the OAuth library uses on
# refresh), then copies the new file into every running rs-pi-* container
# in the inner dockerd. Idempotent — running containers without the
# research.pi_role label are skipped.
#
# Watch idiom: inotifywait on the PARENT DIR for moved_to/create events
# matching .credentials.json — NOT inotifywait on the file itself. The
# OAuth-library write pattern (tmp.write + tmp.replace(canonical)) changes
# the canonical file's inode every time, which would invalidate any
# file-pinned watch. The parent-dir watch survives the rename and fires
# on every replacement. Same gotcha as the single-file-bind-mount rule
# inverted.
#
# Crash recovery: this script is restarted by the supervisor entrypoint's
# `while true; sleep` loop. If inotifywait dies, the loop respawns it
# within 10s. The propagation is best-effort; the manual fallback is
# `rs-pi sync-creds`.

set -uo pipefail

WATCH_DIR="${PI_CREDS_WATCH_DIR:-/home/research/.claude}"
WATCH_FILE="${PI_CREDS_WATCH_FILE:-.credentials.json}"

if ! command -v inotifywait >/dev/null 2>&1; then
    echo "pi-creds-watch: inotifywait missing (supervisor image needs inotify-tools); exiting" >&2
    exit 2
fi

if [[ ! -d "$WATCH_DIR" ]]; then
    echo "pi-creds-watch: $WATCH_DIR not yet present; waiting" >&2
    # The supervisor entrypoint stages ~/.claude after the watcher spawns
    # in some boot orderings. Block until the dir shows up, then proceed.
    while [[ ! -d "$WATCH_DIR" ]]; do
        sleep 2
    done
fi

propagate() {
    local src="${WATCH_DIR}/${WATCH_FILE}"
    [[ -s "$src" ]] || return 0
    local names
    names=$(docker ps --filter 'label=research.pi_role' --format '{{.Names}}' 2>/dev/null || true)
    [[ -z "$names" ]] && return 0
    local sup_hash
    sup_hash=$(sha256sum "$src" | cut -d' ' -f1)
    while IFS= read -r cn; do
        [[ -z "$cn" ]] && continue
        local pi_hash
        pi_hash=$(docker exec "$cn" sha256sum /home/worker/.claude/.credentials.json 2>/dev/null | cut -d' ' -f1 || true)
        if [[ "$sup_hash" == "$pi_hash" ]]; then
            continue
        fi
        # Stage to a temp path first; then `install` with the right uid/gid/mode
        # inside the container's user namespace (docker cp can't cleanly set
        # those across namespaces).
        if docker cp "$src" "$cn:/tmp/.credentials.json.new" 2>/dev/null; then
            docker exec "$cn" sh -c \
                'install -o worker -g worker -m 0600 /tmp/.credentials.json.new ~/.claude/.credentials.json && rm /tmp/.credentials.json.new' \
                2>/dev/null || \
                echo "pi-creds-watch: install failed in $cn" >&2
            echo "pi-creds-watch: propagated to $cn" >&2
        else
            echo "pi-creds-watch: docker cp to $cn failed" >&2
        fi
    done <<< "$names"
}

# Sibling propagation: ~/.claude.json (at $HOME root, NOT under .claude/).
# Carries `oauthAccount` + onboarding state. Without it, interactive claude
# in the PI tab prompts for /login even though .credentials.json is valid.
# This file rotates rarely (mostly account-claim + preferences), so we
# don't watch it live — startup propagate + manual `rs-pi sync-creds`
# covers it. Skipped silently if absent.
#
# The supervisor's raw ~/.claude.json carries `projects[<cwd>]` — per-cwd
# prompt history, allowedTools decisions, mcpContextUris — which would
# leak into the PI's claude UI if propagated whole. Filter via jq to the
# four-key allowlist below before docker-cp'ing. Same expression used by
# rs-pi sync-creds and research.py's `_pi_stage_creds`.
HOME_JSON_ALLOWLIST_JQ='{oauthAccount, userID, hasCompletedOnboarding, lastOnboardingVersion} | with_entries(select(.value != null))'

propagate_home_json() {
    local src="/home/research/.claude.json"
    [[ -s "$src" ]] || return 0
    local names
    names=$(docker ps --filter 'label=research.pi_role' --format '{{.Names}}' 2>/dev/null || true)
    [[ -z "$names" ]] && return 0
    local filtered
    filtered=$(mktemp)
    if ! jq "$HOME_JSON_ALLOWLIST_JQ" "$src" > "$filtered" 2>/dev/null; then
        echo "pi-creds-watch: ~/.claude.json jq filter failed; skipping" >&2
        rm -f "$filtered"
        return 0
    fi
    chmod 600 "$filtered"
    local sup_hash
    sup_hash=$(sha256sum "$filtered" | cut -d' ' -f1)
    while IFS= read -r cn; do
        [[ -z "$cn" ]] && continue
        local pi_hash
        pi_hash=$(docker exec "$cn" sha256sum /home/worker/.claude.json 2>/dev/null | cut -d' ' -f1 || true)
        if [[ "$sup_hash" == "$pi_hash" ]]; then
            continue
        fi
        if docker cp "$filtered" "$cn:/tmp/.claude.json.new" 2>/dev/null; then
            docker exec "$cn" sh -c \
                'install -o worker -g worker -m 0600 /tmp/.claude.json.new ~/.claude.json && rm /tmp/.claude.json.new' \
                2>/dev/null || \
                echo "pi-creds-watch: install ~/.claude.json failed in $cn" >&2
            echo "pi-creds-watch: propagated ~/.claude.json to $cn (filtered)" >&2
        else
            echo "pi-creds-watch: docker cp ~/.claude.json to $cn failed" >&2
        fi
    done <<< "$names"
    rm -f "$filtered"
}

echo "pi-creds-watch: watching $WATCH_DIR/$WATCH_FILE" >&2

# Run once at startup to catch the case where the file already exists +
# any pi container was started before the watcher came up.
propagate
propagate_home_json

# Then watch for changes. -m streams events; we filter for our file in
# bash rather than via --include because the regex syntax differs by
# inotify-tools version.
inotifywait -m -q -e moved_to,create,close_write "$WATCH_DIR" |
while read -r _ events name; do
    if [[ "$name" == "$WATCH_FILE" ]]; then
        propagate
    fi
done
