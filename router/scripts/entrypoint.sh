#!/bin/sh
set -e

echo "=== Sandbox router starting ==="

. /scripts/rules-lib.sh

# The ready marker is for the host: it waits for this file before any rule
# call, so `research start` never interleaves with the replay below. /run is
# a tmpfs (docker-compose.yml), which is what guarantees a restart starts
# without a stale marker; the removal here is belt-and-braces.
rm -f "$READY_MARKER"

# IP forwarding is enabled via docker-compose sysctls (net.ipv4.ip_forward=1)

# --- Base iptables rules ---
# Default FORWARD policy: DROP (fail-closed)
iptables -P FORWARD DROP

# Allow established/related connections (return traffic)
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# NAT: masquerade outbound traffic on eth0 (dev-sandbox, the external interface)
iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE

# --- Re-apply persisted per-network rules ---
# One bad record never takes the router down: each replay is tolerated and
# announced, the other records still apply (the FORWARD policy stays DROP, so
# a skipped record means that bridge has no egress until `research start`
# re-applies it, never that it has unfiltered egress).
if [ -d "$RULES_DIR" ] && [ "$(ls -A "$RULES_DIR" 2>/dev/null)" ]; then
    echo "Re-applying persisted firewall rules..."
    for rulefile in "$RULES_DIR"/*; do
        case "$(basename "$rulefile")" in
            *.tmp)
                # a record apply-rules.sh was writing when it was killed
                continue
                ;;
            mcp-*)
                SUBNET=$(awk '{print $1}' "$rulefile")
                IP=$(awk '{print $2}' "$rulefile")
                PORT=$(awk '{print $3}' "$rulefile")
                if [ -n "$SUBNET" ] && [ -n "$IP" ] && [ -n "$PORT" ]; then
                    echo "  mcp $SUBNET -> $IP:$PORT"
                    /scripts/mcp-allow.sh "$SUBNET" "$IP" "$PORT" \
                        || echo "WARNING: replay of $rulefile failed" >&2
                fi
                ;;
            *)
                SUBNET=$(awk '{print $1}' "$rulefile")
                MODE=$(awk '{print $2}' "$rulefile")
                # Third field: the resolvers the record was applied with
                # (absent on a record written before they were recorded —
                # such a locked record replays with no DNS rule, fail-closed).
                RESOLVERS=$(awk '{print $3}' "$rulefile")
                if [ -n "$SUBNET" ] && [ -n "$MODE" ]; then
                    echo "  $SUBNET ($MODE${RESOLVERS:+, dns $RESOLVERS})"
                    /scripts/apply-rules.sh "$SUBNET" "$MODE" "$RESOLVERS" \
                        || echo "WARNING: replay of $rulefile failed" >&2
                fi
                ;;
        esac
    done
fi

mkdir -p "$(dirname "$READY_MARKER")"
: > "$READY_MARKER"
echo "=== Sandbox router ready ==="

# Keep running (accept docker exec calls)
exec sleep infinity
