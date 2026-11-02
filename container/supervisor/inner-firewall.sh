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
#
# This chain sits at FORWARD position 1, BEFORE Docker's DOCKER-USER jump. What
# it lets through it RETURNs (never ACCEPTs), so evaluation continues into
# DOCKER-USER — where the always-on inner guard (inner-guard.sh) drops what
# leaves toward the project's neighbours — and into Docker's own chains, whose
# inter-bridge isolation now applies to these sources too (an ACCEPT here used
# to let rs-inner sources reach rs-dev-* and docker0 containers directly).
# What this chain drops is unchanged: the final DROP below.

set -euo pipefail

SUBNET="${RS_INNER_SUBNET:-192.168.99.0/24}"
PROXY_IP="${RS_INNER_PROXY_IP:-192.168.99.2}"
# Role-MCP containers (echo-mcp, wrangler, librarian, websearcher, …) live
# in 192.168.99.4-99.9: the four built roles at .4-.7 plus .8-.9 reserved
# for future roles (cli/role_mcp.py ROLE_IPS). Their containers spawn
# `claude -p` subprocesses that talk directly to api.anthropic.com — L3
# egress that crosses the bridge boundary and hits FORWARD. Without a
# RETURN here, the spawned claude can't reach Anthropic and every send_job
# stalls.
#
# Role-MCP↔mcp-proxy traffic stays on the rs-inner bridge (L2) and bypasses
# FORWARD entirely, so the proxy path doesn't need a hole here.
#
# An explicit iprange, never a CIDR: a CIDR whose start is not aligned to its
# prefix length is silently canonicalised by iptables (192.168.99.4/29 becomes
# 192.168.99.0/29, i.e. .0-.7), which let analysis workers at .3 out and cut
# the reserved role slots off. Workers themselves are pinned far away from
# every range below (rs-worker's pool, 192.168.99.128-.254).
ROLE_RANGE_LO="${RS_INNER_ROLE_MCP_RANGE_LO:-192.168.99.4}"
ROLE_RANGE_HI="${RS_INNER_ROLE_MCP_RANGE_HI:-192.168.99.9}"
# PI-direct boxes (rs-box-<name>, allocated by rs-sandbox at .14-.25)
# live in 192.168.99.10-99.25 — sixteen addresses, the box allocator's
# slots plus a reservation. They run interactive `claude` sessions inside
# byobu, which talk directly to api.anthropic.com (L3 egress, FORWARD
# path), so they need the same ACCEPT shape as role-MCPs.
# An iprange for the same reason as the role range above.
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

sudo iptables -A "$CHAIN" -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
sudo iptables -A "$CHAIN" -s "$PROXY_IP" -j RETURN
sudo iptables -A "$CHAIN" -m iprange --src-range "$ROLE_RANGE_LO-$ROLE_RANGE_HI" -j RETURN
sudo iptables -A "$CHAIN" -m iprange --src-range "$PI_RANGE_LO-$PI_RANGE_HI" -j RETURN
sudo iptables -A "$CHAIN" -m limit --limit 10/min --limit-burst 5 \
    -j LOG --log-prefix "rs-inner-fw drop: " --log-level warning
sudo iptables -A "$CHAIN" -j DROP

echo "inner-firewall: applied (subnet=$SUBNET proxy_ip=$PROXY_IP role_range=$ROLE_RANGE_LO-$ROLE_RANGE_HI pi_range=$PI_RANGE_LO-$PI_RANGE_HI)"
