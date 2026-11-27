#!/bin/sh
# up.sh — set the VPN gateway up (or again, with a new configuration), from
# stdin: header lines, then a line `---`, then the tunnel's settings in
# WireGuard's own format (`wg setconf`). The header (service.py vpn_stdin):
#   ADDRESS=<IPv4 address/prefix>   once per tunnel address
#   DNS=<IPv4 address>              once per resolver (reached through the tunnel)
#   MTU=<number>                    optional
#   ENDPOINT=<IPv4 address>:<port>  the VPN server (its UDP way in)
#
# Order, so that nothing is ever forwarded outside the tunnel:
#   1. the firewall: FORWARD dropped by default, only into the tunnel (and the
#      answers back); INPUT dropped but for the answers and the VPN server's
#      UDP; NAT out of the tunnel;
#   2. the routing: what the gateway forwards (anything not for a network it
#      is on) looks up table 100, which holds the tunnel's default route and
#      an `unreachable` default under it — with the tunnel down, a VPN
#      search's packet is refused, never sent out of the host's exit; the
#      gateway's own packets (WireGuard's UDP to the server) use the main
#      table;
#   3. the tunnel, from the settings written into the tmpfs /etc/wireguard.
# Everything is set again from scratch at every run. A failure prints one
# plain line on stderr and exits non-zero; WireGuard's own messages are never
# passed on (they can quote a line of the settings, which hold the keys).
set -eu
umask 077

fail() { echo "$*" >&2; exit 1; }
num() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; }
ipv4() { case "$1" in ''|*[!0-9.]*) return 1 ;; esac; }
cidr4() { case "$1" in ''|*[!0-9./]*|*/*/*) return 1 ;; */*) return 0 ;; esac; return 1; }

ADDRS=""; DNS=""; MTU=""; ENDPOINT=""
header=""
while IFS= read -r line; do
    if [ "$line" = "---" ]; then header=done; break; fi
    case "$line" in
        ADDRESS=*) v=${line#ADDRESS=}; cidr4 "$v" || fail "the tunnel address is not one RS sends"; ADDRS="$ADDRS $v" ;;
        DNS=*) v=${line#DNS=}; ipv4 "$v" || fail "the DNS server is not one RS sends"; DNS="$DNS $v" ;;
        MTU=*) MTU=${line#MTU=}; num "$MTU" || fail "the MTU is not one RS sends" ;;
        ENDPOINT=*) ENDPOINT=${line#ENDPOINT=} ;;
        *) fail "the gateway was handed a line it does not know" ;;
    esac
done
[ "$header" = done ] || fail "the gateway was handed no tunnel settings"
EP_IP=${ENDPOINT%:*}; EP_PORT=${ENDPOINT##*:}
ipv4 "$EP_IP" && num "$EP_PORT" || fail "the VPN server's address is not one RS sends"
[ -n "$ADDRS" ] && [ -n "$DNS" ] || fail "the gateway was handed no address or no DNS server"

# The settings, into the tmpfs (never a mount of the host's file, never argv).
CONF=/etc/wireguard/wg0.conf
cat > "$CONF.new"
mv "$CONF.new" "$CONF"
for d in $DNS; do echo "$d"; done > /etc/wireguard/dns

# The interface the gateway's own route out leaves by (its first network).
WAN=$(ip -4 route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}')
[ -n "$WAN" ] || fail "the gateway has no route to the VPN server"

# 1. The firewall, from scratch.
ip link del wg0 2>/dev/null || true
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -F INPUT
iptables -F FORWARD
# Only RS's own NAT rule goes (Docker keeps its embedded resolver's rules in
# the same table).
while iptables -t nat -D POSTROUTING -o wg0 -j MASQUERADE 2>/dev/null; do :; done
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
iptables -A INPUT -i "$WAN" -p udp -s "$EP_IP" --sport "$EP_PORT" -j ACCEPT
iptables -A FORWARD -i wg0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
iptables -A FORWARD -i "$WAN" -j DROP
iptables -A FORWARD -o wg0 -j ACCEPT
iptables -t nat -A POSTROUTING -o wg0 -j MASQUERADE

# 2. The routing, from scratch.
for prio in 90 100 110; do
    while ip rule del priority "$prio" 2>/dev/null; do :; done
done
ip route flush table 100 2>/dev/null || true
ip rule add priority 90 lookup main suppress_prefixlength 0
ip rule add priority 100 iif lo lookup main
ip rule add priority 110 lookup 100
ip route add unreachable default metric 4096 table 100

# 3. The tunnel.
ip link add wg0 type wireguard || fail "the kernel's WireGuard is not available to the gateway"
wg setconf wg0 "$CONF" 2>/dev/null || fail "WireGuard did not accept the tunnel's settings"
for a in $ADDRS; do ip address add "$a" dev wg0 || fail "the tunnel address $a could not be set"; done
if [ -n "$MTU" ]; then ip link set wg0 mtu "$MTU" 2>/dev/null || fail "the MTU $MTU could not be set"; fi
ip link set wg0 up || fail "the tunnel could not be brought up"
ip route add default dev wg0 table 100
# The gateway's own queries to the resolvers (status.sh) go into the tunnel too.
for d in $DNS; do ip route replace "$d/32" dev wg0; done
echo "up"
