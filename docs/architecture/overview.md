# Architecture overview

spider-hoarder is an MCP server (`mcp-research`) that gives Claude per-user, isolated, real (headed) Chromium sessions, with a
human handoff for CAPTCHAs/logins, optional Tor egress, and fetch/parse/store tools that return Markdown. It knows nothing about
any downstream graph or database: the agent is the orchestrator and carries text from here to wherever it belongs
(see the original framing in [Plan: Agentic research MCP](../plans/agentic-research-mcp.md), whose "independent MCP servers,
no proxying" shape still holds; its layer-access-control / `ingest_text` parts live in another project).

See also: [Plan: Browser sessions](../implemented-plans/browser-sessions-plan.md)

The same stack also serves Android emulators (`/android-clear/mcp`, `/android-tor/mcp`): [Android emulators](android.md).
See also: [Plan: Android hoarder](../implemented-plans/android-hoarder.md), [Plan: Android KVM bring-up](../plans/android-kvm-bringup.md)

## Components (Docker Swarm stack)

```
Claude ──Bearer JWT──▶ nginx ─▶ research-tor ──▶ tor ──▶ internet      /tor/mcp
                         │  └─▶ research-clear ───────▶ internet       /clear/mcp
Human (live view) ─SSO─▶ nginx ─▶ oauth2-proxy ─▶ Keycloak (external OIDC)

research-<egress> container (1..N replicas):
  gw (uid 1000, unprivileged)         sessiond (root, tiny)
   ├ MCP server, JWT auth              ├ owns the uid pool and its invariants
   ├ session registry, reaper          ├ start(uid): create home, spawn session group as uid
   ├ proxies tool calls  ────────────▶ ├ stop(uid): kill, wipe, verify
   ├ extract / storage / blobs / uploads
   └ live-view authz (/authz, /start)  └ /run/sessiond.sock (gateway only)
  per session (uid 20000+n):  Xvfb ─ x11vnc ─ websockify ─ toolserver (Python MCP) ─ Chromium
```

| Service | Role | Source |
|---|---|---|
| `nginx` | Only published port. Routes by egress (`/tor`, `/clear`) and by replica id; SSO for the live view. TLS ends at the load balancer in front of it. | `nginx.conf.template` |
| `oauth2-proxy` | Human SSO for the live view only (MCP uses bearer JWTs verified by the gateway). Registers its own OIDC client. | `oauth2-proxy/` |
| `research-tor`, `research-clear` | The gateway + `sessiond` + per-session browsers; one image, `EGRESS` selects the policy. Shared by all users, **not** one container per session. | `mcp-research/` |
| `tor` | A single Tor daemon with a pool of SocksPorts (and, for the Android slots, a transparent proxy: root entrypoint with `NET_ADMIN` for the redirect rules, tor itself runs as `debian-tor` with no caps). | `tor/` |
| `research-android-clear`, `research-android-tor`, `android-wrapper`, slots | Optional Android emulators: the same gateway image in `MODE=android`, a wrapper that makes the slot containers, one emulator per slot. See [Android emulators](android.md). | `android-*`, `mcp-research/slots.py` |
| Keycloak | External OIDC provider (prod); a throwaway one in `--dev`. | `keycloak/setup.sh` |

### Why one container per egress, not per session
No Docker socket and no container creation: a session is a Unix uid plus a process group inside the container. Parsing
(trafilatura/markitdown), preflight/downloads (`requests`) and blob storage stay in the same service because they are
stateless library calls and must use the same egress as the browser (`socks5h://tor` in the Tor instance).
See also: [Plan: Browser sessions](../implemented-plans/browser-sessions-plan.md) (supersedes the container-per-session design of
[Plan: Newsroom browsers](../plans/newsroom-browsers-plan.md); its Keycloak/Tor background is still referenced).

## Gateway and the tool server

The gateway (`app.py`, `sessions.py`) speaks MCP to Claude (low-level `mcp` SDK, because the tool list is dynamic: proxied from the
session's tool server) and proxies browser tool calls over loopback HTTP to that session's tool server on port `9000+n`
(`Host: mcp-upstream` is required). It adds its own tools (`browser_request_human_help`, `browser_save_page`,
`browser_end_session`, `ingest_page`, file tools) and turns every bulk output into a *job*
([Jobs and storage](jobs-and-storage.md)). Browser internals: [Browser tool server](browser-toolserver.md).

Session lifecycle: `initialize` reserves a uid, the browser starts lazily on the first tool call, and every ending path
(`browser_end_session`, idle timeout `IDLE_TIMEOUT`, MCP session close, MCP client silent for `DISCONNECT_TIMEOUT_MINUTES`,
crash) goes through one `close()` → `sessiond stop`. Limits: `MAX_SESSIONS_PER_USER`, `MAX_SESSIONS_TOTAL` (per container).
See [Session isolation](session-isolation.md).

## Request routing and replicas
See [Scaling and routing](scaling.md). Authentication: [Authentication and live view](auth.md).

## Code map (`mcp-research/`)

| File | Responsibility |
|---|---|
| `app.py` | MCP server, tool schemas/descriptions, job results, blob/upload/live-view routes, `/authz`, replica session-id middleware |
| `sessions.py` | Session registry, reservations, reaper, page HTML capture, `fetch_html` |
| `pool.py`, `sessiond.py`, `ports.py` | uid pool (gateway side / root side) and the port/uid layout derived from pool size and replica slot |
| `slots.py`, `android-tools.json` | `MODE=android`: the replica's slot slice and the pool backend that talks to the slots; static tool list ([Android emulators](android.md)) |
| `session-group.sh`, `entrypoint.sh` | per-session process group; container start (firewall, sessiond, gateway; with `MODE=android` just the unprivileged gateway, restarted on exit) |
| `toolserver/` | Python MCP server driving Chromium ([details](browser-toolserver.md)) |
| `extract.py`, `storage.py`, `blobtoken.py` | Markdown conversion + terse outline + grep, fsspec storage, HMAC links |
| `fetch.py`, `netguard.py`, `filter_proxy.py`, `cmp_rules.py`, `consent.js` | non-HTML downloads, SSRF guard, Tor SOCKS filter relay, consent-banner handling |
| `auth.py` | Keycloak JWT verification |
