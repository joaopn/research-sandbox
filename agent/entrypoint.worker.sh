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
#   /workspace/wake/<unix_ts>.md  worker-written wake-ups: the name is the
#                                 due time (epoch seconds), the body is the
#                                 worker's own resume prompt; the poll loop
#                                 moves a due file into inbox/ so it rides
#                                 the message path (survives restarts, not
#                                 respawns — rs-worker spawn clears them)
#   /workspace/outputs/<slug>/    per-cycle deliverables, accumulate
#   /workspace/research_log.md    accumulating narrative
#   /workspace/scratch/           accumulating working memory
#   /workspace/WAITING            set while idle; cleared while working
#   /workspace/DONE               set on clean shutdown
#   /workspace/FAILED             set when the agent never completed a turn
#                                 (exit=<rc> / reason=<last log line>); the
#                                 container then exits non-zero and the
#                                 supervisor reads `failed` with the reason.
#                                 An unprocessed inbox message (a moved wake
#                                 included) survives that exit and a respawn:
#                                 it runs after the new task.md cycle.

set -euo pipefail

# Restore home skel if the worker's /home was shadowed by a first-boot volume.
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/worker-skel/. ~/
fi

# Deploy the agent (claude) from the supervisor-staged dist into our OWN writable
# ~/.local (no bake; STAGE_AGENT_DIST slice 2). Guard on the LAUNCHER'S ABSENCE
# (not first-boot) so a restart preserves any autoupdater bump; for a worker this
# is a fresh container each spawn, so it always deploys — must land BEFORE the
# run_claude below, which execs `claude --print` immediately.
if [[ -d /opt/agent-dist && ! -e ~/.local/bin/claude ]]; then
    mkdir -p ~/.local
    cp -a /opt/agent-dist/local/. ~/.local/
fi
# Bundled bypass settings (no hooks) — no-clobber; the supervisor-propagated
# settings staged below from /workspace/.claude still overrides it
# (STAGE_AGENT_DIST_SETTINGS; the dist is a fixed tree {local/, claude/}).
if [[ -f /opt/agent-dist/claude/settings.json && ! -e ~/.claude/settings.json ]]; then
    mkdir -p ~/.claude
    cp /opt/agent-dist/claude/settings.json ~/.claude/settings.json
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

cd /workspace
export PATH="$HOME/.local/bin:/opt/conda/bin:$PATH"

# Clear stale sentinels from a prior incarnation on this same bind-mount.
# Pending wake files are deliberately kept: a wake survives a container
# restart (the loop below honours it after the boot cycle).
rm -f /workspace/WAITING /workspace/DONE /workspace/FAILED
mkdir -p /workspace/inbox /workspace/wake

# Clean shutdown on SIGTERM / SIGINT: drop WAITING, leave DONE for the
# supervisor's shutdown CLI to observe.
trap 'rm -f /workspace/WAITING; touch /workspace/DONE; exit 0' TERM INT

MCP_ARG=()
if [[ -f /workspace/.mcp.json ]]; then
    MCP_ARG=(--mcp-config /workspace/.mcp.json)
fi

# Agent model + effort (STAGE_MODEL_SELECT). The supervisor's `rs-worker spawn`
# sets these from the project's *worker* pair; unset means "no flag", i.e. the
# agent's own default. Explicit flags rather than ANTHROPIC_MODEL because we own
# this argv — the flag is visible in the process list and outranks any settings
# file the worker inherited from the supervisor.
MODEL_ARG=()
if [[ -n "${RS_AGENT_MODEL:-}" ]]; then
    MODEL_ARG=(--model "$RS_AGENT_MODEL")
fi
EFFORT_ARG=()
if [[ -n "${RS_AGENT_EFFORT:-}" ]]; then
    EFFORT_ARG=(--effort "$RS_AGENT_EFFORT")
fi

# The agent never completed a turn: a non-zero exit with no stream-json
# `result` event in this run's slice of the log. Distinct from a TASK failure
# (an errored turn still emits a `result` event, e.g. an auth error) — that one
# keeps the poll loop alive as before. Here we leave neither WAITING nor DONE,
# record why, and exit: the supervisor reads `failed` plus the reason, fixes
# the cause (creds, MCP config, model) and respawns.
# The reason recorded in FAILED is a diagnostic pointer, not the log: the full
# line stays in log.jsonl, and the cap keeps FAILED and `rs-worker status`
# output bounded when the last line is a multi-KB stream-json event. cut -c is
# byte-based; the reader tolerates a split multibyte character.
FAILED_REASON_MAX_BYTES=500
launch_failed() {
    local rc="$1" before="$2" last
    last="$(tail -c +$((before + 1)) /workspace/log.jsonl 2>/dev/null \
            | grep -v '^[[:space:]]*$' | tail -n 1 \
            | cut -c1-"$FAILED_REASON_MAX_BYTES" || true)"
    printf 'exit=%s\nreason=%s\n' "$rc" "${last:-no output}" > /workspace/FAILED
    echo "=== Worker agent failed to launch (exit $rc): ${last:-no output} ===" >&2
    exit "$rc"
}

run_claude() {
    local rc=0 before
    before="$(stat -c %s /workspace/log.jsonl 2>/dev/null || echo 0)"
    claude --print "$(cat "$1")" \
        --output-format stream-json \
        --verbose \
        --permission-mode bypassPermissions \
        "${MODEL_ARG[@]}" \
        "${EFFORT_ARG[@]}" \
        "${MCP_ARG[@]}" \
        >> /workspace/log.jsonl 2>&1 || rc=$?
    # Process substitution, not a pipe: grep's early exit must not surface as
    # a pipefail status and misread a launched agent as a launch failure.
    if (( rc != 0 )) && ! grep -qF '"type":"result"' \
            < <(tail -c +$((before + 1)) /workspace/log.jsonl); then
        launch_failed "$rc" "$before"
    fi
}

if [[ ! -f /workspace/task.md ]]; then
    echo "error: /workspace/task.md missing; spawn did not stage the task." >&2
    printf 'exit=2\nreason=task.md missing; spawn did not stage the task\n' > /workspace/FAILED
    exit 2
fi

echo "=== Worker starting ==="
echo "Task: $(head -n 1 /workspace/task.md)"

# Initial cycle: run the task from task.md, then enter the inbox poll loop.
run_claude /workspace/task.md
touch /workspace/WAITING

# Poll inbox FIFO. File names are msg_<unix_ts>.md so a C-locale sort is
# temporal (the image sets LC_ALL=en_US.UTF-8, whose collation ignores
# punctuation — hence the explicit LC_ALL=C). Process one at a time, serial only.
#
# Wake files first: a due /workspace/wake/<epoch>.md becomes
# inbox/msg_<now>_wake_<epoch>.md and rides the message path unchanged. The
# name grammar (digits only) is shared with rs-worker's state resolver and the
# supervisor's Stop hook — a pending numeric wake is what makes a WAITING
# worker read `parked`; anything else in wake/ is ignored by all three.
while true; do
    now="$(date +%s)"
    for w in /workspace/wake/*; do
        [[ -e "$w" ]] || continue
        base="${w##*/}"
        [[ "$base" =~ ^([0-9]+)\.md$ ]] || continue
        due="${BASH_REMATCH[1]}"
        if (( 10#$due <= now )); then
            mv "$w" "/workspace/inbox/msg_${now}_wake_${due}.md"
        fi
    done
    msg="$(ls /workspace/inbox/msg_*.md 2>/dev/null | LC_ALL=C sort | head -n 1 || true)"
    if [[ -n "${msg:-}" ]]; then
        rm -f /workspace/WAITING
        run_claude "$msg"
        rm -f "$msg"
        touch /workspace/WAITING
    fi
    sleep 2
done
