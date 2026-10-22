#!/bin/sh
# Slot start (root, NET_ADMIN + the caps below): route, firewall, then slotd with only the caps it needs.
# NET_ADMIN is gone from slotd on; the emulator and everything else slotd starts has no capabilities at all.
set -eu
: "${EGRESS:?}" "${SLOT_SECRET:?}"
export EMU_UID=${EMU_UID:-2000} SVC_UID=${SVC_UID:-3000}
# Docker's default RLIMIT_NOFILE is ~1e9; the emulator closes every fd up to the limit when it spawns a helper, which
# stalls the whole process (and the guest) for minutes. The wrapper also passes --ulimit; this covers other callers.
ulimit -n 65536 || true

if [ "$EGRESS" = tor ]; then
  # the `internal` network has no default route: the tor container is the only way out (and a transparent
  # proxy: TransPort/DNSPort). The wrapper re-runs tor-route.sh in here (docker exec) when the tor task changes address.
  until ip route show default | grep -q via; do /app/tor-route.sh || true; sleep 1; done
fi
sh /app/firewall.sh

# tmpfs homes: the emulator and adb keep their keys here. One fixed adb key (baked into the image): the guest
# only trusts the key present at the first boot of the userdata, i.e. the one the snapshot was made with.
/app/prep-homes.sh

KVM_GID=$(stat -c %g /dev/kvm) ; export KVM_GID
exec setpriv --no-new-privs \
  --bounding-set=-all,+setuid,+setgid,+setpcap,+kill,+chown,+dac_override,+fowner \
  /venv/bin/python /app/slotd.py
