#!/bin/sh
# Dev only (docker-compose.dev.yml entrypoint wrapper): make `localhost` resolve solely to the
# extra_hosts host-gateway entry, so the issuer http://localhost:8085 reaches the host's Keycloak.
# extra_hosts only appends; musl (Alpine) takes the first match, which would be the loopback.
# Needs root (/etc/hosts is a bind mount: rewritten in place, not replaced). Then execs "$@".
set -eu
hosts=$(awk '$1 == "127.0.0.1" || $1 == "::1" {
  o = $1; for (i = 2; i <= NF; i++) if ($i != "localhost") o = o "\t" $i
  if (o != $1) print o; next
} { print }' /etc/hosts)
printf '%s\n' "$hosts" > /etc/hosts
exec "$@"
