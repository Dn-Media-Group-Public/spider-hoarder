#!/usr/bin/env bash
# Docker Swarm stack deploy (production runs on Swarm).
#   ./deploy.sh          prod: external OIDC IdP (OIDC_ISSUER), TLS ends at the load balancer
#   ./deploy.sh --dev    local: adds a Keycloak (docker-compose.dev.yml), http://localhost, no TLS
# Config comes from .env (or $ENV_FILE).
# Android emulators are opt-in (ANDROID_CLEAR_REPLICAS / ANDROID_TOR_REPLICAS > 0 in .env) and need this host to
# have /dev/kvm: it builds the slot image and makes the golden snapshot here (docs/implemented-plans/android-hoarder.md).
set -e

STACK=spider-hoarder
DEV=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --stack) STACK="$2" ; shift 2 ;;
    --dev) DEV=1 ; shift ;;
    *) shift ;;
  esac
done

# Export vars from .env so docker stack deploy can interpolate them
set -a
# shellcheck source=.env
source "${ENV_FILE:-.env}"
set +a

export STACK_NAME=$STACK
FILES=(-c docker-compose.yml)
REQUIRED=(PUBLIC_HOST OIDC_ISSUER O2P_COOKIE_SECRET BLOB_TOKEN_SECRET GATEWAY_PORT HTTP_PORT)
if [ "$DEV" -eq 1 ]; then
  echo "Deploying in DEV mode (local Keycloak, http://localhost, no TLS)"
  export PUBLIC_HOST=localhost:$HTTP_PORT PUBLIC_SCHEME=http
  export OIDC_ISSUER="http://localhost:${KC_DEV_PORT:-8085}/realms/${KC_REALM:-newsroom}"
  FILES+=(-c docker-compose.dev.yml)
  REQUIRED+=(KC_USER_PASSWORD)
fi
export PUBLIC_SCHEME="${PUBLIC_SCHEME:-https}"
export O2P_COOKIE_SECURE=$([ "$PUBLIC_SCHEME" = https ] && echo true || echo false)

# --- Android (opt in)
ANDROID_CLEAR_REPLICAS=${ANDROID_CLEAR_REPLICAS:-0}
ANDROID_TOR_REPLICAS=${ANDROID_TOR_REPLICAS:-0}
ANDROID_SLOTS_PER_REPLICA=${ANDROID_SLOTS_PER_REPLICA:-2}
ANDROID_API=${ANDROID_API:-34}
ANDROID=0
if [ "$ANDROID_CLEAR_REPLICAS" -gt 0 ] || [ "$ANDROID_TOR_REPLICAS" -gt 0 ]; then
  ANDROID=1
  REQUIRED+=(SLOT_SECRET ANDROID_FDROID_URL ANDROID_FDROID_SHA256)
fi
export ANDROID_CLEAR_REPLICAS ANDROID_TOR_REPLICAS ANDROID_SLOTS_PER_REPLICA ANDROID_API
export ANDROID_CLEAR_SLOTS=$((ANDROID_CLEAR_REPLICAS * ANDROID_SLOTS_PER_REPLICA))
export ANDROID_TOR_SLOTS=$((ANDROID_TOR_REPLICAS * ANDROID_SLOTS_PER_REPLICA))
export ANDROID_WRAPPER_REPLICAS=$ANDROID

for v in "${REQUIRED[@]}"; do
  if [ -z "${!v:-}" ] || [ "${!v}" = "changeme" ] || [ "${!v}" = "change-me" ]; then
    echo "$v is unset or still a placeholder — set it in .env" >&2
    exit 1
  fi
done

# Build local images (build: is ignored in stack deploy)
docker build -t mcp-research:local mcp-research
docker build -t spider-hoarder-tor:local tor
docker build -t oauth2-proxy-dcr:local oauth2-proxy
docker build -t android-wrapper:local android-wrapper

# Initialize swarm if not already active
docker info --format '{{.Swarm.LocalNodeState}}' | grep -q active || docker swarm init

if [ "$ANDROID" -eq 1 ]; then
  [ -e /dev/kvm ] || { echo "Android needs /dev/kvm on this host (KVM-capable x86_64, nested virtualisation on a VM)" >&2; exit 1; }
  [ "$(uname -m)" = x86_64 ] || { echo "Android slots are x86_64 only" >&2; exit 1; }
  # build context = just what the image copies: never the repo root (.env, .git)
  CTX=$(mktemp -d)
  trap 'rm -rf "$CTX"' EXIT
  cp -r android-slot android-toolserver "$CTX"/
  rm -f "$CTX"/android-toolserver/test_*.py "$CTX"/android-toolserver/export_tools.py "$CTX"/android-slot/test_*.py
  docker build -f "$CTX/android-slot/Dockerfile" -t android-slot:local --build-arg "ANDROID_API=$ANDROID_API" \
    --build-arg "FDROID_URL=$ANDROID_FDROID_URL" --build-arg "FDROID_SHA256=$ANDROID_FDROID_SHA256" \
    ${ANDROID_CMDLINE_TOOLS_SHA256:+--build-arg "CMDLINE_TOOLS_SHA256=$ANDROID_CMDLINE_TOOLS_SHA256"} "$CTX"

  # Golden AVD + snapshot `clean`, made here because quickboot snapshots depend on the host CPU and docker build has no
  # /dev/kvm. Skipped when the volume's stamp is this image. A new one must start from an empty volume, and the slots
  # (overlay mounts on top of it) must be gone first: the wrapper removes them when it stops.
  SNAP="${STACK}_android_snapshot"
  IMAGE_ID=$(docker image inspect -f '{{.Id}}' android-slot:local)${ANDROID_RAM_MB:+ ram=$ANDROID_RAM_MB}   # the stamp: image + what is baked into the snapshot
  STAMP=$(docker run --rm -v "$SNAP:/avd" --entrypoint cat android-slot:local /avd/.stamp 2>/dev/null || true)
  if [ "$STAMP" != "$IMAGE_ID" ]; then
    echo "Making the Android snapshot (a few minutes)..."
    if docker service inspect "${STACK}_android-wrapper" >/dev/null 2>&1; then
      docker service scale "${STACK}_android-wrapper=0" >/dev/null
    fi
    while [ -n "$(docker ps -q --filter label=spider-hoarder.android-slot)" ]; do sleep 2; done
    docker volume rm -f "$SNAP" >/dev/null
    docker volume create "$SNAP" >/dev/null
    docker run --rm --ulimit nofile=65536:65536 --device /dev/kvm --group-add "$(stat -c %g /dev/kvm)" \
      --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add KILL --cap-add DAC_OVERRIDE --cap-add FOWNER \
      -v "$SNAP:/avd" -e "IMAGE_ID=$IMAGE_ID" -e "ANDROID_API=$ANDROID_API" ${ANDROID_RAM_MB:+-e "ANDROID_RAM_MB=$ANDROID_RAM_MB"} \
      --entrypoint /app/init-snapshot.sh android-slot:local
  fi
fi

docker stack deploy --resolve-image never "${FILES[@]}" "$STACK"

# Force restart so freshly-built :local images (and an edited nginx.conf.template) are picked up.
for s in nginx research-tor research-clear research-android-clear research-android-tor android-wrapper tor; do
  docker service update --force --no-resolve-image "${STACK}_$s"
done
