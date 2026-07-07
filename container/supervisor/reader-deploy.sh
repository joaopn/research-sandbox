#!/usr/bin/env bash
# reader-deploy.sh — deploy + launch the mobile artifact reader from the reader
# dist (STAGE_READER). Lives IN the dist (/opt/reader-dist/tools/) and is invoked
# by a reader-enabled supervisor's entrypoint (and by the live-toggle exec) ONLY
# when the caller has already checked RS_SERVICE_READER=enabled + the mount is
# populated. Mirrors code-server-deploy.sh's cp-from-dist-into-~/.local shape, but
# the reader is a plain stdlib http server (no lazy-start stub, no extensions, no
# per-user settings), and it carries an ABI guard the editor doesn't need.
set -uo pipefail

DIST="${READER_DIST_MOUNT:-/opt/reader-dist}"
# The conda interpreter reader-server.py runs under (its shebang) — the ABI guard
# and the socket check use THIS, not bare `python3` (the system python, wrong
# minor / no guarantees). Same literal as rscore._CONDA_PY.
PYBIN="/opt/conda/bin/python"
[[ -x "$PYBIN" ]] || PYBIN="$(command -v python3 || echo python3)"   # fallback, best-effort

# 0. Defensive: an empty auto-created mount dir (unconditional -v against an
#    unstaged source) → do nothing rather than cp from a missing tree. The
#    entrypoint guard already checks this; keep the shared script self-safe.
if [[ ! -e "$DIST/.local/bin/jupyter-nbconvert" ]]; then
    echo "reader-deploy: $DIST not populated — reader dist not staged; skipping" >&2
    exit 0
fi

# 1. ABI guard. The reader payload is a pip tree tagged for the CPython minor
#    version it was BUILT against (~/.local/lib/python3.X). If a base-image
#    rebuild has since moved the container's interpreter to 3.Y, the cp would land
#    in lib/python3.X while this container's python3 looks in lib/python3.Y — a
#    silent import failure. Fail LOUD instead, naming the remedy. (The editor dist
#    is a self-contained node bundle and needs no such guard; the agent dist is
#    too — this coupling is unique to a pip-tree dist.)
DIST_ABI="$(cat "$DIST/PYTHON_ABI" 2>/dev/null || true)"
HERE_ABI="$("$PYBIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
if [[ -z "$DIST_ABI" || -z "$HERE_ABI" ]]; then
    echo "reader-deploy: could not determine python ABI (dist='$DIST_ABI' container='$HERE_ABI'); skipping" >&2
    exit 0
fi
if [[ "$DIST_ABI" != "$HERE_ABI" ]]; then
    echo "reader-deploy: reader dist built for python ${DIST_ABI}, container has ${HERE_ABI}." >&2
    echo "reader-deploy: rebuild it on the host with \`research reader pull\`, then recreate/update the project. Skipping." >&2
    exit 0
fi

# 2. Deploy the reader payload into our OWN writable ~/.local ONCE. Absence-guarded
#    (a docker-start restart finds it present and skips), same discipline as the
#    editor/agent dists.
if [[ ! -e "$HOME/.local/bin/jupyter-nbconvert" ]]; then
    mkdir -p "$HOME/.local"
    cp -a "$DIST/.local/." "$HOME/.local/"
fi
export PATH="$HOME/.local/bin:$PATH"

# 3. Launch the reader server (idempotent: skip if already listening on the port).
: "${READER_PORT:=8445}"
export READER_PORT
if "$PYBIN" -c "import socket,sys; s=socket.socket(); r=s.connect_ex(('127.0.0.1',int('${READER_PORT}'))); s.close(); sys.exit(0 if r==0 else 1)" 2>/dev/null; then
    echo "reader-deploy: something already listening on :${READER_PORT}; not relaunching"
    exit 0
fi
nohup "${DIST}/tools/reader-server.py" > /tmp/reader.log 2>&1 &
echo "reader (dist) launched on :${READER_PORT}"
