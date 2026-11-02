#!/usr/bin/env bash
# inner-guard.sh — the always-on guard between a dind project's inner bridges
# and everything one hop from the project container.
#
# Boxes, dev boxes, workers and role services are containers of the project's
# INNER docker daemon (rs-inner, rs-dev-<box>, docker0). They leave the project
# by NAT through its uplink interface, so without this guard they reach every
# neighbour of the project on its bridge — the host's sshd at the bridge
# gateway, the box registry, gitea, the webui — and the project container
# itself (its sshd, its editor), none of which the egress router ever sees
# (one L2 hop, no FORWARD rule in the path). Measured 2026-11-02 and recorded
# as B143; this script closes the box half. The project's own root can remove
# these rules (B142): the guard is for what runs INSIDE the inner daemon.
#
# Two chains, both rebuilt atomically on every run (one iptables-restore
# transaction, so a live jump never points at an empty chain) and serialised
# through a tmpfs lock (the entrypoint, mcp-reload and the host's post-wiring
# exec may run this concurrently):
#
#   RS-INNER-GUARD     jumped from DOCKER-USER (FORWARD): a NEW connection
#                      leaving by the uplink toward a private range is
#                      dropped, except the MCP proxy's registered upstreams
#                      (source = the proxy's pinned address on the rs-inner
#                      bridge, destination = each (ip, port) the project's
#                      allowlist names) and gitea's HTTP port at the staged
#                      gitea address (dev, fetch and history boxes clone and
#                      push through it). A NEW connection that was DNAT'd by a
#                      published port and arrives from an inner interface is
#                      dropped too (a box reaching another box's published
#                      port through the project's address). Traffic that
#                      enters by the uplink (the webui's probes into boxes,
#                      the published SSH port) returns untouched.
#   RS-INNER-GUARD-IN  jumped from INPUT: every NEW connection to the project
#                      container itself from any interface but the uplink and
#                      loopback is dropped. Loopback carries the inner
#                      daemon's embedded DNS (a box's resolver forwards to the
#                      project's 127.0.0.11), so it must return first.
#
# The uplink is the interface of the default route, read at run time. The
# four private ranges mirror router/scripts/apply-rules.sh. The proxy pin is
# RS_INNER_PROXY_IP (inner-firewall.sh's default); the gitea port is
# cli/gitea.py GITEA_INNER_PORT — a lockstep literal also carried by
# cli/rs_sandbox.py DEV_GITEA_URL, cli/rs_fetch.py, container/dev/rs-land,
# container/dev/rs-repo-watch and the history box script (pytest-pinned here).
#
# Composes with the opt-in inner firewall (inner-firewall.sh, RS_INNER_FIREWALL=1),
# whose chain sits before DOCKER-USER and RETURNs what it lets through.
#
# Exit codes: 0 applied; 2 applied but at least one exception entry was
# rejected (announced on stderr, the drops are in place regardless); 1 not
# applied (no default route, no firewall access, no daemon) — the callers
# print a WARNING and continue, the inner firewall's posture.

set -euo pipefail

LOCK="/dev/shm/rs-inner-guard.lock"
# A run takes well under a second; the wait only matters when a sibling run
# is stuck behind a stalled iptables. Half of this (15 s) would still cover a
# loaded host; ten times it would hold a project's boot for minutes behind a
# hung sibling — the warning after this bound is the better outcome.
LOCK_WAIT_S="${RS_INNER_GUARD_LOCK_WAIT_S:-30}"
# flock's own exit code for a busy lock, chosen outside this script's 0/1/2
# so the wrapper can tell "the other run held it" from "the run failed"
# (sysexits EX_TEMPFAIL).
LOCK_BUSY_RC=75
PROXY_IP="${RS_INNER_PROXY_IP:-192.168.99.2}"
INNER_SUBNET="${RS_INNER_SUBNET:-192.168.99.0/24}"
GITEA_PORT=3000
DEV_GITEA_JSON="${RS_INNER_GUARD_DEV_GITEA_JSON:-/workspace/.orchestrator/dev-gitea.json}"
MCP_ALLOW_JSON="${RS_INNER_GUARD_MCP_ALLOW_JSON:-/workspace/.orchestrator/mcp-allow.json}"
FWD_CHAIN="RS-INNER-GUARD"
IN_CHAIN="RS-INNER-GUARD-IN"
PRIVATE_RANGES=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16)

warn() { echo "inner-guard: $*" >&2; }

# --- one writer at a time ---------------------------------------------------
if [[ -z "${RS_INNER_GUARD_LOCKED:-}" ]]; then
    if ! command -v flock >/dev/null 2>&1; then
        warn "flock missing; guard NOT applied"
        exit 1
    fi
    rc=0
    env RS_INNER_GUARD_LOCKED=1 \
        flock -w "$LOCK_WAIT_S" -E "$LOCK_BUSY_RC" "$LOCK" "$0" "$@" || rc=$?
    if [[ "$rc" == "$LOCK_BUSY_RC" ]]; then
        warn "another run held the lock for more than ${LOCK_WAIT_S}s; guard NOT applied by this run"
        exit 1
    fi
    exit "$rc"
fi

# --- preconditions -----------------------------------------------------------
for tool in iptables iptables-restore ip python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        warn "$tool missing; guard NOT applied"
        exit 1
    fi
done
if ! sudo iptables -S FORWARD >/dev/null 2>&1; then
    warn "cannot read the FORWARD chain; guard NOT applied"
    exit 1
fi
UPLINK="$(ip -o route show default 2>/dev/null \
    | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"
if [[ -z "$UPLINK" ]]; then
    warn "no default route, so no uplink to key on; guard NOT applied"
    exit 1
fi
# DOCKER-USER is the chain Docker keeps for operator rules and never flushes.
# Without it, hook FORWARD directly — but only when the daemon is up: on a
# daemon that has not started yet, Docker would later insert its own jumps
# above ours and the guard would sit behind Docker's bridge ACCEPT.
if sudo iptables -S DOCKER-USER >/dev/null 2>&1; then
    HOOK="DOCKER-USER"
elif docker info >/dev/null 2>&1; then
    HOOK="FORWARD"
    warn "DOCKER-USER chain absent on a running daemon; hooking FORWARD directly"
else
    warn "inner dockerd not reachable and DOCKER-USER absent; guard NOT applied"
    exit 1
fi
# The rs-inner bridge, by its subnet's connected route (empty until mcp-reload
# creates the network — then there is no proxy to except either).
INNER_BR="$(ip -o route show "$INNER_SUBNET" 2>/dev/null \
    | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"

# --- exception entries, validated -------------------------------------------
# Prints one line per accepted entry ("gitea <ip>" / "mcp <ip> <port>") and
# "rejected" for each entry that is not an IPv4 address + a port in 1-65535;
# an unreadable or malformed file contributes no entries and a warning. Both
# files live in the bind-mounted workspace, which the project can write: the
# validation is what keeps a stray value from aborting the whole rebuild or
# widening an exception beyond one address and one port.
ENTRIES="$(python3 - "$DEV_GITEA_JSON" "$MCP_ALLOW_JSON" <<'PY'
import ipaddress, json, sys

def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"inner-guard: warning: {path} unreadable ({exc}); its exceptions are skipped",
              file=sys.stderr)
        return None

def ipv4(value):
    try:
        return str(ipaddress.IPv4Address(value)) if isinstance(value, str) else None
    except ValueError:
        return None

dev = load(sys.argv[1])
if isinstance(dev, dict) and dev.get("gitea_ip"):
    ip = ipv4(dev["gitea_ip"])
    if ip:
        print("gitea", ip)
    else:
        print("inner-guard: warning: dev-gitea.json gitea_ip is not an IPv4 address; gitea exception skipped",
              file=sys.stderr)
        print("rejected")

allow = load(sys.argv[2])
if allow is not None and not isinstance(allow, list):
    print(f"inner-guard: warning: {sys.argv[2]} root must be a list; its exceptions are skipped",
          file=sys.stderr)
    allow = []
for entry in allow or []:
    if not isinstance(entry, dict):
        continue
    ip = ipv4(entry.get("ip"))
    port = entry.get("port")
    ok_port = isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535
    if ip and ok_port:
        print("mcp", ip, port)
    else:
        print(f"inner-guard: warning: mcp-allow.json entry {entry.get('name')!r} has no valid ip/port; skipped",
              file=sys.stderr)
        print("rejected")
PY
)"

# --- the rule set, applied as one transaction --------------------------------
RULES="*filter
:${FWD_CHAIN} - [0:0]
:${IN_CHAIN} - [0:0]
-A ${FWD_CHAIN} -i ${UPLINK} -j RETURN
-A ${FWD_CHAIN} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
-A ${FWD_CHAIN} -m conntrack --ctstate DNAT -j DROP
-A ${FWD_CHAIN} ! -o ${UPLINK} -j RETURN
"
REJECTED=0
SKIPPED_NO_BRIDGE=0
GITEA_IP=""
MCP_PAIRS=0
while read -r kind a b; do
    case "$kind" in
        gitea)
            GITEA_IP="$a"
            RULES+="-A ${FWD_CHAIN} -d ${a} -p tcp --dport ${GITEA_PORT} -j RETURN
" ;;
        mcp)
            if [[ -n "$INNER_BR" ]]; then
                MCP_PAIRS=$((MCP_PAIRS + 1))
                RULES+="-A ${FWD_CHAIN} -i ${INNER_BR} -s ${PROXY_IP} -d ${a} -p tcp --dport ${b} -j RETURN
"
            else
                # no rs-inner bridge yet, so no proxy to except either; the
                # run that follows mcp-reload's network step adds the pairs
                SKIPPED_NO_BRIDGE=$((SKIPPED_NO_BRIDGE + 1))
            fi ;;
        rejected) REJECTED=$((REJECTED + 1)) ;;
    esac
done <<< "$ENTRIES"
for range in "${PRIVATE_RANGES[@]}"; do
    RULES+="-A ${FWD_CHAIN} -d ${range} -j DROP
"
done
RULES+="-A ${IN_CHAIN} -i lo -j RETURN
-A ${IN_CHAIN} -i ${UPLINK} -j RETURN
-A ${IN_CHAIN} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
-A ${IN_CHAIN} -j DROP
COMMIT
"
# --noflush keeps every other chain; a chain declared in the stream is
# flushed and refilled inside the one commit.
printf '%s' "$RULES" | sudo iptables-restore --noflush

# --- the jumps, once -----------------------------------------------------------
sudo iptables -C "$HOOK" -j "$FWD_CHAIN" 2>/dev/null \
    || sudo iptables -I "$HOOK" 1 -j "$FWD_CHAIN"
if [[ "$HOOK" == "DOCKER-USER" ]]; then
    # A previous run on a daemon without DOCKER-USER hooked FORWARD; one hook.
    while sudo iptables -C FORWARD -j "$FWD_CHAIN" 2>/dev/null; do
        sudo iptables -D FORWARD -j "$FWD_CHAIN"
    done
fi
sudo iptables -C INPUT -j "$IN_CHAIN" 2>/dev/null \
    || sudo iptables -I INPUT 1 -j "$IN_CHAIN"

echo "inner-guard: applied (uplink=${UPLINK} hook=${HOOK} inner_bridge=${INNER_BR:-none} proxy=${PROXY_IP} mcp_pairs=${MCP_PAIRS} gitea=${GITEA_IP:-none}:${GITEA_PORT} rejected=${REJECTED})"
if [[ "$SKIPPED_NO_BRIDGE" -gt 0 ]]; then
    warn "${SKIPPED_NO_BRIDGE} proxy upstream pair(s) not installed: the rs-inner bridge does not exist yet (the next run after mcp-reload adds them)"
fi
if [[ "$REJECTED" -gt 0 ]]; then
    warn "${REJECTED} exception entr$([[ $REJECTED == 1 ]] && echo y || echo ies) rejected; the drops are in place, the rejected exceptions are not"
    exit 2
fi
