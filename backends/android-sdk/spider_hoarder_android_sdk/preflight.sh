#!/bin/sh
# preflight of the Android backends (run by scripts/ on the deploy host, before anything is built): the slot image needs the F-Droid pin.
# The KVM/x86_64 checks are the wrapper's, on each KVM node (wrapper/reconcile.sh).
# Non-zero exit aborts the deploy with this script's stderr.
for v in SLOT_SECRET ANDROID_FDROID_URL ANDROID_FDROID_SHA256; do
  eval "val=\${$v:-}"
  case "$val" in ""|changeme|change-me) echo "$BACKEND needs $v: set it in .env" >&2; exit 1 ;; esac
done
