#!/usr/bin/env bash
# code-server-deploy.sh — deploy + lazy-launch the editor from the editor dist
# (STAGE_EDITOR_DIST). Lives IN the dist (/opt/editor-dist/tools/) and is invoked by
# an interactive container's entrypoint ONLY when the caller has already checked the
# coexistence guard: RS_SERVICE_CODE_SERVER=enabled, the /opt/editor-dist mount is
# populated, and no system bake exists (! -e /usr/bin/code-server). Single source of
# truth for the deploy so the three interactive entrypoints (pi / pi-isolated /
# sandbox-box) don't each duplicate it. Mirrors the baked code-server block in
# entrypoint.minimal.sh, but cp's the binary from the dist into our OWN ~/.local.
set -uo pipefail

DIST="${EDITOR_DIST_MOUNT:-/opt/editor-dist}"

# 0. Defensive: if the dist isn't actually populated (an empty auto-created mount
#    dir from an unconditional -v against an unstaged source), do nothing rather
#    than cp from a missing tree and then launch a dead stub. The entrypoint guard
#    already checks this, but the deploy is a shared script — keep it self-safe.
if [[ ! -e "$DIST/.local/bin/code-server" ]]; then
    echo "code-server-deploy: $DIST/.local not populated — editor dist not staged; skipping" >&2
    exit 0
fi

# 1. Deploy the editor into our OWN writable ~/.local ONCE. Absence-guarded (not
#    first-boot): a docker-start restart finds the launcher present and skips, so a
#    code-server autoupdater bump is never clobbered. The mount is an inert RO
#    copy-source; the container runs from its own copy.
if [[ ! -e "$HOME/.local/bin/code-server" ]]; then
    mkdir -p "$HOME/.local"
    cp -a "$DIST/.local/." "$HOME/.local/"
fi
export PATH="$HOME/.local/bin:$PATH"

# 2. code-server user dir on the workspace volume (persists across restarts).
CS_USER_DIR=/workspace/.local/share/code-server
CS_EXT_DIR="${CS_USER_DIR}/extensions"
mkdir -p "${CS_USER_DIR}/User" "${CS_EXT_DIR}"

if [[ ! -f "${CS_USER_DIR}/User/settings.json" ]] && \
   [[ -f "${DIST}/templates/User/settings.json" ]]; then
    cp "${DIST}/templates/User/settings.json" "${CS_USER_DIR}/User/settings.json"
fi

# 3. Install pre-staged .vsix extensions (datawrangler) if not already present.
if [[ -d "${DIST}/templates/extensions" ]]; then
    for vsix in "${DIST}"/templates/extensions/*.vsix; do
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

# 3b. Install agent-bound editor extensions (STAGE_AGENT_EXTENSIONS). The agent
#     dist tucks its companion .vsix at ~/.local/share/rs-agent-ext/<agent>.vsix
#     (B-tuck), so this glob is non-empty IFF an agent dist was deployed into
#     this container's ~/.local — the install gate falls out of file presence,
#     no launcher check. An agent-less box (e.g. management) has no such dir →
#     no-op. Same idempotent already-installed skip as step 3; a .vsix version
#     bump only reaches FRESH / recreated / restarted containers (the installed
#     folder name carries the old version, so the skip matches), never an
#     in-place upgrade — identical to the Data Wrangler lane above.
AGENT_EXT_DIR="$HOME/.local/share/rs-agent-ext"
if [[ -d "$AGENT_EXT_DIR" ]]; then
    for vsix in "$AGENT_EXT_DIR"/*.vsix; do
        [[ -f "$vsix" ]] || continue
        base=$(basename "$vsix" .vsix)
        shopt -s nullglob
        existing=( "${CS_EXT_DIR}/"*"${base}"* )
        shopt -u nullglob
        if (( ${#existing[@]} > 0 )); then
            continue
        fi
        echo "installing agent extension: ${base}"
        code-server \
            --install-extension "$vsix" \
            --extensions-dir "${CS_EXT_DIR}" \
            --user-data-dir "${CS_USER_DIR}" \
            || echo "WARNING: failed to install ${base}" >&2
    done
fi

# 4. Launch the lazy-start stub (spawn code-server on first connect, reap on idle).
: "${CODE_SERVER_STUB_PORT:=8443}"
: "${CODE_SERVER_UPSTREAM_PORT:=8444}"
: "${CODE_SERVER_IDLE_SECONDS:=1800}"
export CODE_SERVER_STUB_PORT CODE_SERVER_UPSTREAM_PORT CODE_SERVER_IDLE_SECONDS
nohup "${DIST}/tools/code-server-stub.py" \
    > /tmp/code-server-stub.log 2>&1 &
echo "code-server (dist) stub launched on :${CODE_SERVER_STUB_PORT}; "\
"upstream :${CODE_SERVER_UPSTREAM_PORT}; idle reap ${CODE_SERVER_IDLE_SECONDS}s"
