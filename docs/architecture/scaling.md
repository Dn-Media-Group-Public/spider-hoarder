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
- **SSRF guard** (the `rid` in a URL is attacker-controlled): nginx is only on `sessions_tor`, `sessions_clear` and `auth`, never on
  `public`, and connects only to the dedicated `GATEWAY_PORT`, where the gateway enforces its own auth. Residual: blind probing.
- **Tor**: one shared `tor`; replica slot `k` uses SocksPorts `9050 + (k-1)*SOCKS_STRIDE + index`; the per-uid firewall allows only
  that range. `MAX_REPLICAS` (default 4) sizes the range; scaling past it needs a larger value and a `tor` redeploy.
- **Consequences**: a `job_id` from another replica is not readable ("lives on another replica"); a restarted/rescheduled replica loses
  its sessions and links (new IP); `nginx.conf.template` is bind-mounted (single node; use a Swarm `config:` otherwise).
  Upgrade path for shared files: shared blob storage (`RESEARCH_BLOB_URL` is already a URL).
