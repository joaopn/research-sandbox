#!/usr/bin/env bash
# mcp-reload — render the proxy config from the bind-mounted allowlist, then
# make the mcp-proxy container RUNNING on the staged rs-mcp-proxy:latest image
# with that config: signal it (HUP) when it runs, start it when it is stopped,
# replace it when it runs another image, run it when there is none.
#
# Single source of truth for proxy startup, used at every boot of a dind
# project (entrypoint.supervisor.sh, entrypoint.sandbox-dind.sh), by the host
# after it stages the proxy image (create, a supervisor recreate), and after
# `research project mcp allow|deny` (via `docker exec rs-project-<proj>
# /usr/local/bin/mcp-reload`).
#
# Why the container's STATE and IMAGE are the key, not its existence: the
# inner daemon's state survives a privileged-dind recreate, so a boot meets a
# persisted proxy that its restart policy has not started yet, or one a person
# stopped; a HUP to either is refused. And a recreate that re-stages a new
# proxy image must not leave the old container serving.
#
# Two runs at once are normal: the boot's run and the host's run after a
# recreate land in the same seconds, while the host also removes the old proxy
# (`docker rm -f`) outside any lock. So:
#   - runs are serialised by a lock that wraps the render and the proxy step
#     (two renders write the same config file; two runs that both see no proxy
#     would both `docker run --name mcp-proxy`, and the loser would fail);
#   - the proxy step is tried twice: a first attempt that lost to the host's
#     removal (a HUP to a container being killed, a start refused on one
#     marked for removal, a name still taken) re-reads the state, waits out
#     the removal, and runs a fresh proxy.
#
# The inner-bridge guard excepts exactly the proxy's registered upstreams, so
# every reload re-runs it, on EVERY outcome — once, in the unlocked parent,
# after the reload lock is released (the guard serialises itself on its own
# lock and reads the allowlist and the rs-inner bridge, never the proxy or the
# rendered config, so holding the reload lock across it would only lengthen
# every waiter's wait).
#
# Exit: 0 the proxy is running with the new config (or dockerd is unreachable,
# or the proxy image is not staged yet — both announced); 1 the proxy step
# failed, the render failed, another run held the lock too long, or flock is
# missing; the guard's own non-zero when the guard fails (a guard failure
# fails the reload, so `research project mcp allow` reports it rather than
# leaving the new upstream silently dropped).
set -euo pipefail

rs_inner_guard() {
    if [[ -x /usr/local/bin/rs-inner-guard ]]; then
        /usr/local/bin/rs-inner-guard
    fi
}

# Per-container tmpfs, shared by every `docker exec`'d process of the
# container (as for the guard's lock). Every caller runs as the image user, so
# the file has one owner.
LOCK="/dev/shm/rs-mcp-reload.lock"
# flock's own exit code for a busy lock, chosen outside this script's 0/1 so
# the parent can tell "the other run held it" from "the run failed" (sysexits
# EX_TEMPFAIL, as for the guard).
LOCK_BUSY_RC=75
# How long a run waits for a removal ANOTHER actor started (the host's
# `docker rm -f mcp-proxy` in a recreate) to finish. Removing the proxy is a
# SIGKILL plus a small container's layers: under a second normally, a few
# seconds on a loaded host; half of this (5 s) would still cover that; ten
# times would hold a boot close to two minutes per attempt behind a removal
# that is stuck, where reporting it is the better outcome.
REMOVAL_WAIT_S="${RS_MCP_RELOAD_REMOVAL_WAIT_S:-10}"
# The holder's worst case is the locked child's own work only (the guard runs
# after the lock is released): render (~1 s) + `docker info` and the network
# ensure (~1 s) + two proxy attempts, each at most two inspects (~1 s), a
# removal wait (REMOVAL_WAIT_S), an `rm -f` and a `start`/`run` (a few s each)
# — about 21 s per attempt, about 45 s in all. Worst cases do not stack across
# holders: the long path is the race with the host's ONE removal, and once a
# holder has converged the proxy the next one only signals it. Twice that
# worst case leaves margin; half of it (45 s) would equal it and leave none;
# ten times would hold a boot or a host verb a quarter hour behind a hung
# sibling, where the warning is the better outcome.
LOCK_WAIT_S="${RS_MCP_RELOAD_LOCK_WAIT_S:-90}"

# --- the unlocked parent: take the lock, run the work, then the guard --------
if [[ -z "${RS_MCP_RELOAD_LOCKED:-}" ]]; then
    rc=0
    if ! command -v flock >/dev/null 2>&1; then
        echo "mcp-reload: flock missing; proxy NOT reloaded" >&2
        rc=1
    else
        env RS_MCP_RELOAD_LOCKED=1 \
            flock -w "$LOCK_WAIT_S" -E "$LOCK_BUSY_RC" "$LOCK" "$0" "$@" || rc=$?
        if [[ "$rc" == "$LOCK_BUSY_RC" ]]; then
            echo "mcp-reload: another reload held $LOCK for more than ${LOCK_WAIT_S}s; proxy NOT reloaded by this run" >&2
            rc=1
        fi
    fi
    rs_inner_guard
    exit "$rc"
fi

# --- the locked child: render, then converge the proxy ------------------------
python3 /opt/mcp-proxy-tools/mcp_render_config.py

if ! docker info >/dev/null 2>&1; then
    echo "mcp-reload: inner dockerd not reachable; skipping" >&2
    exit 0
fi

# Ensure rs-inner exists so the proxy and any spawned worker share it.
# Addresses below .128 are pinned by hand (proxy .2, role services .4-.9,
# boxes .14-.25 — the inner firewall lets some of them out), so docker's own
# allocation is confined to 192.168.99.128/25: a container joined without
# --ip can never land on a let-through or pinned address. rs-worker pins
# every worker inside that same pool (WORKER_IP_LO/HI). The gateway is named
# because docker otherwise takes the first address of the ip-range for it.
docker network inspect rs-inner >/dev/null 2>&1 \
    || docker network create --subnet 192.168.99.0/24 --gateway 192.168.99.1 \
        --ip-range 192.168.99.128/25 rs-inner >/dev/null

# Sets `state` (a docker State.Status, or "absent") and `img` (the image id
# the container runs) for mcp-proxy.
read_state() {
    local out
    if out="$(docker container inspect -f '{{.State.Status}} {{.Image}}' mcp-proxy 2>/dev/null)"; then
        state="${out%% *}"
        img="${out#* }"
    else
        state="absent"
        img=""
    fi
}

# One attempt to make the proxy running on the staged image with the rendered
# config. `set -e` does not apply inside a function called in a `||` list, so
# every failure returns explicitly.
proxy_converge() {
    local want waited
    want="$(docker image inspect -f '{{.Id}}' rs-mcp-proxy:latest 2>/dev/null || true)"
    read_state

    # Another actor is removing it: wait for the removal, then act on what is
    # left (normally nothing). Re-read once a second — the same poll as the
    # guard's and the harnesses' waits; a removal is seconds long.
    waited=0
    while [[ "$state" == "removing" || "$state" == "dead" ]]; do
        if (( waited >= REMOVAL_WAIT_S )); then
            echo "mcp-proxy: still being removed after ${REMOVAL_WAIT_S}s ($state); cannot start" >&2
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
        read_state
    done

    # A recreate may have staged a new proxy image: a container on another
    # image is replaced. When the tag is absent (a re-stage is between its
    # image removal and its load), nothing is replaced — the host's own reload
    # after the load converges it.
    if [[ "$state" != "absent" && -n "$want" && "$img" != "$want" ]]; then
        docker rm -f mcp-proxy >/dev/null || return 1
        echo "mcp-proxy: replaced (it ran an image other than the staged rs-mcp-proxy:latest)"
        state="absent"
    fi

    case "$state" in
        running)
            docker kill -s HUP mcp-proxy >/dev/null || return 1
            echo "mcp-proxy: reloaded"
            ;;
        created|exited)
            # Same container, so its 192.168.99.2 pin and mounts are kept; the
            # config is already rendered, so it reads the new one as it starts.
            docker start mcp-proxy >/dev/null || return 1
            echo "mcp-proxy: started (it was $state)"
            ;;
        restarting)
            echo "mcp-proxy: docker is restarting it after an exit, so it is not serving now; it reads the new config when it comes back up" >&2
            return 1
            ;;
        absent)
            if [[ -z "$want" ]]; then
                echo "mcp-proxy: image rs-mcp-proxy:latest not staged; cannot start" >&2
                return 0
            fi
            # Pin mcp-proxy to a known IP on rs-inner so the inner-firewall
            # (opt-in) can distinguish proxy-originated egress from worker
            # egress without depending on br_netfilter. Workers spawn later on
            # addresses from .128 up.
            docker run -d \
                --name mcp-proxy \
                --network rs-inner \
                --ip 192.168.99.2 \
                --restart unless-stopped \
                -v /workspace/.orchestrator/mcp-proxy:/etc/mcp-proxy:ro \
                -v /workspace/.orchestrator/logs:/var/log/mcp-proxy:rw \
                rs-mcp-proxy:latest >/dev/null || return 1
            echo "mcp-proxy: started"
            ;;
        *)
            echo "mcp-proxy: the container is $state; not reloaded" >&2
            return 1
            ;;
    esac
}

proxy_rc=0
proxy_converge || proxy_converge || proxy_rc=$?
exit "$proxy_rc"
