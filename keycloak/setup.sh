#!/usr/bin/env bash
# Initialize a Keycloak realm for spider-hoarder (idempotent: re-running is safe; it resets
# the initial user's password to KC_USER_PASSWORD). Claude registers itself through Dynamic
# Client Registration, so no Claude client is created here, only the anonymous-DCR policy.
# Needs kcadm.sh (shipped in the Keycloak image, /opt/keycloak/bin). Env:
#   KC_URL KC_ADMIN KC_ADMIN_PASSWORD   admin access to Keycloak (master realm)
#   KC_REALM        realm to create/use (default: newsroom)
#   KC_USER KC_USER_PASSWORD [KC_USER_EMAIL]   the initial user (KC_USER default: researcher)
#   plus from .env: PUBLIC_HOST MCP_SCOPE ALLOWED_GROUP O2P_CLIENT_ID O2P_CLIENT_SECRET
set -euo pipefail
export PATH="$PATH:/opt/keycloak/bin"
: "${KC_URL:?}" "${KC_ADMIN:?}" "${KC_ADMIN_PASSWORD:?}" "${KC_USER_PASSWORD:?}"
: "${PUBLIC_HOST:?}" "${O2P_CLIENT_ID:?}" "${O2P_CLIENT_SECRET:?}"
REALM=${KC_REALM:-newsroom}
USERNAME=${KC_USER:-researcher}
EMAIL=${KC_USER_EMAIL:-$USERNAME@example.org}
GROUP=${ALLOWED_GROUP:-/investigations}; GROUP=${GROUP#/}
K="kcadm.sh"
# id_of <collection> <field> <value>: exact match (kcadm -q is ignored by some collections)
id_of() {
  local id val
  while IFS=, read -r id val; do [ "$val" = "$3" ] && { echo "$id"; return; }; done \
    < <($K get "$1" -r "$REALM" --fields "id,$2" --format csv --noquotes)
  return 0
}

$K config credentials --server "$KC_URL" --realm master --user "$KC_ADMIN" --password "$KC_ADMIN_PASSWORD"
$K get "realms/$REALM" >/dev/null 2>&1 || $K create realms -s realm="$REALM" -s enabled=true
# TLS ends at the load balancer; without TLS in dev Keycloak must not mark its cookies Secure
if [ "${PUBLIC_SCHEME:-https}" = http ]; then SSL=NONE; else SSL=EXTERNAL; fi
$K update "realms/$REALM" -s sslRequired=$SSL

# Client scopes added to every token. Created *before* the clients and made realm defaults, so
# every client (incl. ones Claude registers dynamically) gets them.
mkscope() { # name mapper-type claim-config...
  local name=$1 type=$2; shift 2
  local sid; sid=$(id_of client-scopes name "$name" || true)
  if [ -z "$sid" ]; then
    $K create client-scopes -r "$REALM" -s name="$name" -s protocol=openid-connect \
       -s attributes.'"include.in.token.scope"'=false -s attributes.'"display.on.consent.screen"'=false
    sid=$(id_of client-scopes name "$name")
    $K create "client-scopes/$sid/protocol-mappers/models" -r "$REALM" -s name="$name" \
       -s protocol=openid-connect -s protocolMapper="$type" "$@"
    $K update "default-default-client-scopes/$sid" -r "$REALM" -n
  fi
}
mkscope mcp-groups oidc-group-membership-mapper \
  -s 'config."claim.name"=groups' -s 'config."full.path"=true' -s 'config."access.token.claim"=true' -s 'config."id.token.claim"=true'

# Optional MCP_SCOPE: a realm-default scope that appears in the token's "scope" claim; the gateway
# requires it. (No audience mapper: the gateway does not check "aud".)
if [ -n "${MCP_SCOPE:-}" ] && [ -z "$(id_of client-scopes name "$MCP_SCOPE" || true)" ]; then
  $K create client-scopes -r "$REALM" -s name="$MCP_SCOPE" -s protocol=openid-connect \
     -s attributes.'"include.in.token.scope"'=true -s attributes.'"display.on.consent.screen"'=false
  $K update "default-default-client-scopes/$(id_of client-scopes name "$MCP_SCOPE")" -r "$REALM" -n
fi

# Group that gates MCP use and the live view
[ -n "$(id_of groups name "$GROUP" || true)" ] || $K create groups -r "$REALM" -s name="$GROUP"

# Human live-view login (oauth2-proxy): confidential client
if [ -z "$(id_of clients clientId "$O2P_CLIENT_ID" || true)" ]; then
  $K create clients -r "$REALM" -s clientId="$O2P_CLIENT_ID" -s secret="$O2P_CLIENT_SECRET" \
     -s publicClient=false -s standardFlowEnabled=true -s directAccessGrantsEnabled=false \
     -s "redirectUris=[\"${PUBLIC_SCHEME:-https}://$PUBLIC_HOST/oauth2/callback\"]"
fi

# Dynamic Client Registration for Claude (claude.ai/claude.com and Claude Code on localhost).
# Anonymous DCR is governed by the realm's "Trusted Hosts" policy. Claude's registration
# request comes from unknown servers, so the sender host is not checked; what is enforced is
# that every redirect URI of a registered client belongs to a trusted host.
policy=$($K get components -r "$REALM" --fields id,providerId,subType --format csv --noquotes \
  | while IFS=, read -r id prov sub; do [ "$prov" = trusted-hosts ] && [ "$sub" = anonymous ] && { echo "$id"; break; }; done)
$K update "components/$policy" -r "$REALM" \
  -s 'config."trusted-hosts"=["claude.ai","claude.com","localhost"]' \
  -s 'config."host-sending-registration-request-must-match"=["false"]' \
  -s 'config."client-uris-must-match"=["true"]'

# The initial user
if [ -z "$(id_of users username "$USERNAME" || true)" ]; then
  $K create users -r "$REALM" -s username="$USERNAME" -s enabled=true -s email="$EMAIL" \
     -s emailVerified=true -s firstName=Initial -s lastName=User
fi
$K set-password -r "$REALM" --username "$USERNAME" --new-password "$KC_USER_PASSWORD"
UID_=$(id_of users username "$USERNAME"); GID_=$(id_of groups name "$GROUP")
$K update "users/$UID_/groups/$GID_" -r "$REALM" -s realm="$REALM" -s userId="$UID_" -s groupId="$GID_" -n

echo "Realm '$REALM' ready. Issuer: $KC_URL/realms/$REALM  user: $USERNAME"
