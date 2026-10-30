# Plan: Android hoarder (`spider-hoarder-android-clear` / `-tor`)

Status: implemented, and since refactored into the pluggable `android_clear` / `android_tor` backends ([Plan: Pluggable backends](backend-plugins.md): the text below describes the original layout with `research-android-*`, `android-wrapper`, `MODE=android`; current names and paths are in [Android emulators](../architecture/android.md)). (see "Implementation notes" at the end for where the build differs from the text above, and what was
not run on a real emulator). All spikes S1-S6 done (see "Spike results"); several design points were corrected by them. Builds on [Browser sessions](../implemented-plans/browser-sessions-plan.md),
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
- `reset()`: kills VNC/websockify and all session processes, discards the emulator's temp overlay and the AVD changes it made by stopping the
  emulator, wiping the slot's overlay upper directory (S2: the emulator modifies its AVD even with `-read-only`) and restarting the
  emulator from snapshot `clean` (`-read-only -snapshot clean -no-snapshot-save`), then (re)starting the adb server
  once the emulator is up (S6), wipes the slot's out dir, returns
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
- **loopback rejected entirely** for the emulator uid (S6: this works only if the adb server is started after the emulator, never accepted on `127.0.0.1:5037`) (the guest reaches the container's loopback services through the
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
  Spike S4 verified routing through the `tor` container over an `internal` overlay (see "Spike results"); the
  in-slot relay fallback is not needed. Because peers on that overlay are reachable directly, the emulator-uid
  firewall also rejects the overlay subnet on `android-tor` (with an accept for Tor's `10.192.0.0/10` virtual
  addresses placed first).

### Wrapper

Swarm service `android-wrapper`, `network_mode`/networks: none, read-only rootfs, `cap_drop: ALL`, `docker.sock`
bind-mounted, placed on the KVM node by a placement constraint. A ~40-line shell script with the `docker` CLI.
Env: `ANDROID_CLEAR_SLOTS`, `ANDROID_TOR_SLOTS` (derived in compose as replicas x `ANDROID_SLOTS_PER_REPLICA`),
`ANDROID_SLOT_IMAGE`, `ANDROID_SLOT_CPUS` (2), `ANDROID_SLOT_MEMORY` (4g), `SLOT_SECRET`, stack name.

- Reconcile every 10 s: create missing slots, start stopped ones, remove extra ones (label
  `spider-hoarder.android-slot`), recreate a slot whose spec hash label differs.
- Spec (hardcoded except the env above): `--device /dev/kvm`, `--cap-drop ALL --cap-add NET_ADMIN`,
  `--security-opt no-new-privileges`, `--read-only` with tmpfs for `/tmp` and the emulator's temp dir, the
  per-slot overlay volume at `/avd` (lower = the golden `android_snapshot` volume, upper/work = a small per-slot volume; mounted by `dockerd` via the local volume driver, so the slot needs no extra capability, S2), the host's kvm group, memory/cpu limits, `--restart unless-stopped`, name and network alias
  `android-<egress>-<k>`, the egress network.
- SIGTERM (stack removal): remove all labelled slots. After a crash the next start reconciles by label.
- Threat model written in the wrapper's header: `docker.sock` is host root; the safety is that the wrapper takes
  no input except its env and Docker's own answers.

### Image and clean snapshot (`deploy.sh`)

`deploy.sh` builds `android-slot:local` (SDK, pinned emulator and `system-images;android-<API>;google_apis_playstore;x86_64`,
F-Droid APK with sha256 check, `uiautomator2` + toolserver, noVNC) and then, **on the host that has KVM**, runs an init
container with `--device /dev/kvm` and the `android_snapshot` volume: creates the AVD on **empty volumes** with a fixed, pre-generated adb key (S2), boots it, installs the
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

0. **Spikes (done, results in "Spike results" below):**
   - S1 emulator boots as a non-root user with only `/dev/kvm`, `cap_drop ALL`, `no-new-privileges`, default seccomp.
   - S2 shared read-only AVD + quickboot snapshot restored by several slots at once; measure restore time and
     check the overlay is discarded on exit.
   - S3 `uiautomator2` against the image (hierarchy dump on Play Store, F-Droid, a webview screen; text input).
   - S4 Tor routing through the `tor` container over an `internal` overlay (TCP redirect, DNS, UDP dropped,
     per-source-IP isolation); fallback decision if it fails.
   - S5 wrapper-created containers join the stack's attachable overlays and resolve by name/alias from gateway and nginx.
   - S6 firewall: from the emulator uid, metadata server, a peer slot, the gateway and the container loopback via
     `10.0.2.2` are unreachable; public internet works (clear).

### Spike results (2026-10-14/15)

S4 and S5 ran on the dev host (`aarch64`, single-node Swarm, no KVM). S1-S3 and the emulator part of S6 ran on a
temporary GCE VM in `dngroup-eai-sandbox` (`n2-standard-8`, nested virtualisation, Ubuntu 24.04, Docker 29, emulator
37.2, `google_apis_playstore` API 34 x86_64, swiftshader). The VM and its disks were deleted afterwards. Nothing in
the repo was changed.

| Spike | Result |
|---|---|
| S1 non-root emulator, only `/dev/kvm` | **PASS** (details below). |
| S2 shared AVD + snapshot restored by several slots | **PASS, with a design change**: the AVD cannot be shared read-only; each slot gets an overlay mount (read-only golden lower layer + private writable upper). |
| S3 `uiautomator2` on the image | **PASS**: Play Store, Settings, F-Droid, Chrome webview and Unicode input all work. |
| S4 Tor via the `tor` container over an `internal` overlay | **PASS**, no fallback needed. |
| S5 wrapper-created containers on the stack's attachable overlays | **PASS** (single node). |
| S6 per-uid firewall | **PASS, with a design change**: the loopback reject must not block the emulator's own adb registration. |

**S1 (PASS).** The emulator booted as uid 2000 with `--cap-drop ALL --security-opt no-new-privileges`, default
seccomp/AppArmor, `--read-only` rootfs plus tmpfs, and only `--device /dev/kvm` (+ `--group-add <kvm gid>`: the device
is `root:kvm 0660`, so the slot needs the host's kvm gid; the wrapper must look it up). Cold boot 37-43 s, snapshot
`clean` saved with `adb emu avd snapshot save clean`. Details that must go into the entrypoint:
- `setpriv --bounding-set=-all` needs `CAP_SETPCAP`; reading/copying the AVD as root with `cap_drop ALL` needs
  `CHOWN`/`DAC_OVERRIDE`/`FOWNER` (or do those steps as the owning uid). The entrypoint capability set is therefore
  `NET_ADMIN`, `SETUID`, `SETGID`, `SETPCAP` (+ the three file caps if root touches the files), all dropped for the
  emulator and everything else.
- The emulator process ends with an empty capability set (`CapEff/CapBnd = 0`).
- 4 GB container memory limit **kills the emulator silently** (snapshot RAM is 3 GB, observed ~5.8 GB RSS):
  `ANDROID_SLOT_MEMORY` default is raised to **6g**.

**S2 (PASS, design change).**
- Three slots started from the same snapshot concurrently (2 CPUs / 6 GB each): snapshot restore 7-11 s with the shared
  volume, 11-21 s with the private-copy variant below; twice in a row per slot. Guest writes (`/sdcard` file, a
  `settings put`) were gone after restarting from the snapshot, so the emulator's temp overlay is discarded.
- **A read-only AVD mount does not work**: the emulator fails with "A snapshot operation for 'clean' is pending and
  timeout has expired" and exits. Even with `-read-only` on a writable mount it rewrites `snapshot.pb`, lock files and
  the userdata `qcow2` header, so the shared volume's checksum changes and three emulators write to one base image.
- **Chosen design, overlay volume (verified):** the golden AVD volume stays the read-only base; each slot gets a
  Docker local-driver volume of `type=overlay` (`device=overlay`,
  `o=lowerdir=<golden mountpoint>,upperdir=<slot vol>/u,workdir=<slot vol>/w`), mounted by `dockerd` on the host, so
  the slot gets no `SYS_ADMIN`. The wrapper creates a small plain volume per slot, `mkdir`s `u`/`w` (owned by the
  emulator uid) with a one-shot helper container, and reads the golden mountpoint from `docker volume inspect`
  (host-path coupling is acceptable for a root wrapper on the host, but it is a Docker implementation detail). Result,
  three concurrent slots, twice (run 1 fresh, run 2 after wiping the upper directories):
  - restore 12.3-13.6 s first start, 4.9-6.2 s on restart (one outlier 10.9 s) with three emulators at once; guest
    writes gone after restart; `uiautomator2` works;
  - the golden volume's checksum was identical before and after both runs;
  - **upper layer is 752 MB per slot**, almost all of it the copied-up userdata `qcow2` (827 MB file; the emulator
    opens it read-write). `ram.bin` (3 GB) is **not** copied up and is shared through one page cache, so the overlay
    costs ~0.75 GB disk per slot instead of 3.8 GB;
  - `reset()` = remove the container, `rm -rf` the upper and work directories (empty again: 4 KB), start again (verified,
    run 2); no 3.8 GB copy.
  - Caveat: a wipe must happen while the overlay is unmounted (no container using the volume).
  - Superseded alternative (also verified, works but slower and bigger): a private per-slot volume into which the golden
    AVD is `cp -a`'d at start (1.3 s warm, 10.7 s cold, 3.8 GB per slot, restore 11-21 s with three slots). AVD paths
    are absolute in `clean.ini`, so the overlay must be mounted at `/avd` like the golden volume.
  This replaces "the `android_snapshot` volume read-only" in the Wrapper spec.
- **The adb key must be fixed and shared.** The guest only trusts the adb key present at the *first boot of a fresh
  userdata*; a snapshot restored without that key shows the device as `unauthorized`. Generate the key
  (`adb keygen`) *before* the init boot, in the home dir the emulator uses (`getpwuid` home, here `/home/emu/.android`;
  `$HOME` overrides are ignored), and mount/copy the same key for every adb user. Re-running init on a non-fresh AVD
  directory also fails (`unauthorized`): `deploy.sh` must start from empty volumes. The key is only a local adb
  credential inside the slot; bake it into the image.

**S4 (PASS).** Setup: `internal` + `attachable` overlay; a plain `tor` container on it (and on a normal bridge for
internet) with `TransPort 0.0.0.0:9040`, `DNSPort 0.0.0.0:5353`, `AutomapHostsOnResolve 1`,
`VirtualAddrNetworkIPv4 10.192.0.0/10`, `net.ipv4.ip_forward=1`, `NET_ADMIN`, and in the nat table
`PREROUTING -i <overlay if> -p tcp -j REDIRECT --to-ports 9040` and `-p udp --dport 53 -j REDIRECT --to-ports 5353`,
`FORWARD` policy DROP. A client container on the overlay only needed `ip route replace default via <tor ip>`
(`NET_ADMIN`). Observed:
- HTTPS to a public hostname exits via Tor (`IsTor: true`, exit IP differs from the host's).
- DNS to the dummy resolver `8.8.8.8` is answered by Tor's `DNSPort` (so `-dns-server 8.8.8.8` works as designed).
- Raw-IP TCP (`https://1.1.1.1`) also exits via Tor; `.onion` names resolve and connect.
- UDP other than DNS and ICMP get no reply; the tor container's `FORWARD` DROP counter counted them.
- Without the route change the same container has no egress at all (`internal` network works as the safety net).
- Per-source-IP stream isolation: two clients consistently got different exit IPs (3 samples each).
- No fallback (in-slot redsocks relay) needed; the "needs user decision" item for it is dropped.

Findings that change the design:
1. **Peers on the Tor overlay are reachable directly (L2), bypassing Tor**, and so is the tor container's own
   address. The uid firewall must therefore also apply on `android-tor`: reject the overlay subnet and `127.0.0.0/8`
   for the emulator uid, with an `ACCEPT` for the Tor virtual-address range `10.192.0.0/10` placed **before** the
   RFC1918 reject (onion names are mapped into it). The text above ("tor: ... everything else dies") already assumes
   this for loopback only; implement it for peers too. Risk: if Swarm ever allocates an overlay out of
   `10.192.0.0/10`, choose a different `VirtualAddrNetworkIPv4`.
2. The redirect needs the `-i <overlay interface>` match (the tor container also has a normal-bridge interface for
   its own circuits); the plan's "android_tor subnet only" is satisfied that way.

**S5 (PASS).** On a single node: a `--attachable` overlay, a Swarm service on it and a plain `docker run
--network ... --network-alias android-clear-1` container (`cap_drop ALL` + `NET_ADMIN`, `no-new-privileges`,
`--read-only`, tmpfs `/tmp`) resolved and connected to each other both ways, by alias and by `tasks.<service>`.
Not tested: multi-node Swarm (wrapper on the KVM node, gateway elsewhere); VXLAN should make it work but it is
unverified. Also note `docker cp` into a `--read-only` container fails: the slot entrypoint/test tooling must pipe
files via stdin or use tmpfs.

**S3 (PASS).** `uiautomator2` (device APKs auto-installed on first connect) as a separate uid from the emulator:
- `dump_hierarchy()` takes 0.07-0.11 s; Launcher 58 nodes, Play Store 38 (signed-out screen, "Sign in"), Settings 86.
- Unicode: `d.send_keys('Ünïcödé 日本語 😀')` round-trips exactly into an `EditText` (read back via `get_text()`).
- **Webview content is in the accessibility tree**: Chrome showing `example.com` exposes "Example Domain" and the page
  text, including Arabic and Chinese paragraphs. Chrome's first-run screens ("Use without an account", notification
  prompts) must be clicked through; F-Droid also raises the runtime notification permission dialog.
- F-Droid APK (`https://f-droid.org/F-Droid.apk`, 12.5 MB, sha256 `83d3fe522281c3cb89fce3bc05038f81b3c3b32c108536941f184c5f9bb53778`
  on 2026-10-15) installs with `adb install` and launches (`org.fdroid.fdroid`). The checksum changes with each
  F-Droid release: pin a versioned APK URL at build time, not this one.
- Not tested: Google/Play login (needs a human), canvas/game UIs, `device_wait_for` semantics.

**S6 (PASS, design change).** Per-uid OUTPUT rules installed by root with `NET_ADMIN`, then emulator started as uid
2000 with an empty capability set, adb and `uiautomator2` as uid 3000, a stand-in tool server on `127.0.0.1:9000`
(uid 3000), a peer container and the bridge gateway on a user-defined bridge (Docker resolver `127.0.0.11`).
Results from inside the guest (`toybox nc`):
- Public internet works (`ifconfig.me` answered; the guest also talked to Google services on its own); DNS works with
  only `127.0.0.11` allowed.
- Blocked: tool server via `10.0.2.2:9000`, the peer container, the bridge gateway `:22`, `169.254.169.254`. **Control:**
  the same tool-server request returned its banner as soon as an accept for `127.0.0.1:9000` was added, and root in the
  container reaches every one of these targets. This proves guest traffic appears as the emulator uid's connections and
  that `10.0.2.2` maps to the container's loopback, so the uid firewall is the guest firewall.
- **The "reject loopback entirely" rule breaks the emulator**: with it, the emulator's own loopback connections (797
  rejected packets in a minute) kept the device from appearing in adb (`adb devices` empty for 5+ minutes while the
  emulator was fully booted and the guest had internet). The emulator registers itself by connecting to the adb server
  on `127.0.0.1:5037`. Allowing that makes the device appear, **but it also lets the guest reach the adb server
  through `10.0.2.2:5037`** (verified: `host:version` answered), and a guest-issued `host:connect:<peer>:8000` was
  accepted by the adb server, which runs un-firewalled as the tool-server uid (it reaches peers, gateway and metadata):
  an SSRF/pivot out of the VM.
- **Resolution (verified for discovery):** keep loopback rejected entirely. With the 5037 rule removed, restarting the
  adb server *after* the emulator is up (`adb kill-server; adb start-server`) found the device within 10 s, because
  the adb server scans the emulator ports itself. The slot supervisor must therefore start (or restart, until
  `adb devices` lists the emulator) the adb server only after the emulator is listening. As defence in depth also give
  the adb/tool-server uid its own OUTPUT rules (loopback and the gateway only), so a future hole cannot be used as a
  pivot.
- Test-harness notes: `adb shell` swallows the script from stdin (use `</dev/null`); `docker cp` into a tmpfs path
  fails.

## Implementation order (steps)

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

## Implementation notes

Built as planned, with these differences and additions (the text above is the decision record; this is what the code does):

- **Control port.** The slot's control verbs are served by `slotd` (root, narrow caps) on **:9001**; the tool server stays on :9000.
  The tool server also requires `X-Slot-Secret` (and `Host: mcp-upstream`), so nothing in a slot answers unauthenticated except
  noVNC (protected by the per-session VNC password).
- **`reset()` restarts in place; the overlay upper layer is wiped by the wrapper.** An overlay upper directory cannot be wiped from
  inside the container that has it mounted (S2: the wipe must happen unmounted). So the in-slot `reset` kills everything,
  sweeps the tmpfs homes, `/out`, `/in`, and boots from snapshot `clean` again (S2 measured guest writes gone after such a
  restart); the wrapper wipes the upper/work directories whenever it (re)creates a slot (new image, spec change, crash). A new
  verb `GET /internal/verify` lets the gateway do the plan's check (third-party packages equal the baseline recorded by
  `init-snapshot.sh`, no accounts, no `/sdcard` files beyond the baseline) without any adb access of its own.
- **Slot counts** are computed in `deploy.sh` (`ANDROID_*_SLOTS` = replicas x `ANDROID_SLOTS_PER_REPLICA`): compose cannot multiply.
  The feature is **opt-in**: replica counts default to 0, and `deploy.sh` then neither builds nor needs KVM.
- **Capabilities.** The slot entrypoint set is `NET_ADMIN SETUID SETGID SETPCAP KILL CHOWN DAC_OVERRIDE FOWNER` (S1 plus `KILL`, so
  `slotd` can kill the emulator uid's stragglers); only `slotd` keeps the last seven, the emulator and everything else start with
  none (`setpriv --bounding-set=-all`). `ANDROID_SLOT_MEMORY` defaults to 6g (S1).
- **`device_install_apk(job_id, path)`** is a gateway tool: it reads the APK from the job, checks the zip magic, pushes it to the
  slot under a gateway-chosen name (`PUT /internal/in/<uuid>.apk`) and calls the tool server's own `device_install_apk(file)`.
  `.apk` uploads are stored without parsing.
- **Files out.** Tools write to `/out/<uuid>.<ext>` (a plain gateway-chosen name, never the agent's); `GET /internal/out/<name>` reads
  it once (`O_NOFOLLOW`, regular file, size cap) and deletes it.
- **Idle timeout** is also suspended while the user uses the live view, and a pending `device_request_human_help` suspends it for at
  most `ANDROID_HELP_TIMEOUT` (default 3600 s). The tool list is static: `android-toolserver/export_tools.py` writes
  `mcp-research/android-tools.json` and a test fails when it is out of date.
- **Tor side.** `tor` runs `setpriv` from a root entrypoint (it needs `NET_ADMIN` for the redirect rules, selected by interface
  from the fixed `ANDROID_TOR_SUBNET`, default `10.99.0.0/24`, deliberately outside `10.192.0.0/10`) and `sysctls: net.ipv4.ip_forward=1`.
- **Wrapper network.** Swarm has no `network_mode: none`, so the wrapper sits on an `internal` overlay nobody else joins. It finds the
  kvm gid in the host's `/etc/group` (bind-mounted read-only) unless `ANDROID_KVM_GID` is set.
- **Changes found on a real KVM host** (see [Plan: Android KVM bring-up](../implemented-plans/android-kvm-bringup.md); how it works now is in
  [Android emulators](../architecture/android.md)): `--ulimit nofile=65536` (spec `spec-v2`); snapshot saved through `console.py` (root,
  console auth token), not `adb emu`; `/sdcard/` baseline with a stability wait; Chrome first run dismissed in init; `ANDROID_RAM_MB`
  (guest RAM, in the stamp), `ANDROID_BOOT_TIMEOUT`, `ANDROID_RESET_TIMEOUT`; `slotd` boot-loop exit detection, watchdog and
  `ensure_network()`; Tor slots' default route re-pointed by the wrapper via `docker exec -u root <slot> /app/tor-route.sh`.
- **Pinned downloads.** The F-Droid APK URL and sha256 are required build args (`ANDROID_FDROID_URL` / `_SHA256` in `.env`); the
  command-line-tools archive can be verified with `ANDROID_CMDLINE_TOOLS_SHA256` (otherwise `sdkmanager` verifies what it fetches).

**Verified on the host (no KVM, no emulator):** the Android tool server's outline, refs and handlers against captured hierarchy XML
and a fake device; the slot's firewall rule set (rule order for clear and tor, with a fake `iptables`), `slotd`'s auth and file
handling; the wrapper's reconcile pass against a fake `docker`; the gateway's slot backend, quarantine, tool list, `/authz`
upstream, file tools, APK push and idle/help behaviour against a fake `slotd`; the existing browser tests still pass; the compose
file validates with `docker stack config`.

**Verified on a KVM host (2026-10-22 to 2026-10-29, GCE VM with nested KVM, 8 vCPU / 32 GB, Debian 13, Swarm dev stack):** the slot image build, `init-snapshot.sh`,
emulator boot and snapshot restore under `slotd`, the firewall and Tor redirect inside real containers, `uiautomator2` through the gateway, the noVNC handoff through nginx and
oauth2-proxy, the e2e scripts with real Keycloak tokens, a soak of 50 sessions per egress, fill and crash tests, multi-replica, and a Docker daemon restart. What was found and
changed is in [Plan: Android KVM bring-up](../implemented-plans/android-kvm-bringup.md) (fixes 1-33). **Not run:** Google sign-in and real apps with a human
([Android manual test plan](../plans/android-manual-test.md)), and a host reboot (deliberately never run on that host). The "Not run" paragraph that stood here is replaced by this one.
