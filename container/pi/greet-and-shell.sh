#!/usr/bin/env bash
# greet-and-shell.sh — print a starting message (MOTD), then drop into a login
# shell (STAGE_SPAWN_GREETING). Baked into rs-pi-base, so every PI-style tab
# (Wrangler, Websearcher, pi-isolated) can call it as the byobu new-session
# command WITHOUT nesting single-quotes inside the tab's `docker exec … bash
# -lc '…'` wrapper — the helper takes the greeting path as $1 instead.
#
# PI-role tabs deliberately do NOT auto-start `claude`: landing in a shell keeps
# the message on screen (claude's TUI would clear it), and the PI starts claude
# themselves when ready. `exec bash -l` is a login shell, so ~/.bashrc puts
# ~/.local/bin on PATH and `claude` resolves (BUG_BUCKET B6).
#
# $1 — path to the greeting file. Missing/empty file is a clean no-op (the
# pi-isolated case points at the not-yet-conventional /workspace/.rs-greeting).
cat "$1" 2>/dev/null
exec bash -l
