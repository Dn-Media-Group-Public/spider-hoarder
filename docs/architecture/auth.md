# Authentication and live view

## MCP (Claude)
Keycloak (any OIDC) JWT on `/<backend>/mcp` (e.g. `/browser-tor/mcp`) (`spider_hoarder_sdk/gateway/auth.py`): issuer, signature, `exp`; the `email` claim identifies the owner; if
`MCP_SCOPE` is set the token's `scope` must contain it (the audience is deliberately **not** checked: Claude's dynamically
registered clients carry arbitrary audiences). RFC 9728 protected-resource metadata advertises the scope so Claude requests it.
Claude registers itself through anonymous Dynamic Client Registration, so there is no client id/secret to configure.
`keycloak/setup.sh` (idempotent) creates: the realm if missing, the realm-default scope, DCR limited by the Trusted Hosts policy to `claude.ai`,
`claude.com` and `localhost`, and one user. See also: [Plan: Browser sessions](../implemented-plans/browser-sessions-plan.md)
(implementation notes) and the `mcp-development:mcp-fastapi-oauth-dcr` pattern it follows.

## Live view (human handoff)
`browser_request_human_help` returns `…/<egress>/view/<rid>/<view_id>/start`. The human opens it, signs in via oauth2-proxy (same
Keycloak account), and nginx runs two `auth_request` stages: SSO (sets the email, client-supplied values dropped), then the replica's
`/authz`, which checks that the SSO email owns the session and answers `X-Upstream` (its own IP and websockify port; client-supplied
`X-Upstream` is never read). `/start` redirects to noVNC with the session's VNC password. Non-owners get 403.
See also: [Plan: Scale research containers](../implemented-plans/research-replicas.md) (two-stage nginx, `rid` in the URL)

## oauth2-proxy registers itself (DCR)
A wrapper entrypoint (`oauth2-proxy/entrypoint.sh`, alpine variant of the image + `jq` + GNU `wget`; curl would ignore the dev `/etc/hosts` rewrite for `localhost`) registers a client at
`$OIDC_ISSUER/clients-registrations/openid-connect`, persists it in the `o2p_state` volume, reuses it across restarts (validated with the
registration access token), replaces it when `PUBLIC_HOST`/scheme change, and fails loudly if registration is rejected. Single replica.
See also: [Plan: oauth2-proxy DCR](../implemented-plans/oauth2-proxy-dcr.md)

## TLS
TLS ends at the Google load balancer; the stack only speaks http. `PUBLIC_SCHEME` tells services which URLs users see; the LB must
forward `X-Forwarded-Proto`; `/healthz` answers 200.

See also: [Plan: Pluggable backends](../implemented-plans/backend-plugins.md) (the same auth framework serves every backend)
