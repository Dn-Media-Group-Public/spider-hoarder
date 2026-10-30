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
2. It creates `/home/s<uid>` (`0710`, group `gw-collect`) and `out/` (`2750`), then forks a child that sets `umask 027`, drops
   groups, `setgid`/`setuid`, applies `RLIMIT_NPROC`/`NOFILE`, a clean environment (no gateway secrets), `setsid`, and execs
   the session group. `no-new-privileges` is set by the entrypoint (`setpriv`) because Swarm cannot set `security_opt`.
3. `stop(uid)`: SIGKILL the group → sweep with `kill(-1)` as that uid → wait until no process owns the uid → delete everything
   the uid owns (home, `/tmp`, `/var/tmp`, `/dev/shm`, `/run`, X sockets) → verify → mark free. Any failure **quarantines** the uid
   (never reused, capacity shrinks, logged loudly).
4. On gateway/sessiond start every pool uid is stopped (crash recovery; no state is trusted across restarts).

Anything that must outlive a session is copied by the gateway into blob storage before `stop`
([Jobs and storage](jobs-and-storage.md)); the homedir is disposable. Each session has a private gid, so group-readable `out/` files
only ever mean "the gateway" (member of `gw-collect`).
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

The gateway's own `requests` downloads (non-HTML) use `netguard.py` on clear (resolve, reject non-public, pin the IP, re-check every
redirect) and `socks5h://tor:<port>` on Tor.

## Tor

`torrc` exposes one SocksPort per session uid; Tor isolates streams from different SocksPorts, so users do not share circuits.
Chromium cannot do SOCKS auth, so per-session credentials are not an option. Chromium runs with
`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`. Chromium over Tor is not Tor Browser: fingerprint caveats apply.
With several replicas each gets its own slice of the SocksPort range (`SOCKS_STRIDE = POOL_SIZE + 1`, offset by task slot);
see [Scaling](scaling.md).

## Android slots

Android sessions are isolated differently: a slot is a container with `/dev/kvm`, the emulator is the VM boundary, and the
firewall is per uid inside the slot (the guest's traffic is the emulator uid's). See [Android emulators](android.md).
See also: [Plan: Android hoarder](../implemented-plans/android-hoarder.md), [Plan: Android KVM bring-up](../implemented-plans/android-kvm-bringup.md)

- Slot containers: `cap_drop ALL` plus `NET_ADMIN SETUID SETGID SETPCAP KILL CHOWN DAC_OVERRIDE FOWNER` for the entrypoint and `slotd`;
  the emulator, adb, tool server and VNC run under `setpriv --bounding-set=-all` (verified: 0 caps, `no_new_privs`). `slotd` keeps the
  last seven (no `NET_ADMIN`). **PID 1 (`tini`) keeps the full set including `NET_ADMIN`**: nothing else in the container can signal it, so
  it is a documented property, not a hole known to be exploitable.
- Reset is in place: processes of both uids are killed, tmpfs homes swept, the emulator booted again from snapshot `clean`; `verify` then
  compares packages, accounts and `/sdcard/` with the baselines. The overlay upper layer is only wiped by the wrapper when it recreates a slot.
- Tor slots: the `android-wrapper` (docker.sock holder) execs one fixed command, `/app/tor-route.sh`, in each tor slot every pass and reads
  only its exit code, so a compromised slot cannot steer the wrapper.

## Consent banners

`consent.js` (init script in every page) auto-clicks consent buttons. Known consent-vendor hosts (Fanboy Cookiemonster list, ~670, fetched
at image build) are blocked: on clear egress with Chromium `--host-rules`; under Tor `--host-rules` do not apply (the proxy resolves
names), so each Tor session runs a small SOCKS5 filtering relay (`filter_proxy.py`) between Chromium and its SocksPort.
