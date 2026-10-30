#!/bin/sh
# Container start (as root): per-uid firewall, then sessiond (root, narrow caps),
# then the gateway (uid 1000, no caps). The gateway is restarted if it dies; sessiond
# on start and the gateway on start both wipe every pool uid (crash recovery).
set -eu
: "${EGRESS:?}"
: "${GATEWAY_PORT:?}"
if [ "${MODE:-}" = android ]; then
  # Android gateway: the sessions live in the slot containers (android-wrapper), so there is no uid pool,
  # no sessiond and no firewall here: just the unprivileged gateway, restarted on exit. It needs SETUID/SETGID/SETPCAP
  # for setpriv only; nothing remains once it runs.
  while :; do
    setpriv --no-new-privs --reuid=1000 --regid=1000 --groups="${GW_COLLECT_GID:-1500}" --bounding-set=-all --inh-caps=-all \
      uvicorn mcp_research.gateway.app:app --host 0.0.0.0 --port "$GATEWAY_PORT" || true
    echo "gateway exited, restarting" >&2
    sleep 1
  done
fi
UID_BASE=${UID_BASE:-20000}
MAX_SESSIONS_TOTAL=${MAX_SESSIONS_TOTAL:-18}
POOL=$((MAX_SESSIONS_TOTAL + 2))
export UID_BASE MAX_SESSIONS_TOTAL
: "${GATEWAY_PORT:?}"
# this replica's slice of tor's SocksPorts (same formula as common/ports.py)
SLOT=${REPLICA_SLOT:-1}
[ "$SLOT" -ge 1 ] || { echo "REPLICA_SLOT $SLOT < 1" >&2; exit 1; }
# the egress router (tor: one shared service; i2p: this slot's own i2pd) and its first SocksPort
ROUTER=
case $EGRESS in
  tor) ROUTER=tor; SOCKS_BASE=$((9050 + (SLOT - 1) * (POOL + 1)))
       [ "$SLOT" -le "${MAX_REPLICAS:-4}" ] || { echo "REPLICA_SLOT $SLOT outside 1..MAX_REPLICAS" >&2; exit 1; } ;;
  i2p) ROUTER="i2pd-$SLOT"; SOCKS_BASE=${I2P_SOCKS_BASE:-4500} ;;
esac

# --- egress firewall, per session uid: only its own loopback ports (so sessions
# cannot reach the gateway or each other), replies on accepted connections, the
# Docker resolver, and the egress policy. ponytail: IPv6 is rejected outright
# for session uids; add rules if the compose networks ever get v6.
for t in iptables ip6tables; do
  $t -F OUTPUT
  $t -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
done
router_ip() { getent ahostsv4 "$ROUTER" | awk 'NR==1{print $1}'; }
ROUTERIP=
if [ -n "$ROUTER" ]; then  # the router may still be starting
  while [ -z "$ROUTERIP" ]; do ROUTERIP=$(router_ip); [ -n "$ROUTERIP" ] || sleep 2; done
fi
i=0
while [ "$i" -lt "$POOL" ]; do
  U=$((UID_BASE + i))
  iptables -A OUTPUT -m owner --uid-owner "$U" -o lo -p tcp -m multiport --dports "$((5900+i)),$((9100+i))" -j ACCEPT
  iptables -A OUTPUT -m owner --uid-owner "$U" -o lo -d 127.0.0.11 -j ACCEPT
  if [ -n "$ROUTER" ]; then
    iptables -A OUTPUT -m owner --uid-owner "$U" -d "$ROUTERIP" -p tcp --dport "$((SOCKS_BASE+i))" -j ACCEPT
  fi
  i=$((i+1))
done
RANGE="$UID_BASE-$((UID_BASE + POOL - 1))"
if [ "$EGRESS" = clear ]; then
  # the configured resolvers (private IPs on some hosts) must stay reachable
  for ns in $(awk '/^nameserver/ && $2 ~ /^[0-9.]+$/ {print $2}' /etc/resolv.conf); do
    for p in udp tcp; do iptables -A OUTPUT -m owner --uid-owner "$RANGE" -d "$ns" -p $p --dport 53 -j ACCEPT; done
  done
  # clear: public internet only; no RFC1918/link-local/CGNAT/metadata/other compose services
  for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 127.0.0.0/8; do
    iptables -A OUTPUT -m owner --uid-owner "$RANGE" -d "$net" -j REJECT
  done
  iptables -A OUTPUT -m owner --uid-owner "$RANGE" -j ACCEPT
fi
iptables -A OUTPUT -m owner --uid-owner "$RANGE" -j REJECT
ip6tables -A OUTPUT -m owner --uid-owner "$RANGE" -j REJECT

# --- i2p: the rules pin the router's IP. If it changes (router task restarted), exit: Swarm restarts
# this replica and the rules are rebuilt. ponytail: sessions on the replica are lost, as on any restart.
if [ "$EGRESS" = i2p ]; then
  MAIN=$$
  while sleep 15; do
    ip=$(router_ip)
    [ -z "$ip" ] || [ "$ip" = "$ROUTERIP" ] || { echo "$ROUTER moved $ROUTERIP -> $ip, exiting" >&2; kill -TERM "$MAIN"; }
  done &
fi

# --- no-new-privileges is set here (setpriv), since Swarm services cannot set security_opt.
# --- sessiond: root, but only the caps it needs; NET_ADMIN is gone from here on
setpriv --no-new-privs --bounding-set=-all,+setuid,+setgid,+chown,+kill,+dac_override,+fowner \
  python -m mcp_research.session_server.sessiond &
i=0; while [ ! -S /run/sessiond.sock ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i+1)); done

# --- gateway: uid 1000, no caps, restarted on exit
while :; do
  setpriv --no-new-privs --reuid=1000 --regid=1000 --groups="${GW_COLLECT_GID:-1500}" --bounding-set=-all --inh-caps=-all \
    uvicorn mcp_research.gateway.app:app --host 0.0.0.0 --port "$GATEWAY_PORT" || true
  echo "gateway exited, restarting" >&2
  sleep 1
done
