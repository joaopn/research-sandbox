#!/usr/bin/env bash
# boot.sh — the rs-search container's entrypoint (under tini).
#
# 1. The inner-Docker image's own boot (/entrypoint.sh of rs-sandbox-dind), in
#    the background: it starts the inner dockerd with its cgroup delegation
#    and the configured resolvers, then idles. Its log goes to a file this
#    script waits on.
# 2. The service's guard, once the inner daemon is up (DOCKER-USER exists
#    then):
#      RS-SEARCH-IN  (INPUT)  nothing from an inner network reaches this
#                             container (a search agent never reaches the
#                             caller-facing server); from outside only the
#                             service's port answers.
#      RS-SEARCH-FWD (DOCKER-USER) a NEW connection from an inner network
#                             that leaves the inner networks toward a
#                             private address is dropped (traffic between
#                             inner bridges returns to Docker's own chains,
#                             which keep two networks apart), so a search agent
#                             reaches the internet and its own network
#                             (SearXNG) and nothing one hop from this
#                             container (the MCP gateway, RS's router). The
#                             four ranges mirror router/scripts/rules-lib.sh
#                             PRIVATE_RANGES.
#    Docker keeps inner networks apart by itself, so one search never
#    reaches another's.
# 3. The service program, as root (it reads the inner Docker's volumes).
set -euo pipefail

SERVICE_PORT=8080                      # cli/search.py SERVICE_PORT (MIRROR)
SUBSTRATE_LOG=/tmp/rs-search-substrate.log
READY_LINE="=== Sandbox-dind ready ==="
# The dind boot waits up to 30 s for its dockerd, then runs its bridge and
# guard helpers (seconds). Three times that covers a loaded host; past it the
# inner daemon is not coming and the container stops, which the host reports.
BOOT_WAIT_S=90

/entrypoint.sh > "$SUBSTRATE_LOG" 2>&1 &

ready=""
for _ in $(seq 1 "$BOOT_WAIT_S"); do
    if grep -qF "$READY_LINE" "$SUBSTRATE_LOG" 2>/dev/null && docker info >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
if [[ -z "$ready" ]]; then
    echo "rs-search: the inner Docker did not come up within ${BOOT_WAIT_S}s" >&2
    tail -n 20 "$SUBSTRATE_LOG" >&2 || true
    exit 1
fi

if sudo iptables -S DOCKER-USER >/dev/null 2>&1; then
    HOOK=DOCKER-USER
else
    HOOK=FORWARD
fi
RULES="*filter
:RS-SEARCH-IN - [0:0]
:RS-SEARCH-FWD - [0:0]
-A RS-SEARCH-IN -i lo -j ACCEPT
-A RS-SEARCH-IN -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A RS-SEARCH-IN -i docker0 -j DROP
-A RS-SEARCH-IN -i br-+ -j DROP
-A RS-SEARCH-IN -p tcp --dport ${SERVICE_PORT} -j ACCEPT
-A RS-SEARCH-IN -j DROP
-A RS-SEARCH-FWD -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
-A RS-SEARCH-FWD -o br-+ -j RETURN
-A RS-SEARCH-FWD -o docker0 -j RETURN
"
for dev in docker0 br-+; do
    for range in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16; do
        RULES+="-A RS-SEARCH-FWD -i ${dev} -d ${range} -j DROP
"
    done
done
RULES+="COMMIT
"
printf '%s' "$RULES" | sudo iptables-restore --noflush
sudo iptables -C INPUT -j RS-SEARCH-IN 2>/dev/null || sudo iptables -I INPUT 1 -j RS-SEARCH-IN
sudo iptables -C "$HOOK" -j RS-SEARCH-FWD 2>/dev/null || sudo iptables -I "$HOOK" 1 -j RS-SEARCH-FWD

exec sudo -E /opt/conda/bin/python3 -u /opt/rs-search/service.py serve
