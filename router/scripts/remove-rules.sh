#!/bin/sh
# remove-rules.sh <subnet> [resolvers]
# Removes every FORWARD rule this router ever emitted for a source subnet — the
# legacy shapes, and the per-resolver DNS accepts for the resolvers named in the
# subnet's record (what was applied) plus the ones passed (the host's current
# configuration, so a subnet whose record was lost still ends rule-less) — then
# removes the record. Per-MCP allows live in their own records and are left;
# the host purges them separately (purge-mcp-allows.sh, fed over stdin).
set -e

SUBNET="$1"
RESOLVERS="${2:-}"

if [ -z "$SUBNET" ]; then
    echo "Usage: remove-rules.sh <subnet> [resolvers]" >&2
    exit 1
fi

. /scripts/rules-lib.sh

delete_subnet_rules "$SUBNET" "$RESOLVERS"

# Remove the record last: a kill before this line leaves a record that still
# names the (now deleted) set, which the next apply or reapply simply redoes.
rm -f "$(rulefile_for "$SUBNET")"

echo "Removed rules for $SUBNET"
