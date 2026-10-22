#!/bin/sh
# Egress firewall of an Android slot, per uid (root with NET_ADMIN, before anything unprivileged starts).
# The emulator's user-mode NAT makes guest traffic leave as the emulator uid's own connections, and the
# guest reaches the container's loopback through the emulator's 10.0.2.2 alias, so these OUTPUT rules ARE the
# guest's firewall. In: EGRESS (clear|tor) EMU_UID SVC_UID; IPT / IPT6 override the binaries (tests).
# See docs/implemented-plans/android-hoarder.md, "Network and egress".
set -eu
: "${EGRESS:?}" "${EMU_UID:?}" "${SVC_UID:?}"
IPT=${IPT:-iptables}
IPT6=${IPT6:-ip6tables}
RESOLV=${RESOLV:-/etc/resolv.conf}

for t in "$IPT" "$IPT6"; do
  $t -F OUTPUT
  $t -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
done

# --- emulator uid (= the guest)
if [ "$EGRESS" = tor ]; then
  # onion names are mapped into Tor's virtual range: must be accepted before the private ranges are rejected
  $IPT -A OUTPUT -m owner --uid-owner "$EMU_UID" -d 10.192.0.0/10 -j ACCEPT
else
  # Docker's resolver sits on loopback; the configured resolvers may be private IPs on some hosts
  $IPT -A OUTPUT -m owner --uid-owner "$EMU_UID" -o lo -d 127.0.0.11 -j ACCEPT
  for ns in $(awk '/^nameserver/ && $2 ~ /^[0-9.]+$/ {print $2}' "$RESOLV"); do
    for p in udp tcp; do $IPT -A OUTPUT -m owner --uid-owner "$EMU_UID" -d "$ns" -p $p --dport 53 -j ACCEPT; done
  done
fi
# Everything inside the stack and the host: loopback entirely (tool server, adb, VNC and the console are
# reachable from the guest as 10.0.2.2; the emulator must NOT be allowed to reach the adb server: S6),
# overlay peers (other slots, gateway, tor's own address), cloud metadata, CGNAT.
for net in 127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10; do
  $IPT -A OUTPUT -m owner --uid-owner "$EMU_UID" -d "$net" -j REJECT
done
# public internet: direct NAT (clear), or the default route via the tor container, where TransPort/DNSPort
# catch TCP and DNS and everything else dies (tor)
$IPT -A OUTPUT -m owner --uid-owner "$EMU_UID" -j ACCEPT
$IPT6 -A OUTPUT -m owner --uid-owner "$EMU_UID" -j REJECT

# --- tool server / adb server / VNC uid: loopback only (defence in depth: adb takes `host:connect:<ip>`
# commands from whoever reaches it, and must never be usable as a pivot out of the VM)
$IPT -A OUTPUT -m owner --uid-owner "$SVC_UID" -o lo -j ACCEPT
$IPT -A OUTPUT -m owner --uid-owner "$SVC_UID" -j REJECT
$IPT6 -A OUTPUT -m owner --uid-owner "$SVC_UID" -j REJECT
