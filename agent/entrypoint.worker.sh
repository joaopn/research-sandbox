#!/usr/bin/env bash
# entrypoint.worker.sh — session-scoped persistent analysis worker.
#
# Lifecycle:
#   - The bind-mount at /workspace persists across container incarnations
#     (down → live → down). summary.md, outputs/<slug>/, research_log.md,
#     scratch/ all survive between sessions; the container does not.
#   - On start: (re)run the initial task from /workspace/task.md, then poll
#     /workspace/inbox/ for follow-up messages.
#   - On SIGTERM (from `rs-worker shutdown` / docker stop): trap fires,
#     touches /workspace/DONE, exits 0 — the supervisor's registry moves
#     this worker to `state: down`.
#
# Contract with the supervisor (set up by rs-worker spawn):
#   /workspace/task.md            current-session brief
#   /workspace/CLAUDE.md          worker role doc (persistent contract)
#   /workspace/.claude/           creds + settings, staged at spawn
#   /workspace/summary.md         prior-session memory (absent on first spawn)
#   /workspace/inbox/             follow-up messages: msg_<unix_ts>.md
#   /workspace/outputs/<slug>/    per-cycle deliverables, accumulate
#   /workspace/research_log.md    accumulating narrative
#   /workspace/scratch/           accumulating working memory
#   /workspace/WAITING            set while idle; cleared while working
#   /workspace/DONE               set on clean shutdown

set -euo pipefail

# Restore home skel if the worker's /home was shadowed by a first-boot volume.
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/worker-skel/. ~/
fi

# Stage creds + settings from the supervisor-written drop at /workspace/.claude/
# into the worker user's home. Claude Code's OAuth file is a hidden file.
if [[ -f /workspace/.claude/.credentials.json ]]; then
    mkdir -p ~/.claude
    cp /workspace/.claude/.credentials.json ~/.claude/.credentials.json
    chmod 600 ~/.claude/.credentials.json
fi
if [[ -f /workspace/.claude/settings.json ]]; then
    mkdir -p ~/.claude
    cp /workspace/.claude/settings.json ~/.claude/settings.json
fi
# ~/.claude.json (sibling of ~/.claude/, carrying `oauthAccount` claim).
# Staged by rs-worker spawn under the sentinel name `home_claude.json`
# inside .claude/ to avoid colliding with the dir convention. Without
# this, the worker's claude treats the user as logged-out.
if [[ -f /workspace/.claude/home_claude.json ]]; then
    cp /workspace/.claude/home_claude.json ~/.claude.json
    chmod 600 ~/.claude.json
fi

cd /workspace
export PATH="$HOME/.local/bin:/opt/conda/bin:$PATH"

# Clear stale sentinels from a prior incarnation on this same bind-mount.
rm -f /workspace/WAITING /workspace/DONE

# Clean shutdown on SIGTERM / SIGINT: drop WAITING, leave DONE for the
# supervisor's shutdown CLI to observe.
trap 'rm -f /workspace/WAITING; touch /workspace/DONE; exit 0' TERM INT

MCP_ARG=()
if [[ -f /workspace/.mcp.json ]]; then
    MCP_ARG=(--mcp-config /workspace/.mcp.json)
fi

run_claude() {
    claude --print "$(cat "$1")" \
        --output-format stream-json \
        --verbose \
        --permission-mode bypassPermissions \
        "${MCP_ARG[@]}" \
        >> /workspace/log.jsonl 2>&1 || true
}

if [[ ! -f /workspace/task.md ]]; then
    echo "error: /workspace/task.md missing; spawn did not stage the task." >&2
    touch /workspace/DONE
    exit 2
fi

echo "=== Worker starting ==="
echo "Task: $(head -n 1 /workspace/task.md)"

# Initial cycle: run the task from task.md, then enter the inbox poll loop.
run_claude /workspace/task.md
touch /workspace/WAITING

# Poll inbox FIFO. File names are msg_<unix_ts>.md so lexical sort = temporal.
# Process one at a time, serial only.
mkdir -p /workspace/inbox
while true; do
    msg="$(ls /workspace/inbox/msg_*.md 2>/dev/null | sort | head -n 1 || true)"
    if [[ -n "${msg:-}" ]]; then
        rm -f /workspace/WAITING
        run_claude "$msg"
        rm -f "$msg"
        touch /workspace/WAITING
    fi
    sleep 2
done
