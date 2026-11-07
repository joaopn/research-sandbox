#!/bin/bash -p
# playwright-env-clear — start a command with every PLAYWRIGHT_MCP_* variable
# removed from its environment. Baked into the browser box image; a RESTRICTED
# browser box (the default) starts its Playwright MCP server through it
# (`dumb-init -- <this> playwright-mcp …`), so no variable set at runtime — in
# the box's environment or in an `env` block of /workspace/.mcp.json — can
# widen the server's tool list past the one the image build reviewed
# (Playwright reads PLAYWRIGHT_MCP_* at start and lets them override its
# config, e.g. PLAYWRIGHT_MCP_CAPS adds tools). A trusted box starts the server
# without it.
#
# A guardrail, not a wall: an agent that writes a preload (NODE_OPTIONS,
# LD_PRELOAD) or a PATH that changes which node runs can still change the
# server; it keeps a page from steering a well-behaved agent into more tools.
#
# Why each piece:
#   bash -p      privileged mode: bash ignores exported functions and BASH_ENV/
#                ENV, either of which could otherwise redefine `env` or run
#                code before this script and skip the clearing.
#   /usr/bin/env by absolute path, so a PATH from an `.mcp.json` env block
#                cannot put another `env` first.
#   env -0       names are read from NUL-separated entries, so a value holding
#                a newline cannot smuggle in (or hide) a name.
#   env -u       removes a name even when it is not a valid shell identifier
#                (e.g. PLAYWRIGHT_MCP_X-Y), which bash's `unset` leaves in place.
#   exec         keeps the pid and process group dumb-init forwards signals to.
set -euo pipefail

unset_args=()
while IFS= read -r -d '' entry; do
    name=${entry%%=*}
    case $name in
        PLAYWRIGHT_MCP_*) unset_args+=(-u "$name") ;;
    esac
done < <(/usr/bin/env -0)

exec /usr/bin/env "${unset_args[@]}" "$@"
