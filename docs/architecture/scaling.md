# Scaling and routing (sticky replicas)

`docker service scale spider-hoarder_research-clear=N` (or `RESEARCH_*_REPLICAS`) just works. New MCP sessions spread over replicas;
everything belonging to a session sticks to the replica that created it.
See also: [Plan: Scale research containers](../implemented-plans/research-replicas.md)

Decisions: nginx routes (replaces Caddy; TLS already ends at the LB); round-robin with try-next-if-full (no capacity awareness);
files and jobs stay per replica; Swarm only (`tasks.<service>` DNS, `{{.Task.Slot}}`); replica identity = the task's overlay IP.

- **Replica id (`rid`)**: the replica's IP on the sessions network, dashes instead of dots (`10-20-1-7`). It prefixes the MCP session id
  (ASGI middleware: `Mcp-Session-Id: <rid>.<sdk-id>`, the SDK never sees it), the `job_id` (`<rid>.<uuid>`), and appears in
  `/blob/<rid>/…`, `/upload/<rid>/…`, `/view/<rid>/…`. nginx extracts it and proxies straight to that IP on `GATEWAY_PORT`.
- **`initialize`** has no session id → round-robin over `tasks.research-<egress>`; a replica reserves a uid at `initialize` and returns
  503 when full; nginx tries the next one; all full → 503 to Claude. The browser itself still starts lazily on the first tool call.
- **SSRF guard** (the `rid` in a URL is attacker-controlled): nginx is only on `sessions_tor`, `sessions_clear`, `sessions_i2p` and `auth`, never on
  `public`, and connects only to the dedicated `GATEWAY_PORT`, where the gateway enforces its own auth. Residual: blind probing.
- **Tor**: one shared `tor`; replica slot `k` uses SocksPorts `9050 + (k-1)*SOCKS_STRIDE + index`; the per-uid firewall allows only
  that range. `MAX_REPLICAS` (default 4) sizes the range; scaling past it needs a larger value and a `tor` redeploy.
- **I2P**: one `i2pd` per `research-i2p` replica (`RESEARCH_I2P_REPLICAS` sets both counts). Slot `k` finds its router as `i2pd-k`
  (`hostname: "i2pd-{{.Task.Slot}}"`, overlay DNS, no `docker.sock`); ports are `I2P_SOCKS_BASE + index`. The per-uid firewall pins the
  router's IP; if `i2pd-k` gets a new IP the replica exits and Swarm restarts it, rebuilding the rules (sessions on it are lost).
  See also: [Plan: I2P egress](../implemented-plans/i2p-egress.md)
- **Consequences**: a `job_id` from another replica is not readable ("lives on another replica"); a restarted/rescheduled replica loses
  its sessions and links (new IP); `nginx.conf.template` is bind-mounted (single node; use a Swarm `config:` otherwise).
  Upgrade path for shared files: shared blob storage (`RESEARCH_BLOB_URL` is already a URL).

## Android replicas
`research-android-clear` / `research-android-tor` use the same `rid` routing (`/android-<egress>/…`, `tasks.research-android-<egress>`).
Replica `REPLICA_SLOT=s` owns slots `(s-1)*ANDROID_SLOTS_PER_REPLICA+1 ..`, so there is no shared slot allocator; the slot
containers themselves are made by the single `android-wrapper` on the KVM node (`ANDROID_*_SLOTS` = replicas × slots per replica,
computed by `deploy.sh`: scale replicas by changing `.env` and redeploying, not with `docker service scale`).
See [Android emulators](android.md). See also: [Plan: Android hoarder](../implemented-plans/android-hoarder.md), [Plan: Android KVM bring-up](../implemented-plans/android-kvm-bringup.md)

Capacity is bounded by the KVM node, not by Swarm: each slot is a 2-CPU / 6 GB container and the golden-snapshot restore is disk-bound (measured on an 8 vCPU / 32 GB host: reset 44-62 s, ~5.4 GB peak per 3 GB-guest slot; see [Android emulators](android.md#image-and-golden-snapshot)). The android gateways wait `ANDROID_RESET_TIMEOUT` for a reset, so raise it on slow disks.
