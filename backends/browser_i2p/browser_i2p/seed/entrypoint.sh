#!/bin/sh
# Keeps a warm netDb and serves it as snapshot.tgz (netDb + addressbook/addresses.csv only: never router
# identity, destinations, profiles or tags) for the i2pd routers to start from. SOCKS and every other
# local service of this router are off. The volume is /var/lib/i2pd.
set -eu
D=/var/lib/i2pd S=/srv/snap
mkdir -p "$D" "$S"
chown -R i2pd "$D"
cat > /tmp/i2pd.conf <<CONF
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
snap() {
  cd "$D"
  # copy of the live netDb (atomic swap below); tar exit 1 = a file changed while reading, fine
  tar -czf "$S/.snapshot.tmp" $(ls -d netDb addressbook/addresses.csv 2>/dev/null) || [ $? -eq 1 ] || return 0
  mv "$S/.snapshot.tmp" "$S/snapshot.tgz"
}
su-exec i2pd i2pd --datadir "$D" --conf /tmp/i2pd.conf --tunconf /dev/null &
nginx
sleep 300   # first snapshot once the netDb has filled
while :; do
  snap
  sleep "${I2P_SNAPSHOT_INTERVAL:-1800}"
done
