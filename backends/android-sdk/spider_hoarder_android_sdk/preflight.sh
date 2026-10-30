#!/bin/sh
# preflight of the Android backends (run by scripts/ on the deploy host, before anything is built): this host makes the golden
# snapshot and runs the wrapper and the emulators, so it needs KVM and x86_64; the slot image needs the F-Droid pin.
# Non-zero exit aborts the deploy with this script's stderr.
[ -e /dev/kvm ] || { echo "$BACKEND needs /dev/kvm on this host (KVM-capable x86_64, nested virtualisation on a VM)" >&2; exit 1; }
[ "$(uname -m)" = x86_64 ] || { echo "$BACKEND: Android slots are x86_64 only" >&2; exit 1; }
for v in SLOT_SECRET ANDROID_FDROID_URL ANDROID_FDROID_SHA256; do
  eval "val=\${$v:-}"
  case "$val" in ""|changeme|change-me) echo "$BACKEND needs $v: set it in .env" >&2; exit 1 ;; esac
done
