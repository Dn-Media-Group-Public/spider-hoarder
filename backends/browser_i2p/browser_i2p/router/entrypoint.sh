#!/bin/sh
# One i2pd router per browser-i2p gateway replica, one `type=socks` section per session slot (+1 for the gateway's
# own requests) with transient keys: each its own destination, tunnel pool and leaseset. Ports must match
# browser_i2p/egress.py: I2P_SOCKS_BASE + pool index. Stateless: starts from the seed's netDb snapshot.
set -eu
D=/tmp/i2pd
N=$(( ${MAX_SESSIONS_TOTAL:-18} + 3 ))   # POOL_SIZE (= total + 2) + the gateway
Q=${I2P_TUNNEL_QUANTITY:-2}; L=${I2P_TUNNEL_LENGTH:-3}
mkdir -p "$D"
# best effort: a warm netDb. Without it i2pd reseeds from the public servers (slower).
if wget -q -T 20 -O /tmp/snapshot.tgz http://${I2P_SEED_HOST:-browser-i2p-seed}/snapshot.tgz && tar -xzf /tmp/snapshot.tgz -C "$D"; then
  echo "seeded from ${I2P_SEED_HOST:-browser-i2p-seed}"
else
  echo "no snapshot, reseeding" >&2
fi
rm -f /tmp/snapshot.tgz
cat > "$D/i2pd.conf" <<CONF
ipv6 = false
log = stdout
loglevel = warn
[http]
enabled = false
[httpproxy]
enabled = false
[socksproxy]
enabled = false
[sam]
enabled = false
[upnp]
enabled = false
CONF
# no outproxy= anywhere: clearnet names and raw IPs get SOCKS reply 0x08
{
  i=0; while [ "$i" -lt "$N" ]; do
    printf '[socks-%s]\ntype = socks\naddress = 0.0.0.0\nport = %s\nkeys = transient\n' "$i" "$((${I2P_SOCKS_BASE:-4500} + i))"
    printf 'inbound.quantity = %s\noutbound.quantity = %s\ninbound.length = %s\noutbound.length = %s\n\n' "$Q" "$Q" "$L" "$L"
    i=$((i+1))
  done
} > "$D/tunnels.conf"
exec i2pd --datadir "$D" --conf "$D/i2pd.conf" --tunconf "$D/tunnels.conf"
