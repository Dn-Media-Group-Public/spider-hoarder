#!/usr/bin/env bash
# Dev stack only: a public client with direct grants and two throwaway users (alice, bob) for e2e_android.py.
# Run on the swarm host: ./e2e_android_setup.sh  (prints the env for e2e_android.py). Idempotent.
set -euo pipefail
KC=$(docker ps -q -f name=spider-hoarder_keycloak.1)
REALM=${KC_REALM:-newsroom}; PORT=${KC_DEV_PORT:-8085}
PW_ALICE=${KC_ALICE_PASSWORD:-$(openssl rand -hex 8)}; PW_BOB=${KC_BOB_PASSWORD:-$(openssl rand -hex 8)}
k() { docker exec "$KC" /opt/keycloak/bin/kcadm.sh "$@"; }
k config credentials --server http://localhost:$PORT --realm master --user admin --password admin >/dev/null
cid=$(k get clients -r "$REALM" -q clientId=e2e-android --fields id --format csv --noquotes || true)
[ -n "$cid" ] || k create clients -r "$REALM" -s clientId=e2e-android -s publicClient=true -s directAccessGrantsEnabled=true -s enabled=true
for u in alice bob; do
  [ -n "$(k get users -r "$REALM" -q username=$u --fields id --format csv --noquotes)" ] || \
    k create users -r "$REALM" -s username=$u -s enabled=true -s email=$u@example.org -s emailVerified=true -s firstName=$u -s lastName=E2E
done
k set-password -r "$REALM" --username alice --new-password "$PW_ALICE"
k set-password -r "$REALM" --username bob --new-password "$PW_BOB"
echo "export KC_ALICE_PASSWORD=$PW_ALICE KC_BOB_PASSWORD=$PW_BOB"
