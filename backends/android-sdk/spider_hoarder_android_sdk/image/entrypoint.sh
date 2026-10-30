#!/bin/sh
# Android gateway: the sessions live in the slot containers (the backend's android-wrapper), so there is no uid pool, no
# sessiond and no firewall here: just the unprivileged gateway, restarted on exit. It needs SETUID/SETGID/SETPCAP for
# setpriv only; nothing remains once it runs.
set -eu
: "${GATEWAY_PORT:?}" "${BACKEND:?}"
while :; do
  setpriv --no-new-privs --reuid=1000 --regid=1000 --groups="${GW_COLLECT_GID:-1500}" --bounding-set=-all --inh-caps=-all \
    uvicorn spider_hoarder_sdk.gateway.app:app --host 0.0.0.0 --port "$GATEWAY_PORT" || true
  echo "gateway exited, restarting" >&2
  sleep 1
done
