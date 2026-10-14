# Deploying, configuring and testing

The commands to get started are in the [README](../../README.md); this page holds the details.
See also: [Plan: Browser sessions](../implemented-plans/browser-sessions-plan.md) (Deployment),
[Plan: Scale research containers](../implemented-plans/research-replicas.md)

## Stack
`deploy.sh` builds `mcp-research:local`, `spider-hoarder-tor:local`, `oauth2-proxy-dcr:local` (Swarm ignores `build:`), inits Swarm, runs
`docker stack deploy`, then force-updates `nginx`, `research-*`, `tor` so fresh `:local` images and an edited `nginx.conf.template` are
picked up. Swarm constraints: no `security_opt` (entrypoint sets `no_new_privs`), no `shm_size` (Chromium uses
`--disable-dev-shm-usage`). One image: Debian bookworm + Python venv, Playwright's Chromium, Xvfb, x11vnc, noVNC, websockify, iptables.

## Configuration (`.env`, see `.env.example`)
| Variable | Meaning |
|---|---|
| `PUBLIC_HOST`, `PUBLIC_SCHEME` | URLs users see (TLS terminates at the LB) |
| `OIDC_ISSUER`, `MCP_SCOPE` | Keycloak realm URL; scope the token must carry (empty = no check) |
| `O2P_COOKIE_SECRET`, `BLOB_TOKEN_SECRET` | secrets (placeholders refused by `deploy.sh`) |
| `RESEARCH_BLOB_URL` | fsspec URL, `file:///data` or `gs://bucket/prefix` |
| `MAX_SESSIONS_TOTAL` (18, max 48), `MAX_SESSIONS_PER_USER` (3), `IDLE_TIMEOUT` (1800 s), `DISCONNECT_TIMEOUT_MINUTES` (5) | per-container limits |
| `HTTP_PORT`, `GATEWAY_PORT` | published nginx port; port gateways listen on (nothing else may use it) |
| `RESEARCH_TOR_REPLICAS`, `RESEARCH_CLEAR_REPLICAS`, `MAX_REPLICAS` | scaling ([Scaling](../architecture/scaling.md)) |
| `KC_*` | dev Keycloak and `keycloak/setup.sh` only |

## Dev mode
`./deploy.sh --dev` adds `docker-compose.dev.yml`: a throwaway Keycloak (`start-dev`), a `keycloak-init` job running
`keycloak/setup.sh`, and `extra_hosts: localhost:host-gateway` so containers reach it at the same `localhost` URL as the browser. Forces
`PUBLIC_HOST=localhost:$HTTP_PORT`, http, issuer `http://localhost:8085/realms/newsroom`. Keep `localhost` in the Trusted Hosts list:
dev DCR for oauth2-proxy depends on it.

## Keycloak
See [Authentication](../architecture/auth.md). For an external Keycloak run `keycloak/setup.sh` once from the Keycloak image (command in
the README). The user's password is reset to `KC_USER_PASSWORD` on every run: change it outside dev.

## Tests (`mcp-research/`)
Host: `test_pool.py`, `test_security.py`, `test_gateway.py`, `test_replicas.py`; `pytest test_toolserver.py` (needs Playwright + Chromium).
In-container with the deployed capability set (plain root hides permission bugs): `test_sessiond.py`; end to end: `e2e_auth.py` against a
stub Keycloak, `smoke_e2e.py` for real sessions. Exact commands are in the README.
