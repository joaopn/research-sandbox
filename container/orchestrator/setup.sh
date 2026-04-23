#!/usr/bin/env bash
# Claude Code configuration — sourced by entrypoint.orchestrator.sh on first boot.
# Sets bypassPermissions so Claude Code never prompts. Credentials are NOT
# staged here — the user authenticates once per project via VSCode CC
# extension OAuth or `claude` in byobu.

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
