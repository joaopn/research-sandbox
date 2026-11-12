#!/bin/sh
# purge-mcp-allows.sh <subnet>
# Removes every per-MCP allow the router holds for a source subnet, with its
# record, and then every other FORWARD rule still keyed on that source (a rule
# whose record was lost, e.g. a kill between mcp-allow.sh's insert and its
# record write; any leftover egress rule).
#
# The HOST runs this over stdin (`docker exec -i rs-router sh -s -- <subnet>`,
# purge_router_range in cli/rscore.py): the bytes of the installed RS run
# whatever router image is up, so the copy the image carries under /scripts is
# never used. Hence self-contained: it sources nothing, and nothing in it may
# read stdin (stdin IS this script) — every read is redirected.
#
# Run at project destroy and before a network range is (re)used (project
# create, every review's rs-reviewer setup): an allow left on a range must
# never reach the next project docker hands that range to.
set -e

SUBNET="${1:-}"

if [ -z "$SUBNET" ]; then
    echo "Usage: purge-mcp-allows.sh <subnet>" >&2
    exit 1
fi

# Matched on the record's content (its first field is the subnet the host
# passed to mcp-allow.sh), never on the filename token. The record goes first:
# a kill after it leaves only a live rule, which no router restart replays.
for f in /etc/sandbox/rules/mcp-*; do
    [ -f "$f" ] || continue
    read -r s ip port rest < "$f" || true
    [ "$s" = "$SUBNET" ] || continue
    rm -f "$f"
    if [ -n "$ip" ] && [ -n "$port" ]; then
        while iptables -D FORWARD -s "$SUBNET" -d "$ip" -p tcp --dport "$port" -j ACCEPT 2>/dev/null; do :; done
    fi
done

# The sweep. The listing is captured first and iterated from a here-document,
# never streamed into a loop that deletes (no pipe, no subshell: a failing
# delete exits the script non-zero under set -e). Word-split on purpose: RS
# rules carry no quoted arguments; set -f keeps the split from globbing.
rules="$(iptables -S FORWARD)"
set -f
while IFS= read -r line; do
    case "$line" in
        "-A FORWARD -s $SUBNET "*)
            iptables -D ${line#-A }
            ;;
    esac
done <<EOF
$rules
EOF
set +f

echo "Purged $SUBNET"
