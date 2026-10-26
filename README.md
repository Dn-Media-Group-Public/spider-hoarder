# spider-hoarder

Ever tried to **scrape a web site** using your LLM agent, only for it to get stuck on **CAPTCHAs**, **logins** or your model considering
**robots.txt** over your direct request to read a specific page? Or hitting docx or pdf files?

Spider-hoarder is an MCP server that hands every user and conversation a private, isolated,
real (headed) Chromium that Claude drives with a full set of browser tools: navigate, click, type, snapshot, find, cookies and
storage, screenshots, PDFs. It copes with JavaScript-heavy sites, consent walls and logins. When Claude hits a CAPTCHA or login
it cannot pass, it sends you a link to the *same* browser (live view) to solve it, then carries on. Egress is either direct or
anonymised through Tor.

Pages come back as Markdown and everything bulky is stored as a *job* Claude can page through, search
(`grep_file`) or link to, so large pages never flood its context; Claude can also fetch a URL in one shot (`ingest_page`) or have
you upload documents. Subagents can browse in parallel in their own windows while sharing logins. 

Documentation (architecture, API, design decisions): [docs/index.md](docs/index.md).

## Features

* Uses a real headed chromium browser - javascript rendered pages work fine
* Browser-in-browser view (uses vnc) whenever the agent gets stuck on a captcha
* Converts everything to markdown using [markitdown](https://github.com/microsoft/markitdown)
* Sessions live in isolated docker containers and all storage is cleared after use
* TOR support for anonymous browsing and accessing .onion sites
* Browser automation using [playwright](https://playwright.dev/)
* SSO integration (OAUth2)

## Quick start

```bash
cp .env.example .env   # fill in PUBLIC_HOST, OIDC_ISSUER and the secrets
./deploy.sh            # prod: builds the images and deploys the stack (Docker Swarm)
./deploy.sh --dev      # local: same stack + a local Keycloak, http://localhost:7297, no TLS
```

Two MCP endpoints appear:

- `$PUBLIC_SCHEME://$PUBLIC_HOST/tor/mcp` — browsers egress via Tor only
- `$PUBLIC_SCHEME://$PUBLIC_HOST/clear/mcp` — direct egress, public internet only

TLS must end in front of nginx (the stack only speaks http); auth is an external OIDC provider (Keycloak). More replicas:
`docker service scale spider-hoarder_research-clear=3`. Details: [deploying and configuring](docs/operations/deploy-and-configure.md),
[scaling](docs/architecture/scaling.md).

### Dev (`./deploy.sh --dev`)

Adds a throwaway Keycloak. Ports `HTTP_PORT` (7297) and 8085 must be free.

| | |
|---|---|
| MCP | `http://localhost:7297/clear/mcp`, `http://localhost:7297/tor/mcp` |
| Keycloak admin console | <http://localhost:8085> — `admin` / `admin` |
| **User that can use spider-hoarder** | **`researcher` / `researcher-pw`** |

## Keycloak setup

`keycloak/setup.sh` initializes a realm (`KC_REALM`, default `newsroom`) idempotently: a default client scope, Dynamic Client
Registration for Claude (redirects on `claude.ai`, `claude.com`, `localhost` only) and one user (`researcher`; its password is reset
to `KC_USER_PASSWORD` on every run, so **change it in `.env` outside dev**). `--dev` runs it for you. For a real Keycloak, run it
once (re-run after changing `.env`) using the script from the Keycloak image:

```bash
set -a; source .env; set +a
docker run --rm -v $PWD/keycloak:/s -e KC_URL=https://keycloak.example.org \
  -e KC_ADMIN=admin -e KC_ADMIN_PASSWORD=... -e KC_REALM -e KC_USER -e KC_USER_PASSWORD \
  -e PUBLIC_HOST -e PUBLIC_SCHEME -e MCP_SCOPE \
  --entrypoint bash quay.io/keycloak/keycloak:26.0 /s/setup.sh
```

## Using it

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
claude mcp add --transport http spider-hoarder $PUBLIC_SCHEME://$PUBLIC_HOST/clear/mcp   # dev: http://localhost:7297/clear/mcp
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
# needs root + the image's tools; run with the deployed capability set, plain root hides permission bugs:
docker run --rm --cap-drop ALL --cap-add NET_ADMIN --cap-add SETUID --cap-add SETGID --cap-add CHOWN --cap-add KILL \
  --cap-add DAC_OVERRIDE --cap-add FOWNER -v $PWD:/app -w /app --entrypoint python mcp-research:local test_sessiond.py
# end to end (stub Keycloak inside one container; restart it per run, the gateway caches the signing key):
docker run -d --name e2e --add-host nginx:172.17.0.1 <same caps> -e EGRESS=clear -e GATEWAY_PORT=8000 \
  -e PUBLIC_HOST=localhost:8000 -e PUBLIC_SCHEME=https -e OIDC_ISSUER=http://127.0.0.1:9999/realms/r \
  -e BLOB_TOKEN_SECRET=x mcp-research:local
docker exec -u 1000 -w /app e2e python e2e_auth.py
```
