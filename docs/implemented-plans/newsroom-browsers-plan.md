# Newsroom browser automation: implementation plan

Status: superseded by [Browser sessions](browser-sessions-plan.md); kept for its Keycloak/Tor background.

This document is a complete handoff for implementing and deploying a self-hosted,
per-session browser automation service that Claude uses through MCP. It contains
the requirements, the design, what has already been verified, the open work, and
the full contents of every file. Recreate the files exactly as listed under
"Files", then work through "Next steps".

## Context and requirements

A news organization doing investigative journalism needs Claude to browse
JavaScript-heavy sites (SPAs) in a real browser, in a multi-step loop, with a
human able to step in for CAPTCHAs and logins. Requirements:

1. **Self-hosted in our cloud.** Nothing installed on journalists' machines.
2. **Per-user, per-session browsers.** Journalists must never share a browser.
   The MCP server launches a browser per session.
3. **Human handoff.** When the model believes a CAPTCHA, bot check or login is
   shown, the MCP server generates a link. The user clicks it and sees *their own*
   session's browser in a new tab to solve it, then the model continues.
4. **Optional Tor egress.** Some sessions browse only via Tor.
5. **Everything OAuth2-protected** against our existing Keycloak. We already run
   self-built MCP servers on Claude with OAuth2 against Keycloak, so connector-side
   OAuth is a solved problem.

## Architecture

```
Claude ──Bearer JWT──▶ Caddy ──▶ gateway-tor / gateway-clear ──docker API──▶ docker-proxy
                         │            │  verifies JWT, one browser per MCP session,
                         │            │  proxies Playwright MCP tools + adds handoff tools
                         │            ▼
                         │       nb-tor-xxxx  nb-tor-yyyy  nb-clear-zzzz   (per-session containers:
Journalist ─SSO─▶ Caddy ─┘            │                         │          Xvfb + Chromium + noVNC
  /tor/view/<id>/…   oauth2-proxy     ▼                         ▼          + Playwright MCP)
  (owner only)       + gateway authz  tor ──▶ internet          internet
```

Components:

- **Caddy**: TLS and routing for one public hostname.
- **gateway-tor / gateway-clear** (`gateway/app.py`, same image, `EGRESS` env differs):
  an MCP server (Python SDK v1, FastMCP + overridden low-level handlers) that
  - verifies Keycloak bearer JWTs (issuer, `aud`, `email`, optional group) and serves
    RFC 9728 protected-resource metadata + 401 via the SDK's auth support;
  - creates one browser container per MCP session (lazily, on first tool call)
    through a restricted Docker API (docker-socket-proxy);
  - proxies Playwright MCP's tools to that container (tool list fetched once from a
    throwaway container and cached; `browser_install` hidden);
  - adds `browser_request_human_help(reason)` and `browser_end_session()`;
  - exposes `/authz` (Caddy forward_auth) and `/<egress>/view/<id>/start` for the
    live view, both checking that the SSO user owns the session;
  - reaps idle sessions (`IDLE_TIMEOUT`, default 30 min), enforces per-user and total
    limits, and removes orphaned containers on startup.
- **oauth2-proxy**: Keycloak SSO for humans opening the live view; returns the
  verified email in `X-Auth-Request-Email`.
- **Browser image** (`browser/`): Xvfb + headed Chromium + x11vnc (loopback,
  per-session password) + websockify/noVNC + `@playwright/mcp` over streamable HTTP.
  Profiles are ephemeral. Outputs go to a per-session evidence dir on the host.
- **tor**: Tor daemon exposing SOCKS on an internal network.
- **docker-proxy**: tecnativa/docker-socket-proxy limited to containers + networks.

### Request flows

**MCP (Claude → browser):** Claude calls `https://HOST/<egress>/mcp` with a Keycloak
bearer token. The gateway verifies it, maps the MCP session to its browser
container (creating it if needed), checks the caller's email matches the session
owner, and forwards the tool call to `http://nb-<egress>-<id>:8931/mcp` with
`Host: mcp-upstream`.

**Handoff (user → live view):**
1. The model calls `browser_request_human_help`. The gateway returns
   `https://HOST/<egress>/view/<id>/start` (no secrets in the link).
2. The user opens it. Caddy's `sso` snippet strips any client-supplied
   `X-Auth-Request-Email`, then forward_auths to oauth2-proxy (redirecting to
   Keycloak login if needed) and copies the verified email.
3. `/start` is proxied to the gateway, which checks ownership and 302-redirects to
   `/<egress>/view/<id>/vnc.html?path=...&autoconnect=1&password=<vnc password>`.
4. Every live-view request (page assets + websocket) goes through SSO again, then
   forward_auth to the gateway's `/authz`, which returns `X-Upstream: <container>:6080`
   only for the owner. Caddy strips the `/<egress>/view/<id>` prefix and proxies
   to that upstream. Client-supplied `X-Upstream` headers are stripped first.

### Network layout

| Network | Internal | Members |
|---|---|---|
| `public` | no | caddy, oauth2-proxy, gateways (Keycloak JWKS) |
| `control` | yes | gateways, docker-proxy |
| `nb_sessions_tor` | yes | caddy, gateway-tor, tor, tor session containers |
| `tor_egress` | no | tor |
| `nb_sessions_clear` | yes | caddy, gateway-clear, clear session containers |
| `nb_egress_clear` | no | clear session containers |

Tor session containers have no route to the internet. Their only egress is
`socks5://tor:9050`. Chromium is also launched with
`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`.

### Security decisions and the reasons for them

- **Playwright MCP Host check:** the browser config allows only `Host: mcp-upstream`, a
  name that resolves nowhere. Pages inside the browser therefore cannot drive
  their own MCP server via `localhost` (verified: `Host: localhost` → 403).
- **VNC password per session:** stops pages inside the browser from connecting to
  their own websockify/x11vnc. It's only revealed by `/start` after SSO + ownership.
- **Ownership by `email` claim:** set at session creation and re-checked on every
  MCP call and every live-view request.
- **Chromium runs with `--no-sandbox`** (normal in containers), so the container is
  the isolation boundary. gVisor (`runsc`) is recommended for hostile targets.
- **Gateway can create containers.** Even through the socket proxy this is close
  to root on the host. Run on a dedicated VM.

## What has been verified

- `@playwright/mcp` 0.0.83: CLI flags, config schema (`browser.browserName`,
  `launchOptions`, `server.allowedHosts`, `saveSession`, `outputDir`), streamable
  HTTP at `/mcp`, Host check behavior, and that
  `npx --no-install playwright install --with-deps chromium` run inside the
  globally installed package directory resolves the matching Chromium build.
- oauth2-proxy v7.15.5: all config keys used exist.
- Caddy 2.11.7: the Caddyfile adapts. Auth chain tested against mock backends:
  - unauthenticated users are redirected to login;
  - the owner reaches their container (page and websocket paths, query strings preserved);
  - other users, other session ids and the wrong egress get 403;
  - spoofed `X-Auth-Request-Email` and `X-Upstream` are ignored;
  - `/authz` is not publicly reachable;
  - unknown paths return 404.
- Gateway against a real Playwright MCP upstream (Docker mocked out):
  - 401 with `resource_metadata` and correct metadata JSON;
  - 27 tools listed incl. handoff tools, `browser_install` hidden;
  - tool calls proxied;
  - separate sessions per user;
  - non-owner gets 403 on `/start` and `/authz`.

**Not yet verified:** building the images and running the full stack end to end
(no Docker was available where this was written).

## Next steps

Superseded by [Browser sessions](browser-sessions-plan.md). Steps 1-5 (recreate files, build, integration test, Tor
verification, Keycloak/connector setup) were about this original design and are obsolete. The rest were split into
open plans:

- gVisor runtime: [plans/gvisor-runtime.md](../plans/gvisor-runtime.md)
- Prometheus metrics / structured logs: [plans/metrics-and-logging.md](../plans/metrics-and-logging.md)
- `mcp` 2.x SDK: [plans/mcp-sdk-2-migration.md](../plans/mcp-sdk-2-migration.md)
- Persistent profile, Playwright traces: [plans/optional-session-features.md](../plans/optional-session-features.md)
- Kubernetes variant: [plans/kubernetes-variant.md](../plans/kubernetes-variant.md)

### Test harness used for the gateway (reference)

This harness ran the gateway with Docker and Keycloak mocked, pointing sessions
at a local Playwright MCP started with `browser/config.json`:

```python
import os, sys
os.environ.update(EGRESS="tor", PUBLIC_HOST="browsers.example.org",
  OIDC_ISSUER="https://kc.example.org/realms/n", MCP_AUDIENCE="newsroom-mcp",
  SESSION_NETWORK="x", DOCKER_HOST="tcp://127.0.0.1:1")
sys.path.insert(0, "gateway")
import app as g
from mcp.server.auth.provider import AccessToken

def fake_start(bs):
    bs.name = "127.0.0.1"          # local playwright-mcp on :8931
    return "fake-" + bs.id
g._start_container = fake_start
g._stop_container = lambda cid: None

async def fake_verify(self, token):
    if token not in ("alice", "bob"):
        return None
    return AccessToken(token=token, client_id="c", scopes=[], expires_at=None,
                       subject=token, claims={"email": f"{token}@news.org"})
g.KeycloakVerifier.verify_token = fake_verify
app = g.app   # run with: uvicorn harness:app --port 8000
```

Clients must send `Host: browsers.example.org` (the gateway's DNS-rebinding
protection only allows `PUBLIC_HOST`).

## Files

Repository layout:

```
newsroom-browsers/
├── docker-compose.yml
├── .env.example
├── Caddyfile
├── README.md
├── gateway/
│   ├── app.py
│   ├── requirements.txt
│   └── Dockerfile
├── browser/
│   ├── Dockerfile
│   ├── entrypoint.sh      (chmod +x)
│   └── config.json
└── tor/
    ├── Dockerfile
    └── torrc
```

### `docker-compose.yml`

```yaml
# Per-session, OAuth2-protected browser automation for Claude.
#   https://$PUBLIC_HOST/tor/mcp     MCP connector, browsers egress via Tor only
#   https://$PUBLIC_HOST/clear/mcp   MCP connector, browsers egress directly
# Each MCP session (= Claude conversation) gets its own browser container,
# launched by the gateway on first use and removed when ended or idle.
name: newsroom-browsers

x-gateway: &gateway
  build: ./gateway
  restart: unless-stopped
  depends_on: [docker-proxy, browser-image]
  volumes:
    - ${EVIDENCE_HOST_DIR}:${EVIDENCE_HOST_DIR}
  environment: &gateway-env
    PUBLIC_HOST: ${PUBLIC_HOST}
    OIDC_ISSUER: ${OIDC_ISSUER}
    MCP_AUDIENCE: ${MCP_AUDIENCE}
    ALLOWED_GROUP: ${ALLOWED_GROUP}
    EVIDENCE_HOST_DIR: ${EVIDENCE_HOST_DIR}
    BROWSER_IMAGE: newsroom-browser:latest
    DOCKER_HOST: tcp://docker-proxy:2375
    IDLE_TIMEOUT: "1800"
    MAX_SESSIONS_PER_USER: "3"
    MAX_SESSIONS_TOTAL: "30"

services:
  caddy:
    image: caddy:2.11
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    environment:
      PUBLIC_HOST: ${PUBLIC_HOST}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    networks: [public, sessions_tor, sessions_clear]

  # Human SSO for the live view (MCP bearer tokens are verified by the gateways)
  oauth2-proxy:
    image: quay.io/oauth2-proxy/oauth2-proxy:v7.15.5
    restart: unless-stopped
    environment:
      OAUTH2_PROXY_PROVIDER: keycloak-oidc
      OAUTH2_PROXY_OIDC_ISSUER_URL: ${OIDC_ISSUER}
      OAUTH2_PROXY_CLIENT_ID: ${O2P_CLIENT_ID}
      OAUTH2_PROXY_CLIENT_SECRET: ${O2P_CLIENT_SECRET}
      OAUTH2_PROXY_COOKIE_SECRET: ${O2P_COOKIE_SECRET}
      OAUTH2_PROXY_COOKIE_SECURE: "true"
      OAUTH2_PROXY_REDIRECT_URL: https://${PUBLIC_HOST}/oauth2/callback
      OAUTH2_PROXY_HTTP_ADDRESS: 0.0.0.0:4180
      OAUTH2_PROXY_REVERSE_PROXY: "true"
      OAUTH2_PROXY_UPSTREAMS: static://202
      OAUTH2_PROXY_EMAIL_DOMAINS: "*"
      OAUTH2_PROXY_ALLOWED_GROUPS: ${ALLOWED_GROUP}
      OAUTH2_PROXY_SET_XAUTHREQUEST: "true"
      OAUTH2_PROXY_SKIP_PROVIDER_BUTTON: "true"
      OAUTH2_PROXY_CODE_CHALLENGE_METHOD: S256
    networks: [public]

  gateway-tor:
    <<: *gateway
    environment:
      <<: *gateway-env
      EGRESS: tor
      SESSION_NETWORK: nb_sessions_tor
      PROXY_SERVER: socks5://tor:9050
    networks: [public, control, sessions_tor]

  gateway-clear:
    <<: *gateway
    environment:
      <<: *gateway-env
      EGRESS: clear
      SESSION_NETWORK: nb_sessions_clear
      EGRESS_NETWORK: nb_egress_clear
    networks: [public, control, sessions_clear]

  # Narrowed Docker API for the gateways (containers + networks only)
  docker-proxy:
    image: tecnativa/docker-socket-proxy:latest
    restart: unless-stopped
    environment:
      CONTAINERS: "1"
      NETWORKS: "1"
      POST: "1"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [control]

  tor:
    build: ./tor
    restart: unless-stopped
    volumes: [tor_data:/var/lib/tor]
    networks: [tor_egress, sessions_tor]

  # Build-only: the image the gateways launch per session. Exits immediately.
  browser-image:
    build: ./browser
    image: newsroom-browser:latest
    entrypoint: ["true"]
    restart: "no"
    networks: [egress_clear]   # ensures compose creates this network

networks:
  public: {}
  control:        { internal: true }
  tor_egress: {}
  sessions_tor:   { internal: true, name: nb_sessions_tor }   # tor browsers: no internet, only tor:9050
  sessions_clear: { internal: true, name: nb_sessions_clear }
  egress_clear:   { name: nb_egress_clear }                   # clear browsers -> internet

volumes:
  caddy_data: {}
  tor_data: {}
```

### `.env.example`

```bash
PUBLIC_HOST=browsers.example.org
OIDC_ISSUER=https://keycloak.example.org/realms/newsroom

# "aud" of the access tokens your Claude connector gets from Keycloak.
MCP_AUDIENCE=newsroom-mcp

# Keycloak group required for MCP use and the live view (groups mapper, full path).
ALLOWED_GROUP=/investigations

# Confidential Keycloak client for the human live-view login.
# Valid redirect URI: https://browsers.example.org/oauth2/callback
O2P_CLIENT_ID=newsroom-browsers
O2P_CLIENT_SECRET=change-me
# python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
O2P_COOKIE_SECRET=change-me

# Host dir for per-session outputs (screenshots, PDFs, session logs),
# laid out as <email>/<timestamp>-<session>/. Must exist, owned by uid 1000.
EVIDENCE_HOST_DIR=/srv/newsroom-evidence
```

### `Caddyfile`

```caddyfile
# Keycloak SSO for humans (oauth2-proxy). Puts the verified email in
# X-Auth-Request-Email; any client-supplied value is stripped first.
(sso) {
	request_header -X-Auth-Request-Email
	forward_auth oauth2-proxy:4180 {
		uri /oauth2/auth
		copy_headers X-Auth-Request-Email
		@unauth status 401
		handle_response @unauth {
			redir * /oauth2/start?rd={http.request.orig_uri.path}
		}
	}
}

(egress) {
	# MCP endpoint + RFC 9728 metadata: bearer auth is done by the gateway itself
	handle /{args[0]}/mcp* {
		reverse_proxy gateway-{args[0]}:8000 {
			flush_interval -1
		}
	}
	handle /.well-known/oauth-protected-resource/{args[0]}/mcp {
		reverse_proxy gateway-{args[0]}:8000
	}

	# Handoff link given to the user: SSO, then the gateway checks ownership
	# and redirects to noVNC with the session's VNC password.
	@start_{args[0]} path_regexp ^/{args[0]}/view/[A-Za-z0-9_-]+/start$
	handle @start_{args[0]} {
		route {
			import sso
			reverse_proxy gateway-{args[0]}:8000
		}
	}

	# Live view (noVNC page + websocket): SSO, ownership check, then proxy
	# straight to that session's browser container.
	@view_{args[0]} path_regexp ^/{args[0]}/view/[A-Za-z0-9_-]+/
	handle @view_{args[0]} {
		route {
			import sso
			request_header -X-Upstream
			forward_auth gateway-{args[0]}:8000 {
				uri /authz
				copy_headers X-Upstream
			}
			uri path_regexp ^/{args[0]}/view/[A-Za-z0-9_-]+ ""
			reverse_proxy {http.request.header.X-Upstream}
		}
	}
}

{$PUBLIC_HOST} {
	handle /oauth2/* {
		reverse_proxy oauth2-proxy:4180
	}

	import egress tor
	import egress clear

	handle {
		respond 404
	}
}
```

### `gateway/app.py`

```python
"""
Per-session browser gateway.

* Authenticates MCP clients with Keycloak-issued bearer JWTs (RFC 9728 metadata
  + 401 handled by the MCP SDK).
* Each MCP session gets its own browser container (headed Chromium + noVNC +
  Playwright MCP), started lazily on first tool call, removed when idle.
* Proxies Playwright MCP's tools to that container and adds:
    browser_request_human_help  -> returns a link to *this session's* live view
    browser_end_session         -> tears the container down
* /authz and /<egress>/view/<id>/start let Caddy grant the live view only to the
  Keycloak user who owns the session.
"""
import asyncio
import contextlib
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import docker
import httpx
import jwt
from jwt import PyJWKClient
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

log = logging.getLogger("gateway")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

EGRESS = os.environ["EGRESS"]                          # "tor" | "clear"
PUBLIC_HOST = os.environ["PUBLIC_HOST"]
OIDC_ISSUER = os.environ["OIDC_ISSUER"].rstrip("/")
MCP_AUDIENCE = os.environ["MCP_AUDIENCE"]
ALLOWED_GROUP = os.environ.get("ALLOWED_GROUP", "")
BROWSER_IMAGE = os.environ.get("BROWSER_IMAGE", "newsroom-browser:latest")
SESSION_NETWORK = os.environ["SESSION_NETWORK"]        # shared with Caddy, internal
EGRESS_NETWORK = os.environ.get("EGRESS_NETWORK", "")  # clear only
PROXY_SERVER = os.environ.get("PROXY_SERVER", "")      # e.g. socks5://tor:9050
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "1800"))
MAX_PER_USER = int(os.environ.get("MAX_SESSIONS_PER_USER", "3"))
MAX_TOTAL = int(os.environ.get("MAX_SESSIONS_TOTAL", "30"))
EVIDENCE_DIR = os.environ.get("EVIDENCE_HOST_DIR", "")  # same path on host and in this container
UPSTREAM_HOST = "mcp-upstream"                          # must match allowedHosts in browser config
LABEL = "newsroom.egress"

MCP_PATH = f"/{EGRESS}/mcp"
_docker: docker.DockerClient | None = None


def dk() -> docker.DockerClient:
    """Lazily connect, so the gateway starts even if the socket proxy is late."""
    global _docker
    if _docker is None:
        _docker = docker.DockerClient(base_url=os.environ.get("DOCKER_HOST", "tcp://docker-proxy:2375"))
    return _docker


# ---------------------------------------------------------------- auth ----
class KeycloakVerifier(TokenVerifier):
    def __init__(self) -> None:
        self.jwks = PyJWKClient(f"{OIDC_ISSUER}/protocol/openid-connect/certs", cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256", "PS256", "ES256"],
                audience=MCP_AUDIENCE, issuer=OIDC_ISSUER,
                options={"require": ["exp", "iss", "sub", "aud"]},
            )
        except Exception as e:  # noqa: BLE001
            log.info("token rejected: %s", e)
            return None
        if ALLOWED_GROUP and ALLOWED_GROUP not in claims.get("groups", []):
            log.info("token rejected: %s not in %s", claims.get("email"), ALLOWED_GROUP)
            return None
        if not claims.get("email"):
            log.info("token rejected: no email claim")
            return None
        return AccessToken(
            token=token, client_id=claims.get("azp", ""), scopes=claims.get("scope", "").split(),
            expires_at=claims.get("exp"), subject=claims["sub"], claims=claims,
        )


# ------------------------------------------------------------ sessions ----
@dataclass
class BrowserSession:
    owner: str
    mcp_session: Any                       # ServerSession object this browser belongs to
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    vnc_password: str = field(default_factory=lambda: secrets.token_urlsafe(6)[:8])
    name: str = ""
    container_id: str = ""
    client: ClientSession | None = None
    error: BaseException | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    last_used: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.name = f"nb-{EGRESS}-{self.id[:12].lower().replace('_', 'x')}"


by_mcp: dict[int, BrowserSession] = {}     # id(ServerSession) -> session
by_view: dict[str, BrowserSession] = {}    # view id -> session
sessions_lock = asyncio.Lock()
tools_cache: list[types.Tool] | None = None
tools_lock = asyncio.Lock()


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "_", s)


def _start_container(bs: BrowserSession) -> str:
    volumes = {}
    if EVIDENCE_DIR and bs.owner != "probe":
        path = os.path.join(EVIDENCE_DIR, _safe(bs.owner), time.strftime("%Y%m%d-%H%M%S") + "-" + bs.id[:8])
        os.makedirs(path, exist_ok=True)
        volumes[path] = {"bind": "/data/output", "mode": "rw"}
    c = dk().containers.run(
        BROWSER_IMAGE,
        command=["--proxy-server", PROXY_SERVER] if PROXY_SERVER else [],
        name=bs.name, detach=True, auto_remove=True, init=True,
        environment={"VNC_PASSWORD": bs.vnc_password},
        network=SESSION_NETWORK, volumes=volumes,
        shm_size="1g", mem_limit="3g", pids_limit=1024,
        cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
        labels={LABEL: EGRESS, "newsroom.owner": bs.owner, "newsroom.session": bs.id},
    )
    if EGRESS_NETWORK:
        dk().networks.get(EGRESS_NETWORK).connect(c)
    return c.id


def _stop_container(container_id: str) -> None:
    with contextlib.suppress(Exception):
        dk().containers.get(container_id).remove(force=True)


async def _wait_http(url: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    async with httpx.AsyncClient() as h:
        while time.time() < deadline:
            try:
                await h.get(url, headers={"Host": UPSTREAM_HOST}, timeout=2)
                return
            except httpx.HTTPError:
                await asyncio.sleep(0.5)
    raise TimeoutError(f"browser container did not come up: {url}")


async def _run_client(bs: BrowserSession) -> None:
    """Owns the upstream MCP connection for the lifetime of the session."""
    try:
        bs.container_id = await asyncio.to_thread(_start_container, bs)
        url = f"http://{bs.name}:8931/mcp"
        await _wait_http(url)
        async with streamablehttp_client(url, headers={"Host": UPSTREAM_HOST}) as (r, w, _):
            async with ClientSession(r, w) as cs:
                await cs.initialize()
                bs.client = cs
                bs.ready.set()
                log.info("session %s started for %s", bs.name, bs.owner)
                await bs.closing.wait()
    except BaseException as e:  # noqa: BLE001
        bs.error = e
        log.warning("session %s failed: %r", bs.name, e)
    finally:
        bs.ready.set()
        if bs.container_id:
            await asyncio.to_thread(_stop_container, bs.container_id)


async def _start(bs: BrowserSession) -> BrowserSession:
    bs.task = asyncio.create_task(_run_client(bs))
    await bs.ready.wait()
    if bs.error or not bs.client:
        raise RuntimeError(f"could not start browser: {bs.error!r}")
    return bs


async def _close(bs: BrowserSession) -> None:
    by_mcp.pop(id(bs.mcp_session), None)
    by_view.pop(bs.id, None)
    bs.closing.set()
    if bs.task:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(bs.task, 15)
    log.info("session %s closed", bs.name)


async def get_session(mcp_session: Any, owner: str, create: bool = True) -> BrowserSession | None:
    async with sessions_lock:
        bs = by_mcp.get(id(mcp_session))
        if bs and bs.mcp_session is mcp_session:
            if bs.owner != owner:
                raise PermissionError("session owner mismatch")
            if bs.error or (bs.task and bs.task.done()):
                await _close(bs)
            else:
                return bs
        if not create:
            return None
        if sum(1 for s in by_mcp.values() if s.owner == owner) >= MAX_PER_USER:
            raise RuntimeError(f"you already have {MAX_PER_USER} browser sessions; end one first")
        if len(by_mcp) >= MAX_TOTAL:
            raise RuntimeError("browser capacity reached; try again later")
        bs = BrowserSession(owner=owner, mcp_session=mcp_session)
        by_mcp[id(mcp_session)] = bs
        by_view[bs.id] = bs
    try:
        return await _start(bs)
    except Exception:
        await _close(bs)
        raise


async def upstream_tools() -> list[types.Tool]:
    """Tool list of the browser image, fetched once from a throwaway container."""
    global tools_cache
    async with tools_lock:
        if tools_cache is None:
            probe = BrowserSession(owner="probe", mcp_session=object())
            try:
                await _start(probe)
                tools_cache = (await probe.client.list_tools()).tools
            finally:
                await _close(probe)
    return tools_cache


async def reaper() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for bs in list(by_mcp.values()):
            if now - bs.last_used > IDLE_TIMEOUT:
                log.info("reaping idle session %s", bs.name)
                await _close(bs)


def _cleanup_orphans() -> None:
    """Remove browser containers left over from a previous gateway run."""
    try:
        for c in dk().containers.list(all=True, filters={"label": f"{LABEL}={EGRESS}"}):
            with contextlib.suppress(Exception):
                c.remove(force=True)
    except Exception as e:  # noqa: BLE001
        log.warning("orphan cleanup skipped: %r", e)


# ------------------------------------------------------------- server -----
mcp = FastMCP(
    name=f"newsroom-browser-{EGRESS}",
    instructions=(
        f"Each conversation gets its own private, isolated browser ({'egress via Tor' if EGRESS == 'tor' else 'direct egress'}). "
        "When a page shows a CAPTCHA, bot check, login wall, cookie wall you cannot get past, or anything "
        "requiring a human, call browser_request_human_help, show the user the link, and wait for them "
        "to confirm before continuing. Call browser_end_session when the task is finished."
    ),
    token_verifier=KeycloakVerifier(),
    auth=AuthSettings(issuer_url=OIDC_ISSUER, resource_server_url=f"https://{PUBLIC_HOST}{MCP_PATH}"),
    streamable_http_path=MCP_PATH,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[PUBLIC_HOST], allowed_origins=[f"https://{PUBLIC_HOST}"],
    ),
)
server = mcp._mcp_server  # we replace FastMCP's static tool handlers with a proxy

EXTRA_TOOLS = [
    types.Tool(
        name="browser_request_human_help",
        description=(
            "Get a link that lets the user see and control THIS conversation's browser in a new tab. "
            "Use it when the page shows a CAPTCHA, bot challenge, login, 2FA prompt or anything else a human "
            "should do. Give the link to the user, explain what they need to do, then STOP and wait until they "
            "confirm they are done before taking another browser action."
        ),
        inputSchema={"type": "object", "properties": {
            "reason": {"type": "string", "description": "What the user needs to do, e.g. 'solve the CAPTCHA'"}},
            "required": ["reason"]},
    ),
    types.Tool(
        name="browser_end_session",
        description="Close this conversation's browser and discard its state (cookies, tabs).",
        inputSchema={"type": "object", "properties": {}},
    ),
]
HIDDEN = {"browser_install"}


def _caller() -> tuple[Any, str]:
    ctx = server.request_context
    token: AccessToken = ctx.request.scope["user"].access_token
    return ctx.session, token.claims["email"].lower()


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [t for t in await upstream_tools() if t.name not in HIDDEN] + EXTRA_TOOLS


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
    mcp_session, owner = _caller()

    def text(msg: str, error: bool = False) -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], isError=error)

    if name in HIDDEN:
        return text(f"{name} is disabled", True)
    if name == "browser_end_session":
        bs = await get_session(mcp_session, owner, create=False)
        if bs:
            await _close(bs)
        return text("Browser session closed.")
    try:
        bs = await get_session(mcp_session, owner)
    except Exception as e:  # noqa: BLE001
        return text(str(e), True)
    bs.last_used = time.time()

    if name == "browser_request_human_help":
        link = f"https://{PUBLIC_HOST}/{EGRESS}/view/{bs.id}/start"
        reason = (arguments or {}).get("reason", "help with the page")
        return text(
            f"Live view of this conversation's browser: {link}\n"
            f"Ask the user to open it (they will sign in with their newsroom account) and to: {reason}. "
            "Only the user who owns this conversation can open it. "
            "Do not take further browser actions until the user confirms they are done."
        )
    return await bs.client.call_tool(name, arguments)


# ------------------------------------------------------ live-view auth ----
VIEW_RE = re.compile(rf"^/{EGRESS}/view/([A-Za-z0-9_-]+)(/|$)")


def _owner_ok(request: Request, view_id: str) -> BrowserSession | None:
    email = request.headers.get("x-auth-request-email", "").lower()
    bs = by_view.get(view_id)
    if not bs or not email or bs.owner != email or bs.owner == "probe":
        return None
    bs.last_used = time.time()
    return bs


@mcp.custom_route("/authz", methods=["GET"])
async def authz(request: Request) -> Response:
    """Caddy forward_auth: is this SSO user the owner of the session in the URL?"""
    m = VIEW_RE.match(request.headers.get("x-forwarded-uri", ""))
    bs = _owner_ok(request, m.group(1)) if m else None
    if not bs:
        return PlainTextResponse("forbidden", status_code=403)
    return Response(status_code=200, headers={"X-Upstream": f"{bs.name}:6080"})


@mcp.custom_route(f"/{EGRESS}/view/{{view_id}}/start", methods=["GET"])
async def view_start(request: Request) -> Response:
    """Landing link handed to the user; reveals the VNC password only to the owner."""
    view_id = request.path_params["view_id"]
    bs = _owner_ok(request, view_id)
    if not bs:
        return PlainTextResponse("This browser session does not exist or is not yours.", status_code=403)
    base = f"/{EGRESS}/view/{view_id}"
    return RedirectResponse(
        f"{base}/vnc.html?path={base.lstrip('/')}/websockify&autoconnect=1&reconnect=1"
        f"&resize=scale&password={bs.vnc_password}", status_code=302)


# ---------------------------------------------------------------- app ------
app = mcp.streamable_http_app()
_inner_lifespan = app.router.lifespan_context


@contextlib.asynccontextmanager
async def lifespan(a):
    await asyncio.to_thread(_cleanup_orphans)
    async with _inner_lifespan(a):
        task = asyncio.create_task(reaper())
        try:
            yield
        finally:
            task.cancel()
            for bs in list(by_mcp.values()):
                await _close(bs)

app.router.lifespan_context = lifespan
```

### `gateway/requirements.txt`

```text
mcp==1.30.0
docker==7.1.0
pyjwt[crypto]==2.10.1
httpx>=0.27
uvicorn>=0.30
```

### `gateway/Dockerfile`

```dockerfile
FROM python:3.12-slim
RUN useradd -u 1000 -m gw
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# uid 1000 = the "node" user in the browser image, so evidence dirs it creates are writable there
USER gw
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

### `browser/Dockerfile`

```dockerfile
FROM node:22-bookworm-slim

# Pin and bump deliberately; flags/config keys change between releases.
ARG PLAYWRIGHT_MCP_VERSION=0.0.83
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify ca-certificates \
      fonts-noto fonts-noto-cjk fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# Install Playwright MCP and the exact Chromium build it expects.
RUN npm install -g @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} \
 && cd "$(npm root -g)/@playwright/mcp" \
 && npx --no-install playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/* /root/.npm

RUN mkdir -p /data/profile /data/output /tmp/.X11-unix \
 && chmod 1777 /tmp/.X11-unix \
 && chown -R node:node /data

COPY --chmod=755 entrypoint.sh /entrypoint.sh
COPY config.json /etc/playwright-mcp.json

USER node
WORKDIR /data
EXPOSE 8931 6080
ENTRYPOINT ["/entrypoint.sh"]
```

### `browser/entrypoint.sh`

```sh
#!/bin/sh
set -eu
: "${VNC_PASSWORD:?VNC_PASSWORD must be set}"

# Virtual display the headed Chromium renders into
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
i=0; while [ ! -e /tmp/.X11-unix/X99 ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i+1)); done

# VNC server, loopback only, password protected
x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
x11vnc -display :99 -rfbauth /tmp/vncpass -localhost -rfbport 5900 \
       -forever -shared -quiet -bg -o /tmp/x11vnc.log

# noVNC web client + websocket bridge (reached only via Caddy)
websockify --web /usr/share/novnc 0.0.0.0:6080 localhost:5900 &

# Playwright MCP over streamable HTTP; launches headed Chromium on :99 on first use
exec playwright-mcp --config /etc/playwright-mcp.json --caps pdf "$@"
```

### `browser/config.json`

```json
{
  "browser": {
    "browserName": "chromium",
    "userDataDir": "/data/profile",
    "launchOptions": {
      "headless": false,
      "chromiumSandbox": false,
      "args": [
        "--window-position=0,0",
        "--window-size=1440,900",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
      ]
    },
    "contextOptions": {
      "viewport": null
    }
  },
  "server": {
    "host": "0.0.0.0",
    "port": 8931,
    "allowedHosts": [
      "mcp-upstream"
    ]
  },
  "saveSession": true,
  "outputDir": "/data/output"
}
```

### `tor/Dockerfile`

```dockerfile
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends tor \
 && rm -rf /var/lib/apt/lists/*
COPY torrc /etc/tor/torrc
USER debian-tor
EXPOSE 9050
CMD ["tor", "-f", "/etc/tor/torrc"]
```

### `tor/torrc`

```text
SocksPort 0.0.0.0:9050
SocksPolicy accept 10.0.0.0/8
SocksPolicy accept 172.16.0.0/12
SocksPolicy accept 192.168.0.0/16
SocksPolicy reject *
DataDirectory /var/lib/tor
Log notice stdout
```

### `README.md`

````markdown
# newsroom-browsers

Per-session browser automation for Claude, self-hosted, behind Keycloak.

Every MCP session (one Claude conversation) gets **its own browser container**,
launched on the first browser tool call and destroyed when the conversation
ends it or it goes idle. When the model hits a CAPTCHA or login, it calls
`browser_request_human_help`, which returns a link. Only the journalist who
owns that conversation can open it (Keycloak SSO + ownership check), and it
shows *their* browser in a new tab.

```
Claude ──Bearer JWT──▶ Caddy ──▶ gateway-tor / gateway-clear ──docker API──▶ docker-proxy
                         │            │  verifies JWT, one browser per MCP session,
                         │            │  proxies Playwright MCP tools + adds handoff tools
                         │            ▼
                         │       nb-tor-xxxx  nb-tor-yyyy  nb-clear-zzzz   (per-session containers:
Journalist ─SSO─▶ Caddy ─┘            │                         │          Xvfb + Chromium + noVNC
  /tor/view/<id>/…   oauth2-proxy     ▼                         ▼          + Playwright MCP)
  (owner only)       + gateway authz  tor ──▶ internet          internet
```

## What the model sees

All Playwright MCP tools (navigate, click, type, snapshot, …) plus:

- `browser_request_human_help(reason)` returns
  `https://HOST/<egress>/view/<session>/start`. The model is told to show it to
  the user and wait for confirmation.
- `browser_end_session()` destroys the browser and its cookies.

## Setup

1. `cp .env.example .env`, fill in, and create `EVIDENCE_HOST_DIR` owned by uid 1000.
2. Keycloak:
   - Access tokens for your Claude connector must carry `aud = MCP_AUDIENCE`,
     an `email` claim, and a `groups` claim (full path) when `ALLOWED_GROUP` is set.
   - Confidential client `O2P_CLIENT_ID` for the live view, redirect URI
     `https://$PUBLIC_HOST/oauth2/callback`, same groups mapper.
3. `docker compose up -d --build`
4. Add two custom connectors in Claude: `https://$PUBLIC_HOST/tor/mcp` and
   `https://$PUBLIC_HOST/clear/mcp`. Unauthenticated requests get a 401 with
   `resource_metadata` pointing at Keycloak, served by the gateway.

## How sessions work

- **Lifecycle:** The first tools/list starts one throwaway container to learn the
  tool list (cached). A conversation's container starts on its first tool
  call. It's removed by `browser_end_session`, after `IDLE_TIMEOUT` (30 min) without
  activity, or when the gateway restarts (orphans are cleaned up on boot).
- **Ownership:** A session belongs to the `email` claim of the token that
  created it. Every MCP call is re-checked against it. The live view needs an
  SSO session for the same email. A leaked link is useless to anyone else.
- **Live view:** `/start` redirects (owner only) to noVNC with a per-session
  random VNC password. That password stops pages inside the browser from
  connecting to their own VNC server.
- **Limits:** `MAX_SESSIONS_PER_USER` (3) and `MAX_SESSIONS_TOTAL` (30), about 0.5–1 GB
  RAM per active browser.
- **State:** Profiles are ephemeral (nothing persists between conversations).
  Outputs and session logs go to `EVIDENCE_HOST_DIR/<email>/<time>-<id>/`.

## Verify before relying on it

- Through the tor connector, have Claude open `https://check.torproject.org`.
  The clear connector should report *not* Tor.
- Run a WebRTC/DNS leak test page through the tor connector.
- Open a handoff link while logged in as a different user. You should get a 403.
- `docker ps --filter label=newsroom.egress` should show one container per active conversation.

## Caveats

- **The gateway controls Docker.** Even through the socket proxy, the ability to
  create containers is close to root on the host. Run this on a dedicated VM.
  For stronger isolation of hostile pages, use gVisor for the browser containers
  (`runtime="runsc"` in `_start_container`).
- **Chromium over Tor is not Tor Browser.** It hides your IP, but the fingerprint
  is plain Chromium. Each container has its own IP, so Tor's default
  `IsolateClientAddr` gives every session separate circuits.
- Tor exits are heavily challenged by Cloudflare and similar, so expect more handoffs.
- Clear-egress session containers share an internal network. The VNC password
  and the MCP Host check (`mcp-upstream`) keep pages in one session from driving
  another, but they are not a network-level separation.
- An "MCP session" is whatever the client opens. If a client reconnects mid-conversation,
  it gets a fresh browser.
- Pinned versions: `@playwright/mcp` 0.0.83 and the `mcp` Python SDK 1.30.0 (v1 API; the
  2.x SDK renamed FastMCP and needs porting). Re-test when bumping.
- Single Docker host. On Kubernetes, the same gateway logic would create a pod
  and service per session instead of a container.
````
