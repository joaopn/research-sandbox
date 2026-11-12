#!/usr/bin/env bash
# code-server-deploy.sh — deploy + lazy-launch the editor from the editor dist
# (STAGE_EDITOR_DIST). Lives IN the dist (/opt/editor-dist/tools/) and is invoked by
# an interactive container's entrypoint ONLY when the caller has already checked the
# coexistence guard: RS_SERVICE_CODE_SERVER=enabled, the /opt/editor-dist mount is
# populated, and no system bake exists (! -e /usr/bin/code-server). Single source of
# truth for the deploy so the interactive entrypoints (minimal / sandbox-dind /
# supervisor / sandbox-box) don't each duplicate it. Each of them guards the call
# the same way; the deploy cp's the binary from the dist into our OWN ~/.local.
# The host also runs it post-start through `docker exec` (a supervisor's editor
# deploy at create/recreate, and the live editor enable); that caller gates on the
# resolved code-server flag instead of the env guard, and step 0 below checks the
# dist is populated (a supervisor has no system bake).
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
# Remote settings: the browser tab is a remote window, so machine-scoped keys (the
# Claude Code extension's permission keys) apply only from Machine/settings.json.
# No-clobber like User: an operator's own Remote settings are theirs.
if [[ ! -f "${CS_USER_DIR}/Machine/settings.json" ]] && \
   [[ -f "${DIST}/templates/Machine/settings.json" ]]; then
    mkdir -p "${CS_USER_DIR}/Machine"
    cp "${DIST}/templates/Machine/settings.json" "${CS_USER_DIR}/Machine/settings.json"
fi

# 3/3b share one rule: a boot installs a shipped .vsix only when it is STRICTLY
# NEWER than the version this editor has active, or when none is installed. An
# equal version is left alone (the steady-state boot makes no install call), and
# an older shipped version never moves back an operator's newer one (the Extensions
# view can update an extension past the pin). The installed version is the one the
# editor's own registry (extensions.json) lists as active for the exact id — never
# a folder name: an upgrade leaves the previous version's folder on disk, folder
# names carry platform suffixes, and one id can prefix another (ms-toolsai.jupyter
# vs ms-toolsai.jupyter-keymap). The shipped version is read from the .vsix itself.
# The install runs without --force, so code-server's own downgrade refusal stays
# a second guard; when it refuses (e.g. the shipped extension needs a newer editor
# than this container runs), the warning below is printed and the active version
# stays. The project update still forces the shipped agent extension into running
# boxes; that path does not run this script.
AGENT_DIST="${AGENT_DIST_MOUNT:-/opt/agent-dist}"

# _newer_vsix <gate.vsix> [<candidate.vsix>…] — print the path of the .vsix to
# install, or nothing. The first argument is the gate: it fixes the extension id,
# and when it cannot be read nothing is installed. Further candidates carrying the
# same id compete on version (the newest wins, the gate on a tie). Warnings and the
# "keeping" line go to stderr.
_newer_vsix() {
    if ! command -v python3 >/dev/null 2>&1; then
        echo "WARNING: python3 not found; $(basename "$1" .vsix) not checked" >&2
        return 0
    fi
    python3 -I - "$CS_EXT_DIR" "$@" <<'PY'
import json, os, re, sys, zipfile

ext_dir, gate, *others = sys.argv[1:]
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def warn(msg):
    print(msg, file=sys.stderr)


def key(v):
    """Semver precedence key; None when v is not a version."""
    m = SEMVER.match(v) if isinstance(v, str) else None
    if not m:
        return None
    core = tuple(int(x) for x in m.group(1, 2, 3))
    if m.group(4) is None:
        return core + ((1,),)          # a release outranks its pre-releases
    ids = tuple((0, int(i), "") if i.isdigit() else (1, 0, i)
                for i in m.group(4).split("."))
    return core + ((0,) + ids,)


def read_vsix(path):
    with zipfile.ZipFile(path) as z:
        pkg = json.loads(z.read("extension/package.json"))
    pub, name, ver = pkg.get("publisher"), pkg.get("name"), pkg.get("version")
    if not all(isinstance(x, str) and x for x in (pub, name, ver)):
        raise ValueError("package.json lacks publisher, name or version")
    return f"{pub}.{name}".lower(), ver


base = os.path.basename(gate)[:-len(".vsix")] if gate.endswith(".vsix") else os.path.basename(gate)
try:
    ext_id, ver = read_vsix(gate)
except Exception as e:
    warn(f"WARNING: cannot read {gate}: {e}; {base} not checked")
    sys.exit(0)
best = (gate, ver)
for path in others:
    try:
        oid, over = read_vsix(path)
    except Exception as e:
        warn(f"WARNING: cannot read {path}: {e}")
        continue
    if oid != ext_id:
        continue
    if key(over) is None or key(best[1]) is None:
        warn(f"WARNING: {ext_id}: cannot compare versions {best[1]} and {over}; left as it is")
        sys.exit(0)
    if key(over) > key(best[1]):
        best = (path, over)
shipped_path, shipped = best

registry = os.path.join(ext_dir, "extensions.json")
installed = None
if os.path.exists(registry):
    try:
        with open(registry) as f:
            entries = json.load(f)
        if not isinstance(entries, list):
            raise ValueError("not a list")
    except Exception as e:
        warn(f"WARNING: cannot read the editor's extension list {registry}: {e}; {ext_id} left as it is")
        sys.exit(0)
    for e in entries:
        ident = e.get("identifier") if isinstance(e, dict) else None
        if not isinstance(ident, dict) or str(ident.get("id", "")).lower() != ext_id:
            continue
        v = e.get("version")
        if key(v) is None or key(shipped) is None:
            warn(f"WARNING: {ext_id}: cannot compare versions {v} and {shipped}; left as it is")
            sys.exit(0)
        if installed is None or key(v) > key(installed):
            installed = v

if installed is not None and key(shipped) is None:
    warn(f"WARNING: {ext_id}: cannot compare versions {installed} and {shipped}; left as it is")
    sys.exit(0)
if installed is None or key(shipped) > key(installed):
    print(shipped_path)
elif key(installed) > key(shipped):
    warn(f"keeping {ext_id} {installed} (newer than the shipped {shipped})")
PY
}

# 3. Install the bundled editor extensions (the editor dist's .vsix files).
if [[ -d "${DIST}/templates/extensions" ]]; then
    for vsix in "${DIST}"/templates/extensions/*.vsix; do
        [[ -f "$vsix" ]] || continue
        base=$(basename "$vsix" .vsix)
        pick=$(_newer_vsix "$vsix")
        [[ -n "$pick" ]] || continue
        echo "installing code-server extension: ${base}"
        code-server \
            --install-extension "$pick" \
            --extensions-dir "${CS_EXT_DIR}" \
            --user-data-dir "${CS_USER_DIR}" \
            || echo "WARNING: failed to install ${base}" >&2
    done
fi

# 3b. Install agent-bound editor extensions. The agent dist tucks its companion
#     .vsix at ~/.local/share/rs-agent-ext/<id>.vsix, so this glob is non-empty
#     IFF an agent dist was deployed into this container's ~/.local — the install
#     gate falls out of file presence, no launcher check. An agent-less box (e.g.
#     management) has no such dir → no-op. The project's CURRENT copy of the same
#     file is a candidate too: a box or docker project restarted with a plain
#     start keeps its old ~/.local (the agent is copied in only when absent), while
#     the project update re-stages the mounted agent dist in place — so the mount
#     (the supervisor's merged local/, or the docker substrate's per-agent
#     <agent>/local/) is where a newer extension shows up.
AGENT_EXT_DIR="$HOME/.local/share/rs-agent-ext"
if [[ -d "$AGENT_EXT_DIR" ]]; then
    for vsix in "$AGENT_EXT_DIR"/*.vsix; do
        [[ -f "$vsix" ]] || continue
        base=$(basename "$vsix" .vsix)
        name=$(basename "$vsix")
        shopt -s nullglob
        mounted=( "$AGENT_DIST"/local/share/rs-agent-ext/"$name" \
                  "$AGENT_DIST"/*/local/share/rs-agent-ext/"$name" )
        shopt -u nullglob
        candidates=( "$vsix" )
        for m in "${mounted[@]}"; do
            [[ -f "$m" ]] && candidates+=( "$m" )
        done
        pick=$(_newer_vsix "${candidates[@]}")
        [[ -n "$pick" ]] || continue
        echo "installing agent extension: ${base}"
        code-server \
            --install-extension "$pick" \
            --extensions-dir "${CS_EXT_DIR}" \
            --user-data-dir "${CS_USER_DIR}" \
            || echo "WARNING: failed to install ${base}" >&2
    done
fi

# 4. Launch the lazy-start stub (spawn code-server on first connect; once up it
#    lives until the container stops — deliberately NO idle reaper).
: "${CODE_SERVER_STUB_PORT:=8443}"
: "${CODE_SERVER_UPSTREAM_PORT:=8444}"
export CODE_SERVER_STUB_PORT CODE_SERVER_UPSTREAM_PORT
nohup "${DIST}/tools/code-server-stub.py" \
    > /tmp/code-server-stub.log 2>&1 &
echo "code-server (dist) stub launched on :${CODE_SERVER_STUB_PORT}; "\
"upstream :${CODE_SERVER_UPSTREAM_PORT}"
