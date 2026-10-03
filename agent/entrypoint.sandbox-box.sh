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
# regenerates /workspace/.mcp.json from the MCP sources (+ pi's link to it).
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

# Preset input fields (~/.rs-box.env, staged 0600 by the host BEFORE first
# start via create→cp→start). Sourced HERE — above the agent deploy and the
# editor spawn — so every child of this entrypoint (the code-server stub, its
# integrated terminals, the preset setup below) inherits the values; login
# shells get them via the .bashrc source line added further down. On these
# boxes the skel restore above never fires (image-resident home), which is
# what makes the pre-start docker cp safe from being clobbered.
if [[ -f ~/.rs-box.env ]]; then
    . ~/.rs-box.env
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
# Login shells (byobu tabs, the agent, its MCP servers) inherit the preset
# field values too — the agent expands ${FIELD} references in .mcp.json from
# its own environment, which these lines populate. TWO homes, deliberately:
# ~/.bashrc covers interactive non-login bash, while ~/.profile covers every
# LOGIN shell — sh-family AND non-interactive `bash -lc`/`sh -lc` scripts,
# which never reach ~/.bashrc (Debian's early-return + bash-only sourcing;
# the same shell-family gap the /etc/profile.d PATH fix exists for).
if ! grep -q 'rs-box\.env' ~/.bashrc 2>/dev/null; then
    echo '[ -f "$HOME/.rs-box.env" ] && . "$HOME/.rs-box.env"' >> ~/.bashrc
fi
if ! grep -q 'rs-box\.env' ~/.profile 2>/dev/null; then
    echo '[ -f "$HOME/.rs-box.env" ] && . "$HOME/.rs-box.env"' >> ~/.profile
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
# security boundary). Crucially NO `hooks` key — the supervisor's rs-audit-stop
# gate is deliberately absent here. The `attribution` block switches git/PR
# attribution OFF (co-author trailer, PR footer, AND the private session link —
# three keys) and `remoteControlAtStartup: false` keeps the Remote Control bridge
# (/rc) OFF by default (an absent key falls to the rollout default, which
# auto-starts it; rationale for both at rscore._AGENT_SETTINGS_JSON). This
# printf, that constant, and the supervisor setup.sh heredoc are a three-writer
# mirror, pytest-pinned; keep the JSON on ONE line (the pin json-loads it off
# this line).
mkdir -p ~/.claude
if [[ ! -f ~/.claude/settings.json ]]; then
    printf '%s\n' '{"permissions": {"defaultMode": "bypassPermissions"}, "theme": "dark", "env": {"CLAUDE_CODE_DISABLE_MOUSE_CLICKS": "1"}, "attribution": {"commit": "", "pr": "", "sessionUrl": false}, "remoteControlAtStartup": false}' \
        > ~/.claude/settings.json
fi

# --- Dev clone (dev preset): authed fork clone via the shared gitea --------
# GITEA_URL/TOKEN/USER + REPO_NAME are set only for a dev box (rs-sandbox's dev
# lane; mutually exclusive with RS_BOX_CLONE_* — the catalog forces clone:false
# on dev presets). credential-helper-store so later push/pull authenticate;
# origin = the agent's fork, upstream = the read-only mirror. Clone-if-absent
# idempotent, like the byo block below. FAILURE POSTURE = byo parity: a clone
# failure (gitea down, stale staged IP) exits non-zero and the box crash-loops
# under --restart unless-stopped until the designed heal (host re-stage →
# `rs-sandbox restart`) re-runs it with fresh wiring — deliberate, not a bug.
if [[ -n "${GITEA_TOKEN:-}" && -n "${GITEA_URL:-}" && -n "${REPO_NAME:-}" ]]; then
    ( umask 077 && printf '%s\n' \
        "${GITEA_URL//:\/\//:\/\/${GITEA_USER}:${GITEA_TOKEN}@}" > ~/.git-credentials )
    git config --global credential.helper store
    # The clone carries TWO remotes (origin=fork, upstream=mirror) holding the
    # same branch names, which makes `git checkout <branch>` ambiguous for every
    # branch but the cloned one ("matched multiple (2) remote tracking
    # branches"). defaultRemote restores the DWIM against the fork. --global is
    # correct: one repo per container is a structural invariant.
    git config --global checkout.defaultRemote origin
    DEV_REPO_DIR="/workspace/${REPO_NAME}"
    if [[ ! -d "${DEV_REPO_DIR}/.git" ]]; then
        echo "sandbox-box[${RS_SANDBOX_NAME}]: cloning fork ${GITEA_USER}/${REPO_NAME}"
        rm -rf "${DEV_REPO_DIR}"
        # Base branch at FIRST clone only — never re-asserted on a later boot
        # (the agent owns its fork and may be on any branch by then). The `:-`
        # form matches this block's own guard above: a bare deref would be an
        # unbound-variable death on a box created before this field existed.
        if [[ -n "${GITEA_BRANCH:-}" ]]; then
            git clone --branch "${GITEA_BRANCH}" \
                "${GITEA_URL}/${GITEA_USER}/${REPO_NAME}.git" "${DEV_REPO_DIR}"
        else
            git clone "${GITEA_URL}/${GITEA_USER}/${REPO_NAME}.git" "${DEV_REPO_DIR}"
        fi
    fi
    if ! git -C "${DEV_REPO_DIR}" remote get-url upstream >/dev/null 2>&1; then
        git -C "${DEV_REPO_DIR}" remote add upstream \
            "${GITEA_URL}/sandbox-admin/${REPO_NAME}.git"
    fi
    # The agent's git identity, pinned repo-LOCAL in the fork clone (B36): the
    # consumer's gitea account name + that account's email. Every boot,
    # idempotent (same values). LOCAL, not --global: it scopes the identity to
    # this clone and its worktrees (they share .git/config), and the clone
    # lives on the box's workspace volume, so a re-run keeps it with no heal.
    # The `@rs.invalid` literal MIRRORS cli/gitea.py's EMAIL_DOMAIN (this
    # script cannot import it) — a pytest pins the two together.
    git -C "${DEV_REPO_DIR}" config user.name "${GITEA_USER}"
    git -C "${DEV_REPO_DIR}" config user.email "${GITEA_USER}@rs.invalid"
    git -C "${DEV_REPO_DIR}" fetch upstream --quiet || true
fi

# --- BYO clone (byo preset): clone repo@ref + run setup --------------------
# RS_BOX_CLONE_REPO/REF/SETUP are set only for a `byo` box: clone VISIBLY to
# /workspace/<repo-name>, pin REF (no drift), run SETUP in the clone (every boot —
# the container ~ resets on recreate; expected idempotent + cheap). SETUP runs in
# THIS box shell (the value arrived as host -e argv, never via a host shell).
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

# --- Preset setup (RS_BOX_SETUP): run ONCE per container filesystem --------
# Sentinel semantics, deliberately unlike the byo clone setup above: a preset
# setup installs software (e.g. a pinned pip package), so re-running every
# boot would put the network on the RESTART path — the sentinel keeps plain
# docker stop/start offline-safe. A re-run/recreate wipes the fs → the
# sentinel is gone → setup re-runs (the install must land on the fresh fs).
# Runs AFTER the env source above (a setup may rely on the field values) and
# with set -e in force: a failure kills the boot and the box crash-loops under
# --restart unless-stopped until healed (byo parity — deliberate, not a bug).
if [[ -n "${RS_BOX_SETUP:-}" && ! -f ~/.rs-box-setup-done ]]; then
    echo "sandbox-box[${RS_SANDBOX_NAME}]: running preset setup"
    # Plain bash -c, NOT -l: a login shell rebuilds PATH from /etc/profile and
    # LOSES the image's conda/docker ENV PATH (pip → exit 127; the bare-python3
    # class — and this box lineage forks off BEFORE minimal-base's profile.d
    # PATH drop-in, so a login shell here has neither conda nor ~/.local/bin).
    # This entrypoint already sourced ~/.rs-box.env above, so the setup inherits
    # the field values AND the full image environment. The ~/.local/bin prepend
    # lets a setup that installs a console script also RUN it
    # (pip install --user X && X --init).
    ( cd /workspace && PATH="$HOME/.local/bin:$PATH" bash -c "${RS_BOX_SETUP}" )
    touch ~/.rs-box-setup-done
fi

# --- Render /workspace/.mcp.json from three sources (STAGE_BOX_EXT_UX) -----
# Regenerated WHOLESALE every boot — idempotent across reboots (NOT a blind
# append into an already-merged file, which would re-collide on the second boot).
#   source-1 = /workspace/.mcp-proxy.json  — proxy MCP servers the host wrote at
#              create/restart from the project allowlist (may be absent/empty).
#   source-2 = /opt/sandbox-box/extra-mcps.json — image-baked stdio MCPs (the
#              browser box's Playwright); base boxes don't carry the file.
# A name collision between the two is a hard error (refuse to start) so a project
# MCP cannot silently shadow the baked browser tooling.
# pi reads the SAME servers: pi's built-in MCP takes a project's servers from
# <cwd>/.pi/mcp.json (never .mcp.json), in the identical `mcpServers` shape, so
# /workspace/.pi/mcp.json is a symlink to ../.mcp.json. Every entry carries
# `"exposure": "direct"` (pi declares the tools to the model by name, as claude
# does; pi's default hides them behind its scripting tool, and it has no global
# default) — claude ignores the key. pi's `mcp list` shell command trusts a
# project only by its trust.json (never the dist's defaultProjectTrust), so
# /workspace is recorded trusted there too, in a box that deploys pi.
python3 - <<'PYEOF'
import json
import os
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

# Source 3: preset-declared stdio MCPs (host-staged from the box catalog into
# /workspace/.mcp-preset.json). Values are copied VERBATIM — ${FIELD}
# references are expanded by the AGENT at config load, never by this merge.
# Checked against the UNION of sources 1+2 (must run AFTER source 2, or a
# preset-vs-baked collision slips through this backstop — the host pre-flight
# normally refuses first, but a backstop nobody exercises is one nobody
# notices is broken). Collision policy: refuse-to-start, same as source 2.
preset_path = Path("/workspace/.mcp-preset.json")
if preset_path.is_file():
    try:
        pre = json.loads(preset_path.read_text())
    except json.JSONDecodeError as e:
        print(f"sandbox-box: .mcp-preset.json invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    ps = pre.get("mcpServers") if isinstance(pre, dict) else None
    if ps is None:
        ps = {}
    if not isinstance(ps, dict):
        print("sandbox-box: .mcp-preset.json mcpServers must be an object",
              file=sys.stderr)
        sys.exit(1)
    collisions = sorted(n for n in ps if n in servers)
    if collisions:
        print(f"sandbox-box: preset / baked-or-project MCP name collision: "
              f"{', '.join(map(repr, collisions))}; refusing to start. "
              f"Rename the preset's server or drop the colliding source.",
              file=sys.stderr)
        sys.exit(1)
    for n, cfg in ps.items():
        if isinstance(cfg, dict):
            cfg = dict(cfg)
            cfg.setdefault("type", "stdio")
        servers[n] = cfg

pi_link = Path("/workspace/.pi/mcp.json")
PI_LINK_TARGET = "../.mcp.json"


def pi_link_is_ours() -> bool:
    return pi_link.is_symlink() and os.readlink(pi_link) == PI_LINK_TARGET


def pi_link_set() -> None:
    """Point /workspace/.pi/mcp.json at the rendered file — idempotent across
    boots. Anything else at that path (a file the PI made while no link existed,
    a link of their own) is the PI's: never touched, the link is skipped and pi
    uses theirs. Never fatal (set -e would crash-loop the box over pi wiring)."""
    try:
        if pi_link_is_ours():
            return
        if pi_link.is_symlink() or pi_link.exists() or (
                pi_link.parent.exists() and not pi_link.parent.is_dir()):
            print(f"sandbox-box: {pi_link} is the PI's own file; pi will not see "
                  f"the box's MCP servers (remove it to restore the link)", file=sys.stderr)
            return
        pi_link.parent.mkdir(exist_ok=True)
        tmp = pi_link.with_name(pi_link.name + ".rs-tmp")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(PI_LINK_TARGET, tmp)
        os.replace(tmp, pi_link)
    except OSError as e:
        print(f"sandbox-box: could not link {pi_link}: {e}", file=sys.stderr)


def pi_trust_workspace() -> None:
    """Record /workspace trusted in pi's trust.json (merged; a decision already
    there for /workspace is the PI's and stays). Only in a box that deploys pi
    (PI_CODING_AGENT_DIR is pi's env). Never fatal."""
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    if not agent_dir:
        return
    trust = Path(agent_dir) / "trust.json"
    try:
        data = json.loads(trust.read_text()) if trust.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        print(f"sandbox-box: leaving {trust} alone ({e})", file=sys.stderr)
        return
    if "/workspace" in data:
        return
    data["/workspace"] = True
    try:
        trust.parent.mkdir(parents=True, exist_ok=True)
        tmp = trust.with_name(trust.name + ".rs-tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, trust)
    except OSError as e:
        print(f"sandbox-box: could not write {trust}: {e}", file=sys.stderr)


pi_trust_workspace()
if servers:
    for cfg in servers.values():
        if isinstance(cfg, dict):
            cfg.setdefault("exposure", "direct")
    mcp_path.write_text(json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n")
    pi_link_set()
    rows = []
    for n, cfg in sorted(servers.items()):
        t = cfg.get("type", "?") if isinstance(cfg, dict) else "?"
        loc = cfg.get("url", "(stdio)") if isinstance(cfg, dict) else "?"
        rows.append(f"| `{n}` | {t} | {loc} |")
    inv_path.write_text(
        f"# Tools wired into this box\n\n"
        f"Rendered at boot from .mcp-proxy.json (project MCPs) + image-baked + preset tools.\n"
        f"claude auto-discovers /workspace/.mcp.json (pi reads it through "
        f"/workspace/.pi/mcp.json) — call tools by name. /workspace/.mcp.json "
        f"is rewritten at every boot: a server added to it (also by `pi mcp add "
        f"-l`, which writes through the link) lasts until the box restarts.\n\n"
        f"| Name | Type | Location |\n|---|---|---|\n" + "\n".join(rows) + "\n")
else:
    for p in (mcp_path, inv_path):
        if p.exists():
            p.unlink()
    try:
        if pi_link_is_ours():
            pi_link.unlink()
    except OSError as e:
        print(f"sandbox-box: could not remove {pi_link}: {e}", file=sys.stderr)
PYEOF

echo "sandbox-box[${RS_SANDBOX_NAME}]: ready (workspace at /workspace)"

# Idle. byobu sessions are created on first webui-tab / attach connect.
exec tail -f /dev/null
