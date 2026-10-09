# Plan: oauth2-proxy registers itself via Dynamic Client Registration

## Problem / Goal

The live-view login (oauth2-proxy) uses a pre-provisioned confidential Keycloak client:
`O2P_CLIENT_ID` / `O2P_CLIENT_SECRET` live in `.env` and `keycloak/setup.sh` creates the client
from them. That is manual secret handling that has to stay in sync in two places. Claude already
registers through anonymous DCR; the live-view login should do the same, so deploying needs no
client ID/secret at all.

## Decisions (agreed)

1. **Wrapper entrypoint**, not a replacement for oauth2-proxy. A small shell script runs in the
   oauth2-proxy container: it registers a client through Keycloak's DCR endpoint, then `exec`s
   oauth2-proxy with `OAUTH2_PROXY_CLIENT_ID` / `OAUTH2_PROXY_CLIENT_SECRET` set from the result.
2. **Registration is persisted in a named volume** and reused across restarts. Registered once,
   not once per start, so no orphaned clients accumulate.
3. **Anonymous DCR**, as already allowed by the (external) prod Keycloak and by the dev one.
   No initial access token. Prod's registration policy is external and not this stack's concern.
   **Dev:** `setup.sh` already puts `localhost` in the anonymous Trusted Hosts policy (with
   `client-uris-must-match`), and `deploy.sh --dev` forces `PUBLIC_HOST=localhost:$HTTP_PORT`, so
   the redirect URI `http://localhost:$HTTP_PORT/oauth2/callback` is on a trusted host and dev
   registration works with no `setup.sh` change. Keep `localhost` in that list; the dev flow
   depends on it. The oauth2-proxy container reaches Keycloak at the same `localhost` issuer URL
   through the existing `extra_hosts: localhost:host-gateway`.
4. **Single replica** (as today). Two replicas would share the volume only on one node; revisit
   if oauth2-proxy is ever scaled.

## Design

### Wrapper script (`oauth2-proxy/entrypoint.sh`)

State file `/state/registration.json` (volume `o2p_state`) holds `clientId`, `secret`,
`registrationAccessToken`, `registrationClientUri` and the `redirectUri` it was registered with.

On start:

1. If the state file exists and its `redirectUri` equals the current
   `${PUBLIC_SCHEME}://${PUBLIC_HOST}/oauth2/callback`, validate it with
   `GET registrationClientUri` using the registration access token.
   - 200: reuse it.
   - Otherwise (404/401: realm or Keycloak wiped, client deleted): fall through to register.
2. If the state exists but `redirectUri` differs (host or scheme changed): best-effort `DELETE`
   of the old client with its registration token, then register.
3. Register: `POST ${OIDC_ISSUER}/clients-registrations/openid-connect` with
   `client_name`, `redirect_uris`, `grant_types: [authorization_code]`,
   `token_endpoint_auth_method: client_secret_basic`. Write the response fields to the state file.
4. `exec oauth2-proxy` with the ID and secret exported.

Fail loudly (non-zero exit, restart policy retries) if Keycloak is unreachable or registration
is rejected. Never run with a stale or empty client.

The stock oauth2-proxy image is distroless (no shell, no `jq`), so a tiny local image is built
by `deploy.sh` like `mcp-research` and `tor`: `oauth2-proxy/Dockerfile` is `FROM` the `-alpine`
variant of the same pinned version, plus `apk add jq` and the entrypoint script. JSON is read and
written with `jq` only (no `sed`/`grep` parsing).

### Keycloak side (`keycloak/setup.sh`)

- Remove the confidential client block.
- Drop `O2P_CLIENT_ID` / `O2P_CLIENT_SECRET` from the required env and the header comment.

### Things to verify during implementation

- The anonymous DCR "Allowed Client Scopes" policy must permit `openid email profile`. If not,
  extend the allowed scopes in `setup.sh`.
- Existing deployments: the old static client stays in Keycloak, unused. It can be deleted by hand.

## Implementation order

1. `keycloak/setup.sh`: drop the static client and `O2P_CLIENT_*`.
2. `oauth2-proxy/Dockerfile` + `oauth2-proxy/entrypoint.sh`: wrapper as above, built by `deploy.sh`.
3. `docker-compose.yml`: oauth2-proxy uses the local image, the `o2p_state` volume, `PUBLIC_HOST`/`PUBLIC_SCHEME`/`OIDC_ISSUER` for the script; remove
   `OAUTH2_PROXY_CLIENT_ID` / `_SECRET`.
4. `docker-compose.dev.yml`: remove `O2P_CLIENT_*` from `keycloak-init`; keep oauth2-proxy
   ordered after `keycloak-init`.
5. `deploy.sh`, `.env.example`: remove `O2P_CLIENT_ID` / `O2P_CLIENT_SECRET`.
6. `README.md`: update the Keycloak section and the manual `docker run` example.
7. Test in `--dev`: fresh start registers; restart reuses (no new client in the admin console);
   wipe Keycloak data, restart oauth2-proxy and it re-registers; change `PUBLIC_HOST` and the
   old client is replaced; live-view login works end to end.

## Files to change

| File | Change |
|---|---|
| `keycloak/setup.sh` | Remove static client and `O2P_CLIENT_*` |
| `oauth2-proxy/Dockerfile`, `oauth2-proxy/entrypoint.sh` | New: alpine variant + `jq`; register/reuse/validate, then exec oauth2-proxy |
| `docker-compose.yml` | Local image, `o2p_state` volume, env changes |
| `docker-compose.dev.yml` | Drop `O2P_CLIENT_*` from `keycloak-init` |
| `deploy.sh` | Build the image; drop `O2P_CLIENT_*` from `REQUIRED` |
| `.env.example` | Drop `O2P_CLIENT_*` |
| `README.md` | Keycloak section and `docker run` example |

## Docs to update

There are no `docs/architecture/` or `docs/api/` files yet; the README is the only affected doc
(Keycloak section). On implementation, add a "See also" link to this plan there, move this plan
to `docs/implemented-plans/oauth2-proxy-dcr.md` and link it from `docs/implemented-plans/index.md`.
