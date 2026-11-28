# The body of a session-role shortcut of a researcher project:
# claude-plan, claude-work, pi-plan, pi-work. RS's harness installer writes
# each into ~/.local/bin (beside the agents' own launchers, on the terminal's
# PATH) as a shebang, the lines setting RS_ROLE_AGENT (claude or pi),
# RS_ROLE (plan or work) and the role's model (RS_ROLE_MODEL, with
# RS_ROLE_EFFORT for Claude, RS_ROLE_THINKING for pi; empty = none named, so
# the agent starts on its own model), then this text. It starts the plain
# agent on that model with the role's skill as its first prompt; `claude`
# and `pi` themselves are never changed.

me=${0##*/}
if [ "$RS_ROLE" = work ]; then
    if [ $# -eq 0 ]; then
        # No package named: the packages, and how to start on one.
        rs-memory package list || exit $?
        printf '\n%s\n' "Start a work session on one of them: $me <package>"
        exit 0
    fi
    if [ $# -gt 1 ]; then
        echo "usage: $me <package>" >&2
        exit 2
    fi
    # A package's name, as rs-memory checks it (MIRROR of its
    # PACKAGE_NAME_RE): letters, digits, '-' and '_', a letter or digit first.
    case "$1" in
        [A-Za-z0-9]*) case "$1" in *[!A-Za-z0-9_-]*) bad=1 ;; *) bad= ;; esac ;;
        *) bad=1 ;;
    esac
    if [ -n "$bad" ]; then
        echo "$me: '$1' is not a package name: letters, digits, '-' and '_', a letter or digit first (for example station-gaps); $me alone lists the packages" >&2
        exit 2
    fi
    skill="rs-work $1"
else
    if [ $# -ne 0 ]; then
        echo "usage: $me (it takes no arguments)" >&2
        exit 2
    fi
    skill=rs-plan
fi
set --
if [ "$RS_ROLE_AGENT" = claude ]; then
    [ -z "$RS_ROLE_MODEL" ] || set -- "$@" --model "$RS_ROLE_MODEL"
    [ -z "$RS_ROLE_EFFORT" ] || set -- "$@" --effort "$RS_ROLE_EFFORT"
    exec claude "$@" "/$skill"
fi
[ -z "$RS_ROLE_MODEL" ] || set -- "$@" --model "$RS_ROLE_MODEL"
[ -z "$RS_ROLE_THINKING" ] || set -- "$@" --thinking "$RS_ROLE_THINKING"
exec pi "$@" "/skill:$skill"
