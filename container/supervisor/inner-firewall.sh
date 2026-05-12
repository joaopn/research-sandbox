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
# Role-MCP containers (echo-mcp, wrangler, librarian, websearcher, …) live
# in 192.168.99.4-99.11 (a /29 — eight addresses, covers the four B.0-B.3
# role-MCPs plus a small reservation). Their containers spawn `claude -p`
# subprocesses that talk directly to api.anthropic.com — L3 egress that
# crosses the bridge boundary and hits FORWARD. Without an ACCEPT, the
# spawned claude can't reach Anthropic and every send_job stalls.
#
# Role-MCP↔mcp-proxy traffic stays on the rs-inner bridge (L2) and bypasses
# FORWARD entirely, so the proxy path doesn't need a hole here.
ROLE_MCP_RANGE="${RS_INNER_ROLE_MCP_RANGE:-192.168.99.4/29}"
# PI role containers (pi-echo, pi-wrangler, pi-librarian, pi-websearcher,
# …) live in 192.168.99.10-99.25 — sixteen addresses to fit the four v1
# PI roles plus a generous reservation. They run interactive `claude`
# sessions inside byobu, which talk directly to api.anthropic.com (L3
# egress, FORWARD path), so they need the same ACCEPT shape as role-MCPs.
# Using `iprange` rather than a CIDR because .10-.25 does not align to a
# clean prefix; iprange avoids the CIDR-canonicalization ambiguity that
# `192.168.99.4/29` has (the comment claims .4-.11, but the canonical
# prefix is .0-.7 — a known tracking item in BUG_BUCKET for the role-MCP
# range, not re-introduced here).
PI_RANGE_LO="${RS_INNER_PI_RANGE_LO:-192.168.99.10}"
PI_RANGE_HI="${RS_INNER_PI_RANGE_HI:-192.168.99.25}"
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
sudo iptables -A "$CHAIN" -s "$ROLE_MCP_RANGE" -j ACCEPT
sudo iptables -A "$CHAIN" -m iprange --src-range "$PI_RANGE_LO-$PI_RANGE_HI" -j ACCEPT
sudo iptables -A "$CHAIN" -m limit --limit 10/min --limit-burst 5 \
    -j LOG --log-prefix "rs-inner-fw drop: " --log-level warning
sudo iptables -A "$CHAIN" -j DROP

echo "inner-firewall: applied (subnet=$SUBNET proxy_ip=$PROXY_IP role_mcp_range=$ROLE_MCP_RANGE pi_range=$PI_RANGE_LO-$PI_RANGE_HI)"
