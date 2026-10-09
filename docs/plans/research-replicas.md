# Plan: Scale `research-*` containers (sticky replicas on Swarm)

Target is **`docker stack deploy` on Docker Swarm**, in prod and in dev
(`./deploy.sh [--dev]`). Nothing here assumes docker-compose.

## Problem / Goal

`research-tor` and `research-clear` each run as exactly one container
(`deploy.replicas: 1`), capped at `MAX_SESSIONS_TOTAL` (18) browser sessions.
More users need more containers.

Goal: `docker service scale spider-hoarder_research-clear=N` just works.
New MCP sessions are spread round-robin over the replicas; once a session
exists, **everything that belongs to it sticks to the replica that created it**
(MCP calls, live view, job ids, download and upload links).

Non-goals: moving sessions between replicas, shared blob storage, surviving a
replica loss (its sessions die, same as a restart today), capacity-aware
placement beyond "try the next replica if full".

## Decisions (already made)

- **A. nginx routes.** It replaces Caddy (TLS already ends at the Google LB;
  `auto_https` was off, so Caddy's main feature is unused).
- **B. Round-robin, try-next-if-full.** No capacity awareness in the router.
- **C. Files and jobs stay per replica.** Stickiness is encoded in the **URL
  path** (external systems only see the URL, never headers) and in `job_id`.
- **D. Swarm only.** Replica discovery uses Swarm DNS (`tasks.<service>`),
  per-task env uses `{{.Task.Slot}}` (verified to be expanded per task by
  `docker stack deploy`).
- **E. Replica identity = the task's overlay IP**, encoded in ids and URLs;
  nginx extracts it and proxies straight to that IP. No discovery table.
- **F. Tor: one shared `tor` service, SocksPort range offset by task slot.**
  `tor` is not scaled with the research service.
- **H. A uid is reserved at `initialize`, the browser is started lazily** on the
  first tool call (as today).

## Design

### Replica id (`rid`)

At startup a replica computes its own IP **on the sessions network** (the one
nginx shares with it, not `public`): resolve the `nginx` service name, connect a
UDP socket to that address, `getsockname()`. `rid` = that IP with dashes,
e.g. `10-20-1-7`. It never changes while the task lives; a rescheduled task may
get a new IP (old ids then fail cleanly, see below).

`rid` is added in four places:

| What | Form |
|---|---|
| MCP session id (`Mcp-Session-Id` header) | `<rid>.<sdk-id>` |
| `job_id` returned to the agent | `<rid>.<uuid>` |
| Download link | `{BASE}/blob/<rid>/<token>` |
| Upload link | `{BASE}/upload/<rid>/<token>` |
| Live-view link | `{BASE}/view/<rid>/<view_id>/start` (and `/vnc.html`, `/websockify` under it) |

Storage keys keep using the bare uuid (`<owner>/<uuid>`); the prefix is only
checked and stripped at the edge. A `job_id` whose `rid` is not this replica's
gets a clear tool error ("job lives on another replica and is not reachable from
this session"), which is the accepted cost of per-replica storage (decision C).

**MCP session id: ASGI middleware, not an SDK hook.** A small middleware around
the streamable-HTTP app prefixes `<rid>.` on the `Mcp-Session-Id` response
header and strips it from request headers, so the SDK never sees the prefix and
we do not depend on SDK internals. (Verify against the pinned `mcp` version.)

### nginx (replaces Caddy)

Networks: `sessions_tor`, `sessions_clear` and a new `auth` network shared only
with `oauth2-proxy`. nginx is **not** on `public` (see SSRF guard); the published
port 80 uses the Swarm routing mesh and needs no network.
Config is a checked-in `nginx.conf.template`, bind-mounted like `Caddyfile` today into
`/etc/nginx/templates/` (the official image runs `envsubst`, so `${GATEWAY_PORT}` is
filled in from the service env; nginx's own `$vars` are left alone).
`resolver 127.0.0.11 valid=5s;` and variable `proxy_pass`, so Swarm DNS changes
are picked up without reload.

Per egress `E` in {`tor`,`clear`}:

- **`/E/mcp*`**
  - Request **without** `Mcp-Session-Id` (the `initialize`): `proxy_pass` to
    `http://tasks.research-E:${GATEWAY_PORT}`. Swarm DNS returns one A record per task;
    nginx round-robins over them, and
    `proxy_next_upstream error timeout http_503 non_idempotent;
    proxy_next_upstream_tries 0;` makes it try the next task on a 503. A 503
    means "full", and the gateway has done nothing (no side effects), so the
    retry is safe. If every replica is full, the final 503 reaches Claude.
  - Request **with** `Mcp-Session-Id`: a `map` extracts `rid` from the
    `<rid>.` prefix, turns dashes into dots and proxies to `http://<ip>:${GATEWAY_PORT}`.
    Malformed header (not four dash-separated numbers): 400.
  - `proxy_buffering off; proxy_read_timeout 1h;` (SSE), `proxy_http_version 1.1`.
- **`/.well-known/oauth-protected-resource/E/mcp`**: any replica (same as the
  no-session case).
- **`/E/blob/<rid>/*`, `/E/upload/<rid>/*`**: extract `rid`, proxy to that IP.
- **`/E/view/<rid>/<id>/start`**: SSO (`auth_request` to oauth2-proxy, 401 ->
  redirect to `/oauth2/start?rd=`), `X-Auth-Request-Email` set from the
  oauth2-proxy response and any client-supplied value dropped, then proxy to
  `<rid>`.
- **`/E/view/<rid>/<id>/*`** (noVNC + websocket): SSO as above, then
  `auth_request` to `http://<rid ip>:${GATEWAY_PORT}/authz` (with `Upgrade`/`Connection`
  stripped from the subrequest), `auth_request_set $upstream
  $upstream_http_x_upstream` (client-supplied `X-Upstream` is never read; only
  the auth response's value is used), strip the `/E/view/<rid>/<id>` prefix and
  proxy (websocket upgrade headers on) to `$upstream`.
- `/oauth2/*` -> oauth2-proxy, `/healthz` -> 200, `/` -> any `research-clear`
  replica (as the Caddyfile does today), everything else 404.

**SSRF guard (the `rid` in the URL is attacker-controlled).** nginx connects to
any IP an anonymous caller writes into a blob/upload/view URL, so two layers
limit what it can reach (no subnet pinning, no label/DNS membership check:
nginx cannot query Docker or swarm labels):

1. **Topology.** nginx is only on `sessions_tor`, `sessions_clear` and `auth`.
   The sessions networks hold only research replicas, nginx and `tor`; `auth`
   holds only `oauth2-proxy`. The IdP, the dev Keycloak and everything else on
   `public` are unreachable by IP. `oauth2-proxy` joins `auth` and keeps
   `public` for the IdP.
2. **Fixed, dedicated port.** nginx always connects to `GATEWAY_PORT`
   (`.env`; `.env.example` sets **7298**; no default in code, startup fails if
   unset), which nothing but the research gateway listens on. Whatever answers
   is a gateway and enforces its own auth (JWT, signed tokens, owner checks).

Residual: blind probing (error vs timeout tells a caller which IPs on those
three networks are alive on that one port). Accepted. The websocket `$upstream`
still comes only from the gateway's `/authz` answer, never from the client.

### Gateway (`mcp-research`)

- **Reserve at `initialize`.** The middleware that sees an `initialize` (POST
  without `Mcp-Session-Id`) calls a new `sessions.reserve(owner)`: checks
  `MAX_SESSIONS_PER_USER` and capacity, and `pool.take()`s a uid, or returns
  **503** `{"error":"full"}`. The reservation is keyed by the MCP session id
  (header value); `get_session` on the first tool call finds it and starts the
  browser on the reserved uid instead of taking one. Reservations count against
  capacity (so "full" is accurate), are released on MCP session close
  (`DELETE`), and by the existing reaper after `IDLE_TIMEOUT` if no browser was
  ever started. The existing "capacity reached" error in `get_session` can no
  longer trigger for a reserved session and is removed.
- **Tool calls on an unreserved session** (reconnect after the reservation was
  reaped, gateway restart): treated as an unknown session; the client
  re-initializes (existing MCP behaviour for unknown session ids).
- **`/authz`** returns `X-Upstream: <own sessions-net IP>:<ws_port>` instead of
  `research-<egress>:<ws_port>` (the service name would load-balance over
  replicas).
- **Routes** become `/{EGRESS}/blob/{rid}/{token}`,
  `/{EGRESS}/upload/{rid}/{token}`, `/{EGRESS}/view/{rid}/{view_id}/...`;
  `VIEW_RE`, `BASE`-built links and `_key`'s id check change accordingly. A
  request whose `rid` is not this replica's gets 404 (defence in depth; nginx
  already routed it here by `rid`).
- Everything else (per-uid isolation, `sessiond`, wipe/verify, reaper) is
  per container and unchanged; each replica has its own uid pool, `/home`, `/tmp`.

### Tor: shared `tor`, SocksPorts offset by task slot (decision F)

Today `socks_port(uid) = 9050 + index(uid)` on a single `tor`; two replicas would
both hand port `9050+i` to different users and share circuits. Instead:

- Each research task gets `REPLICA_SLOT: "{{.Task.Slot}}"` in its `environment:`.
- `ports.py`: `SOCKS_STRIDE = POOL_SIZE + 1` (the +1 is the gateway's own port,
  as today); `socks_port(uid) = 9050 + (SLOT-1)*SOCKS_STRIDE + index(uid)` and
  `GATEWAY_SOCKS_PORT` likewise. `SLOT` defaults to 1 (single container, tests).
  `SLOT > MAX_REPLICAS` is a startup error (fail loudly, not silently reuse
  ports).
- `tor/entrypoint.sh` opens `MAX_REPLICAS * SOCKS_STRIDE` SocksPorts
  (`MAX_REPLICAS` env, default 4, set from `.env` for both services).
- `mcp-research/entrypoint.sh`: the per-uid firewall rule that allows
  `tor:9050+i` uses the same offset formula, so a session uid can only reach its
  own replica's range. (This script is shell; the offset is computed there from
  `REPLICA_SLOT`.)
- `tor` stays `replicas: 1`. Scaling `research-tor` past `MAX_REPLICAS` needs
  a redeploy of `tor` with a larger `MAX_REPLICAS`.
- The Tor ports exist only in the `tor` container, so they do not collide with
  the research container's `9100+` filter-relay ports.

### Swarm stack changes (`docker-compose.yml`, `deploy.sh`)

- Remove `caddy`; add `nginx` (image `nginx:1.27`, `ports: ["80:80"]`, bind
  mount `./nginx.conf.template:/etc/nginx/templates/default.conf.template:ro`, env `GATEWAY_PORT`, networks `auth`, `sessions_tor`, `sessions_clear` (not `public`)).
- `research-*`: `deploy.replicas: ${RESEARCH_TOR_REPLICAS:-1}` /
  `${RESEARCH_CLEAR_REPLICAS:-1}`; env `REPLICA_SLOT: "{{.Task.Slot}}"`,
  `MAX_REPLICAS`.
- `GATEWAY_PORT` from `.env` (required in `REQUIRED` in `deploy.sh`) replaces the
  hard-coded 8000 for the gateway listener; nginx gets it too.
- New `auth` overlay network (`oauth2-proxy`, `nginx`); `oauth2-proxy` stays on
  `public` as well (IdP access), nginx leaves `public`.
- `research_data` volume: unchanged. Replicas on the same node share the local
  volume (keys are uuids, so no collisions); jobs are still only reachable via
  their own replica's `rid`.
- `deploy.sh`: `caddy` -> `nginx` in the forced-update loop (so an edited
  `nginx.conf.template` and new images are picked up).
- Stack-deploy constraints already in force still hold (no `security_opt`, no
  `shm_size`, `build:` ignored).

## Implementation order

1. `ports.py` slot offset + `REPLICA_SLOT`/`MAX_REPLICAS` plumbing, `tor/entrypoint.sh`,
   `mcp-research/entrypoint.sh` firewall; tests (single replica must be
   byte-for-byte the same as today for `SLOT=1`).
2. Gateway: `rid` discovery, ASGI middleware (session id prefix + reserve at
   initialize + 503), job id / blob / upload / view routes carry `rid`,
   `/authz` upstream, reservation lifecycle in `sessions.py`.
3. `nginx.conf.template` and the stack file (nginx service, replica count,
   env); remove Caddy; update `deploy.sh`.
4. Tests on a real `docker stack deploy` with 2 replicas (below).
5. Docs.

## Files to change

| File | Change |
|---|---|
| `mcp-research/ports.py` | `SLOT`, `SOCKS_STRIDE`, offset `socks_port`/`GATEWAY_SOCKS_PORT` |
| `mcp-research/sessions.py` | `reserve()`, reservation lifecycle + reaper, drop lazy "capacity reached"; `rid` |
| `mcp-research/app.py` | session-id middleware, `rid`-prefixed job ids/URLs/routes, `/authz` upstream IP, `VIEW_RE`, `_key` id check |
| `mcp-research/entrypoint.sh` | firewall rule uses slot-offset Tor port; replica IP/`rid` if needed; gateway listens on `GATEWAY_PORT` |
| `tor/entrypoint.sh` | open `MAX_REPLICAS * SOCKS_STRIDE` SocksPorts |
| `nginx.conf.template` (new), `Caddyfile` (delete) | routing as above |
| `docker-compose.yml` | nginx service, replicas, `REPLICA_SLOT`, `MAX_REPLICAS`, `GATEWAY_PORT`, remove caddy |
| `docker-compose.dev.yml` | check for Caddy-specific bits (none known); same stack otherwise |
| `deploy.sh` | `caddy` -> `nginx`; export replica counts; require `GATEWAY_PORT` |
| `.env.example` | `GATEWAY_PORT=7298`, `RESEARCH_TOR_REPLICAS`, `RESEARCH_CLEAR_REPLICAS`, `MAX_REPLICAS` |
| `mcp-research/test_*.py` | new tests (below); existing tests keep passing |
| `README.md` | Caddy -> nginx, scaling instructions |

## Tests

- **Unit:** `ports.socks_port` for slots 1..N never overlaps across slots, and
  slot 1 equals today's value; `SLOT > MAX_REPLICAS` refuses to start;
  session-id middleware adds/strips the prefix and leaves the SDK unaware;
  `reserve()` honours per-user limit and capacity and returns 503; reservation
  released on DELETE and by the reaper; foreign-`rid` job id gives the clear
  error.
- **Stack test (real `docker stack deploy`, 2 replicas of `research-clear`,
  `MAX_SESSIONS_TOTAL` small):**
  - repeated `initialize` spreads over both replicas (ids carry both `rid`s);
  - fill replica 1, next `initialize` lands on replica 2 (try-next on 503); fill
    both: client gets 503;
  - tool calls with a given session id always reach the replica in its prefix;
  - blob/upload links from replica 1 fetched through nginx reach replica 1 only;
    the same token with replica 2's `rid` gets 404;
  - live view: owner OK through `auth_request`, non-owner 403, spoofed
    `X-Auth-Request-Email` and `X-Upstream` ignored, websocket upgrade works;
  - malformed `rid` gets 400; a well-formed `rid` for an IP that is not a replica
    gets a 502/504 from nginx and nothing else answers on `GATEWAY_PORT`
    (check that `oauth2-proxy` and `tor` do not listen on it), and nginx has no route to `public` (the IdP/dev Keycloak IP is not reachable from the nginx container).
- **Tor, 2 `research-tor` replicas:** each replica's sessions use disjoint
  SocksPort ranges; a session uid cannot connect to the other replica's range
  (firewall); `check.torproject.org` still says Tor.
- Existing `test_gateway.py`, `test_security.py`, `test_sessiond.py` pass.

## Risks and open points

- **Scaling `tor` itself** is out of scope; one `tor` handles
  `MAX_REPLICAS * 20` SocksPorts. Revisit if it becomes a bottleneck.
- **Rescheduled task = new IP**: its old session ids, job ids and links stop
  working (`rid` no longer routable, or hits a different task and 404s). Same
  blast radius as a replica restart today; documented, not mitigated.
- **Jobs from an older conversation on another replica are not readable** from a
  new session on this replica (decision C). If that proves painful, the
  upgrade path is shared blob storage (`RESEARCH_BLOB_URL` is already a URL).
- **Blind probing via `rid`**: see the SSRF guard; accepted.
- **Bind-mounted `nginx.conf.template`** only works on the node holding the file (same as
  `Caddyfile` today); a Swarm `config:` is the fix if nginx ever runs on other
  nodes.
- **MCP SDK session id**: the middleware approach assumes the SDK reads the
  session id only from the `Mcp-Session-Id` header and echoes it back in the
  response header; verify against the pinned version before relying on it.
- **Clients caching tool results across sessions** are unaffected, but links
  returned before the migration (no `rid`) stop working at deploy time; there is
  no compatibility shim (links are short-lived, 15 min / 1 h).

## Docs to update

There is no `docs/architecture/` or `docs/api/` yet; the affected docs are:

- `README.md`: stack description (nginx, replicas, how to scale).
- `docs/implemented-plans/browser-sessions-plan.md`: add "See also" links to
  this plan under Architecture, Tor, Tools and flows (live view / links), and
  Deployment.
- `docs/implemented-plans/file-outputs-as-jobs.md`: "See also" under the result
  shape (`job_id` now carries `rid`).
- On implementation: move this file to `docs/implemented-plans/research-replicas.md`
  and add it to `docs/implemented-plans/index.md`.
