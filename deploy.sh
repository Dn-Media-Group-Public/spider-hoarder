#!/usr/bin/env bash
# Docker Swarm stack deploy (production runs on Swarm).
#   ./deploy.sh          prod: external OIDC IdP (OIDC_ISSUER), TLS ends at the load balancer
#   ./deploy.sh --dev    local: adds a Keycloak (docker-compose.dev.yml), http://localhost, no TLS
# Config comes from .env (or $ENV_FILE).
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

FILES=(-c docker-compose.yml)
REQUIRED=(PUBLIC_HOST OIDC_ISSUER MCP_AUDIENCE O2P_CLIENT_ID O2P_CLIENT_SECRET O2P_COOKIE_SECRET BLOB_TOKEN_SECRET GATEWAY_PORT HTTP_PORT)
if [ "$DEV" -eq 1 ]; then
  echo "Deploying in DEV mode (local Keycloak, http://localhost, no TLS)"
  export PUBLIC_HOST=localhost:$HTTP_PORT PUBLIC_SCHEME=http
  export OIDC_ISSUER="http://localhost:${KC_DEV_PORT:-8085}/realms/${KC_REALM:-newsroom}"
  FILES+=(-c docker-compose.dev.yml)
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

# Build local images (build: is ignored in stack deploy)
docker build -t mcp-research:local mcp-research
docker build -t spider-hoarder-tor:local tor

# Initialize swarm if not already active
docker info --format '{{.Swarm.LocalNodeState}}' | grep -q active || docker swarm init

docker stack deploy --resolve-image never "${FILES[@]}" "$STACK"

# Force restart so freshly-built :local images (and an edited nginx.conf.template) are picked up.
for s in nginx research-tor research-clear tor; do
  docker service update --force --no-resolve-image "${STACK}_$s"
done
