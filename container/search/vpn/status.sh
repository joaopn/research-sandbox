#!/bin/sh
# status.sh WAIT_S MAX_AGE_S — whether the tunnel has a fresh handshake.
# Prints one line: `up <seconds since the last handshake>`, `down none` (no
# handshake within WAIT_S), `down stale <seconds>` or `down nolink` (no tunnel).
# A handshake older than MAX_AGE_S (or none) is renewed first: one DNS query
# to the first resolver goes into the tunnel, which makes WireGuard shake
# hands, and the handshake is awaited up to WAIT_S seconds.
set -u
WAIT=$1; MAX=$2
ip link show wg0 >/dev/null 2>&1 || { echo "down nolink"; exit 0; }
age() {
    hs=$(wg show wg0 latest-handshakes 2>/dev/null | awk 'NR == 1 { print $2 }')
    if [ -z "$hs" ] || [ "$hs" = 0 ]; then echo none; else echo $(( $(date +%s) - hs )); fi
}
fresh() { [ "$1" != none ] && [ "$1" -le "$MAX" ]; }
a=$(age)
if ! fresh "$a"; then
    dns=$(head -n 1 /etc/wireguard/dns 2>/dev/null)
    [ -n "$dns" ] && timeout "$WAIT" nslookup example.org "$dns" >/dev/null 2>&1 &
    end=$(( $(date +%s) + WAIT ))
    while ! fresh "$a" && [ "$(date +%s)" -lt "$end" ]; do
        sleep 0.25
        a=$(age)
    done
fi
if fresh "$a"; then echo "up $a"
elif [ "$a" = none ]; then echo "down none"
else echo "down stale $a"
fi
