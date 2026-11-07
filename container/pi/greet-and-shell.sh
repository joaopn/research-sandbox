#!/usr/bin/env bash
# greet-and-shell.sh — print a starting message (MOTD), then drop into a login
# shell. Baked into rs-ext-base, so every box tab (rs-box-<name>) can call
# it as the byobu new-session
# command WITHOUT nesting single-quotes inside the tab's `docker exec … bash
# -lc '…'` wrapper — the helper takes the greeting path as $1 instead.
#
# PI-role tabs deliberately do NOT auto-start `claude`: landing in a shell keeps
# the message on screen (claude's TUI would clear it), and the PI starts claude
# themselves when ready. `exec bash -l` is a login shell, so ~/.bashrc puts
# ~/.local/bin on PATH and `claude` resolves (BUG_BUCKET B6).
#
# $1 — path to the greeting file. Missing/empty file is a clean no-op (the
# box case points at the not-yet-conventional /workspace/.rs-greeting).
#
# In a browser box (the image's Playwright declaration exists) one more line
# names the browser mode: trusted only when the box was run trusted AND the
# image carries the trusted declaration (the box render's own rule); otherwise
# restricted, listing the tools the IMAGE blocks (so an older image's shorter
# list reads as what it is). Nothing here may stop the shell from starting.
cat "$1" 2>/dev/null
if [ -f /opt/sandbox-box/extra-mcps.json ]; then
    if [ "${RS_BOX_BROWSER:-}" = trusted ] && [ -f /opt/sandbox-box/extra-mcps.trusted.json ]; then
        echo "Browser: trusted — every browser tool is available to the agents in this box."
    else
        blocked=$(jq -r '[.[]? | .[]? | strings] | unique | join(", ")' \
                  /opt/sandbox-box/denied-tools.json 2>/dev/null)
        if [ -n "$blocked" ]; then
            echo "Browser: restricted — blocked for the agents in this box: $blocked"
        else
            echo "Browser: restricted."
        fi
    fi
fi
exec bash -l
