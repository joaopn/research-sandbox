#!/usr/bin/env bash
# entrypoint.worker.sh — analysis worker container entrypoint.
#
# Contract with the orchestrator (spawn_worker):
#   /workspace/task.md            the task brief
#   /workspace/CLAUDE.md          worker role doc (template fill from orchestrator)
#   /workspace/.claude/credentials.json   copied from orchestrator at spawn time
#   /workspace/.claude/settings.json      bypassPermissions
#   /workspace/inbox/             empty at start; orchestrator writes here
#   /workspace/outputs/           empty at start; worker produces deliverables
#   /workspace/research_log.md    worker log
#
# On exit, we touch /workspace/DONE as a sentinel for the orchestrator.

set -euo pipefail

trap 'touch /workspace/DONE 2>/dev/null || true' EXIT

# Restore home skel if volume hides image contents (first boot after docker cp).
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/worker-skel/. ~/
fi

# Copy creds from orchestrator-staged location into the worker user's home.
# Creds were placed by rs-worker spawn in /workspace/.claude/ before the
# container was started. Claude Code's OAuth file is hidden (leading dot).
if [[ -f /workspace/.claude/.credentials.json ]]; then
    mkdir -p ~/.claude
    cp /workspace/.claude/.credentials.json ~/.claude/.credentials.json
    chmod 600 ~/.claude/.credentials.json
fi
if [[ -f /workspace/.claude/settings.json ]]; then
    mkdir -p ~/.claude
    cp /workspace/.claude/settings.json ~/.claude/settings.json
fi

cd /workspace

# Fail loudly if the orchestrator didn't stage a task.
if [[ ! -f /workspace/task.md ]]; then
    echo "error: /workspace/task.md missing; spawn_worker did not stage the task." >&2
    exit 2
fi

echo "=== Worker starting ==="
echo "Task: $(head -n 1 /workspace/task.md)"

# MCP config is optional — present only when the orchestrator plumbed MCPs in
# (Stage 2+). Stage 1 workers run without --mcp-config.
MCP_ARG=()
if [[ -f /workspace/.mcp.json ]]; then
    MCP_ARG=(--mcp-config /workspace/.mcp.json)
fi

# Execute Claude Code headless against the task. stream-json output goes to
# log.jsonl for orchestrator-side parsing. conda base env is on PATH so that
# any subprocess Claude spawns (python, jupyter, papermill) resolves.
export PATH="$HOME/.local/bin:/opt/conda/bin:$PATH"
claude --print "$(cat /workspace/task.md)" \
    --output-format stream-json \
    --verbose \
    --permission-mode bypassPermissions \
    "${MCP_ARG[@]}" \
    > /workspace/log.jsonl 2>&1 || true

echo "=== Worker finished ==="
