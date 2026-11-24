# The environment Claude Code is started with in RS containers. Installed as
# /etc/rs-claude-launch-env.sh and sourced from /etc/bash.bashrc (every
# interactive bash, so every terminal tab a `claude` is typed into) and by the
# headless worker and role-agent launchers before they start the agent.
#
# RS runs Claude Code with every permission prompt off (bypass mode). Claude
# Code still keeps one check of its own in that mode: an `rm`/`rmdir` aimed at
# the filesystem root, a top-level folder, the home, the working folder, or a
# target it cannot resolve in advance waits for a keypress in a terminal and is
# refused in a headless run. This variable turns that check off, so `rm` is
# treated like every other command inside the agent's own container.
#
# It lives here rather than in the agent's settings file because Claude Code
# reads it only from the environment it is launched with; a settings file's
# `env` block does not apply it.
#
# Plain POSIX sh: safe to source more than once and under `set -eu`.
export CLAUDE_CODE_EVEN_MORE_DANGEROUSLY_SKIP_PERMISSIONS=1
