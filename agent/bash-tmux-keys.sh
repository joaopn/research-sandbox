# Sourced from /etc/bash.bashrc (interactive bash only). Companion of
# agent/byobu-tmux.conf, which turns on tmux extended keys.
#
# A program that turns extended keys on for its pane (Claude Code, pi) turns
# them off again when it exits normally. If it is killed or crashes, the pane
# stays in that mode and the shell then receives Ctrl+C, Ctrl+D, ... as escape
# sequences: Ctrl+C no longer interrupts anything. Turning the mode off at
# every prompt clears the leftover state as soon as the shell is back; a
# program started from the prompt turns it on again itself.
if [ -n "${TMUX-}" ]; then
    # Written to stderr, where bash prints the prompt (stdout may be a pipe or a
    # log file), only when that is a terminal; the previous command's exit
    # status is passed through for whatever runs after this in PROMPT_COMMAND.
    __rs_tmux_keys_reset() { local s=$?; [ -t 2 ] && printf '\033[>4m' >&2; return $s; }
    case ";${PROMPT_COMMAND-};" in
        *";__rs_tmux_keys_reset;"*) ;;
        *) PROMPT_COMMAND="__rs_tmux_keys_reset${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
    esac
fi
