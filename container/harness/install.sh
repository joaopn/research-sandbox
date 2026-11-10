#!/bin/sh
# RS's working-mode suite installer — one text for every container that runs
# an agent RS deploys on Docker-in-Docker (the project container and its boxes).
#
# What it writes, every run (RS owns exactly these names and regenerates them;
# temp file + rename, so a reader never sees half a file):
#   Claude Code (claude in RS_HARNESS_AGENTS):
#     ~/.claude/agents/doer.md, ~/.claude/agents/checker.md
#     ~/.claude/skills/harness/{SKILL.md,modes/direct.md,modes/planner-doer-checker.md}
#   pi (pi in RS_HARNESS_AGENTS), under pi's agent dir:
#     agents/doer.md, agents/checker.md, skills/harness/{…same…}
#   either (any agent): ~/.claude/rules/rs-harness.md — the standing mode.
#     Claude Code loads every ~/.claude/rules/*.md at session start; pi loads
#     the same folder through RS's rules extension (rs-rules). Mode none removes
#     that one file, by name; nothing else is ever removed.
#
# Inputs (environment; each re-checked here against the charset the host
# validated — a value that fails is dropped with a warning, never used):
#   RS_HARNESS_AGENTS                 the agents this container runs ("claude pi",
#                                     space or comma separated) — told by the
#                                     caller, never guessed from ~/.local/bin (a
#                                     box may still be copying its agent)
#   RS_HARNESS_MODE                   none | direct | planner-doer-checker (else none)
#   RS_HARNESS_DOER_MODEL             Claude tier for doer (empty: the session's)
#   RS_HARNESS_CHECKER_MODEL          Claude tier for checker
#   RS_HARNESS_PI_DOER_MODEL          pi provider/model for doer (empty: pi's default)
#   RS_HARNESS_PI_DOER_THINKING       pi thinking level for doer
#   RS_HARNESS_PI_CHECKER_MODEL       pi provider/model for checker
#   RS_HARNESS_PI_CHECKER_THINKING    pi thinking level for checker
#   PI_CODING_AGENT_DIR               pi's agent dir (default ~/.local/share/pi-agent/agent)
#
# Never fatal: it always exits 0. A file it could not write prints the line
# RS_HARNESS_INSTALL_FAILED on stdout, which the host turns into a warning.
set -u

SRC=$(cd "$(dirname "$0")" && pwd) || { echo RS_HARNESS_INSTALL_FAILED; exit 0; }
FAILED=0

warn() { echo "rs-harness: $*" >&2; }

# put DEST — write stdin to DEST through a temp file beside it. Returns
# non-zero on failure and never sets state itself: it often runs at the end of
# a pipeline, i.e. in a subshell, so every caller writes `put … || FAILED=1`.
put() {
    # A directory (or a link to one) on the destination would make `mv` move the
    # temp file INTO it and report success: refuse it as a failed write instead.
    # The temp name carries this run's pid: a box can run two installers at
    # once (its boot waiter and an Update's refresh), and a shared name would
    # let one truncate the file the other is renaming into place.
    if [ ! -d "$1" ] && mkdir -p "$(dirname "$1")" && cat > "$1.rs-tmp.$$" && mv -f "$1.rs-tmp.$$" "$1"; then
        return 0
    fi
    rm -f "$1.rs-tmp.$$"
    warn "could not write $1"
    return 1
}

# The charsets the host validates (rscore: the model catalog's aliases, PI_MODEL_RE,
# PI_THINKING_LEVELS). `case` patterns, so a value can never smuggle a newline.
# The values are written double-quoted into the frontmatter (YAML 1.1 readers
# take a bare `off` as false); none of these charsets admits a quote or a
# backslash, so the quoting cannot be broken out of.
claude_tier_ok() {
    case "$1" in ''|*[!A-Za-z0-9._-]*) return 1 ;; esac
    return 0
}
pi_model_ok() {
    case "$1" in */*) ;; *) return 1 ;; esac
    case "${1%%/*}" in ''|*[!A-Za-z0-9._-]*) return 1 ;; esac
    case "${1#*/}" in ''|*[!A-Za-z0-9._:~@+/-]*) return 1 ;; esac
    return 0
}
pi_thinking_ok() {
    case "$1" in off|minimal|low|medium|high|xhigh|max) return 0 ;; esac
    return 1
}

# checked NAME VALUE TEST — VALUE when empty or valid, else "" plus a warning.
checked() {
    if [ -z "$2" ] || "$3" "$2"; then
        printf '%s' "$2"
    else
        warn "ignoring $1: not a valid value"
    fi
}

# Every source must be readable before anything is written: a missing one would
# otherwise leave a frontmatter-only agent file that reads as a success.
for f in agents/doer.md agents/checker.md skill/SKILL.md modes/direct.md modes/planner-doer-checker.md; do
    if [ ! -r "$SRC/$f" ]; then
        warn "the shipped file $f is missing; nothing installed"
        echo RS_HARNESS_INSTALL_FAILED
        exit 0
    fi
done

MODE=${RS_HARNESS_MODE:-none}
case "$MODE" in
    none|direct|planner-doer-checker) ;;
    *) warn "unknown working mode; using none"; MODE=none ;;
esac
DOER_MODEL=$(checked RS_HARNESS_DOER_MODEL "${RS_HARNESS_DOER_MODEL:-}" claude_tier_ok)
CHECKER_MODEL=$(checked RS_HARNESS_CHECKER_MODEL "${RS_HARNESS_CHECKER_MODEL:-}" claude_tier_ok)
PI_DOER_MODEL=$(checked RS_HARNESS_PI_DOER_MODEL "${RS_HARNESS_PI_DOER_MODEL:-}" pi_model_ok)
PI_DOER_THINKING=$(checked RS_HARNESS_PI_DOER_THINKING "${RS_HARNESS_PI_DOER_THINKING:-}" pi_thinking_ok)
PI_CHECKER_MODEL=$(checked RS_HARNESS_PI_CHECKER_MODEL "${RS_HARNESS_PI_CHECKER_MODEL:-}" pi_model_ok)
PI_CHECKER_THINKING=$(checked RS_HARNESS_PI_CHECKER_THINKING "${RS_HARNESS_PI_CHECKER_THINKING:-}" pi_thinking_ok)

HAS_CLAUDE=0
HAS_PI=0
for a in $(printf '%s' "${RS_HARNESS_AGENTS:-}" | tr ',' ' '); do
    case "$a" in
        claude) HAS_CLAUDE=1 ;;
        pi) HAS_PI=1 ;;
    esac
done

DOER_DESC="Implements one unit of work from a self-contained brief (shipped by RS)."
CHECKER_DESC="Reviews a change read-only - verifies against the code, asserts defects, flags forks (shipped by RS)."

# skill DIR — the mode-switch skill (named rs-mode; its folder keeps the name
# harness, so nothing is ever removed) with its own copy of the mode texts.
skill() {
    put "$1/SKILL.md" < "$SRC/skill/SKILL.md" || FAILED=1
    put "$1/modes/direct.md" < "$SRC/modes/direct.md" || FAILED=1
    put "$1/modes/planner-doer-checker.md" < "$SRC/modes/planner-doer-checker.md" || FAILED=1
}

if [ "$HAS_CLAUDE" = 1 ]; then
    {
        printf '%s\n' '---' 'name: doer' "description: $DOER_DESC" "model: \"${DOER_MODEL:-inherit}\"" '---' ''
        cat "$SRC/agents/doer.md"
    } | put "$HOME/.claude/agents/doer.md" || FAILED=1
    {
        printf '%s\n' '---' 'name: checker' "description: $CHECKER_DESC" "model: \"${CHECKER_MODEL:-inherit}\"" \
            'tools: Read, Grep, Glob' '---' ''
        cat "$SRC/agents/checker.md"
    } | put "$HOME/.claude/agents/checker.md" || FAILED=1
    skill "$HOME/.claude/skills/harness"
fi

if [ "$HAS_PI" = 1 ]; then
    PI_DIR=${PI_CODING_AGENT_DIR:-$HOME/.local/share/pi-agent/agent}
    case "$PI_DIR" in
        "~") PI_DIR=$HOME ;;
        "~/"*) PI_DIR=$HOME/${PI_DIR#\~/} ;;
    esac
    {
        printf '%s\n' '---' 'name: doer' "description: $DOER_DESC" 'inheritProjectContext: true' 'acceptanceRole: writer'
        [ -z "$PI_DOER_MODEL" ] || printf '%s\n' "model: \"$PI_DOER_MODEL\""
        [ -z "$PI_DOER_THINKING" ] || printf '%s\n' "thinking: \"$PI_DOER_THINKING\""
        printf '%s\n' '---' ''
        cat "$SRC/agents/doer.md"
    } | put "$PI_DIR/agents/doer.md" || FAILED=1
    {
        printf '%s\n' '---' 'name: checker' "description: $CHECKER_DESC" 'tools: read, grep, find, ls' \
            'inheritProjectContext: true' 'acceptanceRole: read-only'
        [ -z "$PI_CHECKER_MODEL" ] || printf '%s\n' "model: \"$PI_CHECKER_MODEL\""
        [ -z "$PI_CHECKER_THINKING" ] || printf '%s\n' "thinking: \"$PI_CHECKER_THINKING\""
        printf '%s\n' '---' ''
        cat "$SRC/agents/checker.md"
    } | put "$PI_DIR/agents/checker.md" || FAILED=1
    skill "$PI_DIR/skills/harness"
fi

if [ "$HAS_CLAUDE" = 1 ] || [ "$HAS_PI" = 1 ]; then
    RULE="$HOME/.claude/rules/rs-harness.md"
    if [ "$MODE" = none ]; then
        if [ -e "$RULE" ] && ! rm -f "$RULE"; then
            warn "could not remove $RULE"
            FAILED=1
        fi
    else
        {
            printf '%s\n' "# Working mode: $MODE (set by RS for this project)" '' \
                'This sets how the session that talks with the user works. If you were launched as a subagent - `doer`, `checker` or any other child agent - ignore this file and do the task in your brief.' '' \
                "Where the project's own instructions prescribe a different way of working, the project's instructions win. The user can switch this session's mode with the \`rs-mode\` skill: \`/rs-mode <mode>\` in Claude Code, \`/skill:rs-mode <mode>\` in pi (none, direct or planner-doer-checker)." ''
            cat "$SRC/modes/$MODE.md"
        } | put "$RULE" || FAILED=1
    fi
fi

[ "$FAILED" = 0 ] || echo RS_HARNESS_INSTALL_FAILED
exit 0
