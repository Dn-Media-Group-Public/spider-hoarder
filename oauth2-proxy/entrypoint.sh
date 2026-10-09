#!/bin/sh
# Registers (or reuses) a Keycloak client via anonymous DCR, then execs oauth2-proxy.
# Env: OIDC_ISSUER PUBLIC_HOST PUBLIC_SCHEME [MCP_SCOPE]. State: /state/registration.json
set -eu
S=/state/registration.json
REDIRECT="${PUBLIC_SCHEME}://${PUBLIC_HOST}/oauth2/callback"
# Same scopes as the MCP gateway advertises (so the consent screen matches); DCR without a scope gets every realm default
SCOPES="basic email${MCP_SCOPE:+ $MCP_SCOPE}"
field() { jq -r ".$1" "$S"; }

if [ -f "$S" ]; then
  uri=$(field registrationClientUri); tok=$(field registrationAccessToken)
  if [ "$(field redirectUri)" = "$REDIRECT" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $tok" "$uri" || true)
    [ "$code" = 200 ] || rm -f "$S"
  else
    curl -s -o /dev/null -X DELETE -H "Authorization: Bearer $tok" "$uri" || true   # best effort
    rm -f "$S"
  fi
fi

if [ ! -f "$S" ]; then
  body=$(jq -n --arg r "$REDIRECT" --arg s "$SCOPES" '{client_name:"spider-hoarder live view",redirect_uris:[$r],
    grant_types:["authorization_code"],token_endpoint_auth_method:"client_secret_basic",scope:$s}')
  resp=$(curl -sf -X POST -H 'Content-Type: application/json' -d "$body" \
    "$OIDC_ISSUER/clients-registrations/openid-connect") || { echo "DCR registration failed" >&2; exit 1; }
  echo "$resp" | jq --arg r "$REDIRECT" '{clientId:.client_id,secret:.client_secret,
    registrationAccessToken:.registration_access_token,registrationClientUri:.registration_client_uri,redirectUri:$r}' > "$S.tmp"
  [ "$(jq -r '.secret // empty' "$S.tmp")" ] || { echo "DCR response lacks a client secret" >&2; rm -f "$S.tmp"; exit 1; }
  mv "$S.tmp" "$S"
fi

export OAUTH2_PROXY_SCOPE="openid $SCOPES" OAUTH2_PROXY_CLIENT_ID=$(field clientId) OAUTH2_PROXY_CLIENT_SECRET=$(field secret)
exec /bin/oauth2-proxy "$@"
