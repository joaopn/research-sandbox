#!/bin/sh
# mcp-deny.sh <subnet> <dest_ip> <dest_port>
# Remove the matching FORWARD ACCEPT rule and its persisted file.
set -e

SUBNET="$1"
IP="$2"
PORT="$3"

if [ -z "$SUBNET" ] || [ -z "$IP" ] || [ -z "$PORT" ]; then
    echo "Usage: mcp-deny.sh <subnet> <dest_ip> <dest_port>" >&2
    exit 1
fi

while iptables -D FORWARD -s "$SUBNET" -d "$IP" -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; do :; done

TOKEN=$(printf '%s|%s|%s' "$SUBNET" "$IP" "$PORT" | tr -c 'A-Za-z0-9' '_')
rm -f "/etc/sandbox/rules/mcp-$TOKEN"

echo "Denied $SUBNET -> $IP:$PORT (mcp)"
