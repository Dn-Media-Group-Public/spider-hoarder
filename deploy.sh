#!/usr/bin/env bash
set -e

STACK=spider-hoarder
while [[ $# -gt 0 ]]; do
  case $1 in
    --stack) STACK="$2" ; shift 2 ;;
    *) shift ;;
  esac
done

# Build local image (build: is ignored in stack deploy)
docker build -t mcp-research:local mcp-research

# Initialize swarm if not already active
docker info --format '{{.Swarm.LocalNodeState}}' | grep -q active || docker swarm init

# Export vars from .env so docker stack deploy can interpolate them
set -a
# shellcheck source=.env
source .env
set +a

if [ -z "${BLOB_TOKEN_SECRET:-}" ] || [ "${BLOB_TOKEN_SECRET}" = "changeme" ]; then
  echo "BLOB_TOKEN_SECRET is unset or still 'changeme' — set a real secret in .env" >&2
  echo "(generate one with: openssl rand -hex 32)" >&2
  exit 1
fi

docker stack deploy --resolve-image never -c docker-compose.yml "$STACK"

# Force restart so the freshly-built :local image is picked up.
# --no-resolve-image avoids swarm trying to re-pull a :local image from a
# registry that doesn't exist for it.
docker service update --force --no-resolve-image "${STACK}_mcp-research"
