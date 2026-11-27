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

# NAT: masquerade every new connection the router forwards, whatever its way
# out is called. FORWARD accepts only traffic bound for outside the project
# networks (each bridge's rules drop the private ranges, apart from named MCP
# destinations, which leave the same way), so every forwarded connection leaves
# through the router's way out. Naming that interface (`-o eth0`) broke once the
# rs-sandbox endpoint was reconnected (the gateway re-pin at `research start`)
# or the router restarted with its project networks attached: eth0 then named a
# project network, and traffic from an --internal network (the search
# service's egress), which the host never translates, left with its own address
# and got no reply (B305). Loopback is left alone, so the router's own traffic
# to Docker's resolver keeps its address.
iptables -t nat -A POSTROUTING ! -o lo -j MASQUERADE

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
