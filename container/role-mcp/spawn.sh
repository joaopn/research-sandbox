#!/usr/bin/env bash
# spawn.sh — fork `claude -p` for one role-MCP call.
#
# Called by daemon.py per send_job (or per summarize_memories). The daemon
# stages the per-call dir, writes task.md and (for send_job calls) symlinks
# role.md as CLAUDE.md, then execs us with positional args:
#
#   $1  call_id       — sortable id (timestamp + 4-byte random)
#   $2  caller        — caller identity (used as memory subdir)
#   $3  task_md       — path to the per-call task file
#   $4  log_path      — where to redirect stream-json stdout
#   $5  mcp_cfg       — .mcp.json for the spawned claude (may be empty/missing)
#   $6  sys_prompt    — optional system-prompt file. When non-empty (summarize
#                       mode), claude runs with --bare + --system-prompt-file
#                       and skips MCP wiring and CLAUDE.md auto-discovery —
#                       the only context is the supplied file plus --print
#                       task.md. When empty (send_job mode), claude runs
#                       normally: CLAUDE.md auto-discovered from cwd
#                       (symlinked to role.md by the daemon), --mcp-config
#                       wired if non-empty.
#
# Working dir is the per-call dir (daemon cd'd here before exec). Output
# goes to log_path; the daemon parses the last `result` event from there
# on exit.
set -euo pipefail

CALL_ID="$1"
CALLER="$2"
TASK_MD="$3"
LOG_PATH="$4"
MCP_CFG="${5:-}"
SYS_PROMPT_FILE="${6:-}"

[[ -f "$TASK_MD" ]] || { echo "spawn.sh: task file missing: $TASK_MD" >&2; exit 2; }

# Surface the call's identity to the spawned claude as env vars too — role.md
# may reference them when constructing the per-call log path (the daemon
# also passes them in the task preamble for redundancy).
export RS_ROLE_NAME RS_CALL_ID="$CALL_ID" RS_CALLER="$CALLER"

if [[ -n "$SYS_PROMPT_FILE" ]]; then
    # Summarize-mode spawn. --system-prompt sets the entire system prompt
    # to the role's summarize.md, overriding the default. CLAUDE.md
    # auto-discovery is suppressed structurally — the daemon's
    # summarize_mode path doesn't symlink role.md as CLAUDE.md, and no
    # parent dir from /workspace/.calls/<id>/ up to / has a CLAUDE.md
    # the role-MCP container can see, so auto-discovery finds nothing.
    #
    # IMPORTANT: do NOT use --bare here. --bare disables OAuth + keychain
    # reads and demands ANTHROPIC_API_KEY or apiKeyHelper; the supervisor
    # authenticates via OAuth, so --bare breaks auth for the spawned
    # claude (symptom: "Not logged in · Please run /login"). The plain
    # --system-prompt path keeps OAuth working and is sufficient on its
    # own to suppress role.md influence.
    #
    # No MCP wiring — summarize doesn't call any tools, it just emits a
    # text entry. The task body (summarize.md prompt + concatenated
    # per-call logs + existing global.md) goes in via --print.
    [[ -f "$SYS_PROMPT_FILE" ]] || {
        echo "spawn.sh: system-prompt file missing: $SYS_PROMPT_FILE" >&2
        exit 2
    }
    exec claude --print "$(cat "$TASK_MD")" \
        --output-format stream-json \
        --verbose \
        --permission-mode bypassPermissions \
        --system-prompt "$(cat "$SYS_PROMPT_FILE")" \
        > "$LOG_PATH" 2>&1
fi

# Send_job-mode spawn (default). CLAUDE.md auto-discovers from cwd
# (daemon symlinked it to role.md); --mcp-config wires the role's
# upstream MCPs when non-empty.
MCP_ARG=()
if [[ -n "$MCP_CFG" && -s "$MCP_CFG" ]]; then
    if [[ "$(cat "$MCP_CFG")" != "{}" ]]; then
        MCP_ARG=(--mcp-config "$MCP_CFG")
    fi
fi

exec claude --print "$(cat "$TASK_MD")" \
    --output-format stream-json \
    --verbose \
    --permission-mode bypassPermissions \
    "${MCP_ARG[@]}" \
    > "$LOG_PATH" 2>&1
