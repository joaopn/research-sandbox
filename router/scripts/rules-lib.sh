#!/bin/sh
# rules-lib.sh — shared by apply-rules.sh, remove-rules.sh and reapply-rules.sh.
# Sourced, not executed. Busybox sh.
#
# A project's firewall record is /etc/sandbox/rules/<subnet with / as _>,
# one line: `SUBNET MODE [RESOLVERS]`, RESOLVERS comma-separated (locked mode
# only). The record names what was APPLIED, so removal deletes exactly that;
# a record without resolvers predates the resolver-pinned rules and replays
# with no 53 rule at all under locked (fail-closed until `research start`
# re-applies every record with the configured resolvers).
#
# Deleting rules never touches the record: apply-rules.sh writes the new record
# (temp + rename) only after every iptables call succeeded, so a record never
# names a set that was not fully applied when it was written (after a kill
# mid-apply the old record may name a set since deleted — fail-closed, redone
# by the next apply or reapply); remove-rules.sh removes it last.

RULES_DIR="/etc/sandbox/rules"
# Written by the entrypoint after its replay; the host waits for it before any
# rule call so a `research start` never interleaves with the boot replay. /run
# is a tmpfs (docker-compose.yml), so a restart never starts with a stale one.
READY_MARKER="/run/rs-router-ready"
# The four private ranges every mode drops; mirrored by the inner guard
# (container/supervisor/inner-guard.sh) and by Config's resolver validation
# (cli/rscore.py PRIVATE_RESOLVER_RANGES) — keep the three in lockstep.
PRIVATE_RANGES="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16"

rulefile_for() {  # <subnet> -> path of its record
    echo "$RULES_DIR/$(echo "$1" | tr '/' '_')"
}

file_resolvers() {  # <subnet> -> the RESOLVERS field of its record, if any
    f="$(rulefile_for "$1")"
    [ -f "$f" ] && awk '{print $3}' "$f"
}

# Delete every FORWARD rule this router ever emitted for a subnet: the legacy
# shapes (the pre-resolver bare 53 and ICMP accepts included, so an upgrade
# cleans them) and the per-resolver shapes for the union of the record's
# resolvers and the ones passed (what is about to be applied). Literal on
# purpose: a generic "every rule with this source" scan would also take the
# per-MCP allows, which live in their own records and must survive an apply
# or reapply. The host takes them separately at destroy and before a range is
# reused (purge-mcp-allows.sh).
delete_subnet_rules() {  # <subnet> [resolvers]
    s="$1"
    for r in $PRIVATE_RANGES; do
        while iptables -D FORWARD -s "$s" -d "$r" -j DROP 2>/dev/null; do :; done
    done
    while iptables -D FORWARD -s "$s" -p icmp -j ACCEPT 2>/dev/null; do :; done
    while iptables -D FORWARD -s "$s" -p tcp --dport 80 -j ACCEPT 2>/dev/null; do :; done
    while iptables -D FORWARD -s "$s" -p tcp --dport 443 -j ACCEPT 2>/dev/null; do :; done
    while iptables -D FORWARD -s "$s" -p udp --dport 53 -j ACCEPT 2>/dev/null; do :; done
    while iptables -D FORWARD -s "$s" -p tcp --dport 53 -j ACCEPT 2>/dev/null; do :; done
    for r in $(printf '%s,%s' "$(file_resolvers "$s")" "${2:-}" | tr ',' ' '); do
        while iptables -D FORWARD -s "$s" -d "$r" -p udp --dport 53 -j ACCEPT 2>/dev/null; do :; done
        while iptables -D FORWARD -s "$s" -d "$r" -p tcp --dport 53 -j ACCEPT 2>/dev/null; do :; done
    done
    while iptables -D FORWARD -s "$s" -j DROP 2>/dev/null; do :; done
    while iptables -D FORWARD -s "$s" -j ACCEPT 2>/dev/null; do :; done
}

sorted_list() {  # <comma list> -> the same addresses, sorted, comma-joined
    echo "$1" | tr ',' '\n' | grep . | sort | tr '\n' ',' | sed 's/,$//'
}
