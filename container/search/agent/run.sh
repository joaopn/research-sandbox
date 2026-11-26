#!/bin/sh
# run.sh — one search: Claude Code headless, signed in with the service's
# long-lived token (CLAUDE_CODE_OAUTH_TOKEN, from the env file the service
# hands docker), its only tools the plain tier's tool server, its
# instructions RS's. The question arrives in RS_SEARCH_QUESTION; model and
# effort come from the searcher (aliases the host checked against the model
# catalog). Its stream-json output is the container's log, which the service
# reads at the end.
set -eu

: "${RS_SEARCH_QUESTION:?}"
: "${RS_SEARCH_MODEL:?}"
AGENT_DIR=/opt/rs-search-agent
CLAUDE=/opt/claude/local/bin/claude

mkdir -p /search/files /search/out

if [ ! -x "$CLAUDE" ]; then
    echo "rs-search-agent: Claude Code is not mounted at /opt/claude" >&2
    exit 3
fi

PROMPT="The question to search for:

$RS_SEARCH_QUESTION"

if [ -n "${RS_SEARCH_EFFORT:-}" ]; then
    set -- --effort "$RS_SEARCH_EFFORT"
else
    set --
fi

exec "$CLAUDE" -p "$PROMPT" \
    --output-format stream-json --verbose \
    --permission-mode bypassPermissions \
    --model "$RS_SEARCH_MODEL" "$@" \
    --tools "" \
    --strict-mcp-config --mcp-config "$AGENT_DIR/mcp.json" \
    --system-prompt "$(cat "$AGENT_DIR/instructions.md")" \
    < /dev/null
