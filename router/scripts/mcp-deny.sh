#!/bin/sh
# mcp-deny.sh <subnet> <dest_ip> <dest_port> [tcp|udp]
# Remove the matching FORWARD ACCEPT rule of that one protocol (TCP when none
# is given) and its persisted file (the token mcp-allow.sh gave it).
set -e

SUBNET="$1"
IP="$2"
PORT="$3"
PROTO="${4:-tcp}"

if [ -z "$SUBNET" ] || [ -z "$IP" ] || [ -z "$PORT" ]; then
    echo "Usage: mcp-deny.sh <subnet> <dest_ip> <dest_port> [tcp|udp]" >&2
    exit 1
fi
case "$PROTO" in
    tcp) TOKEN_SRC="$SUBNET|$IP|$PORT" ;;
    udp) TOKEN_SRC="$SUBNET|$IP|$PORT|udp" ;;
    *)
        echo "mcp-deny.sh: the protocol must be tcp or udp, not $PROTO" >&2
        exit 1
        ;;
esac

while iptables -D FORWARD -s "$SUBNET" -d "$IP" -p "$PROTO" --dport "$PORT" -j ACCEPT 2>/dev/null; do :; done

TOKEN=$(printf '%s' "$TOKEN_SRC" | tr -c 'A-Za-z0-9' '_')
rm -f "/etc/sandbox/rules/mcp-$TOKEN"

echo "Denied $SUBNET -> $IP:$PORT/$PROTO (mcp)"
