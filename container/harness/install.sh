#!/bin/sh
# RS's working-mode suite installer — one text for every container that runs
# an agent RS deploys on Docker-in-Docker (the project container and its boxes).
#
# What it writes, every run (RS owns exactly these names and regenerates them;
# temp file + rename, so a reader never sees half a file):
#   Claude Code (claude in RS_HARNESS_AGENTS):
#     ~/.claude/agents/doer.md, ~/.claude/agents/checker.md — in the LEAN form
#       (the role starts without the session's instruction files and is handed
#       its own part of RS's text plus the project's instruction files as
#       preloaded skills) when the deployed Claude Code honours that and RS's
#       instruction writer has laid the role's part; else as before
#     ~/.claude/skills/rs-<role>-rules/SKILL.md — a link, made for a lean role;
#     ~/.claude/skills/rs-project-instructions/SKILL.md and
#       rs-workspace-instructions/SKILL.md — links, made when a role can be lean
#     ~/.claude/skills/harness/{SKILL.md,modes/<mode>.md for every staged mode}
#   pi (pi in RS_HARNESS_AGENTS), under pi's agent dir:
#     agents/doer.md, agents/checker.md, skills/harness/{…same…}
#     and beside RS's rules extension, ~/.local/share/pi-agent/rs/roles/<role>.md
#       — a link to the role's part, which the extension reads for that child
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
#   RS_HARNESS_MODE                   none, or a mode this staged suite carries
#                                     (modes/<mode>.md; the host generated them
#                                     from RS's catalog, modes.json) — else none
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
# RS_HARNESS_CLAUDE_OLD / RS_HARNESS_PI_OLD say that the deployed Claude Code /
# RS rules extension for pi is present but too old for the lean roles (the
# roles keep working as before); an agent that is not deployed yet prints
# neither.
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

# link DEST TARGET — make DEST a link to TARGET through a temp name beside it
# (the same refusal of a real directory as put; an existing file or link is
# replaced, never followed — a link is tested as a link, so one whose target is
# a folder is replaced too). TARGET may be absent: the agents then load nothing
# from it.
link() {
    if { [ -L "$1" ] || [ ! -d "$1" ]; } && mkdir -p "$(dirname "$1")" && ln -s "$2" "$1.rs-tmp.$$" \
            && mv -fT "$1.rs-tmp.$$" "$1"; then
        return 0
    fi
    rm -f "$1.rs-tmp.$$"
    warn "could not link $1"
    return 1
}

# ver_ok VALUE — a dotted run of numbers and nothing else.
ver_ok() {
    case "$1" in ''|*[!0-9.]*|.*|*.|*..*) return 1 ;; esac
    return 0
}
# ver_ge A B — A is at least B in version order.
ver_ge() {
    [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$2" ]
}

# The oldest Claude Code on which a role with `omitClaudeMd: true` starts
# without the session's instruction files: 2.1.322, the lowest build measured
# to honour it (2.1.258 ignores the key and would hand a lean role everything
# twice). The first RS rules extension for pi that builds a lean child's
# section: 0.8.0.
CLAUDE_LEAN_FLOOR=2.1.322
PI_LEAN_FLOOR=0.8.0
ROLES_DIR="$HOME/.claude/rs/roles"

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
# otherwise leave a frontmatter-only agent file that reads as a success. A
# mode's own rule body is checked when the mode is chosen, below.
for f in agents/doer.md agents/checker.md skill/SKILL.md modes/LIST; do
    if [ ! -r "$SRC/$f" ]; then
        warn "the shipped file $f is missing; nothing installed"
        echo RS_HARNESS_INSTALL_FAILED
        exit 0
    fi
done

# The mode: none, or one whose rule body this suite carries. Its characters
# first (MIRROR of rscore.HARNESS_MODE_ID_RE: lowercase words joined by single
# hyphens), since the value becomes a path below and a newline must never
# reach it; then the file.
MODE=${RS_HARNESS_MODE:-none}
case "$MODE" in
    none) ;;
    [a-z]*[!a-z0-9-]*|*--*|*-|[!a-z]*) warn "unknown working mode; using none"; MODE=none ;;
    *) [ -r "$SRC/modes/$MODE.md" ] || { warn "unknown working mode; using none"; MODE=none; } ;;
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
CHECKER_DESC="Reviews a change or a plan read-only - verifies against the code, asserts defects, flags forks (shipped by RS)."

# skill DIR — the mode-switch skill (named rs-mode; its folder keeps the name
# harness, so nothing is ever removed) with its own copy of every staged mode's
# rule body.
skill() {
    put "$1/SKILL.md" < "$SRC/skill/SKILL.md" || FAILED=1
    for m in "$SRC"/modes/*.md; do
        [ -r "$m" ] || continue
        put "$1/modes/${m##*/}" < "$m" || FAILED=1
    done
}

# has_part ROLE — RS's instruction writer laid ROLE's part here: a regular,
# non-empty file (a folder or anything else at that name is not a part).
has_part() {
    [ -f "$ROLES_DIR/$1.md" ] && [ -s "$ROLES_DIR/$1.md" ]
}

# lean_lines ROLE — the frontmatter lines of the lean form when ROLE is in
# LEAN_ROLES (set below, once the role's links exist); nothing otherwise.
lean_lines() {
    case " $LEAN_ROLES " in *" $1 "*) ;; *) return 0 ;; esac
    printf '%s\n' 'omitClaudeMd: true' 'skills:' "  - rs-$1-rules" \
        '  - rs-project-instructions' '  - rs-workspace-instructions'
}

if [ "$HAS_CLAUDE" = 1 ]; then
    # The deployed Claude Code's version, from its launcher (a link whose
    # target ends versions/<version>). No launcher: the agent is not deployed
    # here yet (an Update that adds it runs this before the deploy), so the
    # roles stay as before and nothing is reported. A launcher below the floor
    # or with an unreadable version: as before, and reported.
    CLAUDE_LEAN=0
    if [ -L "$HOME/.local/bin/claude" ] || [ -e "$HOME/.local/bin/claude" ]; then
        CLAUDE_VER=$(basename "$(readlink "$HOME/.local/bin/claude" 2>/dev/null)" 2>/dev/null)
        if ver_ok "$CLAUDE_VER" && ver_ge "$CLAUDE_VER" "$CLAUDE_LEAN_FLOOR"; then
            CLAUDE_LEAN=1
        else
            echo RS_HARNESS_CLAUDE_OLD
        fi
    fi
    # The links first, the lean form only for a role whose links exist: a role
    # written lean without them would start with nothing of RS's or the
    # project's. The two project links (where RS's notice says the project's
    # own instruction files live; an absent file loads nothing) are shared, so
    # a failure there keeps both roles as before.
    LEAN_ROLES=
    if [ "$CLAUDE_LEAN" = 1 ] && { has_part doer || has_part checker; }; then
        if link "$HOME/.claude/skills/rs-project-instructions/SKILL.md" /workspace/.claude/CLAUDE.md \
                && link "$HOME/.claude/skills/rs-workspace-instructions/SKILL.md" /workspace/CLAUDE.md; then
            for role in doer checker; do
                has_part "$role" || continue
                if link "$HOME/.claude/skills/rs-$role-rules/SKILL.md" "$ROLES_DIR/$role.md"; then
                    LEAN_ROLES="$LEAN_ROLES $role"
                else
                    FAILED=1
                fi
            done
        else
            FAILED=1
        fi
    fi
    {
        printf '%s\n' '---' 'name: doer' "description: $DOER_DESC" "model: \"${DOER_MODEL:-inherit}\""
        lean_lines doer
        printf '%s\n' '---' ''
        cat "$SRC/agents/doer.md"
    } | put "$HOME/.claude/agents/doer.md" || FAILED=1
    {
        printf '%s\n' '---' 'name: checker' "description: $CHECKER_DESC" "model: \"${CHECKER_MODEL:-inherit}\"" \
            'tools: Read, Grep, Glob'
        lean_lines checker
        printf '%s\n' '---' ''
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
    # The role parts for RS's rules extension, beside it (a folder pi never
    # scans). The extension builds a doer's or checker's section from the link
    # when it reads as a non-empty file, so the link is made only where the
    # writer laid the part.
    PI_RS_DIR="$HOME/.local/share/pi-agent/rs"
    for role in doer checker; do
        if has_part "$role"; then
            link "$PI_RS_DIR/roles/$role.md" "$ROLES_DIR/$role.md" || FAILED=1
        fi
    done
    # The deployed extension's version is its first line (`// rs-rules v<ver>`).
    # Absent: pi is not deployed here yet, nothing is reported.
    if [ -e "$PI_RS_DIR/rs-rules.ts" ]; then
        PI_RULES_VER=$(sed -n '1s#^// rs-rules v##p' "$PI_RS_DIR/rs-rules.ts" 2>/dev/null)
        if ! ver_ok "$PI_RULES_VER" || ! ver_ge "$PI_RULES_VER" "$PI_LEAN_FLOOR"; then
            echo RS_HARNESS_PI_OLD
        fi
    fi
fi

if [ "$HAS_CLAUDE" = 1 ] || [ "$HAS_PI" = 1 ]; then
    # The header's list of modes, as the host joined it (one line).
    MODE_LIST=$(head -n 1 "$SRC/modes/LIST")
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
                "Where the project's own instructions prescribe a different way of working, the project's instructions win. The user can switch this session's mode with the \`rs-mode\` skill: \`/rs-mode <mode>\` in Claude Code, \`/skill:rs-mode <mode>\` in pi ($MODE_LIST)." ''
            cat "$SRC/modes/$MODE.md"
        } | put "$RULE" || FAILED=1
    fi
fi

[ "$FAILED" = 0 ] || echo RS_HARNESS_INSTALL_FAILED
exit 0
