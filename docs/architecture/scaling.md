# Scaling and routing (sticky replicas)

`docker service scale spider-hoarder_browser-clear-gateway=N` (or `BROWSER_CLEAR_REPLICAS` etc.) just works. New MCP sessions spread over replicas;
everything belonging to a session sticks to the replica that created it.
See also: [Plan: Scale research containers](../implemented-plans/research-replicas.md), [Plan: Pluggable backends](../implemented-plans/backend-plugins.md)

Decisions: nginx routes (replaces Caddy; TLS already ends at the LB); round-robin with try-next-if-full (no capacity awareness);
files and jobs stay per replica; Swarm only (`tasks.<service>` DNS, `{{.Task.Slot}}`); replica identity = the task's overlay IP.

- **Replica id (`rid`)**: the replica's IP on the sessions network, dashes instead of dots (`10-20-1-7`). It prefixes the MCP session id
  (ASGI middleware: `Mcp-Session-Id: <rid>.<sdk-id>`, the SDK never sees it) and appears in `/view/<rid>/…`: both are tied to one replica's
  browser processes. nginx extracts it and proxies straight to that IP on `GATEWAY_PORT`. Job ids and `/blob/…` / `/upload/…` links carry no `rid`:
  nginx sends them to any replica (`tasks.<backend>-gateway`), which serves them from the shared bucket.
- **`initialize`** has no session id → round-robin over `tasks.<prefix>-gateway` (prefix = the backend name with dashes, e.g. `browser-tor`; nginx captures any `[a-z0-9-]+`, so there is no backend list in the config and an unknown or disabled prefix does not resolve: 502); a replica reserves a uid at `initialize` and returns
  503 when full; nginx tries the next one; all full → 503 to Claude. The browser itself still starts lazily on the first tool call.
- **SSRF guard** (the `rid` in a URL is attacker-controlled): nginx is only on `auth` and the session networks each backend's overlay adds to it (`browser-tor-sessions`, `android-clear-slots`, ...), never on
  `public`, and connects only to the dedicated `GATEWAY_PORT`, where the gateway enforces its own auth. Residual: blind probing.
- **Tor**: `browser_tor` has its own daemon (`browser-tor-daemon`; `android_tor` has another, nothing is shared); replica slot `k` uses SocksPorts `9050 + (k-1)*SOCKS_STRIDE + index`; the per-uid firewall allows only
  that range. `MAX_REPLICAS` (default 4) sizes the range; scaling past it needs a larger value and a redeploy of the daemon.
- **I2P**: one `i2pd` per `browser-i2p-gateway` replica (`BROWSER_I2P_REPLICAS` sets both counts). Slot `k` finds its router as `browser-i2p-router-k`
  (`hostname: "browser-i2p-router-{{.Task.Slot}}"`, overlay DNS, no `docker.sock`); ports are `I2P_SOCKS_BASE + index`. The per-uid firewall pins the
  router's IP; if `browser-i2p-router-k` gets a new IP the replica exits and Swarm restarts it, rebuilding the rules (sessions on it are lost).
  See also: [Plan: I2P egress](../implemented-plans/i2p-egress.md)
- **Consequences**: a restarted/rescheduled replica loses its sessions and live-view links (new IP) but not its jobs or blob / upload
  links: jobs live in the shared bucket (`RESEARCH_BLOB_URL`: GCS, S3 or the stack's SeaweedFS; no local-disk store, replicas would not share it). nginx has no node-local state: `deploy.sh` builds `spider-hoarder-nginx` with `nginx.conf.template`, the aliases map and the landing page baked in (`nginx/Dockerfile`), so it runs on any node.
  See also: [Plan: project access model](../implemented-plans/project-access-model.md)

## Android replicas
`android-clear-gateway` / `android-tor-gateway` use the same `rid` routing (`/android-<egress>/…`, `tasks.android-<egress>-gateway`).
Replica `REPLICA_SLOT=s` owns slots `(s-1)*ANDROID_SLOTS_PER_REPLICA+1 ..`, so there is no shared slot allocator; the slot
containers themselves are made by the backend's own wrapper (`android-<egress>-wrapper`) on the KVM node (`ANDROID_*_SLOTS` = replicas × slots per replica,
derived by the backend's descriptor at deploy time: scale replicas by changing `.env` and redeploying, not with `docker service scale`).
See [Android emulators](android.md). See also: [Plan: Android hoarder](../implemented-plans/android-hoarder.md), [Plan: Android KVM bring-up](../implemented-plans/android-kvm-bringup.md)

Capacity is bounded by the KVM node, not by Swarm: each slot is a 2-CPU / 6 GB container and the golden-snapshot restore is disk-bound (measured on an 8 vCPU / 32 GB host: reset 44-62 s, ~5.4 GB peak per 3 GB-guest slot; see [Android emulators](android.md#image-and-golden-snapshot)). The android gateways wait `ANDROID_RESET_TIMEOUT` for a reset, so raise it on slow disks.

## Parser replicas
The parser service is shared and scales by replicas (`PARSE_REPLICAS`, `docker service scale spider-hoarder_parser=N`): each replica has its own uid pool (`PARSE_POOL_SIZE`) and
answers **503** when full (the gateway retries once, then notes `not parsed`); the swarm VIP in front of it is enough. A `POST` lands on any replica; status reads
land on any replica too: the owner answers from memory, the others read `status.json` from the store with the credential in the request (a heartbeat older than 30 s
reads as "wrapper lost"). A tree of files is parsed by one parser, so wall time per job does not shrink with replicas; throughput does. [Plan: parser wrapper](../implemented-plans/parser-wrapper.md)

## Multi-node swarm

All opt-in through `.env`; unset keeps the single-node behaviour (`:local` images, no constraints).

- **Registry**: `IMAGE_PREFIX` (e.g. `localhost:5000/`) makes every locally built image `${IMAGE_PREFIX}<name>:${IMAGE_TAG}` (tag defaults to the git revision), pushed after the build; deploys then resolve images and skip the forced restart.
- **Pinning**: `PIN_DATA` (e.g. `node.labels.storage == primary`) constrains the services with node-local state: base `oauth2-proxy` (its DCR registration volume), and each backend's descriptor `pinned` services (none by default: the Tor/I2P daemons only cache, they cold-start fine elsewhere). `deploy.sh` generates `.build/docker-compose.pin.yml` only when it is set. Gateway replicas stay schedulable anywhere (routing by task IP over the overlay; blobs are lost on reschedule, as before).
- **Android**: wrappers go to the nodes matching `PIN_KVM`, one per node (`ANDROID_KVM_NODES`); see [Android](android.md).

See also: [Plan: Multi-node swarm support](../implemented-plans/multi-node.md)
