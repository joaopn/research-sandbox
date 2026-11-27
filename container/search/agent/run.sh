#!/bin/sh
# run.sh — one search: Claude Code headless, signed in with the service's
# long-lived token (CLAUDE_CODE_OAUTH_TOKEN, from the env file the service
# hands docker), its only tools those of the tool servers the service lists
# for this search in RS_SEARCH_MCP_CONFIG (the plain tier's, always, and the
# browser sidecars that came up), its instructions RS's with only the tiers
# named in RS_SEARCH_TIERS (space-separated: browser, stealth) kept. The
# question arrives in RS_SEARCH_QUESTION; model and effort come from the
# searcher (aliases the host checked against the model catalog). Its
# stream-json output is the container's log, which the service follows.
set -eu

: "${RS_SEARCH_QUESTION:?}"
: "${RS_SEARCH_MODEL:?}"
: "${RS_SEARCH_MCP_CONFIG:?}"
AGENT_DIR=/opt/rs-search-agent
CLAUDE=/opt/claude/local/bin/claude

mkdir -p /search/files /search/out

if [ ! -x "$CLAUDE" ]; then
    echo "rs-search-agent: Claude Code is not mounted at /opt/claude" >&2
    exit 3
fi

# The instructions with each `<!-- tier: NAME -->` … `<!-- end tier -->`
# block kept only when NAME is one of this search's tiers (a tier that is off
# or did not come up is not mentioned to the agent).
INSTRUCTIONS=$(awk -v tiers=" ${RS_SEARCH_TIERS:-} " '
    /^<!-- tier: [a-z]+ -->$/ { skip = index(tiers, " " $3 " ") == 0; next }
    /^<!-- end tier -->$/ { skip = 0; next }
    !skip' "$AGENT_DIR/instructions.md")

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
    --strict-mcp-config --mcp-config "$RS_SEARCH_MCP_CONFIG" \
    --system-prompt "$INSTRUCTIONS" \
    < /dev/null
