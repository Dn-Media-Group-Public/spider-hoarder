# Architecture overview

spider-hoarder is a set of MCP servers (one per enabled backend, [Pluggable backends](backend-plugins.md)); the browser ones give Claude per-user, isolated, real (headed) Chromium sessions, with a
human handoff for CAPTCHAs/logins, Tor or I2P egress, and fetch/parse/store tools that return Markdown. It knows nothing about
any downstream graph or database: the MCP client is the orchestrator and carries text from here to wherever it belongs
(see the original framing in [Plan: Agentic research MCP](../implemented-plans/agentic-research-mcp.md), whose "independent MCP servers,
no proxying" shape still holds; its layer-access-control / `ingest_text` parts live in another project).

See also: [Plan: Browser sessions](../implemented-plans/browser-sessions-plan.md)

The same stack can also serve Android emulators (`/android-clear/mcp`, `/android-tor/mcp`): [Android emulators](android.md).
See also: [Plan: Android hoarder](../implemented-plans/android-hoarder.md), [Plan: Android KVM bring-up](../implemented-plans/android-kvm-bringup.md)

## Components (Docker Swarm stack)

What runs is the `BACKENDS` list in `.env`: every backend is a pip package that registers a descriptor and brings its own stack
overlay, images and helpers ([Pluggable backends](backend-plugins.md)). Names are canonical (`browser_tor`); URLs, services and DNS use the
hyphenated form (`/browser-tor/mcp`, service `browser-tor-gateway`). A disabled backend has no images, services, networks, routes or hints.
See also: [Plan: Pluggable backends](../implemented-plans/backend-plugins.md)

```
Claude ──Bearer JWT──▶ nginx ─▶ browser-tor-gateway ──▶ browser-tor-daemon ──▶ internet   /browser-tor/mcp
                         │  ├─▶ browser-clear-gateway ───────────────────────▶ internet   /browser-clear/mcp
                         │  └─▶ browser-i2p-gateway ─▶ browser-i2p-router-k ─▶ I2P (*.i2p) /browser-i2p/mcp   (browser-i2p-seed: warm netDb snapshot)
Human (live view) ─SSO─▶ nginx ─▶ oauth2-proxy ─▶ Keycloak (external OIDC)

browser-<egress>-gateway container (1..N replicas):
  gw (uid 1000, unprivileged)         sessiond (root, tiny)
   ├ MCP server, JWT auth              ├ owns the uid pool and its invariants
   ├ session registry, reaper          ├ start(uid): create home, spawn session group as uid
   ├ proxies tool calls  ────────────▶ ├ stop(uid): kill, wipe, verify
   ├ storage / blobs / uploads, parse client
   └ live-view authz (/authz, /start)  └ /run/sessiond.sock (gateway only)
  per session (uid 20000+n):  Xvfb ─ x11vnc ─ websockify ─ toolserver (Python MCP) ─ Chromium
```

| Service | Role | Source |
|---|---|---|
| `nginx` | Only published port. Routes by backend prefix (generic `[a-z0-9-]+`, proxied to `tasks.<prefix>-gateway`; an unknown or disabled prefix is a 502) and by replica id; SSO for the live view; serves the static landing page generated at deploy time. TLS ends at the load balancer in front of it. | `nginx.conf.template` |
| `oauth2-proxy` | Human SSO for the live view only (MCP uses bearer JWTs verified by the gateway). Registers its own OIDC client. | `oauth2-proxy/` |
| `browser-clear-gateway`, `browser-tor-gateway`, `browser-i2p-gateway` | The gateway + `sessiond` + per-session browsers; one base image, the backend's egress policy object selects the policy. Shared by all users, **not** one container per session. | `backends/browser-sdk/`, `backends/browser_*/` |
| `browser-tor-daemon` | `browser_tor`'s own Tor daemon with a pool of SocksPorts (runs as `debian-tor`, no capabilities, no `NET_ADMIN`). | `backends/browser_tor/browser_tor/daemon/` |
| `browser-i2p-router`, `browser-i2p-seed` | I2P egress: one `i2pd` router per `browser-i2p-gateway` replica (slot k ↔ `browser-i2p-router-k`), a SOCKS destination per session; the seed serves a netDb snapshot so routers start fast. See also: [Plan: I2P egress](../implemented-plans/i2p-egress.md) | `backends/browser_i2p/browser_i2p/{router,seed}/` |
| `android-clear-gateway`, `android-tor-gateway`, `android-*-wrapper`, `android-tor-daemon`, slots | Optional Android emulators: a light gateway, one wrapper per backend that makes the slot containers, one emulator per slot, and (tor) the backend's own transparent-proxy Tor daemon. See [Android emulators](android.md). | `backends/android-sdk/`, `backends/android_*/` |
| `parser` (shared, `PARSE_REPLICAS`) | Everything that interprets an untrusted file: the wrapper (storage ↔ parser, uid pool, tokens) and sandboxed parser processes on pool uids with no credential and no route out. Gateways only say "parse job X" and read the result from the store. [Plan: parser wrapper](../implemented-plans/parser-wrapper.md) | `parser/`, `docker-compose.parse.yml` |
| `blob-sidecar` | The parser's authenticated egress proxy (squid): the only way the wrapper reaches the store; own copy of the public-internet-only egress rules. | `blob-sidecar/` |
| `ocr-sidecar` (optional: `OCR_PROFILES`) | Stateless forwarder of images to the configured OpenAI-compatible OCR endpoints, one token per profile, own pinned egress. | `ocr-sidecar/` |
| Keycloak | External OIDC provider (prod); a throwaway one in `--dev`. | `keycloak/setup.sh` |
| `seaweedfs` (optional: `SEAWEEDFS=1`, default with `--dev`) | The stack's own object store (S3 + STS), an alternative to GCS or another S3: jobs live in one bucket so any gateway replica serves any job, and the gateway can mint per-job credentials. Bucket created by `deploy.sh` (`weed shell`), no published port. See also: [Plan: project access model](../implemented-plans/project-access-model.md) | `docker-compose.seaweedfs.yml` |

### Why one container per backend, not per session
No Docker socket and no container creation: a session is a Unix uid plus a process group inside the container. Preflight/downloads
(`requests`) and blob storage stay in the gateway because they must use the same egress as the browser (`socks5h://browser-tor-daemon` in
the Tor backend). **Parsing is the exception**: it is one shared parser service, because interpreting an untrusted file must run as a uid that holds nothing
and has no network ([Jobs and storage](jobs-and-storage.md#the-parser-service)); [Plan: parser wrapper](../implemented-plans/parser-wrapper.md)
See also: [Plan: Browser sessions](../implemented-plans/browser-sessions-plan.md) (supersedes the container-per-session design of
[Plan: Newsroom browsers](../implemented-plans/newsroom-browsers-plan.md); its Keycloak/Tor background is still referenced).

## Gateway and the tool server

The gateway framework (`spider_hoarder_sdk/gateway/app.py`, `sessions.py`) speaks MCP to Claude (low-level `mcp` SDK, because the tool list is dynamic: proxied from the
session's tool server) and proxies browser tool calls over loopback HTTP to that session's tool server on port `9000+n`
(`Host: mcp-upstream` is required). Everything backend-specific goes through the driver named by `$BACKEND` ([Pluggable backends](backend-plugins.md)). It adds its own tools (`browser_request_human_help`, `browser_save_page`,
`browser_end_session`, `ingest_page`, file tools) and turns every bulk output into a *job*
([Jobs and storage](jobs-and-storage.md)). Browser internals: [Browser tool server](browser-toolserver.md).

Session lifecycle: `initialize` reserves a uid, the browser starts lazily on the first tool call, and every ending path
(`browser_end_session`, idle timeout `IDLE_TIMEOUT`, MCP session close, MCP client silent for `DISCONNECT_TIMEOUT_MINUTES`,
crash) goes through one `close()` → `sessiond stop`, which also drops the session's media policy (the next `open_session` asks the user again). Limits: `MAX_SESSIONS_PER_USER`, `MAX_SESSIONS_TOTAL` (per container).
See [Session isolation](session-isolation.md).

## Request routing and replicas
See [Scaling and routing](scaling.md). Authentication: [Authentication and live view](auth.md).

## Naming

| Name | Meaning |
|---|---|
| **Backend** | A pip package registered under the entry-point group `spider_hoarder.backends` (`browser_tor`, `android_clear`, ...): descriptor, stack overlay, images, helpers, driver. |
| **Driver** | The backend's gateway-side object (`$BACKEND`): pool backend, upstream MCP URL, tool hooks. |
| **Gateway** | The shared MCP server in each `<backend>-gateway` container (uid 1000). |
| **Content pipeline** | extract / store / fetch of bytes -> Markdown; part of the gateway. |
| **Sessiond** | The only root process; owns the uid pool; starts/stops sessions. |
| **Session** | One uid + process group (Xvfb, x11vnc, websockify, toolserver, Chromium, egress filter). Not a Docker container. |
| **Toolserver** | The Python MCP browser server inside a session. |
| **Session server** | Umbrella for everything that makes sessions happen on a container: sessiond, session, toolserver. |
| **SDK** | `spider-hoarder-sdk`: the gateway framework every backend builds on; family SDKs (`browser-sdk`, `android-sdk`) hold what several backends share. |

The MCP client is whatever drives the tools over MCP (Claude); the user is the human using the live view.

## Code map

Separate pip distributions in one repository, installed into `.venv/` by `deploy.sh` (descriptors only) and built into the images from source by `docker build`. Tests live next to each package (run per package: [Deploying](../operations/deploy-and-configure.md#tests)). See also: [Plan: Build everything inside `docker build`](../implemented-plans/build-in-docker.md)

| Path | Responsibility |
|---|---|
| `sdk/spider_hoarder_sdk/gateway/app.py` | MCP server, JWT/auth wiring, job tools, blob/upload/live-view routes, `/authz`, replica session-id middleware; loads the driver for `$BACKEND`; builds the instructions incl. the other-servers list |
| `sdk/spider_hoarder_sdk/gateway/driver.py` | the `Driver` base class (the seam) and the entry-point loader |
| `sdk/spider_hoarder_sdk/gateway/sessions.py`, `pool.py`, `jobs.py`, `ident.py` | session registry, reservations, reaper; uid pool; job ids/store/parse helpers; replica id |
| `sdk/spider_hoarder_sdk/gateway/auth.py`, `blobtoken.py` | Keycloak JWT verification, HMAC links |
| `sdk/spider_hoarder_sdk/gateway/projects.py`, `creds.py` | projects, roles, `access.json` and the `users/` index, the per-MCP-session project binding; scoped store credentials (STS / Credential Access Boundary). See also: [Plan: project access model](../implemented-plans/project-access-model.md) |
| `sdk/spider_hoarder_sdk/content/`, `textcap.py` | `storage.py` (fsspec), `grep.py` (regex search of job text); text caps. `gateway/parse_client.py`: the client of the parser service |
| `parser/spider_hoarder_parser/` | `wrapper.py` (API, uid pool, record validation, upload, manifest/status), `worker.py` (the parser process, stdout record protocol), `parse.py`, `extract.py`, `metadata.py`, `ocr.py` (sidecar client) |
| `blob-sidecar/`, `ocr-sidecar/` | squid proxy with auth and its own egress rules; OCR forwarder with pinned egress |
| `backends/browser-sdk/spider_hoarder_browser_sdk/` | `driver.py` (BrowserDriver), `egress.py` (egress-policy base), `firewall.py`, `start.py` (container start), `files.py`, `pages.py`, `fetch.py`, `netguard.py`, `ports.py`, `mediapolicy.py`, `cmp_rules.py`, `sessiond_client.py`, `image/` (base image) |
| `backends/browser-sdk/.../session_server/` | `sessiond/` (root side of the uid pool), `session/` (`session-group.sh`, `filter_proxy.py`), `toolserver/` ([details](browser-toolserver.md)) |
| `backends/android-sdk/spider_hoarder_android_sdk/` | `driver.py` (AndroidDriver), `slots.py` (slot slice + pool backend), `android-tools.json`, `slot/` (slot image context incl. `slot/toolserver/`), `wrapper/`, `image/` (gateway base), `preflight.sh`, `post_build.sh` ([Android emulators](android.md)) |
| `backends/<backend>/<backend>/` | per backend: `descriptor.py`, `runtime.py` (the driver object), `docker-compose.yml` (overlay), `image/Dockerfile`, own helpers (`browser_tor/daemon`, `browser_i2p/router` + `seed`, `android_tor/daemon`) and, for browsers, `egress.py` |
| `scripts/spider_hoarder.py` | deploy tooling run by `deploy.sh` (validate, preflight, build, post-build, env, landing, compose, services, dev-overlay) |
| `docker-compose.yml`, `nginx.conf.template`, `deploy.sh` | base stack (nginx, oauth2-proxy, shared networks), routing, deploy |

See also: [Plan: Restructure directories](../implemented-plans/restructure-directories.md) (the earlier layout), [Plan: Pluggable backends](../implemented-plans/backend-plugins.md)
