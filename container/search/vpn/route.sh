#!/bin/sh
# route.sh GATEWAY [CLOSED_ADDRESS] — run in a sidecar's network namespace by
# a one-shot helper (the sidecar has no capability to do it itself): the
# sidecar's default route goes through the VPN gateway at GATEWAY. With
# CLOSED_ADDRESS (the sidecar's address on a network it only calls out on: the
# plain tier's on its SearXNG network), no new connection is let in on that
# interface, so nothing there (the VPN's SearXNG) reaches the sidecar's tool
# server.
set -eu
ipv4() { case "$1" in ''|*[!0-9.]*) return 1 ;; esac; }
ipv4 "$1" || { echo "the gateway address is not one RS sends" >&2; exit 2; }
ip route replace default via "$1"
if [ $# -ge 2 ]; then
    ipv4 "$2" || { echo "the closed address is not one RS sends" >&2; exit 2; }
    dev=$(ip -o -4 address show | awk -v a="$2" '{ split($4, p, "/"); if (p[1] == a) { print $2; exit } }')
    [ -n "$dev" ] || { echo "no interface holds $2" >&2; exit 3; }
    iptables -A INPUT -i "$dev" -m conntrack --ctstate NEW -j DROP
fi
