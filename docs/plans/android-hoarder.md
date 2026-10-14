# Plan: Android hoarder (`spider-hoarder-android-clear` / `-tor`)

Status: plan, not implemented. Builds on [Browser sessions](../implemented-plans/browser-sessions-plan.md),
[Scale research containers](../implemented-plans/research-replicas.md) and
[Tool file outputs become jobs](../implemented-plans/file-outputs-as-jobs.md).

## Problem / Goal

Give Claude per-user, isolated, real Android devices over MCP, the way `research-*` gives it browsers:
same auth, same human handoff (live view) for logins and CAPTCHAs, same jobs/blobs/uploads, and a tool set
that mirrors the browser one. Two **separate** MCP servers, not mixed with the web ones:

- `$PUBLIC_SCHEME://$PUBLIC_HOST/android-clear/mcp`: direct egress, public internet only
- `$PUBLIC_SCHEME://$PUBLIC_HOST/android-tor/mcp`: egress via Tor only

Out of scope: host provisioning/KVM enablement (a KVM-capable x86_64 host is a prerequisite, for dev laptop and
prod alike: one stack everywhere), arm64 hosts, webview/Chrome DOM automation (accessibility tree only),
Appium, several devices per session, Play Integrity/attestation.

## Discarded options (how to run and start Android)

| Option | Why discarded |
|---|---|
| redroid with `--privileged` | Shares the host kernel; privileged container is a trivial host escape. Needs `binder_linux` on the host. |
| redroid with narrowed caps (`SYS_ADMIN`, unconfined seccomp/AppArmor, mapped devices) | Still near root-equivalent; Swarm cannot set `security_opt` or map devices anyway. |
| Emulator inside the `research-*` containers as `sessiond` uid sessions | Different image (multi-GB SDK), different privileges (KVM) and capacity profile (2-4 GB, 2 cores each); no clean per-session wipe; couples the browser and Android release cycles. |
| Slots as Swarm services (`replicas: N`) | `stack deploy` ignores `devices:`; no `/dev/kvm`. |
| Swarm generic resources + custom OCI runtime hook for `/dev/kvm` | A runtime shim to maintain for one device node. |
| Wrapper with a control API (verbs: start/stop slot) | Adds attack surface to a component with `docker.sock`. Replaced by a zero-input reconciler. |
| Gateway launching containers per session through `docker.sock` | The gateway parses untrusted documents; the socket is host root. The browser plan dropped this for the same reason. |
| Appium (UiAutomator2 driver) | Chosen only for webview/Chrome automation, which is out of scope. Needs Node, a driver and chromedriver to match; more state to reset. `uiautomator2` gives the same accessibility-tree access. |
| Raw `uiautomator dump` + `input` over adb | Slow dump, ASCII-only `input text`, no selectors or waits. |
| Playwright/CDP attach or chromedriver for webviews | Out of scope; native tree plus screenshots/taps is enough for v1. |
| Android Emulator on arm64, Cuttlefish/crosvm | Dev and prod are both x86_64 with KVM. |
| HTTP proxy egress for clear (`-http-proxy` via a relay) | HTTP(S) only: breaks FCM (5228), QUIC/UDP and non-HTTP app protocols. Browsers get direct NAT + uid firewall; Android does the same. |
| Host-level `DOCKER-USER` iptables for the slot egress | Config outside the stack that `deploy.sh` would have to install and persist. The in-container per-uid firewall (as `entrypoint.sh` does today) needs none. |
| Tor `HTTPTunnelPort` / HTTP proxy for the tor server | HTTP(S) only. TransPort carries all TCP. |
| adb exposed on the network, tool server in the gateway | adb is unauthenticated; anything on the network could drive a device. Tool server runs in the slot; only MCP/control/noVNC ports leave it. |
| One shared Google account baked into the snapshot | Shared identity across users, token theft, ToS. A human logs in per session instead. |
| Baking the clean snapshot during `docker build` | `docker build` has no `/dev/kvm`. `deploy.sh` creates it with an init container. |
| Queueing when all slots are busy | Decided: fail immediately with a "busy" error. |

## Design

### Decisions (already made)

- **Slots:** a fixed number of long-lived containers `android-<egress>-<k>`, each running the Android Emulator
  (x86_64, KVM), Xvfb, x11vnc + websockify (noVNC), and the Android tool server. One session per slot at a time
  (serialized); wiped between sessions. Busy = error immediately.
- **VM boundary, no extra caps:** slots get `/dev/kvm` and nothing else: `cap_drop: ALL`, `no-new-privileges`, default
  seccomp/AppArmor. One exception: `NET_ADMIN` for the entrypoint firewall step only (as in `research-*`), dropped
  with `setpriv` before the emulator starts. Emulator runs non-root with `-gpu swiftshader_indirect`.
- **One wrapper** (`android-wrapper`, Swarm service) creates and supervises all slots from env config. Zero inputs:
  no ports, no API, no network. Holds `docker.sock`; spec hardcoded.
- **Gateway = the existing `mcp-research` image with `MODE=android`**, deployed as `research-android-clear` and
  `research-android-tor`. It keeps auth, jobs, blobs, uploads, live-view authz, replica routing and the reaper, and
  proxies tool calls to `http://android-<egress>-<k>:9000/mcp` (the slot's tool server), like it proxies to the
  Playwright tool server today. The Playwright and Android tool servers share no code.
- **Tool server:** Python MCP server using the `uiautomator2` library, in the slot (`android-toolserver/`).
- **System image:** `google_apis_playstore`, API level pinned (default 34), plus F-Droid (pinned APK, checksum
  verified at build). **No pre-logged-in accounts**: a human logs in through the live view each session; the tool
  descriptions tell the model to call `device_request_human_help` at a Google/Play login. Play Store on `android-tor`
  is best-effort (Google tends to block Tor exits).
- **One device per session**, no multi-window/multi-session concept.
- **Idle timeout:** `ANDROID_IDLE_TIMEOUT` (default 600 s, set in `docker-compose.yml`): no tool call for that long
  kills the session and wipes the slot. A pending `device_request_human_help` suspends the timer.
- **Slot ownership across gateway replicas:** gateway replica `REPLICA_SLOT=s` owns slots
  `(s-1)*P+1 .. s*P`, `P = ANDROID_SLOTS_PER_REPLICA`. No shared allocator.

### Architecture

```
Claude ─Bearer─▶ nginx ─▶ research-android-<egress> (gateway, MODE=android, task IP = rid)
Human ─SSO─▶ nginx ─/authz─▶ gateway ──ok──▶ nginx ─▶ android-<egress>-k:6080 (noVNC)
                                │ MCP (proxied) + control verbs, SLOT_SECRET
                                ▼
android-wrapper ──docker.sock──▶ creates android-<egress>-1..N (plain containers on the stack's overlay networks)
  slot k:  Xvfb ─ emulator (uid emu, firewalled, -read-only, snapshot "clean") ─ adb server
           toolserver :9000 (uid svc)   x11vnc ─ websockify :6080 (uid svc, only while a session is active)
```

### Slot lifecycle and the gateway-to-slot interface

A slot is `free`, `busy(session)` or `quarantined` (state kept by the owning gateway replica). A free slot is
**warm**: emulator booted from the clean snapshot, no VNC running.

- `start(vnc_password)`: starts x11vnc/websockify with a fresh per-session password.
- `reset()`: kills VNC/websockify and all session processes, discards the emulator's temp overlay by restarting the
  emulator from snapshot `clean` (`-read-only -snapshot clean -no-snapshot-save`), wipes the slot's out dir, returns
  when booted. The gateway then verifies the wipe (adb up, third-party package list equals the baseline, no accounts,
  `/sdcard` empty). Failure or timeout: **quarantined** (never reused, logged loudly, capacity shrinks).
- Both verbs are `POST /internal/*` on the slot, requiring `X-Slot-Secret` (`SLOT_SECRET`, shared by the wrapper
  spec and the gateways). Nothing else in a slot is reachable unauthenticated.
- **A view cannot be inherited:** the VNC password and noVNC link are per session; `reset()` kills x11vnc and
  websockify (and their open connections) before the slot is freed; the gateway's `/authz` answers 403 for an ended
  session id, so an old link never reaches a later session on the same slot.
- **Files:** tools that produce files (`device_take_screenshot`, `device_logcat` with a filename, ...) write to the
  slot's out dir; the gateway fetches them over `GET /internal/out/<name>` (secret) into jobs, then the slot deletes
  them. Input files (APKs) are pushed by the gateway to `PUT /internal/in/<name>` and referenced by
  `device_install_apk`. This replaces the shared-filesystem `gw-collect` copy of the browser design.

### Network and egress (mirrors the browser design)

Networks, in `docker-compose.yml`: `android_clear` (overlay, attachable, internet via the usual gwbridge) and
`android_tor` (overlay, attachable, `internal: true`). Gateway, nginx and the slots of that egress attach; slots are
plain containers, so the networks must be `attachable`. nginx joins them (it proxies noVNC to `android-<egress>-k:6080`
using the `X-Upstream` answer of the gateway's `/authz`).

The slot entrypoint (root, `NET_ADMIN`) installs per-uid OUTPUT rules for the **emulator uid only**, then drops all
capabilities and starts everything unprivileged. The emulator's user-mode NAT makes guest traffic come out as the
emulator uid's own connections, so these rules are the guest's firewall:

- replies on established connections accepted;
- **loopback rejected entirely** for the emulator uid (the guest reaches the container's loopback services through the
  emulator's `10.0.2.2` alias: tool server, adb, VNC, console), IPv6 rejected;
- **clear:** the Docker resolver and configured resolvers allowed, then RFC1918, `169.254.0.0/16` (cloud metadata),
  `100.64.0.0/10`, `127.0.0.0/8` rejected (this includes the overlay, so a guest can neither reach other slots, the
  gateway nor any stack service); everything else accepted (direct NAT).
- **tor:** no default route in an `internal` network, so the slot entrypoint sets
  `ip route replace default via <tor ip>`. The `tor` service gets `TransPort 9040` and `DNSPort 5353` (stream
  isolation per source IP), `NET_ADMIN`, `net.ipv4.ip_forward=1`, and nat `PREROUTING` rules **for the
  `android_tor` subnet only**: TCP -> `REDIRECT 9040`, UDP/53 -> `REDIRECT 5353`; `FORWARD` default `DROP`, no
  masquerade. Everything else (UDP, QUIC, ICMP) dies. The emulator is started with `-dns-server 8.8.8.8` (a dummy
  public address: the redirect catches it) so that no firewall exception for private addresses is needed.
  **Risk:** TransPort needs the redirect on the Tor host, so the slot's traffic has to be routed through the `tor`
  container (the Whonix pattern); routing arbitrary next-hop traffic over a Swarm overlay is unverified. Spike S4
  decides. If it fails the fallback, **which needs a user decision**, is an in-slot transparent relay
  (`iptables REDIRECT` to a local redsocks-style SOCKS client talking to per-slot SocksPorts of the existing `tor`
  service, DNS to `DNSPort`).

### Wrapper

Swarm service `android-wrapper`, `network_mode`/networks: none, read-only rootfs, `cap_drop: ALL`, `docker.sock`
bind-mounted, placed on the KVM node by a placement constraint. A ~40-line shell script with the `docker` CLI.
Env: `ANDROID_CLEAR_SLOTS`, `ANDROID_TOR_SLOTS` (derived in compose as replicas x `ANDROID_SLOTS_PER_REPLICA`),
`ANDROID_SLOT_IMAGE`, `ANDROID_SLOT_CPUS` (2), `ANDROID_SLOT_MEMORY` (4g), `SLOT_SECRET`, stack name.

- Reconcile every 10 s: create missing slots, start stopped ones, remove extra ones (label
  `spider-hoarder.android-slot`), recreate a slot whose spec hash label differs.
- Spec (hardcoded except the env above): `--device /dev/kvm`, `--cap-drop ALL --cap-add NET_ADMIN`,
  `--security-opt no-new-privileges`, `--read-only` with tmpfs for `/tmp` and the emulator's temp dir, the
  `android_snapshot` volume read-only, memory/cpu limits, `--restart unless-stopped`, name and network alias
  `android-<egress>-<k>`, the egress network.
- SIGTERM (stack removal): remove all labelled slots. After a crash the next start reconciles by label.
- Threat model written in the wrapper's header: `docker.sock` is host root; the safety is that the wrapper takes
  no input except its env and Docker's own answers.

### Image and clean snapshot (`deploy.sh`)

`deploy.sh` builds `android-slot:local` (SDK, pinned emulator and `system-images;android-<API>;google_apis_playstore;x86_64`,
F-Droid APK with sha256 check, `uiautomator2` + toolserver, noVNC) and then, **on the host that has KVM**, runs an init
container with `--device /dev/kvm` and the `android_snapshot` volume: creates the AVD, boots it, installs the
`uiautomator2` on-device APKs and F-Droid, disables animations, sets stay-awake/locale, saves snapshot `clean`, shuts
down, and writes a stamp with the image id. Skipped when the stamp matches. (Quickboot snapshots depend on the host
CPU, which is why it is created where it runs.)

### Android tool server (`android-toolserver/`)

Python MCP server (streamable HTTP on `:9000`, `Host: mcp-upstream` allow-list like `toolserver/server.py`),
`uiautomator2` against the local emulator. Responses follow the browser server's sections (`### Result`,
`### Snapshot`, `### Error`).

| Android tool | Browser analogue | Notes |
|---|---|---|
| `device_snapshot` | `browser_snapshot` | `dump_hierarchy()` -> compact outline of visible/interactive nodes with `[ref=eN]`; stale ref = "ref not found, take a new snapshot"; `filename` writes a file |
| `device_find` | `browser_find` | text / resource-id / description / class |
| `device_click`, `device_long_click` | `browser_click` | by ref |
| `device_tap`, `device_swipe` | `browser_mouse_*` (not in browsers) | coordinates, for canvas/game UIs |
| `device_type` | `browser_type` | ref + text, optional submit; Unicode-safe |
| `device_press_key` | `browser_press_key` | back, home, enter, recents, ... |
| `device_wait_for` | `browser_wait_for` | text appears/disappears, or seconds |
| `device_take_screenshot` | `browser_take_screenshot` | always a file |
| `device_open_url` | `browser_navigate` | `am start -a VIEW` |
| `device_install_apk`, `device_app_start`, `device_app_stop`, `device_app_list`, `device_current_app` | (none) | install from an uploaded file |
| `device_logcat` | `browser_console_messages` | `filename` writes a file |
| `device_shell` | `browser_evaluate` | arbitrary adb shell (contained by the VM and the firewall) |
| `device_request_human_help`, `device_end_session` | same | gateway-owned |
| `list_files`, `get_file_text`, `get_file_url`, `grep_file`, `get_upload_link` | same | gateway, unchanged |

Not carried over (web-specific): cookies/localStorage/sessionStorage tools, `ingest_page`, `browser_save_page`,
`browser_pdf_save`, network tools, multi-browser (`browser_start_session`, `browser` parameter).

### Gateway changes (`mcp-research/`)

The gateway today is coupled to `sessiond` (uid pool, ports, session home, `FILE_TOOLS` copy-out). `MODE=android`
adds a second backend behind the same session interface:

- `slots.py` (new): slot table for this replica's slice, allocator (`reserve` fails immediately when none is free),
  `start`/`reset`/verify, quarantine, file fetch/push. Replaces `pool.py`/`ports.py`/`sessiond` calls in this mode.
- `app.py`/`sessions.py`: session creation/teardown and tool proxying go through a small backend seam; the
  upstream URL is the slot's `:9000`; the tool list comes from a static `android-tools.json`; web-only gateway tools
  (`browser_save_page`, `ingest_page`, cookie helpers, `fetch_html`) are not registered in this mode; idle
  timeout from `ANDROID_IDLE_TIMEOUT`; `MAX_SESSIONS_PER_USER` default 1 in this mode.
- `/authz` returns `X-Upstream: android-<egress>-<k>:6080` for the session's slot.
- `entrypoint.sh`: in `MODE=android` skip the uid firewall and `sessiond` (the gateway container needs neither
  `NET_ADMIN` nor the sessiond caps).
- `nginx.conf.template`: the egress capture becomes `(tor|clear|android-tor|android-clear)`; services are named
  `research-android-clear` / `research-android-tor` so `tasks.research-$egress` keeps working; nginx joins the
  `android_*` networks.

## Implementation order

0. **Spikes (go/no-go, results into this plan's notes):**
   - S1 emulator boots as a non-root user with only `/dev/kvm`, `cap_drop ALL`, `no-new-privileges`, default seccomp.
   - S2 shared read-only AVD + quickboot snapshot restored by several slots at once; measure restore time and
     check the overlay is discarded on exit.
   - S3 `uiautomator2` against the image (hierarchy dump on Play Store, F-Droid, a webview screen; text input).
   - S4 Tor routing through the `tor` container over an `internal` overlay (TCP redirect, DNS, UDP dropped,
     per-source-IP isolation); fallback decision if it fails.
   - S5 wrapper-created containers join the stack's attachable overlays and resolve by name/alias from gateway and nginx.
   - S6 firewall: from the emulator uid, metadata server, a peer slot, the gateway and the container loopback via
     `10.0.2.2` are unreachable; public internet works (clear).
1. `android-slot` image and `deploy.sh` build + snapshot init step.
2. `android-toolserver/` with tests (against a real emulator; skipped without `/dev/kvm`).
3. Slot entrypoint (firewall, route, privilege drop, supervisor, `start`/`reset`, `/internal/*`).
4. `android-wrapper` and compose services/networks/env; tor `TransPort`/`DNSPort`.
5. Gateway `MODE=android` backend, `slots.py`, nginx routing, `/authz`.
6. Tests: allocator/quarantine unit tests, wrapper reconcile test with a fake `docker`, firewall script test, e2e
   smoke through nginx for both servers (install an APK, snapshot, tap, screenshot job, human-help link, reset, check
   the old link is dead).
7. Docs.

## Files to change

| File | Change |
|---|---|
| `android-slot/` (new) | Dockerfile, entrypoint/supervisor, firewall, init (snapshot) script |
| `android-toolserver/` (new) | MCP server, `uiautomator2` tools, snapshot formatter, tests |
| `android-wrapper/` (new) | Dockerfile, reconcile script |
| `mcp-research/slots.py` (new), `app.py`, `sessions.py`, `entrypoint.sh` | `MODE=android` backend, authz upstream, firewall skip |
| `mcp-research/android-tools.json` (new) | static tool list for the gateway |
| `docker-compose.yml` | `research-android-clear/tor`, `android-wrapper`, networks, volumes, env (`ANDROID_*`, `SLOT_SECRET`) |
| `tor/entrypoint.sh` | `TransPort`, `DNSPort`, forwarding/redirect rules (plus compose `cap_add`/sysctl) |
| `nginx.conf.template` | `android-*` routes, extra networks |
| `deploy.sh` | build slot/wrapper images, snapshot init, restart new services, `.env` required vars |
| `.env.example` | new variables |

## Docs to update

There are no `docs/architecture/` or `docs/api/` files in this repo yet; the affected docs are:

- `README.md`: the two new servers, their tools, env vars, KVM prerequisite, the Google-login note.
- `docs/implemented-plans/browser-sessions-plan.md` and `research-replicas.md`: a "See also" link to this plan
  where the architecture/routing sections are extended.
- On completion: move this file to `docs/implemented-plans/android-hoarder.md` and add it to
  `docs/implemented-plans/index.md`.
