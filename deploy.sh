#!/usr/bin/env bash
# Docker Swarm stack deploy (production runs on Swarm).
#   ./deploy.sh          prod: external OIDC IdP (OIDC_ISSUER), TLS ends at the load balancer
#   ./deploy.sh --dev    local: a Keycloak (docker-compose.keycloak.yml), http://localhost, no TLS
#   ./deploy.sh --dev-keycloak   prod-style (PUBLIC_HOST and OIDC_ISSUER from .env as they are) plus that local dev Keycloak:
#                        set OIDC_ISSUER=http://<PUBLIC_HOST's host>:${KC_DEV_PORT:-8085}/realms/${KC_REALM:-newsroom}
# Config comes from .env (or $ENV_FILE). What runs is BACKENDS (space-separated canonical names, e.g.
# "browser_clear browser_tor android_tor"); every backend is a pip package that registers a descriptor under the entry-point
# group spider_hoarder.backends. In-tree packages (sdk/, backends/*) are installed into .venv/ (made here with `virtualenv`);
# BACKEND_PACKAGES (pip requirement specs, e.g. git+ssh://...) adds out-of-tree backends. docs/architecture/backend-plugins.md.
# Backends removed from BACKENDS are pruned from the stack; their networks and volumes are kept (docker network/volume rm).
set -e
cd "$(dirname "$0")"

STACK=spider-hoarder
DEV=0
DEV_KC=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --stack) STACK="$2" ; shift 2 ;;
    --dev) DEV=1 ; DEV_KC=1 ; shift ;;
    --dev-keycloak) DEV_KC=1 ; shift ;;
    *) shift ;;
  esac
done

# Export vars from .env so docker stack deploy can interpolate them
set -a
# shellcheck source=.env
source "${ENV_FILE:-.env}"
set +a

# Multi-node (docs/architecture/scaling.md): IMAGE_PREFIX (a registry, with trailing slash) makes images pushed and deploys resolve them;
# unset = images stay :local on this node. IMAGE_TAG defaults to the git revision then. PIN_DATA: see `pin` below.
export IMAGE_PREFIX="${IMAGE_PREFIX:-}"
if [ -n "$IMAGE_PREFIX" ]; then
  export IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"
else
  export IMAGE_TAG="${IMAGE_TAG:-local}"
fi

export STACK_NAME=$STACK
REQUIRED=(PUBLIC_HOST OIDC_ISSUER O2P_COOKIE_SECRET BLOB_TOKEN_SECRET GATEWAY_PORT HTTP_PORT BACKENDS)
if [ "$DEV" -eq 1 ]; then
  echo "Deploying in DEV mode (local Keycloak, http://localhost, no TLS)"
  export PUBLIC_HOST=localhost:$HTTP_PORT PUBLIC_SCHEME=http
  export OIDC_ISSUER="http://localhost:${KC_DEV_PORT:-8085}/realms/${KC_REALM:-newsroom}"
fi
if [ "$DEV_KC" -eq 1 ]; then
  export KC_SETUP_HASH=$(md5sum keycloak/setup.sh | cut -c1-8)   # swarm configs are immutable: the hash is in the name
  # the issuer the tokens will carry: the origin of OIDC_ISSUER
  export KC_HOSTNAME="${OIDC_ISSUER%%/realms/*}"
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

# --- the virtualenv (well-known location): only reads the descriptors (entry points); the images build from source in docker build.
# In-tree: all packages (BACKENDS decides what is enabled). --no-deps: descriptors are stdlib-only.
[ -x .venv/bin/python ] || virtualenv -q .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q pyyaml
# shellcheck disable=SC2086
pip install -q --no-deps --force-reinstall ./sdk ./backends/*/ ${BACKEND_PACKAGES:-}

TOOL="python scripts/spider_hoarder.py"
$TOOL validate
$TOOL preflight

# Build local images (build: is ignored in stack deploy)
$TOOL build-image oauth2-proxy-dcr:local oauth2-proxy
$TOOL build

# Initialize swarm if not already active
docker info --format '{{.Swarm.LocalNodeState}}' | grep -q active || docker swarm init

$TOOL post-build

# Variables the backends derive (slot counts, ...), BACKENDS and BACKEND_SERVERS (what each gateway tells the model about the others)
eval "$($TOOL env)"
$TOOL landing .build/landing.html
# ALIASES="old=new ..." -> nginx map of URL prefixes (underscores or dashes accepted); always written, empty = no aliases
: > .build/aliases.map
for a in ${ALIASES:-}; do
  [[ "$a" =~ ^[A-Za-z0-9_-]+=[A-Za-z0-9_-]+$ ]] || { echo "bad ALIASES entry: $a (want old=new)" >&2; exit 1; }
  printf '%s %s;\n' "${a%%=*}" "${a#*=}" | tr _ - >> .build/aliases.map
done
# nginx image with the config baked in: not pinned to a node
rm -rf .build/nginx && mkdir -p .build/nginx
cp nginx/Dockerfile nginx.conf.template .build/aliases.map .build/landing.html .build/nginx/
$TOOL build-image spider-hoarder-nginx:local .build/nginx
FILES=(-c docker-compose.yml)
if [ "$DEV_KC" -eq 1 ]; then
  FILES+=(-c docker-compose.keycloak.yml)
fi
if [ "$DEV" -eq 1 ]; then
  $TOOL dev-overlay .build/docker-compose.dev-gateways.yml
  FILES+=(-c docker-compose.dev.yml -c .build/docker-compose.dev-gateways.yml)
fi
mapfile -t BACKEND_FILES < <($TOOL compose)
FILES+=("${BACKEND_FILES[@]}")
if [ -n "${PIN_DATA:-}" ]; then
  $TOOL pin .build/docker-compose.pin.yml
  FILES+=(-c .build/docker-compose.pin.yml)
fi

if [ -n "$IMAGE_PREFIX" ]; then
  docker stack deploy --prune "${FILES[@]}" "$STACK"   # tags are unique per revision: nothing to force
else
  docker stack deploy --prune --resolve-image never "${FILES[@]}" "$STACK"
  # Force restart so freshly-built :local images (and an edited nginx.conf.template or landing page) are picked up.
  for s in nginx $($TOOL services); do
    docker service update --force --no-resolve-image "${STACK}_$s"
  done
fi
