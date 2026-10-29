# Android emulators

Two more MCP servers give Claude a real Android device per conversation, with the same auth, jobs/blobs/uploads and human
handoff (live view) as the browsers:

- `$PUBLIC_SCHEME://$PUBLIC_HOST/android-clear/mcp` — direct egress, public internet only
- `$PUBLIC_SCHEME://$PUBLIC_HOST/android-tor/mcp` — egress via Tor only (TCP and DNS; everything else is dropped)

See also: [Plan: Android hoarder](../implemented-plans/android-hoarder.md) (decisions, discarded options, spike results).
Off by default: set `ANDROID_CLEAR_REPLICAS` / `ANDROID_TOR_REPLICAS` above 0 in `.env`. Needs a KVM-capable x86_64 host.

## Components

```
Claude ─Bearer─▶ nginx ─▶ research-android-<egress> (gateway, MODE=android)
Human ─SSO─▶ nginx ─/authz─▶ gateway ──ok──▶ nginx ─▶ android-<egress>-<k>:6080 (noVNC)
                                │ MCP (proxied) + control verbs, X-Slot-Secret
                                ▼
android-wrapper ──docker.sock──▶ creates android-<egress>-1..N (plain containers on the stack's overlay networks)
  slot k:  Xvfb ─ emulator (uid emu, firewalled, snapshot "clean") ─ adb server (uid svc, loopback only)
           toolserver :9000 (uid svc)   slotd :9001 (root, narrow caps)   x11vnc ─ websockify :6080 (only during a session)
```

| Piece | Role | Source |
|---|---|---|
| `research-android-clear/tor` | The `mcp-research` image with `MODE=android`: auth, jobs, blobs, uploads, live-view authz, replica routing, reaper. No uid pool, `sessiond` or firewall. | `mcp-research/` (`slots.py`, `sessions.py`, `app.py`) |
| `android-wrapper` | Creates and supervises the slot containers from its env alone (counts, sizes, image, secret). Holds `docker.sock`: no ports, no API, no network. | `android-wrapper/reconcile.sh` |
| slot `android-<egress>-<k>` | One emulator, one session at a time, wiped between sessions. `/dev/kvm` and nothing else: `cap_drop ALL`, `no-new-privileges`, default seccomp, `nofile` 65536, memory-capped (`ANDROID_SLOT_MEMORY`). | `android-slot/` |
| Android tool server | MCP server in the slot, `uiautomator2` against the local emulator. | `android-toolserver/` |
| `tor` | Also a transparent proxy for `android_tor`: TCP → `TransPort 9040`, DNS → `DNSPort 5353`. | `tor/entrypoint.sh` |

## Slot lifecycle

A slot is `free`, `busy(session)` or `quarantined` (kept by the owning gateway replica in `pool.Pool`, with `slots.SlotBackend` as
its backend). Gateway replica `REPLICA_SLOT=s` owns slots `(s-1)*P+1 .. s*P` (`P = ANDROID_SLOTS_PER_REPLICA`): no shared
allocator. A free slot is warm: emulator booted from the golden snapshot, no VNC. `initialize` reserves a slot (503 "busy" at once
if none is free; nginx tries the next replica); the session starts lazily on the first tool call.

Control verbs, on `slotd` (port 9001), each needing `X-Slot-Secret`:

| Verb | Effect |
|---|---|
| `GET /internal/health` | `{state: booting\|free\|busy\|resetting\|broken}` |
| `POST /internal/start {vnc_password}` | session begins: x11vnc + websockify with a fresh per-session password; 409 unless `free` |
| `POST /internal/reset` | session over: answers `ok`, then `slotd` exits (after 0.5 s) and the container stops. The wrapper recreates it from scratch (empty overlay upper, fresh tmpfs); the new `slotd` boots from snapshot `clean` (see below). `health` says `resetting` until the old process is gone |
| `GET /internal/verify` | `{ok, problems}`: emulator running, guest booted, third-party packages equal the baseline, no accounts. No `/sdcard` listing: a slot is recreated with an empty overlay after every session, and system apps keep creating and deleting files there for minutes after a restore. See also: [Plan: Android bring-up](../plans/android-kvm-bringup.md) (fix 21) |
| `GET /internal/out/<name>` | a file a tool wrote; read once, then deleted (`O_NOFOLLOW`, regular files, size cap) |
| `PUT /internal/in/<name>` | a file for a tool (an APK); exclusive create |

After every session the gateway calls `reset`, waits for the replacement container to report `free` (`SLOT_RESET_TIMEOUT`), then calls
`verify`; a slot that fails any of that (or times out) is **quarantined** (never
reused, logged loudly, capacity shrinks until the gateway restarts). A gateway start resets every slot of its slice that is not
`free`. Nothing is inherited: the container, its overlay upper layer and its tmpfs are new for every session, the VNC password and noVNC
link are per session, and `/authz` answers 403 for an ended session id.

### Boot, reset and failure detection (`slotd.py`)

**A slot is never reset in place.** The wrapper has no input channel by design (it holds `docker.sock`), so it cannot be told to recreate a
slot; instead it only looks at whether its own slots run. `reset` makes `slotd` exit, the slot has no restart policy (`--restart no`), and a
stopped slot (finished session, crash, or a host reboot) is recreated by the wrapper's next pass (`RECONCILE_INTERVAL`, 3 s) with an empty
overlay upper: the guest, the emulator uid's files and anything an escaped guest could have written under `/avd` are all gone. Cost: each new
container copies the userdata qcow2 up into its overlay again (~2.1 GB), on top of the snapshot restore.
Container logs go with the container, so before recreating a slot that ended abnormally (non-zero exit, `OOMKilled`, or `slot broken` in its log) the wrapper writes the exit code, the OOM flag and the last 60 log lines to its own log (`docker service logs spider-hoarder_android-wrapper`); a normal session end stays quiet.
See also: [Plan: Android KVM bring-up](../plans/android-kvm-bringup.md) (fix 32).

`slotd` boots the slot once, when the container starts (`boot_slot()`; `health` answers `booting` meanwhile). Steps: wipe both tmpfs homes
(a no-op in a fresh container), start Xvfb and the emulator (`-read-only -snapshot clean -no-snapshot-save`, `ANDROID_EMU_TMP=/tmp`), wait for
the console port, start the adb server **after** the emulator, wait for `sys.boot_completed`, `ensure_network()`, start the tool
server. Every wait loop checks that the emulator is still alive ("emulator exited during boot", e.g. an OOM kill during the snapshot
load) instead of running out the `BOOT_TIMEOUT` (`ANDROID_BOOT_TIMEOUT`, 240 s); the tool server wait is `TOOLSERVER_TIMEOUT`
(180 s) and also notices a dead tool server. A failed boot leaves the slot `broken`; the gateway's `reset` then replaces the container.

- **`ensure_network()`**: after a snapshot restore the guest keeps a stale `wlan0` address for about 10 s, then drops it and its DHCP
  requests go unanswered ("No internet"). `slotd` waits for 5 consecutive pings of the emulator gateway `10.0.2.2`; if that does not
  happen within 25 s it toggles wifi (`svc wifi disable/enable`, up to 3 times) and logs "guest network recovered by toggling wifi".
  Best effort: it never fails the boot (an egress that is down on purpose, such as Tor not up, is not a boot failure). Root cause unknown.
- **Watchdog**: a thread checks every 2 s that the emulator and tool server of a `free` or `busy` slot are running; if not, the slot is
  `broken` at once (not when the next tool call fails) and the gateway's reset after the session replaces the container. `verify` also reports
  "emulator not running".
- **Timeouts at the gateway**: `SLOT_RESET_TIMEOUT` (wait for the replacement to be `free` after `reset`, code default 300 s; the `reset` call itself 30 s) and `SLOT_READY_TIMEOUT` (wait for `free` before
  `start`, code default 60 s) are both set from `ANDROID_RESET_TIMEOUT` (300 s) on the android gateways in `docker-compose.yml`; keep it
  above `ANDROID_BOOT_TIMEOUT`.

Measured on the first KVM host (GCE VM, 8 vCPU, 32 GB, nested KVM, ~48 MB/s disk reads, 3 GB guest): a reset takes 44-62 s; the
restore reads `ram.bin`, which is as large as the guest RAM, so **disk read speed decides** (a 2 vCPU / 7.9 GB host with a ~7.6 MB/s disk
needed 240-540 s). The guest clock is the snapshot time plus the time since restore, so it lags by the snapshot's age (52 min seen);
`slotd.set_guest_time()` sets it from the host clock (`cmd alarm set-time`, works as the shell uid) at the end of boot and again when a session starts.
See also: [Plan: Android KVM bring-up](../plans/android-kvm-bringup.md)

Idle timeout `ANDROID_IDLE_TIMEOUT` (600 s) ends the session and wipes the slot (the reaper looks every 60 s, so it takes up to a minute
longer); a pending `device_request_human_help` suspends it (until the next tool call, at most `ANDROID_HELP_TIMEOUT`, 3600 s). A request through
the live view's authz check (opening the page, the websocket handshake) also counts as use; an already open noVNC connection does not.
A client that vanished without ending its session loses it after `DISCONNECT_TIMEOUT_MINUTES` (5). `MAX_SESSIONS_PER_USER` is 1 in this mode:
a second `initialize` of the same user gets `503 {"error": "full", "message": "you already have 1 device sessions; end one first"}`, and any
`initialize` with every slot taken gets `503 ... capacity reached; try again later` at once (the reservation is made at `initialize`).
Ending a session (`device_end_session`, DELETE, reaper) waits for the slot's replacement: the gateway's `close()` allows `SLOT_RESET_TIMEOUT + 90 s`
and `pool.release` finishes on its own task, so a caller that gives up cannot leave the slot `busy` in the pool.
Bluetooth is switched off in the golden snapshot: the emulator's virtual controller (`netsimd`) needs loopback, which the slot firewall rejects for the
emulator uid, so a restored guest with Bluetooth on crashes its stack in every session and sometimes shows "Bluetooth keeps stopping" over the app (hiding it
from uiautomator). Wi-Fi uses the emulator's user-mode stack either way. See also: [Plan: Android KVM bring-up](../plans/android-kvm-bringup.md) (fix 29)

If the slot container dies mid-session (killed, OOM, daemon restart), the gateway's upstream task ends with the connection error and every call
in flight or made afterwards fails at once with "the device session ended unexpectedly (...)" (`sessions._Guarded`); the cleanup does not need the
dead container: it waits for the replacement to be `free` and verifies it. The gateway-to-slot connections also use TCP keepalive and
`TCP_USER_TIMEOUT` (about a minute) for peers that vanish without closing their sockets.
See also: [Plan: Android KVM bring-up](../plans/android-kvm-bringup.md) (fixes 22 and 26-28, W5 results)

Through nginx, uploads (`get_upload_link`, APKs) may be up to 50 MB (`client_max_body_size` on the upload/blob location, the gateway's
`MAX_FILE_BYTES` default; nginx's own 1 MB stopped every real APK), and the SSO redirect is relative (`absolute_redirect off`), so it keeps the
published port in dev and the `https` of the load balancer in prod. The emulator runs with `-no-nested-warnings`: otherwise a modal "nested
virtualization" dialog covers the device in every live view.

## Network and egress

`android_clear` (overlay, attachable) and `android_tor` (overlay, attachable, `internal`, fixed subnet `ANDROID_TOR_SUBNET`).
The emulator's user-mode NAT makes guest traffic leave as the emulator uid's own connections, and the guest reaches the
container's loopback as `10.0.2.2`, so the per-uid OUTPUT rules of `android-slot/firewall.sh` are the guest's firewall:

- emulator uid: loopback rejected **entirely** (tool server, adb, VNC, console; the adb server must therefore be started
  *after* the emulator, which finds it by scanning), RFC1918 (overlay peers: other slots, gateway, tor's own address),
  link-local/metadata, CGNAT rejected; IPv6 rejected; clear also allows the Docker/configured resolvers first; tor accepts
  Tor's virtual onion range `10.192.0.0/10` first, and its default route is the `tor` container (the dummy `-dns-server 8.8.8.8`
  is answered by `DNSPort`).
- svc uid (tool server, adb server, VNC): loopback only, so a future hole in adb cannot pivot out of the VM.

Spike S4 verified the Tor routing; S6 the firewall (see the plan's "Spike results").

**Tor slots follow the tor task's address.** A slot's default route points at the tor *task* IP (`tasks.tor`, not the service VIP), and
that IP changes whenever the task is replaced (every deploy). `android-slot/tor-route.sh` resolves `tasks.tor`, validates a dotted quad,
runs `ip route replace default via <ip>` if it differs and exits 10 when it changed (0 otherwise, also when tor does not resolve yet).
The slot entrypoint loops it until a default route exists; afterwards the wrapper runs
`docker exec -u root <slot> /app/tor-route.sh` for every tor slot on every pass (10 s): a fixed command, only the exit code is read
(10 logs "tor task moved, default route re-pointed"), nothing the slot prints is used. The exec has `NET_ADMIN` because the container
is created with it (`slotd` itself does not). Between a tor task replacement and the next pass the slot has no network (fails closed);
measured: re-pointed within 18 s of `docker service update --force spider-hoarder_tor`.
See also: [Plan: Android KVM bring-up](../plans/android-kvm-bringup.md)

**Known property: `tini`.** PID 1 (`tini`) keeps the container's full capability set including `NET_ADMIN`; only `slotd` (the entrypoint execs it
under `setpriv --bounding-set`) and its children are bounded. Nothing else in the container can signal PID 1, so the risk is low, but
the "`NET_ADMIN` is dropped" statement applies to `slotd` and below, not to PID 1.

## Image and golden snapshot

`deploy.sh` builds `android-slot:local` (SDK, pinned emulator, `google_apis_playstore` API `ANDROID_API`, F-Droid APK with a
checksum check, tool server, noVNC) and, on the host with `/dev/kvm`, runs `init-snapshot.sh` in a one-off container (`--ulimit
nofile=65536`) on an **empty** `android_snapshot` volume:

1. AVD `slot` (pixel_6) with a fixed adb key and `hw.ramSize=$ANDROID_RAM_MB` (default 3072) appended to `config.ini`; Xvfb 800x1000.
2. Boot (no snapshot), install F-Droid, start uiautomator2's on-device agent (`start_uiautomator()`, up to 8 retries: a slow host misses
   the library's 30 s window; the agent then runs inside the snapshot), animations off.
3. Chrome first run is dismissed once ("Use without an account", notifications "No thanks"; init fails if it does not finish), so
   `device_open_url` works at once in every session.
4. Baseline: `baseline-packages.txt` (`pm list packages -3`); init refuses one without F-Droid.
5. `console.py avd snapshot save clean`, then `console.py kill`. `console.py` runs as root and reads the console auth token from
   `/home/emu/.emulator_console_auth_token`: as the svc uid `adb emu` has no token and a trimmed command set, and the emulator's own
   `-snapshot clean` autosave on exit does not work (emulator 37). init checks that `snapshots/clean/{snapshot.pb,ram.bin}` exist.
6. Stamp `/avd/.stamp`.

**Stamp and re-snapshot.** The stamp is the image id, plus ` ram=<n>` when `ANDROID_RAM_MB` is set in `.env`; `deploy.sh` skips init
only when it equals the current value, so any change under `android-slot/` or `android-toolserver/` (new image id) or to the guest RAM
re-makes the snapshot. Before that it scales the wrapper to 0 and **waits until no container labelled `spider-hoarder.android-slot`
is left** (hand-run slots included: remove them first), then runs init into a scratch volume (`<stack>_android_snapshot-new`). Only when init
succeeds is the working volume replaced by a copy of it; a failed init keeps the old snapshot, restarts the wrapper and aborts the deploy.
Every `deploy.sh` run, snapshot or not, force-updates the wrapper, and a stopping wrapper removes all slots: **a redeploy ends live sessions** and the slots are back ~1-2 min later.
See also: [Plan: Android KVM bring-up](../plans/android-kvm-bringup.md) (fix 31).

**Guest RAM and memory.** `ANDROID_RAM_MB` is baked into the snapshot: a restore reads that much from disk, and the slot needs about
**guest RAM + ~5 GB** of container memory (`ANDROID_SLOT_MEMORY`, default 8g for the 3 GB guest; qemu measured at 4.7-5.3 GB RSS: 3 GB guest + 1.4-2 GB heap, plus the `/tmp` tmpfs; 6g got the emulator OOM-killed in a Chrome session). Measured: slot peak cgroup usage incl.
page cache about 5.4 GB (3 GB or 2 GB guest alike); a 4 GB limit gets the emulator OOM-killed once the tool server starts, 5 GB worked. A smaller guest means less disk read per
restore and less RAM per slot, at the cost of a sluggish guest.

**Guest storage cap.** The emulator runs `-read-only`, so everything the guest writes lands in a temp qcow2 on the slot's `/tmp` tmpfs, which counts against
the slot's memory limit. With the default 6 GiB data partition, about 3 GB of guest writes (a download, an app install) OOM-killed the emulator at the 8g limit, from inside
the guest. `init-snapshot.sh` therefore reserves most of the free space with a `fallocate` ballast (`/data/local/tmp/ballast`, shell-owned, part of the snapshot) so the guest has
**~1.2 GB free** and gets `No space left on device` first; init fails if it does not end up within 1.0-1.4 GB. `disk.dataPartition.size` in `config.ini` and the emulator's
`-partition-size` do not shrink the partition (both tried), and `fallocate` writes no data, so the image does not grow. Measured: 14 GB of attempted guest writes leave `/tmp` at 1.6 GB, no OOM, tools unaffected. The
file can be deleted by the `shell` uid (`device_shell`), which only removes the protection for that session. Writes through `/sdcard` (FUSE) can appear to succeed after the disk is full.
See also: [Plan: Android KVM bring-up](../plans/android-kvm-bringup.md) (fix 30)

**File descriptors.** Docker's default `RLIMIT_NOFILE` (~1e9) makes the emulator loop `fcntl` over every fd; the whole process, guest
included, stalls and the emulator's watchdog kills it ("detected a hanging thread QEMU2 CPU0"). Hence `--ulimit nofile=65536:65536` in the
wrapper's `create_slot` (it is part of the spec hash) and in `deploy.sh`'s init run, and `ulimit -n 65536` in `entrypoint.sh` and
`init-snapshot.sh`. A ~20 s "hanging thread" warning on every restore is harmless.

Measured (same host): init 3 min 20 s (3 GB guest); golden volume 3.8 GB; each slot's overlay upper is ~2.1 GB (a full copy-up of the userdata qcow2; constant across sessions, see the KVM bring-up plan) plus tmpfs
(`/tmp` 4g, of which the emulator's own temp file takes ~1.5 GB, `/home/emu` 256m, `/out`, `/in` 512m each).
See also: [Plan: Android KVM bring-up](../plans/android-kvm-bringup.md)

Each slot mounts an overlay volume at `/avd` (golden volume read-only below, a private upper layer; mounted by `dockerd`, so no
`SYS_ADMIN`); the wrapper creates it fresh whenever it (re)creates a slot. The wrapper recreates a slot when its spec hash changes
(slot image id, `ANDROID_SLOT_CPUS`, `ANDROID_SLOT_MEMORY`, `ANDROID_BOOT_TIMEOUT`, kvm gid, snapshot volume, `SLOT_SECRET`, restart policy and slot spec version);
a stopped slot is recreated (never restarted: see "A slot is never reset in place"), a missing one created, and slots above the wanted count are removed with their volumes.
There is no pre-logged-in Google account: a human signs in through the live view each session.

## Tools

[Tool reference](../api/tools.md#android-tools). Code map: `android-toolserver/android_ui.py` (hierarchy → outline and refs,
no device access), `android_tools.py` (tool schemas and handlers), `device.py` (the one `uiautomator2` connection),
`android_server.py` (MCP + secret gate), `export_tools.py` (writes `mcp-research/android-tools.json`, checked by the tests).
