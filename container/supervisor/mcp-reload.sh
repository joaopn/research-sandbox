#!/usr/bin/env bash
# mcp-reload — render the proxy config from the bind-mounted allowlist and
# either SIGHUP the running mcp-proxy or spawn it.
#
# Single source of truth for proxy startup, used both at supervisor first
# boot (entrypoint.supervisor.sh) and after `research project mcp-allow|deny`
# mutations (via `docker exec rs-project-<proj> /usr/local/bin/mcp-reload`).
set -euo pipefail

# The inner-bridge guard excepts exactly the proxy's registered upstreams, so
# every reload that may have changed the allowlist re-runs it — on EVERY exit
# path below (the host's allow/deny reload takes the SIGHUP path). A guard
# failure fails the reload, so `research project mcp allow` reports it rather
# than leaving the new upstream silently dropped.
rs_inner_guard() {
    if [[ -x /usr/local/bin/rs-inner-guard ]]; then
        /usr/local/bin/rs-inner-guard
    fi
}

python3 /opt/mcp-proxy-tools/mcp_render_config.py

if ! docker info >/dev/null 2>&1; then
    echo "mcp-reload: inner dockerd not reachable; skipping" >&2
    rs_inner_guard
    exit 0
fi

# Ensure rs-inner exists so the proxy and any spawned worker share it.
docker network inspect rs-inner >/dev/null 2>&1 \
    || docker network create --subnet 192.168.99.0/24 rs-inner >/dev/null

if docker inspect mcp-proxy >/dev/null 2>&1; then
    docker kill -s HUP mcp-proxy >/dev/null
    echo "mcp-proxy: reloaded"
    rs_inner_guard
    exit 0
fi

if ! docker image inspect rs-mcp-proxy:latest >/dev/null 2>&1; then
    echo "mcp-proxy: image rs-mcp-proxy:latest not staged; cannot start" >&2
    rs_inner_guard
    exit 0
fi

# Pin mcp-proxy to a known IP on rs-inner so the inner-firewall (Stage 2.3,
# opt-in) can distinguish proxy-originated egress from worker egress without
# depending on br_netfilter. Workers spawn later and get auto-allocated IPs
# from .3 onward.
docker run -d \
    --name mcp-proxy \
    --network rs-inner \
    --ip 192.168.99.2 \
    --restart unless-stopped \
    -v /workspace/.orchestrator/mcp-proxy:/etc/mcp-proxy:ro \
    -v /workspace/.orchestrator/logs:/var/log/mcp-proxy:rw \
    rs-mcp-proxy:latest >/dev/null
echo "mcp-proxy: started"
rs_inner_guard
