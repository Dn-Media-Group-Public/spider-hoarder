#!/usr/bin/env bash
# Initialize a Keycloak realm for spider-hoarder (idempotent: re-running is safe; it resets
# the initial user's password to KC_USER_PASSWORD). Claude and oauth2-proxy register themselves through
# Dynamic Client Registration, so no clients are created here, only the anonymous-DCR policy.
# Needs kcadm.sh (shipped in the Keycloak image, /opt/keycloak/bin). Env:
#   KC_URL KC_ADMIN KC_ADMIN_PASSWORD   admin access to Keycloak (master realm)
#   KC_REALM        realm to create/use (default: newsroom)
#   KC_USER KC_USER_PASSWORD [KC_USER_EMAIL]   the initial user (KC_USER default: researcher)
#   plus from .env: PUBLIC_HOST MCP_SCOPE
set -euo pipefail
export PATH="$PATH:/opt/keycloak/bin"
: "${KC_URL:?}" "${KC_ADMIN:?}" "${KC_ADMIN_PASSWORD:?}" "${KC_USER_PASSWORD:?}"
: "${PUBLIC_HOST:?}"
REALM=${KC_REALM:-newsroom}
USERNAME=${KC_USER:-researcher}
EMAIL=${KC_USER_EMAIL:-$USERNAME@example.org}
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

# Optional MCP_SCOPE: a realm-default scope that appears in the token's "scope" claim; the gateway
# requires it. (No audience mapper: the gateway does not check "aud".)
if [ -n "${MCP_SCOPE:-}" ] && [ -z "$(id_of client-scopes name "$MCP_SCOPE" || true)" ]; then
  $K create client-scopes -r "$REALM" -s name="$MCP_SCOPE" -s protocol=openid-connect \
     -s attributes.'"include.in.token.scope"'=true -s attributes.'"display.on.consent.screen"'=false
  $K update "default-default-client-scopes/$(id_of client-scopes name "$MCP_SCOPE")" -r "$REALM" -n
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

echo "Realm '$REALM' ready. Issuer: $KC_URL/realms/$REALM  user: $USERNAME"
