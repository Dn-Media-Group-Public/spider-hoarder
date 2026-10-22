#!/bin/sh
# One SocksPort per session slot (+1 for the gateway's own requests). Tor isolates
# streams from different SocksPorts, so different sessions/users never share circuits.
# Port numbers must match mcp-research/ports.py: 9050 + (replica slot - 1) * STRIDE + pool index;
# POOL_SIZE = MAX_SESSIONS_TOTAL + 2, STRIDE = POOL_SIZE + 1. One range per research replica (MAX_REPLICAS).
# Also a transparent proxy for the Android slots (docs/implemented-plans/android-hoarder.md): on the android_tor
# overlay (ANDROID_TOR_SUBNET) every TCP connection is redirected to TransPort and DNS to DNSPort; nothing else is
# forwarded. Stream isolation is per source IP, i.e. per slot.
set -eu
ANDROID_TOR_SUBNET=${ANDROID_TOR_SUBNET:-10.99.0.0/24}
POOL_SIZE=$(( ${MAX_SESSIONS_TOTAL:-18} + 2 ))
N=$(( (POOL_SIZE + 1) * ${MAX_REPLICAS:-4} ))
{
  i=0; while [ "$i" -lt "$N" ]; do echo "SocksPort 0.0.0.0:$((9050+i))"; i=$((i+1)); done
  echo "SocksPolicy accept 10.0.0.0/8"
  echo "SocksPolicy accept 172.16.0.0/12"
  echo "SocksPolicy accept 192.168.0.0/16"
  echo "SocksPolicy reject *"
  echo "TransPort 0.0.0.0:9040"
  echo "DNSPort 0.0.0.0:5353"
  echo "AutomapHostsOnResolve 1"
  echo "VirtualAddrNetworkIPv4 10.192.0.0/10"   # onion names are mapped here (slots accept it in their firewall)
  echo "DataDirectory /var/lib/tor"
  echo "Log notice stdout"
} > /tmp/torrc

# The redirect must match the android_tor interface only: this container also has the normal networks (its own
# circuits, the browsers' SocksPorts). Forwarding is otherwise off: no masquerade, FORWARD drops everything.
IF=
i=0
while [ -z "$IF" ] && [ "$i" -lt 60 ]; do
  IF=$(ip -4 route show "$ANDROID_TOR_SUBNET" | awk '{for (j = 1; j < NF; j++) if ($j == "dev") { print $(j+1); exit }}')
  [ -n "$IF" ] || { sleep 1; i=$((i+1)); }
done
if [ -n "$IF" ]; then
  iptables -P FORWARD DROP
  iptables -t nat -A PREROUTING -i "$IF" -p tcp -j REDIRECT --to-ports 9040
  iptables -t nat -A PREROUTING -i "$IF" -p udp --dport 53 -j REDIRECT --to-ports 5353
else
  echo "no interface in $ANDROID_TOR_SUBNET: the Android Tor slots will have no egress" >&2
fi

exec setpriv --no-new-privs --reuid=debian-tor --regid=debian-tor --init-groups --bounding-set=-all --inh-caps=-all \
  tor -f /tmp/torrc
