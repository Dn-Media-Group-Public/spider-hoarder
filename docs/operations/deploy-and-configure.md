# Deploying, configuring and testing

The commands to get started are in the [README](../../README.md); this page holds the details.
See also: [Plan: Browser sessions](../implemented-plans/browser-sessions-plan.md) (Deployment),
[Plan: Scale research containers](../implemented-plans/research-replicas.md)

## Stack
`deploy.sh` builds `mcp-research:local`, `spider-hoarder-tor:local`, `spider-hoarder-i2pd:local`, `spider-hoarder-i2pd-seed:local`, `oauth2-proxy-dcr:local` (Swarm ignores `build:`), inits Swarm, runs
`docker stack deploy`, then force-updates `nginx`, `research-*`, `tor`, `i2pd`, `i2pd-seed` so fresh `:local` images and an edited `nginx.conf.template` are
picked up. Swarm constraints: no `security_opt` (entrypoint sets `no_new_privs`), no `shm_size` (Chromium uses
`--disable-dev-shm-usage`). One image: Debian bookworm + Python venv, Playwright's Chromium, Xvfb, x11vnc, noVNC, websockify, iptables.

## Configuration (`.env`, see `.env.example`)
| Variable | Meaning |
|---|---|
| `PUBLIC_HOST`, `PUBLIC_SCHEME` | URLs users see (TLS terminates at the LB) |
| `OIDC_ISSUER`, `MCP_SCOPE` | Keycloak realm URL; scope the token must carry (empty = no check) |
| `O2P_COOKIE_SECRET`, `BLOB_TOKEN_SECRET` | secrets (placeholders refused by `deploy.sh`) |
| `RESEARCH_BLOB_URL` | fsspec URL, `file:///data` or `gs://bucket/prefix` |
| `MAX_SESSIONS_TOTAL` (18, max 48), `MAX_SESSIONS_PER_USER` (3), `IDLE_TIMEOUT` (1800 s), `DISCONNECT_TIMEOUT_MINUTES` (5) | per-container limits |
| `HTTP_PORT`, `GATEWAY_PORT` | published nginx port; port gateways listen on (nothing else may use it) |
| `RESEARCH_TOR_REPLICAS`, `RESEARCH_CLEAR_REPLICAS`, `MAX_REPLICAS` | scaling ([Scaling](../architecture/scaling.md)) |
| `RESEARCH_I2P_REPLICAS` (1) | replicas of both `research-i2p` and `i2pd` |
| `I2P_SOCKS_BASE` (4500), `I2P_TUNNEL_QUANTITY` (2), `I2P_TUNNEL_LENGTH` (3) | first SOCKS port per router; tunnels per session destination; hops. See also: [Plan: I2P egress](../implemented-plans/i2p-egress.md)
| `I2P_SNAPSHOT_INTERVAL` (1800 s), `I2P_PROBE_HOST` (`stats.i2p`) | seed snapshot period; eepsite probed for readiness |
| `KC_*` | dev Keycloak and `keycloak/setup.sh` only |

The size/time limits `MAX_PARSE_BYTES`, `MAX_ARCHIVE_BYTES`, `MAX_ARCHIVE_MEMBERS`, `MAX_REQUEST_BODY`, `MAX_UPLOAD_BYTES`,
`MAX_REQUEST_TIMEOUT_MS` (and `OIDC_JWKS_URL`) are read from the container environment but `docker-compose.yml` does not pass them
through; add them to the `research-env` block to change the defaults.

## Android (opt in)
Set `ANDROID_CLEAR_REPLICAS` / `ANDROID_TOR_REPLICAS` above 0. `deploy.sh` then requires `SLOT_SECRET`, `ANDROID_FDROID_URL`,
`ANDROID_FDROID_SHA256`, `/dev/kvm` and an x86_64 host; builds `android-wrapper:local` and `android-slot:local`; makes the golden
snapshot on this host (skipped while the volume's stamp matches the image; a new image stops the wrapper, so the slots go, and
starts from an empty volume); and the wrapper creates the slots on this node (`ANDROID_PLACEMENT`). Slot capacity per egress is
replicas × `ANDROID_SLOTS_PER_REPLICA`; each slot takes `ANDROID_SLOT_CPUS` (2) and `ANDROID_SLOT_MEMORY` (8g).
See also: [Android emulators](../architecture/android.md), [Plan: Android hoarder](../implemented-plans/android-hoarder.md),
[Plan: Android KVM bring-up](../implemented-plans/android-kvm-bringup.md)

**Host sizing.** Per slot: 2 CPUs, 8 GB RAM (guest RAM + ~5 GB: emulator heap ~2 GB, tmpfs, page cache), ~0.75 GB disk for its overlay upper; the golden volume is ~3.8 GB. Plan
for 8 vCPU / 24 GB / 40 GB disk for 2 slots, and **SSD-class disk reads**: a slot (re)start reads a snapshot as large as the guest RAM,
so disk speed sets the reset time. Measured on a GCE VM (8 vCPU, 32 GB, nested KVM, ~48 MB/s disk reads, 3 GB guest): reset 44-62 s,
snapshot init 3 min 20 s; on a 2 vCPU / 7.9 GB host with ~7.6 MB/s reads a reset took 240-540 s and the slot thrashed. A 4g slot limit
gets the emulator OOM-killed; 5g worked. The device `/dev/kvm` must be readable by the `kvm` group found in `/etc/group` (else set
`ANDROID_KVM_GID`).

| Variable | Meaning |
|---|---|
| `ANDROID_CLEAR_REPLICAS`, `ANDROID_TOR_REPLICAS` (0), `ANDROID_SLOTS_PER_REPLICA` (2) | gateways per egress and emulators per gateway |
| `ANDROID_IDLE_TIMEOUT` (600 s), `ANDROID_MAX_SESSIONS_PER_USER` (1) | session limits |
| `ANDROID_API` (34) | system image level |
| `ANDROID_RAM_MB` (3072) | guest RAM, baked into the golden snapshot via `hw.ramSize`; part of the snapshot stamp (`<image id> ram=<n>` when set), so changing it re-makes the snapshot. Slot memory limit should be about this + 5 GB |
| `ANDROID_BOOT_TIMEOUT` (240 s) | wrapper env, passed to the slot as `BOOT_TIMEOUT`: how long `slotd` waits for the emulator/guest per boot; in the slot spec hash (a change recreates the slots) |
| `ANDROID_RESET_TIMEOUT` (300 s) | the android gateways' `SLOT_RESET_TIMEOUT` (wait for a reset) and `SLOT_READY_TIMEOUT` (wait for a slot to be `free` before a session starts); keep above the boot timeout |
| `ANDROID_HELP_TIMEOUT` (3600 s) | max time a pending `device_request_human_help` suspends the idle timer (not in `.env.example`; read by the gateway) |
| `SLOT_SECRET` | shared by the gateways and the slots |
| `ANDROID_FDROID_URL`, `ANDROID_FDROID_SHA256`, `ANDROID_CMDLINE_TOOLS_SHA256` | pinned downloads, verified at build |
| `ANDROID_PLACEMENT`, `ANDROID_SLOT_CPUS`, `ANDROID_SLOT_MEMORY`, `ANDROID_KVM_GID`, `ANDROID_TOR_SUBNET` | wrapper placement, slot size, kvm gid override, Tor-only network subnet |

## Dev mode
`./deploy.sh --dev` adds `docker-compose.dev.yml`: a throwaway Keycloak (`start-dev`), a `keycloak-init` job running
`keycloak/setup.sh`, and `extra_hosts: localhost:host-gateway` so containers reach it at the same `localhost` URL as the browser. Forces
`PUBLIC_HOST=localhost:$HTTP_PORT`, http, issuer `http://localhost:8085/realms/newsroom`. Keep `localhost` in the Trusted Hosts list:
dev DCR for oauth2-proxy depends on it. The oauth2-proxy entrypoint uses GNU `wget` (not curl: curl resolves `localhost` itself and
ignores the `/etc/hosts` rewrite of `dev/localhost-is-host.sh`; busybox wget lacks `--method=DELETE`).

## Keycloak
See [Authentication](../architecture/auth.md). For an external Keycloak run `keycloak/setup.sh` once from the Keycloak image (command in
the README). The user's password is reset to `KC_USER_PASSWORD` on every run: change it outside dev.

## Tests (`tests/`)
Host (`pip install -e mcp-research`, then `pytest tests`): `tests/common`, `tests/gateway`, `tests/egress`, and `tests/session_server/test_toolserver.py` (needs Playwright + Chromium).
`tests/live/` is not run by plain `pytest`: in-container with the deployed capability set (plain root hides permission bugs): `test_sessiond.py`; end to end: `e2e_auth.py` against a
stub Keycloak, `smoke_e2e.py` for real sessions. Exact commands are in the README.
See also: [Plan: Restructure directories](../implemented-plans/restructure-directories.md)

Android: `tests/gateway/test_android.py` (run on its own), `android-toolserver/test_android_toolserver.py`,
`android-slot/test_slot.py` and `android-wrapper/test_reconcile.py` run on the host without KVM; a real emulator is only exercised on a KVM
host: `tests/live/smoke_android.py` inside a `research-android-*` container, and against the deployed `--dev` stack **through nginx** with
real Keycloak tokens `e2e_android.py`, `e2e_android_view.py` (noVNC handoff in headless Chromium) and `e2e_android_lifecycle.py` (idle, disconnect,
failure and quarantine phases), after `e2e_android_setup.sh` (throwaway Keycloak users). Commands: the scripts' docstrings.
See also: [Plan: Android KVM bring-up](../implemented-plans/android-kvm-bringup.md) (W5)

Slot internals not configurable from `.env`: `TOOLSERVER_TIMEOUT` (180 s, `slotd` waiting for the tool server), `--ulimit nofile=65536:65536`
(required: with Docker's default the emulator stalls), pids limit 4096, tmpfs sizes. Tor slots: the wrapper re-points each slot's
default route to the current `tor` task every pass (10 s), see [Android emulators](../architecture/android.md).
