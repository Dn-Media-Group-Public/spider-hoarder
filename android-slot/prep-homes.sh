#!/bin/sh
# (Re)create the emulator's and the svc user's adb homes with the baked key. Root; also used by slotd's reset.
set -eu
EMU_UID=${EMU_UID:-2000}; SVC_UID=${SVC_UID:-3000}
for u in "emu:$EMU_UID" "svc:$SVC_UID"; do
  name=${u%:*}; id=${u#*:}
  d=/home/$name/.android
  mkdir -p "$d"
  cp /opt/adb/adbkey /opt/adb/adbkey.pub "$d/"
  chmod 600 "$d/adbkey"
  chown -R "$id:$id" "/home/$name"
done
