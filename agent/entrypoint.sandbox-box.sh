#!/usr/bin/env bash
# entrypoint.sandbox-box.sh — disposable sandbox box boot (STAGE_SANDBOX_PROJECT,
# preset-driven since STAGE_BOX_EXT_UX).
#
# An isolated box for running un-vetted code / scoped agent work. Deliberately
# clean: NO artifact-contract (no published/ or internal/ dirs, no manifest verb,
# no Stop-hook gate), NO credentials. The PI drives the box from the webui tab /
# `project attach`. Instructions (CLAUDE.md) + the proxy MCP source (.mcp-proxy.json)
# are pre-staged into /workspace by the host's `rs-sandbox create` BEFORE boot;
# this entrypoint deploys the agent/editor, optionally clones a BYO repo, and
# regenerates /workspace/.mcp.json from the two MCP sources.
#
# Environment:
#   RS_SANDBOX_NAME      — box name (e.g. box-1); used for the role marker + logs.
#   RS_BOX_AGENT         — agent to deploy: claude | none (default none).
#   RS_SERVICE_CODE_SERVER — enabled | disabled: the box's OWN editor toggle.
#   RS_BOX_CLONE_REPO/REF/SETUP — (byo preset) repo to clone + ref + setup cmd.

set -euo pipefail

: "${RS_SANDBOX_NAME:?RS_SANDBOX_NAME must be set}"

# Restore home from skel on first boot (the volume mount hides image contents).
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/worker-skel/. ~/
fi

# Deploy the agent (claude) from the management-supervisor-staged dist into our
# OWN writable ~/.local (no bake; STAGE_AGENT_DIST slice 2) — ONLY when the box
# was created with an agent (RS_BOX_AGENT=claude). Blank by default (unset/none
# → no agent binary at all). Even with claude the box is auth-free (run `claude`
# + /login inside). Absence-guarded so a restart preserves an autoupdater bump.
if [[ "${RS_BOX_AGENT:-none}" == "claude" && -d /opt/agent-dist && ! -e ~/.local/bin/claude ]]; then
    mkdir -p ~/.local
    cp -a /opt/agent-dist/local/. ~/.local/
fi
# Bundled bypass settings (no hooks) — no-clobber, only when claude is deployed
# (STAGE_AGENT_DIST_SETTINGS; the dist is a fixed tree {local/, claude/}).
if [[ "${RS_BOX_AGENT:-none}" == "claude" && -f /opt/agent-dist/claude/settings.json && ! -e ~/.claude/settings.json ]]; then
    mkdir -p ~/.claude
    cp /opt/agent-dist/claude/settings.json ~/.claude/settings.json
fi
if ! grep -q '\.local/bin' ~/.bashrc 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
fi

# --- code-server editor (dist) — STAGE_EDITOR_DIST. Deploy + lazy-launch from
#     /opt/editor-dist when this box opted in (RS_SERVICE_CODE_SERVER=enabled —
#     the box-level toggle, default off since STAGE_BOX_EXT_UX), the mount is
#     populated, and no system bake exists. ONE shared script; non-fatal.
if [[ "${RS_SERVICE_CODE_SERVER:-disabled}" == "enabled" ]] \
   && [[ -e /opt/editor-dist/.local/bin/code-server ]] \
   && [[ ! -e /usr/bin/code-server ]]; then
    bash /opt/editor-dist/tools/code-server-deploy.sh || true
fi

# Role marker for the byobu status-bar plugin (~/.byobu/bin/60_rolename).
echo "${RS_SANDBOX_NAME}" > ~/.rs-role

# bypassPermissions so an in-box `claude` doesn't prompt (the container is the
# security boundary). Crucially NO `hooks` key — the artifact-contract gate is
# deliberately absent here, unlike rs-pi-base.
mkdir -p ~/.claude
if [[ ! -f ~/.claude/settings.json ]]; then
    printf '%s\n' '{"permissions": {"defaultMode": "bypassPermissions"}, "theme": "dark"}' \
        > ~/.claude/settings.json
fi

# --- BYO clone (byo preset): clone repo@ref + run setup --------------------
# RS_BOX_CLONE_REPO/REF/SETUP are set only for a `byo` box. Mirrors
# entrypoint.pi-isolated.sh: clone VISIBLY to /workspace/<repo-name>, pin REF (no
# drift), run SETUP in the clone (every boot — the container ~ resets on recreate;
# expected idempotent + cheap). SETUP runs in THIS box shell (the value arrived as
# host -e argv, never via a host shell).
if [[ -n "${RS_BOX_CLONE_REPO:-}" ]]; then
    REPO_DIR="/workspace/$(basename "${RS_BOX_CLONE_REPO%.git}")"
    if [[ ! -d "${REPO_DIR}/.git" ]]; then
        echo "sandbox-box[${RS_SANDBOX_NAME}]: cloning ${RS_BOX_CLONE_REPO} → ${REPO_DIR}"
        rm -rf "${REPO_DIR}"
        git clone "${RS_BOX_CLONE_REPO}" "${REPO_DIR}"
    fi
    if [[ -n "${RS_BOX_CLONE_REF:-}" ]]; then
        echo "sandbox-box[${RS_SANDBOX_NAME}]: checkout ${RS_BOX_CLONE_REF}"
        git -C "${REPO_DIR}" fetch --depth 1 origin "${RS_BOX_CLONE_REF}" 2>/dev/null || true
        git -C "${REPO_DIR}" checkout --quiet "${RS_BOX_CLONE_REF}"
    fi
    if [[ -n "${RS_BOX_CLONE_SETUP:-}" ]]; then
        echo "sandbox-box[${RS_SANDBOX_NAME}]: running setup"
        ( cd "${REPO_DIR}" && bash -lc "${RS_BOX_CLONE_SETUP}" )
    fi
fi

# --- Render /workspace/.mcp.json from two sources (STAGE_BOX_EXT_UX) -------
# Regenerated WHOLESALE every boot — idempotent across reboots (NOT a blind
# append into an already-merged file, which would re-collide on the second boot).
#   source-1 = /workspace/.mcp-proxy.json  — proxy MCP servers the host wrote at
#              create/restart from the project allowlist (may be absent/empty).
#   source-2 = /opt/sandbox-box/extra-mcps.json — image-baked stdio MCPs (the
#              browser box's Playwright); base boxes don't carry the file.
# A name collision between the two is a hard error (refuse to start) so a project
# MCP cannot silently shadow the baked browser tooling.
python3 - <<'PYEOF'
import json
import sys
from pathlib import Path

proxy_path = Path("/workspace/.mcp-proxy.json")
extras_path = Path("/opt/sandbox-box/extra-mcps.json")
mcp_path = Path("/workspace/.mcp.json")
inv_path = Path("/workspace/.tools-inventory.md")

servers: dict = {}

# Source 1: proxy MCPs (host-resolved, may be absent/empty).
try:
    data = json.loads(proxy_path.read_text())
    s = data.get("mcpServers") if isinstance(data, dict) else None
    if isinstance(s, dict):
        servers.update(s)
except (OSError, json.JSONDecodeError):
    pass

# Source 2: image-baked stdio MCPs (browser box only).
if extras_path.is_file():
    try:
        extras = json.loads(extras_path.read_text())
    except json.JSONDecodeError as e:
        print(f"sandbox-box: extra-mcps.json invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    es = extras.get("mcpServers") if isinstance(extras, dict) else None
    if es is None:
        es = {}
    if not isinstance(es, dict):
        print("sandbox-box: extra-mcps.json mcpServers must be an object",
              file=sys.stderr)
        sys.exit(1)
    collisions = sorted(n for n in es if n in servers)
    if collisions:
        print(f"sandbox-box: image-baked / project MCP name collision: "
              f"{', '.join(map(repr, collisions))}; refusing to start. "
              f"Rename or drop the colliding project MCP.", file=sys.stderr)
        sys.exit(1)
    for n, cfg in es.items():
        if isinstance(cfg, dict):
            cfg = dict(cfg)
            cfg.setdefault("type", "stdio")
        servers[n] = cfg

if servers:
    mcp_path.write_text(json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n")
    rows = []
    for n, cfg in sorted(servers.items()):
        t = cfg.get("type", "?") if isinstance(cfg, dict) else "?"
        loc = cfg.get("url", "(stdio)") if isinstance(cfg, dict) else "?"
        rows.append(f"| `{n}` | {t} | {loc} |")
    inv_path.write_text(
        f"# Tools wired into this box\n\n"
        f"Rendered at boot from .mcp-proxy.json (project MCPs) + image-baked tools.\n"
        f"claude auto-discovers /workspace/.mcp.json — call tools by name.\n\n"
        f"| Name | Type | Location |\n|---|---|---|\n" + "\n".join(rows) + "\n")
else:
    for p in (mcp_path, inv_path):
        if p.exists():
            p.unlink()
PYEOF

echo "sandbox-box[${RS_SANDBOX_NAME}]: ready (workspace at /workspace)"

# Idle. byobu sessions are created on first webui-tab / attach connect.
exec tail -f /dev/null
