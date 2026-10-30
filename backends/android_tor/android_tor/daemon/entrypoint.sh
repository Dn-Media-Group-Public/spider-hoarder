#!/bin/sh
# This backend's own Tor daemon: a transparent proxy for the Android slots (docs/implemented-plans/android-hoarder.md). On the
# slot network (ANDROID_TOR_SUBNET) every TCP connection is redirected to TransPort and DNS to DNSPort; nothing else is
# forwarded. Stream isolation is per source IP, i.e. per slot.
set -eu
ANDROID_TOR_SUBNET=${ANDROID_TOR_SUBNET:-10.99.0.0/24}
{
  echo "TransPort 0.0.0.0:9040"
  echo "DNSPort 0.0.0.0:5353"
  echo "AutomapHostsOnResolve 1"
  echo "VirtualAddrNetworkIPv4 10.192.0.0/10"   # onion names are mapped here (slots accept it in their firewall)
  echo "DataDirectory /var/lib/tor"
  echo "Log notice stdout"
} > /tmp/torrc

# The redirect must match the slot network's interface only: this container also has the egress network. Forwarding is
# otherwise off: no masquerade, FORWARD drops everything.
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
