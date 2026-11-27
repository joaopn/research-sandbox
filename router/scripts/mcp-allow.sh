#!/bin/sh
# mcp-allow.sh <subnet> <dest_ip> <dest_port> [tcp|udp]
# Insert a FORWARD ACCEPT rule allowing project <subnet> to reach a named
# destination (an MCP, or a destination of a server RS runs) at
# <dest_ip>:<dest_port> over exactly one protocol, TCP when none is given.
# Inserted at position 1 so it precedes the RFC1918 DROP rules added by
# apply-rules.sh. Idempotent: an existing identical rule is removed first to
# avoid duplicates.
#
# The record (/etc/sandbox/rules/mcp-<token>) is `SUBNET IP PORT PROTO`,
# written after the iptables calls. A TCP record keeps the token it had before
# protocols were recorded (subnet|ip|port), so re-allowing or replaying an old
# three-field record (TCP) rewrites that same file and mcp-deny.sh finds it.
set -e

SUBNET="$1"
IP="$2"
PORT="$3"
PROTO="${4:-tcp}"

if [ -z "$SUBNET" ] || [ -z "$IP" ] || [ -z "$PORT" ]; then
    echo "Usage: mcp-allow.sh <subnet> <dest_ip> <dest_port> [tcp|udp]" >&2
    exit 1
fi
case "$PROTO" in
    tcp) TOKEN_SRC="$SUBNET|$IP|$PORT" ;;
    udp) TOKEN_SRC="$SUBNET|$IP|$PORT|udp" ;;
    *)
        echo "mcp-allow.sh: the protocol must be tcp or udp, not $PROTO" >&2
        exit 1
        ;;
esac

while iptables -D FORWARD -s "$SUBNET" -d "$IP" -p "$PROTO" --dport "$PORT" -j ACCEPT 2>/dev/null; do :; done

iptables -I FORWARD 1 -s "$SUBNET" -d "$IP" -p "$PROTO" --dport "$PORT" -j ACCEPT

# Persist for re-application after router restart.
TOKEN=$(printf '%s' "$TOKEN_SRC" | tr -c 'A-Za-z0-9' '_')
RULEFILE="/etc/sandbox/rules/mcp-$TOKEN"
mkdir -p /etc/sandbox/rules
echo "$SUBNET $IP $PORT $PROTO" > "$RULEFILE"

echo "Allowed $SUBNET -> $IP:$PORT/$PROTO (mcp)"
