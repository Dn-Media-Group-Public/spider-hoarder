# Plan: Android bring-up and verification on a KVM host

Status: **in progress** (stages 0-1 done, 2-3 mostly done, 4 partly, 5-6 not started). Work started 2026-10-20, last updated 2026-10-22.
Follows [Android hoarder](../implemented-plans/android-hoarder.md), which was built and tested on a host without `/dev/kvm`. This plan
covers what was **not** run there (see that plan's "Implementation notes", "Not run") and the assumptions in the code that were never
checked against a real emulator. Nothing is committed yet. Tags: **[done]**, **[partly]**, **[todo]**.

## Problem / Goal

Everything between "the slot image builds" and "a human signs in to Google through nginx" had only been verified in pieces
(spikes S1-S6 by hand, host tests with fakes). Goal: run the assembled system once, in stages that each isolate one layer, fix
what breaks, and leave behind a repeatable smoke test and a record of the results. Done = every exit criterion below passes on a
fresh deploy, on both egresses.

## Status overview

| Stage | State | Left |
|---|---|---|
| 0 host sanity | [done] | - |
| 1 image + golden snapshot | [done] | - |
| 2 one slot by hand | [partly] | reset during a tool call, overlay hygiene, tmpfs sizes, 5x repeat with the full dirtying set, tor-slot firewall list, TCP-level firewall proof |
| 3 tool server on the device | [partly] | Unicode `device_type`, two slots at once, first-call latency after restore, `KEYS` trimming |
| 4 stack | [partly] | wrapper kill/recreate/scale-0, wrapper isolation, tor leak checks, noVNC in a browser, e2e through nginx, quarantine path |
| 5 Google sign-in (manual) | [todo] | all |
| 6 soak and capacity | [todo] | all |

## Host

| | Plan wanted | First host (2026-10-20) | Current host (2026-10-21) |
|---|---|---|---|
| vCPU | 8 | 2 | 8 |
| RAM | 24 GB | 7.9 GB | 32 GB |
| Disk reads | >= 100 MB/s | ~7.6 MB/s (pd-standard) | ~48 MB/s |

GCE VM, nested KVM works (`/dev/kvm` root:kvm gid 991), Xeon 2.8 GHz, 100 GB disk, no swap. `/tmp` is a 4 GB tmpfs, lost on reboot.
Findings from the first host, which was too small:
- Snapshot restore reads `ram.bin` (same size as guest RAM): cold boot/reset of a slot took 240-540 s (3 GB guest) and 390-450 s (2 GB
  guest); the plan assumed 5-13 s (S2, SSD). On the current host a reset takes 44-62 s (3 GB guest), so the disk is still the limit.
- With the dev stack up on 7.9 GB the slot thrashed (load 20-30, 70-90% iowait, tool calls took minutes, a reset hit the tool-server
  start timeout). Slot peak cgroup usage incl. page cache was ~5.4 GB with either a 3 GB or a 2 GB guest; a **4 GB limit gets the emulator
  OOM-killed** once the tool server starts, 5 GB worked. `.env` now uses `ANDROID_SLOT_MEMORY=6g`.

## Prerequisites

- x86_64 host with `/dev/kvm` (`ls -l /dev/kvm`, `kvm-ok`); on a VM, nested virtualisation. Note the device's group and gid
  (`stat -c '%G %g' /dev/kvm`): the wrapper looks up the group named `kvm` in `/etc/group`; if the device belongs to another group,
  set `ANDROID_KVM_GID`. [done]
- At least 8 CPUs, 24 GB RAM and 40 GB disk free for 2 slots (each slot: 2 CPUs, 6 GB, ~0.75 GB overlay upper; golden volume ~4 GB),
  and SSD-class reads. [done on the current host except disk speed]
- Docker with Swarm active, a working `.env` (`./deploy.sh --dev` is fine; Keycloak dev user `researcher`). [done]
- `.env`: `ANDROID_CLEAR_REPLICAS=1`, `ANDROID_TOR_REPLICAS=1`, `ANDROID_SLOTS_PER_REPLICA=1` first (smallest case), `SLOT_SECRET`,
  a **versioned** `ANDROID_FDROID_URL` and its `ANDROID_FDROID_SHA256`. [done: the local `.env` has an "android bring-up" block; the
  old unversioned URL/hash in `.env.example` was stale, now `org.fdroid.fdroid_2000052.apk` with its sha256]
- Keep a results log: this file is updated in place (per stage, what was run, result). Findings that change the design go into the
  implemented plan's notes, not only here.

## Tooling

### Host tests
No pip on the host: run them in the `mcp-research` image. All four pass (also `test_pool.py`, `test_security.py`, `test_gateway.py`).
```
docker run --rm --user $(id -u):$(id -g) -e HOME=/tmp -v $PWD:/repo -w /repo/<dir> --entrypoint python mcp-research:local <test.py>
```
for `mcp-research/test_android.py`, `android-toolserver/test_android_toolserver.py`, `android-slot/test_slot.py`,
`android-wrapper/test_reconcile.py`.

### Stage-2 recipe (one slot by hand, through the wrapper's own spec)
```
docker network create s2_android_clear
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v /etc/group:/host-etc-group:ro \
  -e STACK_NAME=s2 -e ANDROID_CLEAR_SLOTS=1 -e ANDROID_SLOT_IMAGE=android-slot:local -e SLOT_SECRET=s3cret \
  -e ANDROID_SLOT_MEMORY=5g -e ANDROID_BOOT_TIMEOUT=900 -e ANDROID_SNAPSHOT_VOLUME=spider-hoarder_android_snapshot \
  -e RECONCILE_ONCE=1 android-wrapper:local
IP=$(docker inspect -f '{{(index .NetworkSettings.Networks "s2_android_clear").IPAddress}}' s2-android-clear-1)
curl -H 'X-Slot-Secret: s3cret' http://$IP:9001/internal/health          # booting/resetting -> free
curl -X POST -H 'X-Slot-Secret: s3cret' -d '{"vnc_password":"pw123456"}' http://$IP:9001/internal/start
curl -X POST -H 'X-Slot-Secret: s3cret' http://$IP:9001/internal/reset ; curl -H ... /internal/verify
curl -X PUT  -H 'X-Slot-Secret: s3cret' --data-binary @x.apk http://$IP:9001/internal/in/x.apk
```
Tool calls: a ~25-line MCP client (`mcp.client.streamable_http.streamablehttp_client("http://$IP:9000/mcp",
headers={"Host":"mcp-upstream","X-Slot-Secret":"s3cret"})`, `ClientSession.initialize/call_tool`) run in the `mcp-research` image with
`--network host`. Test APK: `org.woheller69.arity` v159 from `https://f-droid.org/repo/org.woheller69.arity_159.apk`. Dirtying commands
that work via `device_shell`: write `/sdcard/leak.txt`, `/sdcard/Download/d.txt`, `/data/local/tmp/leak2`,
`settings put global device_name LEAKED`.
Clean up afterwards: `docker rm -f s2-android-clear-1; docker volume rm s2-android-clear-1-avd s2-android-clear-1-state; docker network rm s2_android_clear`.
`deploy.sh` waits for **every** container labelled `spider-hoarder.android-slot` to exit before making the snapshot, so hand-run slots must
be removed before a deploy that re-snapshots.

### Debug tricks
- `emulator ... -verbose -show-kernel`; `strace -f -c -p <qemu pid>` (apt-get install strace in a throwaway root container).
- Emulator console as root: `printf 'auth <token>\r\ncmd\r\n' | nc 127.0.0.1 5554` (token in `/home/emu/.emulator_console_auth_token`);
  `android-slot/console.py` does this for init.
- `pkill -f emulator` kills your own `sh -c` (its command line matches): use `pkill -x qemu-system-x86` (15-char comm limit).
- A killed emulator leaves `slot.avd/*.lock` in a persistent AVD dir ("Running multiple emulators with the same AVD"); slot overlays are
  fresh per creation, so this only bites hand-run containers on a shared volume.
- Plain upstream QEMU boots the Android kernel with KVM fine (Alpine `qemu-system-x86_64` + `kernel-ranchu`): a stall is the emulator
  process, not KVM.
- Test-harness gotcha: calling `sessions.pool.reset()` (gateway pool) from a script while a session is still open quarantined the slot
  ("not clean after reset"), whereas direct `/internal/reset` + `/internal/verify` were clean.

## Staged approach

Each stage has an exit criterion; do not start the next before it holds. Stages 1-3 run **without** the stack (plain
`docker run`), so a failure points at one layer.

### Stage 0: host sanity (10 min) [done]
| # | Step | Status |
|---|---|---|
| 1 | `uname -m` is `x86_64`; `/dev/kvm` exists and is `rw` for the kvm group | [done] |
| 2 | Host tests pass: `test_android.py`, `test_android_toolserver.py`, `test_slot.py`, `test_reconcile.py`, browser tests | [done] (re-run after every fix) |
| 3 | `docker stack config` with the real `.env` is clean | [done] (`./deploy.sh --dev` deploys) |

Exit: all green. **Met.**

### Stage 1: slot image and golden snapshot [done]
Build exactly as `deploy.sh` does (staged context, build args); run `init-snapshot.sh` in the one-off container (the `deploy.sh`
command), with `-it` and a shell trap if it has to be inspected. Watch for: `sdkmanager --licenses`, system-image download size, apt
package names (`libasound2` vs `libasound2t64`), `adb keygen` at build time.

| # | Check (each was an unverified assumption) | Status / result |
|---|---|---|
| 1 | Image builds as `deploy.sh` does | [done] |
| 2 | `avdmanager create avd ... -d pixel_6` and the appended `hw.ramSize` take effect | [done]. Guest RAM is now `ANDROID_RAM_MB` (default 3072) |
| 3 | Emulator starts as uid 2000 with only `/dev/kvm` and the given caps; Xvfb 800x1000 | [done] after the nofile fix (fix 1) |
| 4 | adb server started after the emulator finds the device, key matches (no `unauthorized`) | [done] |
| 5 | On-device uiautomator2 agent starts | [done] after fix 3 |
| 6 | F-Droid installs; `baseline-packages.txt` contains it; `baseline-sdcard.txt` plausible | [done] after fix 4 (see below) |
| 7 | Chrome first run does not block `device_open_url` | [done]: init dismisses it ("Use without an account", then notifications "No thanks"; init fails if first run does not finish). Verified: `device_wait_for "Example Domain"` passes |
| 8 | Snapshot saved, emulator exits | [done] after fix 2 |
| 9 | Stamp written; second `deploy.sh` skips init | [done]: confirmed on a deploy where only oauth2-proxy changed. The stamp is `<image id>` plus ` ram=<n>` when `ANDROID_RAM_MB` is set in `.env` (see `deploy.sh`), so changing the guest RAM re-makes the snapshot. With the variable unset the stamp is the image id only |
| 10 | Record wall time, golden volume size, snapshot RAM | [done]: init 3 m 20 s (3 GB guest, normal load); 7-8 min (2 GB guest, starved host, first host). Volume 3.8 GB (3 GB guest). Snapshot RAM = guest RAM |

Exit: `/avd` holds `slot.avd`, snapshot `clean`, baselines and `.stamp`; second `deploy.sh` skips it. **Met.**

Fixes made in this stage (all uncommitted):
1. **File-descriptor limit stalls the emulator.** Docker's default `RLIMIT_NOFILE` is ~1e9; the emulator loops `fcntl` over every fd up to
   the limit (strace: 120k failing fcntl per 4 s), the whole process incl. the guest stalls, the watchdog kills it ("detected a hanging
   thread QEMU2 CPU0 thread" + segfault). Fix: `--ulimit nofile=65536:65536` in the wrapper's `create_slot` (spec hash `spec-v2`) and in
   `deploy.sh`'s init run; `ulimit -n 65536` in `entrypoint.sh` and `init-snapshot.sh`. `test_reconcile.py` checks the flag.
2. **Snapshot save.** As the svc uid, `adb emu` has no console auth token and a trimmed command set ("bad sub-command"), and `adb emu kill`
   silently failed so init hung. New `android-slot/console.py` (root, reads the token, waits for OK/KO); init runs
   `console.py avd snapshot save clean` then `console.py kill`, and checks `snapshots/clean/{snapshot.pb,ram.bin}` exist. Emulator 37's
   `-snapshot clean` autosave on exit does **not** work ("Snapshots have been disabled by the user"): do not go back to it.
3. **uiautomator2 3.7 has no `healthcheck()`.** init uses `u2.connect(...).start_uiautomator()` with up to 8 retries (the library's 30 s
   launch window is missed on a slow host); the agent then runs inside the snapshot.
4. **`/sdcard` is a symlink**: `find /sdcard -type f` lists nothing, so the baseline was empty and `verify` could never see a leftover
   file. init and `slotd.verify` use `find /sdcard/`; init refuses an empty baseline. init also waits until the listing is stable before
   writing `baseline-sdcard.txt`: system apps create `.nomedia` / speech-model cache files after boot, which were in the snapshot but not
   the baseline, so `verify` failed after every reset.
5. **`ANDROID_RAM_MB`** (default 3072, baked in via `hw.ramSize`): read by `init-snapshot.sh`, passed by `deploy.sh`, part of the stamp. Slot
   memory limit should be about guest RAM + 3 GB.

Every change under `android-slot/` or `android-toolserver/` changes the image id and so forces a re-snapshot in `deploy.sh` (by design).

### Stage 2: one slot by hand (no wrapper, no gateway) [partly]
Start one container with the wrapper's exact spec (recipe above), `EGRESS=clear` first.

| # | Step | Status / result |
|---|---|---|
| 1 | **Boot:** `/internal/health` walks `booting` -> `free`; `-read-only -snapshot clean -no-snapshot-save` loads the snapshot from the overlay at `/avd`; `ANDROID_EMU_TMP` effect | [done]: boots and goes `free` (fixes 6-8 below); boot/reset 44-62 s on the current host (3 GB guest). [todo] whether `ANDROID_EMU_TMP` has any effect / that `wipe()` covers the emulator's temp files |
| 2 | **Privileges:** `/proc/<pid>/status` caps of slotd, emulator, adb, toolserver; `no_new_privs`; read-only rootfs; tmpfs sizes | [partly]: slotd has the 7 caps and no `NET_ADMIN`; emulator, adb, toolserver have 0; `no_new_privs` on all. **Open:** `tini` (PID 1) keeps the pre-drop caps incl. `NET_ADMIN` (`ENTRYPOINT ["tini", "--", ...]`; only `slotd` is started under `setpriv --bounding-set`). Low risk (nothing else can signal it) but the design says `NET_ADMIN` is dropped: drop it for tini or document. [todo] tmpfs sizes under a session (`/tmp` 2g, `/home/emu` 256m) |
| 3 | **Control verbs** with/without secret; good/bad password; second start 409; reset; verify; `PUT /in`; `GET /out` | [done]: wrong secret 403, second `start` 409 |
| 4 | **Reset semantics** (core security property): dirty everything, reset, all gone, verify ok, launcher on screen, 5x | [partly], see below |
| 5 | **Overlay hygiene:** upper size after sessions, golden checksum unchanged, recreate gives a fresh upper | [todo] |
| 6 | **Firewall** from inside the guest | [partly], see below |

Step 4 results: dirty (sdcard files, a `/data/local/tmp` file, `device_name` setting) -> reset -> `verify` ok -> files gone; 3 consecutive
cycles through the gateway client and 6 consecutive direct `/internal/reset` + `/internal/verify` cycles all ok; `verify` reports the dirt
before the reset; a reset takes 44-62 s (84-90 s before fix 4 with the old snapshot; 240-540 s on the first host). Reset after `kill -9` of
the emulator: the slot goes `broken`, a reset recovers it to clean. **Not done:** reset while a tool call is running; the 5x repeat with the
full dirtying set (installed APK, accounts, Chrome login, clipboard).

Step 6 results (clear guest): the internet is reachable; the own slot IP, the Docker gateway and other overlay peers (research gateway,
nginx, other slots) are not reachable by ping. **Not done:** TCP-level blocking of `10.0.2.2:9000/9001/5037/5900`, `169.254.169.254`, IPv6,
the svc-uid loopback-only check (`docker exec -u 3000 ... nc -z <peer> 9000`), and the whole list for a **tor** slot. Any "reachable" result is
a blocker.

More fixes made in this stage:
6. **slotd:** emulator exit detected in every boot loop (an OOM kill during snapshot load used to leave it waiting the whole timeout);
   `check_alive()` plus a 2 s `watchdog` thread mark a free/busy slot `broken` when the emulator or tool server died; `verify` reports
   "emulator not running"; `TOOLSERVER_TIMEOUT` default 180 (was 60) with a "tool server exited" check. `test_slot.py` has
   `test_watchdog_marks_dead_emulator_broken`.
7. **Wrapper:** "missing slot" detection (`docker inspect` prints a newline for a missing container, so the log said "spec changed,
   recreating"): now `cur=$(... 2>/dev/null) || cur=missing`. New `ANDROID_BOOT_TIMEOUT` (default 240, in the spec hash, passed as
   `BOOT_TIMEOUT` to the slot).
8. **Compose/env:** wrapper gets `ANDROID_BOOT_TIMEOUT`; the android gateways get `SLOT_RESET_TIMEOUT` and `SLOT_READY_TIMEOUT` from
   `ANDROID_RESET_TIMEOUT` (default 300). `.env.example` documents both and `ANDROID_RAM_MB`.
9. **`slotd.ensure_network()`** (end of `boot()`): after every snapshot restore the guest keeps a stale wlan0 address for ~10 s, then drops
   it and its DHCPDISCOVERs go unanswered ("No internet", `DNS_PROBE_FINISHED_NO_INTERNET`). Toggling wifi fixes it every time.
   `ensure_network` waits for 5 consecutive pings of `10.0.2.2`, else toggles wifi up to 3x and logs "guest network recovered by toggling
   wifi" (it fired on the restores checked). Root cause unknown.

Exit: reset provably clean 5/5, privileges as expected, firewall rejects every internal target, tmpfs sizes settled. **Not yet met** (see the
[todo] items).

Observations: the guest clock is the snapshot time plus the time elapsed since restore, so it falls behind with snapshot age (not fixed; TLS
works so far; consider setting the time at session start). The emulator logs a harmless ~20 s "hanging thread QEMU2 CPU0..3" warning on every
restore. `qemu: Unable to connect character device modem ... ::1` (no IPv6 in the container) and `adb command ... failed: device offline`
during init are harmless.

### Stage 3: the tool server on the real device (still no gateway) [partly]
Run an MCP client against `slot:9000/mcp` (headers `Host: mcp-upstream`, `X-Slot-Secret`).

| # | Step | Status / result |
|---|---|---|
| 1 | Every tool once against known screens (Launcher, Settings, F-Droid, Chrome `example.com`, Play Store signed-out): outline, `device_find`, stale refs, `device_type` Unicode/`clear`/`submit`, `device_wait_for`, `device_swipe`, `device_tap` on canvas, `device_open_url`, `device_app_*`, `device_logcat`, `device_shell`, screenshot PNG | [partly]: all 18 tools called once and behave; `device_open_url` + `wait_for "Example Domain"` pass. [todo] Unicode `device_type`/`device_type` options, canvas tap, Play Store screen |
| 2 | **Install APK** via `PUT /internal/in/...` then `device_install_apk`, deleted afterwards, non-APK fails cleanly | [done] (install works) |
| 3 | uiautomator2 assumptions: `app_list("-3")`, every `KEYS` name accepted by `d.press`, `install(..., nolaunch=True)`, `send_keys(clear=...)`, Chrome first-run dialogs, first `u2.connect` latency after restore | [partly]: install works; Chrome first run handled in init (stage 1 step 7). [todo] check every `KEYS` name and trim the list; first-call latency after restore (first call after boot took 64 s on the starved host, not re-measured). F-Droid asks for a permission on its first start: add a "first steps" hint to the tool descriptions |
| 4 | Concurrency: two slots at once, `docker stats`, emulator RSS vs the 6g limit, OOM in `dmesg` | [todo]. (First host: 4 GB limit OOM-kills the emulator, 5 GB works; see Host) |

Also: `smoke_android.py` passes on the clear slot (session, 18 tools, snapshot, `open_url` + `wait_for`, screenshot, second start refused, old
view id gone, free after close, verify ok). Its "home screen has F-Droid" print is False because F-Droid is in the app drawer, not on the first
home page: a script expectation, not a defect.
Fix made: **`device_logcat`** applied the regex to the last N lines, so it usually returned nothing; it now filters the whole buffer and then
takes the last N matches (test added).

Exit: all tools behave as documented; tool descriptions fixed where reality differs (then `python export_tools.py`). **Not yet met.**

### Stage 4: the stack (wrapper, networks, gateway, nginx, noVNC) [partly]
`./deploy.sh --dev`, then watch `docker service logs spider-hoarder_android-wrapper`. **Done:** the whole stack comes up (wrapper, both android
gateways `research-android-clear`/`-tor`, both slots reaching `free`, tor, nginx, keycloak, oauth2-proxy).

| # | Step | Status / result |
|---|---|---|
| 1 | **Wrapper:** slot names, overlay volumes, `volume inspect` lower/upper/work; kill a slot (restarted/recreated); change `ANDROID_SLOT_CPUS` (only changed slots recreated, upper wiped); `scale wrapper=0` removes all slots within the 120 s grace; wrapper has no network and is unreachable | [partly]: slots exist and run. [todo] kill/recreate, spec-hash change, scale-0, wrapper isolation check |
| 2 | **Networks/DNS:** gateway and nginx resolve the slots; nginx reaches `:6080`; firewall list from a stack slot incl. overlay peers and gateway | [partly]: gateways reach their slots; clear-slot overlay peers unreachable by ping (stage 2). [todo] full list, nginx -> `:6080` |
| 3 | **Tor** (see below) | [partly] |
| 4 | **Gateway start:** slot slice, no errors; reset-at-start resets a slot left busy by killing the gateway mid-session | [partly]: gateways start and use their slice. [todo] the kill-gateway-mid-session check |
| 5 | **MCP end to end through nginx** with a real client; script it (an Android case in `e2e_auth.py`) | [todo] (list below) |
| 6 | **Quarantine path:** make verify fail on purpose; slot quarantined, capacity shrinks, gateway restart reinstates | [todo] |

Step 3 (Tor), done: `tor` service reachable and route fixed (below); Chrome in the tor slot reaches `check.torproject.org`: `IsTor` true (exit
204.8.96.176 / .141 on different runs). [todo] redirect rules check (`iptables -t nat -S PREROUTING` matches only the android_tor interface);
two slots get different exits; `.onion` loads; UDP/QUIC and ICMP fail; with the tor container stopped the slot has **no** egress; `-dns-server
8.8.8.8` is answered by `DNSPort`; the `research-tor` browsers still work with the `tor` image (root + `setpriv`, runs as `debian-tor`, no caps,
SocksPorts unchanged).

Step 5 checklist, all [todo]:
- 401 unauthenticated; protected-resource metadata at `/.well-known/oauth-protected-resource/android-clear/mcp`;
- tool list = device tools + file tools, no browser tools; instructions text present;
- `initialize` with all slots busy: 503 immediately (and the client's error message is understandable);
- per-user limit of 1 session; second user on a free slot works; first user's session stays isolated;
- snapshot / click / type / screenshot job / `get_file_url` download / grep over a snapshot job;
- `get_upload_link` -> upload an APK in a browser -> `device_install_apk` it;
- **human handoff:** `device_request_human_help` link -> SSO -> noVNC page loads (path rewrite `.../view/<rid>/<id>/` in nginx for the
  `android-` prefix, websocket upgrade, password auto-fill via `password=`), the screen is visible and **controllable with mouse and
  keyboard**, sized sensibly; another user gets 403;
- end the session: the old link returns 403, a new session on the same slot has a new password, the old VNC connection is dropped at reset;
- idle timeout (`ANDROID_IDLE_TIMEOUT=60`): session ends and the slot is wiped; a pending human-help does **not** end it (until
  `ANDROID_HELP_TIMEOUT`), live-view activity also keeps it alive;
- MCP client disconnect: slot freed after `DISCONNECT_TIMEOUT_MINUTES`;
- gateway failures: kill the slot mid-session (clear error, session ends, slot recovers); kill only the emulator; fill `/out`.

Fixes made in this stage:
10. **Tor slot stale route.** Slots resolved `tasks.tor` once at start and installed a static default route; the tor task gets a new IP whenever
    it is replaced (every deploy), leaving the slot without network. Decided with the user: exec into the slot and resolve there. New
    `android-slot/tor-route.sh` (resolves `tasks.tor`, validates a dotted quad, `ip route replace default via <ip>` if different, exit 10 =
    changed); `entrypoint.sh` loops it until a default route exists; the wrapper (`reconcile.sh`, `fix_tor_route`) runs
    `docker exec -u root <slot> /app/tor-route.sh` for every tor slot on every pass (fixed command, only the exit code is read; logs "tor task
    moved, default route re-pointed"). `docker exec` has `NET_ADMIN` because the container is created with it; slotd's own set stays without.
    `test_reconcile.py` covers it. Verified live: `docker service update --force spider-hoarder_tor` -> route re-pointed within 18 s. Between a
    tor task replacement and the next wrapper pass (10 s) the slot has no network (fails closed).
11. **oauth2-proxy (dev) "DCR registration failed".** curl 8.x resolves `localhost` itself and ignores `/etc/hosts`, so
    `dev/localhost-is-host.sh` had no effect on the entrypoint's curl. `oauth2-proxy/Dockerfile` now installs GNU `wget` (busybox has no
    `--method=DELETE`) instead of curl, and `oauth2-proxy/entrypoint.sh` uses it for GET/POST/DELETE. Service 1/1, registers its client. The
    DELETE path is untested.

Exit: the step 5 script passes on both servers; handoff works in a real browser. **Not yet met.**

### Stage 5: Google sign-in and real apps (manual, with a human) [todo]
1. Through the live view, sign in to a throwaway Google account on `android-clear`, open Play Store, install an app; end the session; start
   another: **no account, no app** (`verify` ok and visually). The headline privacy guarantee: do it twice.
2. Same on `android-tor`: record what happens (expected: Google often blocks; document in the README note).
3. Use two or three real apps for `device_snapshot` quality (native list app, webview-heavy app, game/canvas): note where the outline is
   unusable and whether `device_tap`/screenshots are enough (feeds the "webview/Chrome automation out of scope" decision).

Exit: no state survives a session; usability notes recorded.

### Stage 6: soak and capacity [todo]
1. Loop 50 sessions on one slot (start, a few tool calls, end): reset time stays flat, upper-layer size bounded (reset does not recreate it;
   only the wrapper wipes it on recreate: if the upper grows without bound add a periodic recreate every N sessions), no leaked processes
   (`ps` in the slot), no tmpfs growth, no zombie `adb`/`uiautomator`, gateway memory flat.
2. Fill all slots with sessions from different users, free one; the next `initialize` succeeds on that slot; a multi-replica layout
   (`ANDROID_SLOTS_PER_REPLICA`, replica counts 2) gets distinct slot ranges with nginx's round-robin-on-503.
3. Host reboot / `docker restart` of the daemon: slots and wrapper come back; stale overlay mounts do not break the next start.
4. Redeploy with an unchanged image (no re-snapshot, slots untouched: `deploy.sh` force-updates the wrapper, which must **not** recreate
   matching slots; the no-re-snapshot half is confirmed, see Stage 1 step 9) and with a changed image (wrapper stops, slots go, snapshot
   rebuilt, slots recreated).

Exit: 50 clean sessions with flat resource use; redeploys behave as described.

## Likely trouble spots (check first when something fails)

| Symptom | Where to look |
|---|---|
| Slot never leaves `booting` | `docker logs`: emulator exit (KVM permission/gid, memory limit), snapshot not found (`ANDROID_AVD_HOME`), adb never sees device (the adb-after-emulator ordering, adb key), Xvfb not up |
| Emulator stalls for minutes, then "detected a hanging thread QEMU2 CPU0" and a segfault | `RLIMIT_NOFILE`: the slot needs `--ulimit nofile=65536:65536` (the emulator loops `fcntl` over every fd). A ~20 s hanging-thread warning on every snapshot restore is harmless |
| Emulator killed with no message | memory: 4g OOM-kills it, use 5-6g (guest RAM + ~3 GB); tmpfs `/tmp` full counts against it |
| `unauthorized` device | adb key differs from the one the golden userdata trusts: init must run on an empty volume with the baked key |
| Snapshot save hangs / "bad sub-command" | `adb emu` as the svc uid has no console auth token: use `console.py` (root); `-snapshot clean` autosave does not work |
| Reset passes but `verify` fails | baseline files differ from reality (Play auto-updates, GMS files on `/sdcard`, files created after boot such as `.nomedia`): fix the baseline (init waits for a stable listing), do not weaken the check silently. `/sdcard` is a symlink: listings need `find /sdcard/`; an empty baseline is a bug |
| Wrapper says "spec changed, recreating" for a missing slot | the `docker inspect` newline-on-missing handling in `reconcile.sh` |
| Wrapper cannot create overlay volume | `lowerdir` path (host path of the golden volume), SELinux/AppArmor on the daemon, kernel overlay support |
| noVNC loads, no picture | Xvfb size vs emulator window, x11vnc `-localhost` and websockify target, `Host`/path rewrite in nginx |
| `android-tor` slot has no network | tor interface match (`ANDROID_TOR_SUBNET` vs the real subnet), `ip_forward` sysctl actually set in the task, slot default route (a replaced tor task changes IP: the wrapper re-points it within ~10-20 s via `tor-route.sh`), `FORWARD` policy, firewall `10.192.0.0/10` ordering |
| Guest has "No internet" / `DNS_PROBE_FINISHED_NO_INTERNET` right after a restore (clear or tor) | stale wlan0 address after snapshot restore: `slotd.ensure_network()` toggles wifi; look for "guest network recovered by toggling wifi" in the slot log |
| TLS errors / odd dates in the guest | guest clock lags by the snapshot's age; not fixed |
| Gateway says slot "not ready" | slot health, secret mismatch (403), DNS alias, `SLOT_READY_TIMEOUT` shorter than a cold boot |
| Dev oauth2-proxy "DCR registration failed" | curl ignores `/etc/hosts` for `localhost`; the entrypoint uses `wget` for that reason |
| Slot quarantined after a script called `pool.reset()` with a session still open | test-harness artifact, not a product bug: end the session first |

## Deliverables

| Deliverable | Status |
|---|---|
| Fixes for everything found, each with a host regression test where it can be modelled without KVM | [partly]: tests added for watchdog, nofile flag, logcat, tor route; `ensure_network`, console.py and the sdcard baseline are only covered by live runs |
| `smoke_android.py` run and output recorded; an Android case in `e2e_auth.py` (stub Keycloak, through nginx) that runs unattended on a KVM host | [partly]: smoke passes on the clear slot; e2e case [todo] |
| Measured numbers in the docs: boot/restore/reset times, per-slot CPU/RAM/disk, image and golden-volume size, slots per host | [todo]; numbers are in this file (Host, Stage 1, Stage 2) |
| README "Android" section corrected (Play Store on Tor, first-run dialogs, host resources) | [todo] |
| The implemented plan's "Not run" paragraph replaced by "Verified on KVM host <date, host type>" | [todo] |
| Docs for the new env vars `ANDROID_BOOT_TIMEOUT`, `ANDROID_RESET_TIMEOUT`, `ANDROID_RAM_MB` (`docs/operations/deploy-and-configure.md`, `docs/architecture/android.md`) and for the tor route repair (the wrapper now execs one fixed command, `tor-route.sh`, in tor slots) | [todo] (`.env.example` is done) |

## Files likely to change

| File | Change |
|---|---|
| `android-slot/init-snapshot.sh`, `slotd.py`, `Dockerfile`, `firewall.sh`, `entrypoint.sh`, `console.py`, `tor-route.sh` | whatever Stages 1-2 break (done so far: fixes 1-6, 9, 10) |
| `android-toolserver/android_tools.py`, `device.py` | key names, install call, retries; then `export_tools.py` (done: logcat) |
| `android-wrapper/reconcile.sh` | overlay creation, recreate-after-N-sessions if Stage 6 needs it (done: nofile, missing detection, boot timeout, tor route) |
| `docker-compose.yml`, `deploy.sh`, `.env.example`, `tor/entrypoint.sh`, `nginx.conf.template`, `oauth2-proxy/` | Stage 4 findings (done: timeouts, RAM stamp, F-Droid URL, oauth2-proxy wget) |
| `mcp-research/e2e_auth.py` | Android case |
| `README.md`, `docs/architecture/android.md`, `docs/operations/deploy-and-configure.md` | measured numbers, corrected claims, new env vars |
| `docs/implemented-plans/android-hoarder.md` | replace "Not run" with the verification record |

## Docs to update

`docs/architecture/android.md` (numbers, tor route repair, any changed behaviour), `docs/operations/deploy-and-configure.md` (host sizing,
new env vars), `README.md` (Android section), and the implemented plan's notes. On completion (all stages done), move this file to
`docs/implemented-plans/` and link it from `docs/implemented-plans/index.md`; until then it stays in `docs/plans/`.
