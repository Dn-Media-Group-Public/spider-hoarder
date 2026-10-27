# Plan: I2P egress (`/i2p/mcp`)

## Problem / Goal

Add I2P as a third egress next to Tor and clear: `/i2p/mcp`, browsers and all fetch tools reach **only `*.i2p`** (eepsites, `.b32.i2p`)
through an I2P router. No outproxy; clearnet stays on `/clear` and `/tor`. Same tool surface, same session model, same per-session
isolation as far as I2P allows.

## Decisions (already made)

- **Router:** C++ `i2pd`. Chromium cannot do SOCKS auth, so isolation is by port, as with Tor.
- **Isolation model:** one i2pd per `research-i2p` replica, one `type=socks` section with `keys=transient` per session slot
  (= its own destination, tunnel pool and leaseset). `tunnels.conf` is static, generated at start: SIGHUP reload recreates *all*
  client tunnels with new destinations (tested), so dynamic add/remove is out.
- **Known limitation (document it):** sessions on one replica share the router identity, exploratory tunnels and transports. A
  router-level observer, or a hop that sees several of the pools, could correlate sessions. Upgrade path: one router per session slot
  (needs the seed snapshot, below, to be practical).
- **No outproxy:** i2pd answers clearnet hostnames and raw IPs with SOCKS reply 0x08 (tested). `filter_proxy.py` additionally enforces
  `*.i2p`-only as defence in depth. `fetch.py` and all other tools in `research-i2p` use only the session's i2p SOCKS port (`socks5h`).
- **Pairing without `docker.sock`:** `research-i2p` slot `k` talks to router `i2pd-k` through a templated service hostname
  (`hostname: "i2pd-{{.Task.Slot}}"`), resolved by overlay DNS. Tested on a single-node Swarm: resolves to the right replica and
  follows IP changes after a forced update; templated network aliases and `<service>.<slot>` names do **not** work. Multi-node is
  expected (overlay DNS only) but unproven: see spike 1. One `RESEARCH_I2P_REPLICAS` sets both service counts.
- **Internet access only for routers:** gateway and browsers are on `sessions_i2p` (`internal: true`) and reach nothing but `i2pd-k`.
  The gateway is also on `public` for the IdP, as `research-tor` is; session uids are firewalled to their own SOCKS port.
- **Seed snapshot over HTTP:** a singleton `i2pd-seed` keeps a warm netDb and serves `snapshot.tgz` with nginx on the internal
  network. Each `i2pd-k` fetches it at start, falling back to the public reseed if the seed is down. No shared volume, multi-node safe.
- **Tunnel parameters:** `I2P_TUNNEL_QUANTITY=2` (i2pd's default 5 in/5 out per destination times `POOL_SIZE` destinations is a lot of
  idle build traffic), `I2P_TUNNEL_LENGTH=3` (i2pd's default; shorter weakens anonymity).
- **Readiness is a real probe**, not console metrics.

## Design

### Components

| Service | Role | Networks |
|---|---|---|
| `research-i2p` | gateway + `sessiond` + browsers, `EGRESS=i2p`, replicas `${RESEARCH_I2P_REPLICAS:-1}` | `sessions_i2p`, `public` |
| `i2pd` | one router per replica, `hostname: "i2pd-{{.Task.Slot}}"`, same replica count, stateless | `i2p_egress`, `sessions_i2p` |
| `i2pd-seed` | singleton, warm router + nginx serving the snapshot, volume `i2pd_seed_data`, SOCKS disabled | `i2p_egress` |
| `nginx` | adds `/i2p/...` routes, joins `sessions_i2p` | + `sessions_i2p` |

Networks: `sessions_i2p` overlay `internal: true`; `i2p_egress` overlay (internet).

### `i2pd` router container

- Entrypoint: fetch `http://i2pd-seed/snapshot.tgz` into a fresh datadir (best effort, short timeout), unpack, start i2pd. Keep the
  default reseed enabled as the fallback (reseed only triggers below `reseed.threshold`, default 25 routers).
- Generate `tunnels.conf`: `POOL_SIZE` sections (same `POOL_SIZE` as `research-tor`):
  ```
  [socks-<index>]
  type = socks
  address = 0.0.0.0
  port = <I2P_SOCKS_BASE + index>
  keys = transient
  inbound.quantity = ${I2P_TUNNEL_QUANTITY}   outbound.quantity = ${I2P_TUNNEL_QUANTITY}
  inbound.length = ${I2P_TUNNEL_LENGTH}       outbound.length = ${I2P_TUNNEL_LENGTH}
  ```
  Built-in `[socksproxy]`, `httpproxy`, `sam` disabled; `ipv6=false`; no `outproxy=` anywhere. Config file is read from the datadir.
- Ports are `I2P_SOCKS_BASE + index` (no `SOCKS_STRIDE`: a router serves exactly one replica).
- Reachable only from `sessions_i2p` / `i2p_egress`; nothing is published.

### `i2pd-seed`

- i2pd + nginx in one container; the entrypoint starts both.
- Every `I2P_SNAPSHOT_INTERVAL` (default 1800 s) it packs `netDb/` and `addressbook/addresses.csv` into a temp file and renames it
  onto `snapshot.tgz` (atomic swap). Default is a copy of the live netDb; a clean stop-copy is the fallback if spike 2 finds corruption.
- **Never in the snapshot:** `router.keys`, `router.info`, `ntcp2.keys`, `ssu2.keys`, `destinations/`, `peerProfiles/`, `tags/`, logs.
- Why `addresses.csv`: a snapshot alone does not fix name resolution (names resolved only after ~3 min, when the addressbook import
  finished); a pre-seeded `addresses.csv` brought it to ~80 s, level with `.b32.i2p`.
- Test results (a few runs, arm64, i2pd 3.0.0): snapshot-seeded first SOCKS CONNECT ~32-36 s (one run 134 s) vs 74-142 s control;
  `[reseed] threshold = 0` bootstraps fine from the snapshot alone.

### `research-i2p` (`EGRESS=i2p`)

- `ports.py`: `socks_port(index) = I2P_SOCKS_BASE + index`; host is `i2pd-${REPLICA_SLOT}`.
- `entrypoint.sh` (mirrors the Tor branch): wait for `getent ahostsv4 i2pd-$REPLICA_SLOT`, then per session uid ACCEPT only
  `<that IP>:<socks port>`, REJECT everything else, IPv6 rejected. `NET_ADMIN` is dropped afterwards as today.
- **Router IP change = exit.** A small supervisor loop re-resolves `i2pd-<slot>` periodically; if the IP differs from the one in the
  firewall rules it exits non-zero, Swarm restarts the replica, the entrypoint rebuilds the rules. (Alternatives rejected: keep
  `NET_ADMIN` in a helper; allow the subnet, which would let a session use another replica's router.) Sessions on the replica are lost,
  as they are on any replica restart.
- `fetch.py`: `socks5h://i2pd-<slot>:<port>` for all downloads; no direct path.
- `filter_proxy.py`: runs between Chromium and the session's port as on Tor; adds a `*.i2p`-only rule (reject anything else with a clean error).
- Chromium: same flags as Tor (`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`); HTTPS-first upgrades off for `.i2p`; longer
  navigation/proxy timeouts (first connect to an eepsite can take tens of seconds, a new destination about a minute).
- **Readiness:** before a session's first browser/fetch use, `sessiond` probes with a `socks5h` CONNECT to a known `.b32.i2p` through
  the session's port (retry with backoff, bounded). Until it succeeds, tools return a clear "router/tunnels not ready, retry" error
  instead of letting Chromium hang. Tool descriptions tell the model the first request on a fresh session can take a minute.
- Fingerprinting caveats are those of Chromium over Tor ("not Tor Browser"); sessions on one build are linkable by fingerprint regardless of tunnel isolation.

### nginx

`nginx.conf.template`: egress capture `(tor|clear|i2p)` for `/mcp`, `/blob`, `/upload`, `/view`, and the OAuth protected-resource path;
upstream `tasks.research-i2p`; join `sessions_i2p`.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RESEARCH_I2P_REPLICAS` | 1 | replicas of both `research-i2p` and `i2pd` |
| `I2P_SOCKS_BASE` | (pick in implementation, outside Tor's range) | first SOCKS port on each router |
| `I2P_TUNNEL_QUANTITY` | 2 | in/out tunnels per session destination |
| `I2P_TUNNEL_LENGTH` | 3 | hops per tunnel |
| `I2P_SNAPSHOT_INTERVAL` | 1800 | seconds between snapshots on the seed |

## Implementation order

1. `i2pd/` and `i2pd-seed/` images (entrypoints, `tunnels.conf` generation, snapshot builder, nginx config), built by `deploy.sh`.
2. `docker-compose.yml`: networks, `i2pd`, `i2pd-seed`, `research-i2p`, volume; `.env.example`.
3. `mcp-research`: `EGRESS=i2p` in `entrypoint.sh` (firewall, IP-change exit), `ports.py`, `fetch.py`, `filter_proxy.py`, `sessiond` readiness probe, Chromium flags.
4. `nginx.conf.template`: `/i2p` routes, network.
5. Spikes (below), adjust defaults, then docs.

## Spikes / verify before relying on it

1. Templated-hostname pairing on a **real multi-node** Swarm (including scale-down then up, and the brief window while a task restarts).
2. A live copy of the seed's netDb: atomic swap yields a usable snapshot (no half-written entries); otherwise switch to stop-copy.
3. Snapshot staleness: bootstrap from snapshots hours and days old.
4. Idle tunnel-build traffic and CPU at `POOL_SIZE` destinations with quantity 2 (per-destination memory was only ~0.3 MB); whether `i2cp.closeIdleTime` / reduce-on-idle apply to `type=socks` sections.
5. Time from router start to the first successful probe for a fresh session destination.
6. Tests: clearnet host and raw IP through `research-i2p` fail closed (browser and `fetch`); a session cannot reach another slot's port or router; router restart makes the replica exit and come back with working rules.

## Files to change

| File | Change |
|---|---|
| `docker-compose.yml` | `research-i2p`, `i2pd`, `i2pd-seed`, `sessions_i2p`, `i2p_egress`, `i2pd_seed_data` |
| `nginx.conf.template` | `(tor\|clear\|i2p)` routing, `sessions_i2p` |
| `mcp-research/entrypoint.sh` | `EGRESS=i2p` firewall, router resolution, IP-change exit |
| `mcp-research/` ports, `fetch.py`, `filter_proxy.py`, sessiond, Chromium launch | I2P egress, `*.i2p`-only, readiness probe |
| `i2pd/` (new) | image, entrypoint, `tunnels.conf` generator |
| `i2pd-seed/` (new) | image, i2pd + nginx, snapshot builder |
| `deploy.sh`, `.env.example` | build new images, new variables |

## Docs to update

- `docs/architecture/overview.md`: components and diagram.
- `docs/architecture/session-isolation.md`: I2P egress/isolation section with the **shared router identity limitation**.
- `docs/architecture/scaling.md`: slot pairing via templated hostname; exit on router IP change.
- `docs/operations/deploy-and-configure.md`: new variables.
- `docs/api/tools.md`: `/i2p/mcp`, first-request latency note.
- `docs/index.md`, `docs/plans/index.md`; on implementation move this plan to `docs/implemented-plans/` and update its index. Each updated section gets a "See also" link to the plan.
