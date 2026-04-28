#!/usr/bin/env bash
# inner-firewall.sh — defense-in-depth bridge-boundary ACL on the supervisor's
# rs-inner bridge.
#
# Threat model: a worker hardcodes an external URL or IP, bypassing the
# config-layer MCP allowlist. We block any rs-inner egress except via the
# pinned mcp-proxy.
#
# Why this works without br_netfilter: filtering at the BRIDGE BOUNDARY, not
# inside the bridge. Worker → proxy on rs-inner is L2-bridged and bypasses
# FORWARD entirely, which is fine — that path is the one we want to allow.
# Worker → anything outside rs-inner (internet, host network, other docker
# networks) is L3-routed across interfaces and DOES traverse FORWARD, where
# our DROP applies. mcp-proxy itself needs egress to upstream MCPs, so its
# pinned IP gets a source-based ACCEPT.
#
# Idempotent: rebuilds the RS-INNER-FW chain on every invocation. Default OFF
# (gated by RS_INNER_FIREWALL=1 in entrypoint.supervisor.sh).

set -euo pipefail

SUBNET="${RS_INNER_SUBNET:-192.168.99.0/24}"
PROXY_IP="${RS_INNER_PROXY_IP:-192.168.99.2}"
CHAIN="RS-INNER-FW"

if ! command -v iptables >/dev/null 2>&1; then
    echo "inner-firewall: iptables missing; skipping" >&2
    exit 0
fi
if ! sudo iptables -L FORWARD -n >/dev/null 2>&1; then
    echo "inner-firewall: cannot read FORWARD chain; skipping" >&2
    exit 0
fi

if sudo iptables -L "$CHAIN" -n >/dev/null 2>&1; then
    sudo iptables -F "$CHAIN"
else
    sudo iptables -N "$CHAIN"
fi

if ! sudo iptables -C FORWARD -s "$SUBNET" -j "$CHAIN" 2>/dev/null; then
    sudo iptables -I FORWARD 1 -s "$SUBNET" -j "$CHAIN"
fi

sudo iptables -A "$CHAIN" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
sudo iptables -A "$CHAIN" -s "$PROXY_IP" -j ACCEPT
sudo iptables -A "$CHAIN" -m limit --limit 10/min --limit-burst 5 \
    -j LOG --log-prefix "rs-inner-fw drop: " --log-level warning
sudo iptables -A "$CHAIN" -j DROP

echo "inner-firewall: applied (subnet=$SUBNET proxy_ip=$PROXY_IP)"
