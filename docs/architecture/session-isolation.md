# Session isolation, egress and Tor

Per-session isolation is **one Unix uid per session**, enforced by `sessiond`, plus a per-uid egress firewall.
See also: [Plan: Browser sessions](../implemented-plans/browser-sessions-plan.md)

## The uid lifecycle

The pool is `UID_BASE .. UID_BASE+N-1` (default 20000; N = `MAX_SESSIONS_TOTAL` + 2 spare for `ingest_page` and the tool-list
probe). Display number, VNC/websockify port and tool-server port are all derived from `uid - UID_BASE` (`ports.py`), so nothing else
is allocated or leaked.

**Invariant: a uid is in exactly one state: `free`, `busy(session)` or `quarantined`.** `sessiond` (the only root process: fixed
verbs over a gateway-only socket, no parsing, no network) is the authority:

1. `start(uid)` is refused unless the uid is free, no process owns that uid, and its homedir does not exist.
2. It creates `/home/s<uid>` (`0710`, group `gw-collect`) `out/` (`2750`) and `in/` (`2770`, owned by the session, group `gw-collect`), then forks a child that sets `umask 027`, drops
   groups, `setgid`/`setuid`, applies `RLIMIT_NPROC`/`NOFILE`, a clean environment (no gateway secrets), `setsid`, and execs
   the session group. `no-new-privileges` is set by the entrypoint (`setpriv`) because Swarm cannot set `security_opt`.
3. `stop(uid)`: SIGKILL the group → sweep with `kill(-1)` as that uid → wait until no process owns the uid → delete everything
   the uid owns (home, `/tmp`, `/var/tmp`, `/dev/shm`, `/run`, X sockets) → verify → mark free. Any failure **quarantines** the uid
   (never reused, capacity shrinks, logged loudly).
4. On gateway/sessiond start every pool uid is stopped (crash recovery; no state is trusted across restarts).

Anything that must outlive a session is copied by the gateway into blob storage before `stop`
([Jobs and storage](jobs-and-storage.md)); the homedir is disposable. Each session has a private gid, so group-readable `out/` files
only ever mean "the gateway" (member of `gw-collect`).
`in/` is the opposite channel, gateway to sandbox, used only for request bodies of `browser_network_request_make`: the gateway creates a
`0644` file with a random name through `O_PATH`/`O_NOFOLLOW`/`O_EXCL` (the session owns `in/` and could plant a symlink or swap the dir); the
home is not traversable by other sessions, and the file is deleted after use.
See also: [Plan: network request make](../implemented-plans/network-request-make.md)
See also: [Plan: Tool file outputs become jobs](../implemented-plans/file-outputs-as-jobs.md)

## What isolation does and does not give

Sessions cannot read each other's profile, X socket, `/proc/<pid>/environ` or `/dev/shm`, cannot signal each other, and cannot read
gateway or `sessiond` files. They share a kernel and a network namespace.

- **Loopback** is shared, so the per-uid firewall lets each session uid reach only its own x11vnc and filter-relay ports, the Docker
  resolver and established replies. A session cannot reach the gateway or another session's VNC/tool-server ports. VNC/websockify
  use a per-session random password; the tool server only accepts `Host: mcp-upstream`; secrets are 0600 files, never argv.
- **Chromium sandbox** is off (`chromiumSandbox: false`; the container and uids are the boundary). Resource limits are per-uid
  `RLIMIT_*` plus the container memory limit: a runaway session can OOM all sessions of that container. gVisor is the recommended
  upgrade for hostile targets.
- The container runs with `cap_drop: ALL` and only `NET_ADMIN` (entrypoint firewall, then dropped), `SETUID SETGID CHOWN KILL
  DAC_OVERRIDE FOWNER` for `sessiond`.

## Egress

Firewall rules are installed per session uid by `entrypoint.sh` (`iptables -m owner`):

| Instance | Policy |
|---|---|
| `research-clear` | Public internet only. REJECT RFC1918, link-local, CGNAT, loopback, cloud-metadata and other stack services (this is the SSRF fix for browsers). Configured DNS resolvers stay reachable. |
| `research-tor` | ACCEPT only the session's own Tor SocksPort on `tor`; REJECT everything else. IPv6 is rejected for all session uids. |
| `research-i2p` | ACCEPT only the session's own SOCKS port on `i2pd-<slot>`; REJECT everything else. |

The gateway's own `requests` downloads (non-HTML) use `netguard.py` on clear (resolve, reject non-public, pin the IP, re-check every
redirect) and `socks5h://tor:<port>` on Tor.

## I2P

Each router has one `type=socks` section per session slot with `keys=transient`: its own destination, tunnel pool and leaseset
(`tunnels.conf` is generated at start, no dynamic add/remove). No outproxy: i2pd refuses clearnet names and raw IPs (SOCKS 0x08), and
`filter_proxy.py` (`ALLOW_SUFFIX=.i2p`) refuses everything but `*.i2p` first; `fetch.py` does the same. Browsers are on the internal
network `sessions_i2p`, so only the routers see the internet. Before a session's first use the gateway probes an eepsite
(`I2P_PROBE_HOST`) and returns "not ready, retry" instead of hanging; Chromium's HTTPS upgrades are off and navigation timeout is 120 s.
**Known limitation:** sessions on one replica share the router identity, exploratory tunnels and transports, so a router-level observer
(or a hop seeing several pools) could correlate them; tunnel isolation is per destination only. Upgrade path: one router per session
slot. Fingerprinting caveats are those of Chromium over Tor. See also: [Plan: I2P egress](../implemented-plans/i2p-egress.md)

## Tor

`torrc` exposes one SocksPort per session uid; Tor isolates streams from different SocksPorts, so users do not share circuits.
Chromium cannot do SOCKS auth, so per-session credentials are not an option. Chromium runs with
`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`. Chromium over Tor is not Tor Browser: fingerprint caveats apply.
With several replicas each gets its own slice of the SocksPort range (`SOCKS_STRIDE = POOL_SIZE + 1`, offset by task slot);
see [Scaling](scaling.md).

## No-media sessions

Loading an image, video or audio file can be a legal risk on the dark web, so the user chooses once per gateway session (before anything
else; fail-closed gate in the gateway and the toolserver) via `open_session(media?, allow?, deny?)`. The policy is immutable, lives in
`sessions.policies` keyed by MCP session id (outliving individual browsers), is installed in the toolserver at browser start, and applies
to every window and to `ingest_page`. `media` defaults to false on Tor / I2P. `get_session()` reports it read-only.

| Layer | Where | Blocks |
|---|---|---|
| A | `context.route`, by resource type | `<img>`, `srcset`, favicons, CSS backgrounds, `<video>`, `<audio>`, `<object>`, web fonts: never requested |
| D | CDP `Fetch` at response headers, documents only | navigations and iframes whose `Content-Type` is not allowed: text allowlist + `allow`, minus `deny` (deny, then allow, then default) |
| fetch.py | `requests` streaming | same Content-Type rule; connection closed before the body is read |

`allow` / `deny` only affect layer D and fetch.py: `allow=["image/svg+xml"]` lets you navigate to an SVG, `<img src=svg>` still does not
load. Not done: disk-cache flags, placeholders, extension denylists, relay-level filtering. **Residual risks:** a server lying about
`Content-Type`; headers of blocked responses (and for D, the request itself) reach the browser; the first request of a popup and
cross-origin iframes can bypass D; the policy is only as good as the question the agent asked the user. The effective policy is logged
(`app` logger) and returned by `open_session`. See also: [Plan: No-media sessions](../implemented-plans/no-media-sessions.md)

## Consent banners

`consent.js` (init script in every page) auto-clicks consent buttons. Known consent-vendor hosts (Fanboy Cookiemonster list, ~670, fetched
at image build) are blocked: on clear egress with Chromium `--host-rules`; under Tor `--host-rules` do not apply (the proxy resolves
names), so each Tor session runs a small SOCKS5 filtering relay (`filter_proxy.py`) between Chromium and its SocksPort.
