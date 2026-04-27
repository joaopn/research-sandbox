#!/bin/sh
# mcp-allow.sh <subnet> <dest_ip> <dest_port>
# Insert a FORWARD ACCEPT rule allowing project <subnet> to reach an MCP at
# <dest_ip>:<dest_port>. Inserted at position 1 so it precedes the RFC1918
# DROP rules added by apply-rules.sh. Idempotent: an existing identical rule
# is removed first to avoid duplicates.
set -e

SUBNET="$1"
IP="$2"
PORT="$3"

if [ -z "$SUBNET" ] || [ -z "$IP" ] || [ -z "$PORT" ]; then
    echo "Usage: mcp-allow.sh <subnet> <dest_ip> <dest_port>" >&2
    exit 1
fi

while iptables -D FORWARD -s "$SUBNET" -d "$IP" -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; do :; done

iptables -I FORWARD 1 -s "$SUBNET" -d "$IP" -p tcp --dport "$PORT" -j ACCEPT

# Persist for re-application after router restart.
TOKEN=$(printf '%s|%s|%s' "$SUBNET" "$IP" "$PORT" | tr -c 'A-Za-z0-9' '_')
RULEFILE="/etc/sandbox/rules/mcp-$TOKEN"
mkdir -p /etc/sandbox/rules
echo "$SUBNET $IP $PORT" > "$RULEFILE"

echo "Allowed $SUBNET -> $IP:$PORT (mcp)"
