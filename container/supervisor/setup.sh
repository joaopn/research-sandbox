#!/usr/bin/env bash
# Claude Code configuration — sourced by entrypoint.supervisor.sh on first boot.
# Sets bypassPermissions so Claude Code never prompts, and switches git/PR
# attribution OFF (co-author trailer, PR footer, AND the private session link —
# three keys, the session link is a separate switch) and the Remote Control
# bridge (/rc) OFF by default (`remoteControlAtStartup: false` — an absent key
# falls to the rollout default, which auto-starts it; rationale for both at
# rscore._AGENT_SETTINGS_JSON). This heredoc, that constant, and the sandbox-box
# entrypoint's printf fallback are a three-writer mirror, pytest-pinned; this
# one alone adds the rs-audit-stop Stop hook. Credentials are NOT staged here —
# the user authenticates once per project via VSCode CC extension OAuth or
# `claude` in byobu.

# Ensure ~/.local/bin is on PATH (Claude Code installs there).
if ! grep -q '.local/bin' ~/.bashrc 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
fi

mkdir -p ~/.claude

if [[ ! -f ~/.claude/settings.json ]]; then
    cat > ~/.claude/settings.json <<'SETTINGS'
{
  "permissions": {
    "defaultMode": "bypassPermissions"
  },
  "theme": "dark",
  "env": {
    "CLAUDE_CODE_DISABLE_MOUSE_CLICKS": "1"
  },
  "attribution": {
    "commit": "",
    "pr": "",
    "sessionUrl": false
  },
  "remoteControlAtStartup": false,
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/usr/local/bin/rs-audit-stop"
          }
        ]
      }
    ]
  }
}
SETTINGS
fi

if [[ ! -f ~/.claude.json ]]; then
    cat > ~/.claude.json <<'SETTINGS'
{
  "theme": "dark"
}
SETTINGS
fi
