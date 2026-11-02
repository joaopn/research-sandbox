#!/bin/sh
# apply-rules.sh <subnet> <locked|open> [resolvers]
# Idempotent: deletes this subnet's existing rules (every shape this router ever
# emitted, see rules-lib.sh) before appending the new set, then records what was
# applied. `resolvers` is the comma-separated list of the project's configured
# DNS servers (validated IPv4 literals, the host's `SANDBOX_DNS`): under
# `locked`, 53 is open to those addresses only — a project can resolve names
# through its configured resolvers and nowhere else, and there is no ICMP
# accept (ICMP errors ride the established/related accept, so path-MTU and
# unreachables still work; echo does not). A locked record with no resolvers
# (one written before the resolvers were recorded) applies with no 53 rule at
# all and a warning: fail-closed, never a refusal — the entrypoint's replay
# must survive it, and `research start` re-applies every record with the
# configured resolvers right after.
set -e

SUBNET="$1"
MODE="$2"
RESOLVERS="${3:-}"

if [ -z "$SUBNET" ] || [ -z "$MODE" ]; then
    echo "Usage: apply-rules.sh <subnet> <locked|open> [resolvers]" >&2
    exit 1
fi
if [ "$MODE" != "locked" ] && [ "$MODE" != "open" ]; then
    echo "apply-rules.sh: mode must be locked or open, got '$MODE'" >&2
    exit 1
fi

. /scripts/rules-lib.sh

# The record is left in place until the new set is fully applied (below).
delete_subnet_rules "$SUBNET" "$RESOLVERS"

# Block RFC1918 destinations (both modes; the mirror of the inner guard's drops)
for r in $PRIVATE_RANGES; do
    iptables -A FORWARD -s "$SUBNET" -d "$r" -j DROP
done

if [ "$MODE" = "locked" ]; then
    # Allow only HTTP, HTTPS and DNS to the configured resolvers.
    iptables -A FORWARD -s "$SUBNET" -p tcp --dport 80 -j ACCEPT
    iptables -A FORWARD -s "$SUBNET" -p tcp --dport 443 -j ACCEPT
    if [ -z "$RESOLVERS" ]; then
        echo "WARNING: $SUBNET locked with no resolver list — no DNS rule applied; run \`research start\` to re-apply with the configured resolvers" >&2
    fi
    for r in $(echo "$RESOLVERS" | tr ',' ' '); do
        iptables -A FORWARD -s "$SUBNET" -d "$r" -p udp --dport 53 -j ACCEPT
        iptables -A FORWARD -s "$SUBNET" -d "$r" -p tcp --dport 53 -j ACCEPT
    done
    iptables -A FORWARD -s "$SUBNET" -j DROP
fi
# open mode: only RFC1918 blocked, all other traffic falls through to default
# (which is DROP, but established/related is ACCEPTed, and new outbound needs
#  an explicit ACCEPT)

if [ "$MODE" = "open" ]; then
    iptables -A FORWARD -s "$SUBNET" -j ACCEPT
fi

# Record what was applied, atomically, only now that every rule is in.
RULEFILE="$(rulefile_for "$SUBNET")"
mkdir -p "$RULES_DIR"
if [ -n "$RESOLVERS" ] && [ "$MODE" = "locked" ]; then
    echo "$SUBNET $MODE $RESOLVERS" > "$RULEFILE.tmp"
else
    echo "$SUBNET $MODE" > "$RULEFILE.tmp"
fi
mv "$RULEFILE.tmp" "$RULEFILE"

if [ "$MODE" = "locked" ]; then
    echo "Applied $MODE rules for $SUBNET (dns: ${RESOLVERS:-none})"
else
    echo "Applied $MODE rules for $SUBNET"
fi
