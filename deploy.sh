#!/usr/bin/env bash
# Docker Swarm stack deploy (production runs on Swarm).
#   ./deploy.sh          prod: external OIDC IdP (OIDC_ISSUER), TLS ends at the load balancer
#   ./deploy.sh --dev    local: adds a Keycloak (docker-compose.dev.yml), http://localhost, no TLS
# Config comes from .env (or $ENV_FILE). What runs is BACKENDS (space-separated canonical names, e.g.
# "browser_clear browser_tor android_tor"); every backend is a pip package that registers a descriptor under the entry-point
# group spider_hoarder.backends. In-tree packages (sdk/, backends/*) are installed into .venv/ (made here with `virtualenv`);
# BACKEND_PACKAGES (pip requirement specs, e.g. git+ssh://...) adds out-of-tree backends. docs/architecture/backend-plugins.md.
# Backends removed from BACKENDS are pruned from the stack; their networks and volumes are kept (docker network/volume rm).
set -e
cd "$(dirname "$0")"

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
REQUIRED=(PUBLIC_HOST OIDC_ISSUER O2P_COOKIE_SECRET BLOB_TOKEN_SECRET GATEWAY_PORT HTTP_PORT BACKENDS)
if [ "$DEV" -eq 1 ]; then
  echo "Deploying in DEV mode (local Keycloak, http://localhost, no TLS)"
  export PUBLIC_HOST=localhost:$HTTP_PORT PUBLIC_SCHEME=http
  export OIDC_ISSUER="http://localhost:${KC_DEV_PORT:-8085}/realms/${KC_REALM:-newsroom}"
  export KC_SETUP_HASH=$(md5sum keycloak/setup.sh | cut -c1-8)
  REQUIRED+=(KC_USER_PASSWORD)
fi
export PUBLIC_SCHEME="${PUBLIC_SCHEME:-https}"
export O2P_COOKIE_SECURE=$([ "$PUBLIC_SCHEME" = https ] && echo true || echo false)

for v in "${REQUIRED[@]}"; do
  if [ -z "${!v:-}" ] || [ "${!v}" = "changeme" ] || [ "${!v}" = "change-me" ]; then
    echo "$v is unset or still a placeholder — set it in .env" >&2
    exit 1
  fi
done

# --- the virtualenv (well-known location) and the packages: wheels are built once, installed here and copied into the
# images by the Dockerfiles (`COPY --from=wheels`). In-tree: all of them (BACKENDS decides what is enabled).
[ -x .venv/bin/python ] || virtualenv -q .venv
# shellcheck disable=SC1091
source .venv/bin/activate
rm -rf .build/wheels && mkdir -p .build/wheels
SPECS=(./sdk ./backends/*/)
# shellcheck disable=SC2206
SPECS+=(${BACKEND_PACKAGES:-})
for spec in "${SPECS[@]}"; do
  pip wheel -q --no-deps -w .build/wheels "$spec"
done
pip install -q pyyaml
pip install -q --no-deps --force-reinstall .build/wheels/*.whl

TOOL="python scripts/spider_hoarder.py"
$TOOL validate
$TOOL preflight

# Build local images (build: is ignored in stack deploy)
docker build -t oauth2-proxy-dcr:local oauth2-proxy
$TOOL build "$PWD/.build/wheels"

# Initialize swarm if not already active
docker info --format '{{.Swarm.LocalNodeState}}' | grep -q active || docker swarm init

$TOOL post-build

# Variables the backends derive (slot counts, ...), BACKENDS and BACKEND_SERVERS (what each gateway tells the model about the others)
eval "$($TOOL env)"
$TOOL landing .build/landing.html
FILES=(-c docker-compose.yml)
if [ "$DEV" -eq 1 ]; then
  $TOOL dev-overlay .build/docker-compose.dev-gateways.yml
  FILES+=(-c docker-compose.dev.yml -c .build/docker-compose.dev-gateways.yml)
fi
mapfile -t BACKEND_FILES < <($TOOL compose)
FILES+=("${BACKEND_FILES[@]}")

docker stack deploy --prune --resolve-image never "${FILES[@]}" "$STACK"

# Force restart so freshly-built :local images (and an edited nginx.conf.template or landing page) are picked up.
for s in nginx $($TOOL services); do
  docker service update --force --no-resolve-image "${STACK}_$s"
done
