# spider-hoarder

`mcp-research`: per-user, isolated, real (headed) browser sessions for Claude
over MCP, with a human handoff (live view) for CAPTCHAs/logins, optional Tor
egress, and fetch/parse/blob tools that return Markdown. No knowledge of any
downstream graph.

Design: [docs/implemented-plans/browser-sessions-plan.md](docs/implemented-plans/browser-sessions-plan.md)
(builds on [agentic-research-mcp.md](docs/plans/agentic-research-mcp.md); Keycloak/Caddy/Tor
background in [newsroom-browsers-plan.md](docs/plans/newsroom-browsers-plan.md)).

## Run

```bash
cp .env.example .env   # fill in PUBLIC_HOST, OIDC_ISSUER and the secrets
./deploy.sh            # prod: builds the images and deploys the stack (Docker Swarm)
./deploy.sh --dev      # local: same stack + a local Keycloak, http://localhost, no TLS
```

The stack is Caddy, oauth2-proxy, `research-tor`, `research-clear` and `tor`. It only speaks
**http**: TLS ends at the Google load balancer in front of Caddy (`PUBLIC_SCHEME=https` tells the
services which URLs users see; the LB must forward `X-Forwarded-Proto`; `/healthz` answers 200
for LB health checks). Auth is an external OIDC provider (`OIDC_ISSUER`, Keycloak).

- `$PUBLIC_SCHEME://$PUBLIC_HOST/tor/mcp` — browsers egress via Tor only
- `$PUBLIC_SCHEME://$PUBLIC_HOST/clear/mcp` — direct egress, public internet only

Tools: the browser tools of the Python tool server in `mcp-research/toolserver/` (a `@playwright/mcp`-compatible set plus cookie /
localStorage / sessionStorage tools; one Chromium per session, several browser windows via `browser_start_session` and a `browser` id
on every call, `main` being the shared default; see
[the plan](docs/implemented-plans/python-browser-tool-server.md)), plus `browser_request_human_help`,
`browser_save_page`, `browser_end_session`, `ingest_page`, `get_upload_link`,
`list_files`, `get_file_text`, `get_file_url`.

### Dev (`./deploy.sh --dev`)

Adds `docker-compose.dev.yml`: a throwaway Keycloak (`start-dev`, no TLS) plus a `keycloak-init`
job that runs `keycloak/setup.sh` against it, and `extra_hosts: localhost:host-gateway` so the
gateways and oauth2-proxy reach it at the same `localhost` URL as your browser. Forces
`PUBLIC_HOST=localhost`, `PUBLIC_SCHEME=http`, issuer `http://localhost:8085/realms/newsroom`
(`KC_DEV_PORT` to change the port). Ports 80 and 8085 must be free.

| | |
|---|---|
| MCP | `http://localhost/clear/mcp`, `http://localhost/tor/mcp` |
| Keycloak admin console | <http://localhost:8085> — `admin` / `admin` |
| **User that can use spider-hoarder** | **`researcher` / `researcher-pw`** |

(Keycloak marks its cookies `Secure`; browsers still accept them on `http://localhost`. Other
dev hostnames would not work without TLS.)

## Keycloak

`keycloak/setup.sh` initializes a realm (`KC_REALM`, default `newsroom`), idempotently:

- the group `ALLOWED_GROUP` (default `/investigations`) that gates MCP use and the live view;
- two **realm-default client scopes**, so every client, including the ones Claude registers
  dynamically, gets them: `mcp-audience` (`aud` = `MCP_AUDIENCE`) and `mcp-groups` (`groups`);
- the confidential client `O2P_CLIENT_ID` for the live-view login (oauth2-proxy);
- **Dynamic Client Registration (DCR)** for Claude: anonymous registration is allowed, and the
  realm's *Trusted Hosts* policy only accepts redirect URIs on `claude.ai`, `claude.com` and
  `localhost` (Claude Code). Anything else (e.g. `https://evil.example/cb`) gets a 403;
- **one user who can use spider-hoarder**: username `researcher`, password `researcher-pw`
  (`KC_USER` / `KC_USER_PASSWORD`), in the allowed group. The password is reset to
  `KC_USER_PASSWORD` on every run, so **change it in `.env` outside dev**.

`--dev` runs it for you. For a real Keycloak run it once (re-run after changing `.env`), from
anywhere that can reach the admin API, using the script from the Keycloak image:

```bash
set -a; source .env; set +a
docker run --rm -v $PWD/keycloak:/s -e KC_URL=https://keycloak.example.org \
  -e KC_ADMIN=admin -e KC_ADMIN_PASSWORD=... -e KC_REALM -e KC_USER -e KC_USER_PASSWORD \
  -e PUBLIC_HOST -e PUBLIC_SCHEME -e MCP_AUDIENCE -e ALLOWED_GROUP -e O2P_CLIENT_ID -e O2P_CLIENT_SECRET \
  --entrypoint bash quay.io/keycloak/keycloak:26.0 /s/setup.sh
```

## Connecting Claude

Claude registers itself via DCR, so there is no client id or secret to configure. Give it the MCP URL
of the egress you want, then sign in as the Keycloak user above (a consent screen appears once per client).

| | URL |
|---|---|
| browsers via Tor | `$PUBLIC_SCHEME://$PUBLIC_HOST/tor/mcp` |
| browsers direct | `$PUBLIC_SCHEME://$PUBLIC_HOST/clear/mcp` |

**claude.ai / Claude Desktop:** Settings → Connectors → *Add custom connector* → paste the URL →
*Connect* → sign in.

**Claude Code:**

```bash
claude mcp add --transport http spider-hoarder $PUBLIC_SCHEME://$PUBLIC_HOST/clear/mcp   # dev: http://localhost/clear/mcp
# then in Claude Code: /mcp -> spider-hoarder -> Authenticate (opens the Keycloak login)
```

What to tell Claude: nothing special, the server sends its own instructions. In short: it gets a
private browser per conversation (`browser_*` tools), `browser_request_human_help` gives a link
for the user to solve a CAPTCHA/login (they sign in with the same Keycloak account),
`browser_save_page` parses and stores the current page, `ingest_page` fetches a URL in one shot,
and `browser_end_session` frees the browser. Use `tor` for sensitive research, `clear` when a site
blocks Tor.

## Tests

```bash
cd mcp-research
python test_pool.py && python test_security.py && python test_gateway.py   # host
# needs root + the image's tools:
docker run --rm -v $PWD:/app -w /app --entrypoint python mcp-research:local test_sessiond.py
```
