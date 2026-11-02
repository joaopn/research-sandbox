#!/bin/sh
# reapply-rules.sh <resolvers>
# Re-apply every project's and the reviewer's recorded rule set with the
# configured resolvers (comma-separated, validated IPv4 literals). The router
# knows every bridge's egress mode from its own records — the host stores it
# nowhere else — so `research start` runs this right after the router is up
# and its boot replay is done, and a change of `SANDBOX_DNS` reaches every
# bridge at the next start. Prints one line per record and, for a bridge whose
# applied resolver list differs from the new one, `changed SUBNET OLD->NEW`
# (the host counts those: running projects keep their previous resolvers
# until recreated). Per-MCP records are not touched.
set -e

RESOLVERS="${1:-}"
if [ -z "$RESOLVERS" ]; then
    echo "Usage: reapply-rules.sh <resolvers>" >&2
    exit 1
fi

. /scripts/rules-lib.sh

[ -d "$RULES_DIR" ] || exit 0
for rulefile in "$RULES_DIR"/*; do
    [ -f "$rulefile" ] || continue
    case "$(basename "$rulefile")" in
        mcp-*|*.tmp) continue ;;
    esac
    SUBNET=$(awk '{print $1}' "$rulefile")
    MODE=$(awk '{print $2}' "$rulefile")
    OLD=$(awk '{print $3}' "$rulefile")
    [ -n "$SUBNET" ] && [ -n "$MODE" ] || continue
    # order-insensitive compare: the same set in another order is not a change
    if [ "$MODE" = "locked" ] && [ "$(sorted_list "$OLD")" != "$(sorted_list "$RESOLVERS")" ]; then
        echo "changed $SUBNET ${OLD:-none}->$RESOLVERS"
    fi
    /scripts/apply-rules.sh "$SUBNET" "$MODE" "$RESOLVERS" \
        || echo "WARNING: re-apply of $rulefile failed" >&2
done
