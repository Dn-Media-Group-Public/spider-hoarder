# Plan: Android bring-up and verification on a KVM host

Status: **in progress** (stages 0-4 done, 5-6 not started). Work started 2026-10-20, last updated 2026-10-26.
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
| 4 stack | [done] (W4, W5) | - |
| 5 Google sign-in (manual) | [todo] | all |
| 6 soak and capacity | [todo] | all |

## Remaining work, grouped into work stages (W1-W9)

The remaining [todo]/[partly] items of stages 2-6 and the deliverables, regrouped by the layer they touch so each work stage can be run in
one sitting. Do them in order; W1-W3 need only a slot (no stack), W4-W5 need the stack, W6-W7 are long or manual, W8 closes out. Tick
state is tracked here; the detailed checklists stay in the stage sections above.

| W | Name | Contents (from) | State |
|---|---|---|---|
| W1 | Slot isolation checks | Stage 2 step 6: TCP-level firewall proof (`10.0.2.2:9000/9001/5037/5900`, `169.254.169.254`, IPv6, svc-uid loopback-only), same list for a tor slot. Step 2: drop `NET_ADMIN` from `tini` or document it; tmpfs sizes under a session; whether `ANDROID_EMU_TMP` has any effect / `wipe()` covers emulator temp files | [done], results below (incl. fix 12) |
| W2 | Reset and overlay hygiene | Stage 2 steps 4-5: reset during a running tool call; 5x reset with the full dirtying set (APK, account, Chrome login, clipboard); overlay upper size, golden checksum unchanged, recreate gives a fresh upper. Host regression tests for `ensure_network`, `console.py`, sdcard baseline | [done, see "W2 re-run 2" and fix 16] |
| W3 | Tool server and device behaviour | Stage 3 leftovers: Unicode `device_type`, canvas tap, Play Store screen, check and trim `KEYS`, first-call latency after restore, F-Droid "first steps" hint in tool descriptions, then `export_tools.py`. Guest clock lag after restore (set time at session start) | [done], results below (fixes 17-20), re-tested on the deployed stack |
| W4 | Stack lifecycle and Tor | Stage 4 steps 1-4: wrapper kill/recreate, spec-hash change, scale-0, wrapper isolation; networks/DNS and nginx -> `:6080`; Tor leak checks (iptables scope, distinct exits, `.onion`, UDP/ICMP fail, tor stopped = no egress, `-dns-server`, `research-tor` unaffected); kill-gateway-mid-session reset-at-start; oauth2-proxy DELETE path | [done], results below; finding: `verify`'s `/sdcard` check flapped, removed as fix 21 |
| W5 | End to end through nginx | Stage 4 steps 5-6: Android case in `e2e_auth.py` (whole step 5 checklist), quarantine path, noVNC handoff in a real browser (mouse/keyboard, sizing, 403 for other user, link revoked after session) | [done], results in "W5 results" (fixes 22-29); the scripts are `e2e_android*.py`, not an `e2e_auth.py` case (see there) |
| W6 | Google sign-in and real apps (manual) | Stage 5, needs a human | [todo] |
| W7 | Soak and capacity | Stage 6, plus stage 3 step 4 (two slots at once, RSS vs limit, OOM in `dmesg`); decide on periodic slot recreate | [todo]; **start with the memory finding in the W3 results (slot at its 6 GiB limit, OOM kill seen)** |
| W9 | Copy less data per slot recreate | New slots copy the userdata qcow2 (~2.1 GB) up into their overlay on every session (see fix 15). Find out why the file is copied up at all (`-read-only` already puts guest writes in a temp qcow2 in `/tmp`), and how to avoid it: e.g. a qcow2 backing file on the golden volume instead of an overlayfs copy-up, mounting `/avd` read-only for everything but the few files the emulator must write, hard-linking, or making the userdata smaller. Measure recreate time before and after; goal: recreate no slower than the old in-place reset (44-83 s) | [todo] |
| W8 | Docs and close-out | Deliverables: measured numbers, README Android section, implemented plan's "Not run" paragraph, new env vars and `tor-route.sh` in the docs; move this plan to `docs/implemented-plans/` and link it in the index | [todo] |

### W1 results (2026-10-22, stack slots `spider-hoarder-android-{clear,tor}-1`, guest traffic modelled as `docker exec -u 2000 ... nc -z -w4`)

- **Firewall, TCP level: every internal target is rejected for the emulator uid, on both egresses.** Blocked: `127.0.0.1` ports 9000, 9001, 5037,
  5900, 5554, 5555, 6080 (the 10.0.2.2 alias lands on loopback); the slot's own overlay IP (9000, 9001, 80, 53); the Docker gateway / the tor
  container IP (same ports); `169.254.169.254`; `10.0.6.1/2`, `172.18.0.1`, `192.168.0.1`, `100.64.0.1`; IPv6 to `2606:4700:4700::1111`. Positive
  control: `1.1.1.1:443` is open on both. The loopback listeners are really up (svc uid reaches `127.0.0.1:9001`), so "blocked" is the rule and
  not a closed port. The live `iptables -S OUTPUT` and `ip6tables -S OUTPUT` match `firewall.sh` on both slots.
- **svc uid (3000):** loopback only. `127.0.0.1:9001` open; gateway / peer, `1.1.1.1:443`, `169.254.169.254` blocked; IPv6 rejected.
- **Not covered here:** the tor-specific leak checks (UDP/QUIC, ICMP, tor stopped) are in W4. The test is from the container's OUTPUT chain, not
  from inside the guest; the guest's own traffic is the same uid's connections by design (see `firewall.sh`), and W5's browser run will show it
  from the guest side.
- **Capabilities:** slotd `0x1eb` (no `NET_ADMIN`); Xvfb, adb, tool server and emulator children `0`; `no_new_privs` on every process. `tini`
  (PID 1) had `0x11eb`, i.e. `NET_ADMIN` (bit 12). **Fixed (fix 12):** `entrypoint.sh` is now the image `ENTRYPOINT` and does the root work
  (route loop, `firewall.sh`, `prep-homes.sh`), then `exec setpriv --no-new-privs --bounding-set=... tini -- python slotd.py`, so `tini` replaces
  it as PID 1 with slotd's 7 caps. Verified after a redeploy on both slots: PID 1 `CapEff 0x1eb`, `NoNewPrivs 1`, slots reach `free`, default
  routes present (tor slot via the tor task), same iptables rule counts as before; the golden snapshot was re-made (image id changed).
  `tor-route.sh` via the wrapper's `docker exec` is unaffected (exec caps come from the container config).
- **tmpfs sizes: `/tmp` is too tight.** `/tmp` (2 GB) holds the emulator's own temp dir `/tmp/android-unknown/` (so `ANDROID_EMU_TMP=/tmp` is the
  effective location, and `wipe()`'s `SWEEP` covers it, emu-owned files are removed): one qcow2 of **1.54 GB while idle and `free`** (stable over 60 s),
  plus 78 MB and 0.3 MB ones. That leaves ~512 MB for everything a session writes (APK installs, downloads, app data all go through the
  guest's userdata overlay). A session that writes more than that fills `/tmp` and the emulator dies or hangs. `/tmp` counts against the slot's
  memory limit (`Shmem` 3.1 GB in the container; `docker stats` 1.58 GiB of 6 GiB at idle). `/home/emu` 256 MB, `/home/svc` 128 MB,
  `/in` and `/out` 512 MB each: unused so far (not stressed). **Decided: raise `/tmp` to 4g (fix 14, see W2 results).** Stress test (fill the guest's storage until something gives) moves to W2/W7.

### W5 results (2026-10-26, deployed dev stack after `./deploy.sh --dev` with fix 21; real Keycloak, through nginx at `127.0.0.1:7297`; clear and tor slot)

Why not an `e2e_auth.py` case: that script runs its own gateway with a stub IdP, but the deployed gateways validate tokens against the real
Keycloak, and nginx routes only to them. So the new scripts use real tokens (password grant, throwaway client `e2e-android` and users alice, bob made
by `mcp-research/e2e_android_setup.sh`) and talk to the real stack. Run with `docker run --rm --network host -v $PWD:/app -w /app -e KC_ALICE_PASSWORD=...
-e KC_BOB_PASSWORD=... --entrypoint python mcp-research:local <script>` (they use `127.0.0.1` plus a `Host: localhost:7297` header: on this host `localhost`
resolves to `::1` first and hangs; the gateway only accepts its public host name).

**`e2e_android.py` (both egresses): all pass.** 401 without a token; protected-resource metadata; 25 tools (20 `device_*` + file tools + `device_request_human_help`,
`device_end_session`), no `browser_*`; instructions present; snapshot job, `grep_file` over it, `device_open_url`, `device_wait_for`, `device_find` ->
`device_click` by ref, screenshot job -> `get_file_url` -> PNG download through nginx, tampered link rejected, `get_upload_link` -> APK upload through
nginx -> `device_install_apk` -> package listed, install of a missing file fails cleanly; second initialize of the same user and of another user with the slot
busy: `503` in 0.1 s with a readable message (`you already have 1 device sessions; end one first` / `capacity reached; try again later`).
Not testable with one slot per egress: "second user on a free slot works" (W7, two slots).

**`e2e_android_view.py` (human handoff in headless Chromium through nginx + oauth2-proxy + Keycloak): all pass.** The link is under the public host and the
`android-` prefix; SSO login (and Keycloak's first-login consent page) -> `vnc.html` with `password=` and `autoconnect=1`; noVNC connects and shows the
800x1000 screen, scaled to fit a 1280x1100 viewport; **mouse and keyboard work** (a click on Chrome's address bar plus typed text was found by `device_find`; the first version of the test
also saw a click dismiss the nested-virtualization dialog, which fix 25 then removed). Observed once: text typed at 80 ms per key straight after the click lost all but the first
character (Chrome's address bar re-takes focus while its search view opens); with a 2 s wait and 200 ms per key it arrived whole in 2 of 2 runs, and the test retries once and reports the attempt number; another user gets 403 on the start link, `vnc.html` and the websocket path; without login: redirect to the IdP; after
`device_end_session` the websocket is dropped, the old link and `vnc.html` give 403, and the next session has another link and another VNC password.

**`e2e_android_lifecycle.py`** (short timeouts set on the clear gateway with `docker service update --env-add`, removed afterwards):
- **idle** (`ANDROID_IDLE_TIMEOUT=60`): a planted device setting is gone after idle + one reaper tick (the next call starts a *new* session on the recreated slot,
  so a call succeeding after a reap proves nothing: look at device state). **help:** with `device_request_human_help` pending the session survives idle + tick;
  after a tool call ended the wait, idle reaping applies again. Not tested: that opening the live view refreshes the timer (by code: `/authz` sets `last_used`
  on each page/websocket request, an open noVNC connection does not).
- **disconnect** (`DISCONNECT_TIMEOUT_MINUTES=1`): a client that vanished without DELETE/`device_end_session` held the slot (`503` for others), reaped and free again after 173 s.
- **kill only the emulator:** the tool call fails in 1 s with `device 'emulator-5554' not found`, the slot is replaced, a new session works.
- **fill `/out`** (512 MB tmpfs): `device_snapshot` fails with `[Errno 28] No space left on device`, the session works again after the space is freed.
- **quarantine:** a bogus line added to the golden `baseline-packages.txt` (restored byte for byte, same sha256) makes the recreated slot fail `verify`: gateway log
  `uid 1 QUARANTINED: slot 1 not clean after reset: ['third-party packages differ from the baseline']`, a new `initialize` gets `503 capacity`, after the restore and a
  gateway restart the slot is back.
- **kill the whole slot container mid-session:** first run NOT OK (hang, slot unusable for 5+ minutes); fixed, see fixes 26-28. Now the call fails at once with `the device session ended unexpectedly (ConnectError: [Errno -2] Name or service not known); start a new one`, and the replacement is usable 72 s later, not quarantined.

**Found and fixed in W5**
- **Fix 22, a slot could stay `busy` in the pool forever (every client got `503 capacity`).** `sessions.close()` waited 60 s for the session task, but an Android reset takes
  60-120 s (74 s seen). The timeout cancelled the task inside `pool.release()`; `release` shields `stop()`, but the `busy -> free` bookkeeping after it ran in the cancelled
  task and was skipped, so the clean, free slot was never handed out again (until a gateway restart). Now `Pool.release` runs stop + bookkeeping as its own task
  (cancel-safe) and `close()` waits `SLOT_RESET_TIMEOUT + 90 s` on Android. Test `test_pool.py::test_cancelled_release_still_frees_the_uid` (fails on the old code).
  The browser pool gets the same `release` (no behaviour change there).
- **Fix 23, nginx rejected uploads above 1 MB** (`413`): no real APK could be uploaded through `get_upload_link`. `client_max_body_size 50m` on the blob/upload location (= the gateway's `MAX_FILE_BYTES`).
- **Fix 24, the SSO redirect lost the published port** (`http://localhost/oauth2/start`, dev: connection refused) and would downgrade `https` to `http` behind the LB in prod:
  `absolute_redirect off;` makes the 401 -> login redirect relative.
- **Fix 25, a modal "Emulator is running using nested virtualization" dialog sat on the device in every live view** (it was in the first screenshot): the emulator is now started with
  `-no-nested-warnings` (`slotd.emulator_cmd()`, test added). Changes the image, so the golden snapshot was re-made on that deploy.

- **Fix 26, a tool call in flight when the slot container dies hung for good.** Diagnosis (my first two explanations, "no FIN, silent drop" and "needs TCP keepalive", were wrong; checked with `/proc/net/tcp` in the gateway
  during the hang: the connections to the old IP were in `TIME_WAIT`, i.e. closed cleanly): the SDK's GET stream reconnects, gets `ConnectError: Name or service not known` (the slot's DNS name is absent until the wrapper
  recreates the container, ~3 s), the session task `_run_client` ends with `bs.error`, and the SDK never wakes a call that is already waiting for its answer. `sessions._Guarded` (the session's `client`) now races every `call_tool`
  against `bs.dead`, which `_run_client` sets first thing in its `finally`; the user gets an error naming the cause. Also applies to the browser sessions. Tests `test_a_call_in_flight_fails_when_the_session_task_dies`.
- **Fix 27, a slot killed mid-session ended up quarantined** (`QUARANTINED: [Errno -2] Name or service not known`, until a gateway restart): the cleanup POSTed `/internal/reset` to the vanished name and failed. `_reset_and_verify`
  now treats "cannot reach the old container" as "already gone": it waits for the replacement to be `free` (fresh by construction) and still runs `verify`. Test `test_reset_of_a_vanished_slot_waits_for_the_replacement_instead_of_failing`.
- **Fix 28, TCP keepalive and `TCP_USER_TIMEOUT` on the gateway-to-slot MCP connections** (decided by the user: these connections stay inside the stack, so a minute without an answer means the peer is gone): keepalive 20 s idle +
  3 probes x 10 s, `TCP_USER_TIMEOUT` 60 s (needed as well: keepalive probes only run when no data is unacknowledged), Android sessions only (`sessions._stack_client`). Verified in place (keepalive timer `02` on the sockets) but it
  was **not** what fixed the hang above; it covers a peer that dies without closing its sockets (host crash, network partition). Test `test_gateway_to_slot_connections_detect_a_dead_peer`.

- **Fix 29, "Bluetooth keeps stopping" covered the screen in some first sessions (the "3 of about 10 first runs" whose `device_wait_for` timed out).** The dialog hides the app from uiautomator (its tree is the dialog's
  window only), so `device_wait_for`/`device_find` saw nothing and the first snapshot was tiny. Root cause, found by reading the crash log of a session: the emulator provides Bluetooth (and a Wi-Fi packet stream) through a helper,
  `netsimd`, over **loopback**, and the slot firewall rejects loopback for the emulator uid on purpose. In a slot `netsimd` panics (`failed to create signal pipe: Connection refused`), the emulator logs `Unable to connect
  to packet streamer, no bluetooth emulation available`, and the guest's Bluetooth stack, still switched on (`bluetooth_on=1`, Android 14), has no controller: `libbluetooth_jni` aborts (`Can't start stack, last instance: starting
  Controller`) and is restarted. This happened in **every** session (`dumpsys bluetooth_manager`: "Bluetooth crashed 1 time"), the visible dialog only when a crash landed at the wrong moment (hence intermittent, 0 of 22 reproductions
  after the first hit). The golden image is built without the firewall, where `netsimd` works, so its snapshot held a running stack that cannot survive in a slot. Fix: `init-snapshot.sh` turns Bluetooth off before the snapshot
  (`cmd bluetooth_manager disable`, `wait-for-state:STATE_OFF`, `bluetooth_on` = 0, each checked). Wi-Fi was never affected (the emulator falls back to its user-mode network stack, which is what has been working all along).
  Verified after the redeploy: 6 of 6 fresh sessions (3 per egress) `bluetooth_on=0`, "Bluetooth crashed 0 times", no Bluetooth lines in `logcat -b crash`, no dialog now or 30 s later; both e2e runs pass.
  Test `test_init_turns_bluetooth_off_before_the_snapshot_is_saved`. Not done: stopping a guest user from switching it back on in Settings (they would only get the old crash).

**Open findings**
1. Low priority: a call with a missing required argument answers `Error: 'ref'` (a leaked `KeyError`); fixing it touches `android-toolserver/` (a re-snapshot), so it was left.
2. Every deploy that changes `docker-compose.yml` or env restarts the wrapper, whose `SIGTERM` trap removes all slots (see W4): such a `./deploy.sh` costs ~2 min without capacity even when only nginx changed.

### Fix 21: `verify` no longer lists `/sdcard` (decided by the user, 2026-10-26; supersedes fix 16)

The listing compared the guest with itself (taken in the same boot, seconds earlier), so it could not see anything a previous session left, and it flapped on system churn from ~14 s after `free`.
Since fix 15 a slot is recreated with an empty overlay after every session, which is what guarantees clean files. `verify` now checks emulator alive, guest booted, third-party packages equal
the image-time baseline, zero accounts (these compare with fixed references). Removed: `sdcard_listing`, `settled_sdcard_listing`, `sdcard_at_free` (and the 3-30 s settle wait before `free`) in `slotd.py`,
`baseline-sdcard.txt` in `init-snapshot.sh` (so the golden snapshot is re-made on the next deploy). Test `test_verify_checks_packages_and_accounts_not_sdcard` (also asserts no `find`). Host tests pass.
**Not yet deployed or run live**: after the next `./deploy.sh`, check `verify` stays ok on an idle slot for minutes (the W4 polling loop). Optional later: check at slot start that the overlay upper is empty.

### Fix 15: a slot is recreated from scratch after every session (decided by the user, 2026-10-22; replaces the in-place reset)

Why: an in-place reset keeps the overlay upper layer (emulator-uid owned, mounted read-write at `/avd`, holding the userdata copy-up, `ram.bin`
and the snapshot), so an attacker who escaped the guest could persist state across sessions; recreating throws everything away and is simpler.
The wrapper must stay input-free (it holds `docker.sock`), so it is not asked to recreate; it only looks at whether its own slots run.
- `slotd` `POST /internal/reset` now answers and then exits (`end_session()`, `os._exit(0)` after 0.5 s, state `resetting` meanwhile);
  `boot_slot()` (was `reset_locked`) runs once at container start.
- `reconcile.sh`: `--restart no`, a stopped slot is **recreated** (`create_slot`: `wipe_state`, new container) instead of started, spec `spec-v4`,
  default `RECONCILE_INTERVAL` 3 s (was 10). A crash or a host reboot is handled the same way.
- Gateway `slots.py`: `_reset_and_verify` = `POST /reset` (30 s), `_wait_free(RESET_TIMEOUT)` for the replacement, then `verify`.
- Tests: `test_slot.py::test_end_session_...`, `test_reconcile.py` (stopped slot recreated, `--restart no`), `test_android.py` fake slot goes
  `resetting` then `free`. Host tests pass.
- **Deployed and W2 redone** (see "W2 re-run 2"). Resets took 59-122 s end to end (not analysed, speed is W9's). Still to check, in W4/W5: what
  the gateway and client see between the old slot's exit and the new one's `free` (and the DNS alias); that a slot that crashes at start does not
  recreate in a tight loop (3 s pass, each with a wipe). The stage-2 recipe's `curl .../reset` must now wait for `free`.
  The unresolved `verify` `/sdcard` baseline problem (below) is not affected: it appears after every snapshot restore, fresh container or not.

### W2 re-run 2 on fix 15 (2026-10-22, deployed stack, clear slot `spider-hoarder-android-clear-1` driven directly; speed not assessed)

- **Reset during a running tool call:** `POST /internal/reset` answered 200 at once; the slot came back as a **new container** and `free` after 40 s. The
  in-flight client call again just ended with an empty `TimeoutError` (client side, 40 s); nothing on the slot side to fix, W5 checks what a user sees.
- **5 cycles, full dirtying** (same set as before: APK installed and started, `/sdcard/leak.txt`, `/sdcard/Download/d.txt`, `/data/local/tmp/leak2`,
  `device_name`, a contact row, Chrome on example.com): state clean 5/5 after the reset; every cycle a **new container id**; during every
  reset the health walked `unreachable` -> `booting` -> `free`, never `free` from the old slot.
- **Persistence test (the point of fix 15):** before each reset a marker file was created as the emulator uid in `/avd/slot.avd/` (the writable
  overlay upper) and in `/tmp`; **both gone after the reset, 5/5**. (With the in-place reset the `/avd` one would have survived.)
- **Fix to the fix:** `boot_slot()` first set the state to `resetting`, which the gateway's start-up reset treats as "not clean, replace it"; it now
  stays `booting` (a gateway starting during a slot's boot leaves it alone). `health` was `booting` then `free` on a fresh deploy.
- **`verify` after a reset still fails (4 of 5; passed once, cycle 4), now for a clearer reason:** the init-time `/sdcard` baseline cannot match system
  activity after a restore: missing `/sdcard/Android/obb/.nomedia`; extra **randomly named** files such as
  `/sdcard/Android/data/com.google.android.apps.youtube.music/cache/exo/<16 hex>.uid` (a new name per boot) and the GMS
  `download_cache/soda-*.zip`. A fixed baseline or a fixed ignore list (option a) cannot cover the random names without patterns; option (b) (record
  the listing when the slot becomes `free`, compare with that) handles all of it. **Decided: (b), done as fix 16.**
- Exit criteria of stage 2 now: reset provably clean 5/5 **met** (state, persistence), overlay hygiene **met** (fresh upper by construction),
  `verify` **met** after fix 16.

### Fix 16: `verify` compares `/sdcard` with the listing taken when the slot became free (option b, decided by the user, 2026-10-22)

`slotd` takes `sdcard_at_free = settled_sdcard_listing()` (polls `find /sdcard/ -type f` every 3 s until two listings are equal, at most 30 s) just
before the state becomes `free`; `verify` compares the live listing with that, and with the init-time packages/accounts baselines as before.
`baseline-sdcard.txt` is still written and checked by init (non-empty) but `verify` no longer uses it. A slot with no such listing fails
`verify` ("no /sdcard listing from when the slot became free"). Tests: `test_verify_compares_sdcard_with_the_listing_taken_when_the_slot_became_free`,
`test_settled_listing_waits_until_the_listing_stops_changing`. **Live (deployed, same 5-cycle driver as re-run 2): `verify` after the reset was
ok 6/6** (mid-call reset plus the 5 cycles; was 1 of 6 before), state clean 5/5, new container and planted markers gone 5/5. Mid-session
`verify` correctly names the dirt (`leak.txt`, `d.txt`, changed packages) and also shows system noise appearing during a session (the GMS
`soda-*.zip`, `obb/.nomedia` come and go), which is irrelevant at reset time. Residual risk: a system file that appears in the seconds
between `free` and the gateway's `verify` would quarantine a slot; not seen in 6 runs. Watch for it in W5/W7 (the message now names the files).

### W2 results (2026-10-22, hand-run clear slot `s2-android-clear-1` on the new image; script kept outside the repo)

- **Reset during a running tool call:** `device_wait_for` (120 s) in flight, `POST /internal/reset` after 5 s: 200 after 70 s, slot `free`, `verify`
  ok. The in-flight client call did **not** get a clear error: it hung until the client's own 30 s timeout (`TimeoutError`, empty message) and the
  later session close got 404. Fine for the slot, but W5 must check what the gateway/client reports to a user in this case.
- **5 cycles, full dirtying** (installed APK `org.woheller69.arity` and started it, `/sdcard/leak.txt`, `/sdcard/Download/d.txt`,
  `/data/local/tmp/leak2`, `device_name=LEAKED`, a contact row, Chrome opened on example.com): after every reset all of it was gone and the launcher
  was in front (5/5). Resets took 72-81 s with the new image. `verify` reported the dirt before each reset (packages, `/sdcard`). Not covered:
  a Google account, clipboard (no tool sets it), Chrome login (needs an account: W6).
- **One unexplained `verify` failure:** after the *first* reset (cycle 1) `verify` said "/sdcard has files the baseline does not"; cycles 2-5 and
  the reset before them were ok, and no user data was left (probe clean). Cause not found: the message did not name the files, and the slot
  was torn down after the run. **Changed** `verify` to name up to 10 extra and 10 missing files (`slotd.py`), so the next occurrence is
  diagnosable; the golden baseline has 9 files (`.nomedia`, `.thumbnails/*`, YouTube exo `.uid`). If it recurs: compare
  `baseline-sdcard.txt` with the live listing right after a restore (likely files created late after boot, as in fix 4). **Exit criterion
  "reset provably clean 5/5" is therefore met for state, not yet for `verify`: re-run once on the next deploy.**
- **Overlay hygiene:** golden volume untouched (no file newer than `.stamp` after 3 slots and 7+ resets). A slot's upper layer is **2.1 GB**
  (`slot.avd/userdata-qemu.img.qcow2`, a full copy-up of the userdata qcow2 on first write), identical for the slot after 7 resets with APK installs
  and for a stack slot after one: bounded, does not grow per session. This is **~3x the plan's 0.75 GB**: budget ~2.2 GB per slot of disk
  (plus the 3.7 GB golden volume). `create_slot` does `wipe_state` (`rm -rf u w`) so a recreated slot starts with an empty upper (by code; the
  spec-hash recreate is exercised in W4).
- **Regression tests added** (`android-slot/test_slot.py`, all pass): `verify` names sdcard differences and handles `find /sdcard/` output;
  `ensure_network` toggles wifi only when the gateway stays silent; `console.py` auth handshake, command, and KO exit.
- **Found while testing, fixed (fix 13):** the wrapper's slot label was not scoped to a stack. A second wrapper pass (`STACK_NAME=s2`) deleted the
  real stack's `spider-hoarder-android-tor-1` as an "extra slot" (its tor count was 0) and the stack wrapper re-created it. Now slots also carry
  `spider-hoarder.stack=<STACK_NAME>` and every `docker ps` filter in `reconcile.sh` (extras, `remove_all`) includes it, so a wrapper touches
  only its own stack's slots. `test_reconcile.py::test_wrapper_only_touches_its_own_stack` fails without the filter. `deploy.sh`'s wait for
  `label=spider-hoarder.android-slot` to be empty stays global on purpose (conservative). Slots created before this change have no stack label;
  they are recreated by name by the spec bump below.
- **`/tmp` raised to 4g (fix 14)** (`TMP_SIZE` in `reconcile.sh`; was 2g, 1.5 GB used idle by the emulator's temp qcow2). The slot memory limit stays
  6g: the tmpfs is only charged for what is written. Still to be seen under load: peak slot memory with a busy session (W7 checks RSS vs the limit
  and `dmesg`). Spec hash is now `spec-v3`, so the next wrapper pass recreates every slot (new `/tmp` size and stack label).

**W2 re-run after the deploy (2026-10-22, new image with `spec-v3`, `/tmp` 4g, `verify` naming files; same script, 5 cycles).**
- State was clean 5/5 (files, setting, APK, contact gone; launcher in front); resets 64-83 s. Reset during a tool call: 200 after 58 s, same
  client-side `TimeoutError` as before. The stack's own slots (clear, tor) were left alone while the `s2` test slot was created by a wrapper with
  another `STACK_NAME` (fix 13 confirmed live), both stack slots came up with `/tmp` 4.0G and the new label.
- **`verify` after a reset failed 6/6 (the 5 cycles and the mid-call reset), and now says why:** the `/sdcard` listing right after a restore differs from
  `baseline-sdcard.txt` in system-managed files, in both directions: **missing** `/sdcard/Android/obb/.nomedia` (in 5 of 6 post-reset checks; it was
  present during one running session), and once **extra**
  `/sdcard/Android/data/com.google.android.googlequicksearchbox/files/download_cache/soda-en-US-v1001.zip` (the Google app's on-device speech
  model, downloaded in the background after the restore now that the guest has network). So the earlier single failure was this, not a fluke;
  it passed 4 of 5 last time by luck of timing. No user data is involved in any of them. **The check as written cannot pass reliably**:
  a baseline taken once at init cannot match files that system apps create or delete after each restore.
- **Open decision for the user** (the plan's rule is to fix the baseline and not weaken the check silently): (a) an explicit, documented
  ignore list for system-managed paths (the obb `.nomedia`, `.thumbnails/*`, `*/cache/*`, GMS `download_cache`), compared exactly for everything
  else; (b) make the check a *diff against the live state at restore*: slotd records the `/sdcard` listing right after boot settles
  (when it declares the slot `free`) and `verify` compares with that instead of the init-time baseline (catches what a session adds, ignores what
  the system did after restore); (c) drop the `/sdcard` listing from `verify` since a reset boots a pristine userdata overlay anyway, and keep
  packages and accounts. Recommendation: (b), with (a) not needed.

### W3 results (2026-10-22, stack slot `spider-hoarder-android-clear-1` driven by an MCP client / the handlers run inside the container; changed code copied to `/tmp/ts` there; re-tested deployed, see the last paragraph)

- **Unicode `device_type`: works** (`Ünï 日本 ñ €` typed into Chrome's address bar with `clear`, read back exactly). The 😀 emoji case was not reached (see ref
  finding). `data:` URLs through `device_open_url` fail with "unable to resolve Intent": Chrome refuses `data:` intents, not a quoting problem (list and
  `shlex.quote` forms behave the same, `;` `|` `&` URLs are fine). No code change.
- **Fix 17, ref of an input field died when its text changed:** `Node.sig` included the text, so after `device_type` the field's ref answered "gone from
  the screen" and a second `device_type` into the same ref failed. `sig` now leaves out the text of `EditText` nodes (other nodes unchanged; a changed
  button label is still another element). Test `test_ref_of_an_input_field_survives_typing_into_it`.
- **Fix 18, `KEYS` checked against the device, `tab`/`space`/`escape` did nothing:** uiautomator2's `pressKey` returns `False` for those three names
  (tool still said "Pressed"); the other 17 work (`back`/`home` also return `False` on the launcher, so the return value is not usable as an error).
  They now go out as keycodes (`KEYCODES` = 61, 62, 111). Live: `space` appended to the field text, `tab` moved focus off the address bar. **No key
  trimmed**: all 20 names are accepted. `left`/`right`/`center` took ~5 s each on the memory-starved guest (below).
- **Fix 19, guest clock (decided in the plan, no new design):** the guest was **52 min behind** the host after a restore. `cmd alarm set-time <ms>` works as the
  shell uid; `slotd.set_guest_time()` runs at the end of `boot()` and in `start_session()`. Verified by hand (clock matched the host right after); test
  `test_guest_time_is_set_from_the_host_clock_at_session_start`. Not yet seen in a deployed slot.
- **Fix 20, `device_app_start` reported the wrong foreground app:** it printed "Current app" at once; the Play Store took ~7 s, F-Droid's first start shows a
  notification-permission dialog (`permission_allow_button` / `permission_deny_button`) over which `current_app` still says Chrome. It now waits up to
  `START_WAIT` (5 s) for the package and otherwise says "<pkg> is not the foreground app yet ... take a device_snapshot". Test added.
- **First-call latency after restore: 0.9 s** (first `current_app` on a freshly created slot, 0.1 s the second time, 0.4 s for a snapshot). The 64 s of the
  starved first host is not reproduced on this host.
- **Canvas tap / swipe:** `device_tap` on raw coordinates focused Chrome's address bar; `device_swipe` ran (10 s on the starved guest). **Play Store signed
  out:** the outline shows "Sign in to find the latest Android apps..." and a `Button "Sign in"` (obfuscated ids `0_resource_name_obfuscated`), usable.
  **F-Droid first start:** the notification dialog above; hint added to the `device_app_start` description (F-Droid is there for getting apps, the Play
  Store starts signed out). The F-Droid install flow itself (unknown-sources prompt) was not walked through: do it in W6 with a human.
- `export_tools.py` re-run (`mcp-research/android-tools.json`); docs updated (`docs/api/tools.md`, `docs/architecture/android.md`). Host tests: all four suites pass.
- **Blocker found for W7: a slot is at its memory limit within minutes of normal use.** The emulator was **OOM-killed** (`OOMKilled=true`, slot `broken`)
  during a Chrome session about 30 min after start. On the fresh slot: cgroup `memory.current` 6.37 GB of 6.44 GB (`anon` 5.38 GB, `file` 0.95 GB, `shmem`
  0.53 GB), **qemu RSS 5.29 GB for a 3 GB guest**, the guest itself had 250 MB free and `lmkd` killed Chrome's processes; apps then took 30 s to start. So the
  rule "slot limit = guest RAM + 3 GB" (6 GiB) is too tight here: either raise `ANDROID_SLOT_MEMORY` (8g?), lower `ANDROID_RAM_MB`, or find why qemu holds
  ~2.3 GB over guest RAM (swiftshader, the snapshot load, 4 GB `/tmp` tmpfs with the 1.5 GB temp qcow2 is counted separately under `shmem`). **Decided by the user: `ANDROID_SLOT_MEMORY=8g`** (default in `reconcile.sh`, `docker-compose.yml`, `.env.example`, local `.env`; docs updated; spec hash changes, so a deploy recreates the slots).
  Experiments on a scratch slot (qemu RSS idle, 30 s after `free`): control 4.75 GB (heap 1.44 GB); `-no-window` 4.53 GB; `-gpu guest` 4.22 GB but `free` after 110 s (was 65 s);
  both 4.08 GB, 174 s. Guest RAM is 3 GB resident from the restore; the rest is emulator heap that grows ~0.6 GB under use. Flags not adopted. Also seen: the reset after the OOM kill took 87 s to `free`.

**W3 re-test on the deployed stack (2026-10-22, after `./deploy.sh --dev`: image and golden snapshot rebuilt, both slots `free`, 8 GiB limit, `OOMKilled` false,
memory 5.7 of 8 GiB after the run).** Driven through the real MCP tool server of the clear slot (session started via slotd `/internal/start`): **15 of 16
checks pass, the 16th is a test-expectation error.** Passed: 18 tools listed; first call 1.0 s; guest clock diff 0 s from the host after session start
(fix 19 live); all 20 keys accepted; open_url + wait_for; Unicode `Ünï 日本 ñ €` typed three times into the **same ref** (fix 17 live); `space` typed, `tab`
moved focus (fix 18 live); canvas tap focused the address bar; swipe; F-Droid first start (7.3 s, `app_start` says "not the foreground app yet" under the
dialog, fix 20 live); Play Store signed-out outline has "Sign in". The emoji case typed correctly (screenshot: `😀 emoji` in the address bar, Chrome even offers
to paste it from the clipboard, which is how uiautomator2's `send_keys` types) but the accessibility dump returns characters outside the BMP as `..`, so
`device_find`/`device_snapshot` cannot read an emoji back: a limit of the tree, noted in `docs/api/tools.md`. After the test the slot was reset: replacement
`free`, `verify` ok.

### W4 results (2026-10-22, deployed stack, clear slot and tor slot; guest traffic modelled as `docker exec -u 2000` in the slot; scripts kept outside the repo)

**Wrapper lifecycle**
- **Slot killed (`docker kill`):** the wrapper logged "exited, recreating from scratch" and had a new container within 3 s (the new slot reaches `free` after ~70-90 s).
- **Wrapper `SIGKILL`ed** (swarm restarts it): both slots untouched (same container ids), nothing recreated.
- **Any env change restarts the wrapper, and its `SIGTERM` trap removes every slot** (seen with `ANDROID_TOR_SLOTS` 1 to 2 and 2 to 1). So "only changed slots are recreated" cannot happen in
  practice: the spec hash and the "extra slot" branch (`reconcile_egress`) are only reached by a wrapper that survives the change, which
  does not occur with the compose file as it is. They are harmless, covered by `test_reconcile.py`, and the end result is right: after 2 to 1 the
  `tor-2` container **and its `-avd`/`-state` volumes** were gone (no leaked volumes), slot 1 had a new container and empty upper. Not run separately: a `ANDROID_SLOT_CPUS` change (same path).
- **`scale wrapper=0`:** all slots and all `*-avd`/`*-state` volumes gone after **7 s** (grace is 120 s); scale back to 1 recreates both, `free` after ~2-4 min on the cold start.
- **Wrapper isolation:** only on `android_wrapper` (internal, alone), no published ports, no default route (`wget 1.1.1.1`: "Network unreachable"), no listener except Docker's own resolver on `127.0.0.11`.
- **Not run:** a slot that crashes at start (tight recreate loop, 3 s pass each with a wipe): needs a deliberately broken image; do it in W7.

**Networks / DNS**
- nginx resolves `android-clear-1` and `android-tor-1` and the gateways; the clear gateway resolves only clear slots, the tor gateway only tor slots (`android-tor-1` is "Name or service not known" from the clear one).
  From the gateway the slot's `9000` and `9001` are open; `6080`, `5900`, `5037` are refused (VNC is `-localhost`, websockify only after a session start). nginx
  reaches the slot's `:6080` (`/vnc.html` 200) a second or two after `/internal/start`; `9000`/`9001` from nginx answer 403 without the secret (by design: the secret is the gate there).
- The overlay IPs of the slots are not reachable from the host (use `docker exec` into a slot for direct control calls; `curl` from the host times out).

**Tor** (tor slot, emu uid, two runs each, plus a clear-slot control)
- nat `PREROUTING` has exactly two rules, both `-i eth1` (the `android_tor` interface, 10.99.0.x): TCP to `9040`, UDP 53 to `5353`; `FORWARD` policy DROP, `ip_forward` 1; tor's other interfaces untouched.
- `check.torproject.org/api/ip`: `IsTor true` on every run; **two tor slots at once got different exits** (e.g. `...101.0`/`...101.6`, `...101.102`/`...101.6`, `...116.51`), single slot changes across tor restarts.
- `.onion`: DNS via `8.8.8.8` (the emulator's `-dns-server`) is answered by `DNSPort` with a `10.192.0.0/10` virtual address (`dns .onion` and a normal name both answered), and the Tor Project's onion service loads over the transparent proxy. (The first attempt used a wrongly remembered onion address: "Invalid hostname" in tor's log, not a defect.)
- **UDP and ICMP fail:** NTP (udp/123) and ICMP echo to public addresses get **no reply** in the tor slot, **and do get replies in the clear slot** (control). The container's own resolver (`127.0.0.11`) is rejected for the emu uid.
- **Tor stopped (`scale tor=0`): no egress:** TCP to `1.1.1.1:443` and `93.184.216.34:80` fail, DNS to `8.8.8.8` times out. After `scale tor=1` the wrapper re-pointed the default route (log "tor task moved, default route re-pointed"), and the slot is back on Tor. **Replacement (`service update --force tor`): route re-pointed within 10 s** and `IsTor true` again.
- **`research-tor` unaffected:** `tor` runs as uid 100, `CapEff 0`, `NoNewPrivs 1`; SocksPorts 9050/9051/9060 from the `research-tor` container each give `IsTor true` with different exits; 51 listeners (SocksPorts, 9040, 5353) up.

**Gateway start:** with the clear slot **busy** (session started directly on slotd, nobody holding it), `service update --force research-android-clear` -> the new gateway sent `reset` + `verify` (both 200, no "not clean" error), the slot came back as a new container and `free` after 92 s. (The old gateway task's own shutdown does not touch slots.)

**oauth2-proxy DELETE path:** the entrypoint only deletes at start, when the stored redirect URI differs from `${PUBLIC_SCHEME}://${PUBLIC_HOST}/oauth2/callback` (no trap on stop). Changing `PUBLIC_HOST` to `localhost:7299` -> the old Keycloak client was gone (`clients?clientId=` empty) and a new one registered with the new redirect URI; changing it back did the same again. Works. Note: from the host, `localhost:8085` hangs (IPv6?), `127.0.0.1:8085` works.

**Finding (resolved by fix 21, below): `verify` was only true for ~10 s after `free`.** Polling `/internal/verify` every 6 s from the moment a fresh slot became `free`: `ok` at +1 s and +7 s, **false from +14 s on and for the rest of the 4 minutes watched**:
the system keeps creating and removing files on `/sdcard` after the "settled" listing (the obb `.nomedia` disappears, the GMS `soda-*.zip` disappears (it was in the listing), a new `exo/<hex>.uid` appears), so a slot that has simply been idle for a minute fails `verify`
(the tor slot, free for 25 min, too). It is not a leak (no user data). Today this does not quarantine anything because the gateway calls `verify` only straight after `_wait_free` (polls every 1 s), i.e. inside the window, but it is timing luck (a slow poll or a busy
gateway and a clean slot is quarantined), and fix 16's "6/6 ok" was that luck. Options: (a) keep as is and document; (b) make `settled_sdcard_listing` wait for a long quiet period (>= 60 s, slows every start); (c) ignore a documented list of system paths (`*/cache/*`, `download_cache`, `obb/.nomedia`); (d) drop the `/sdcard` listing from `verify` (packages and accounts stay; a recreated slot has a fresh overlay anyway, fix 15). Recommendation: (d), or (c) if the file check is wanted.

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
Use a different `STACK_NAME` than the deployed stack (the wrapper only touches slots of its own stack since fix 13; before that it removed the other stack's slots). Clean up afterwards: `docker rm -f s2-android-clear-1; docker volume rm s2-android-clear-1-avd s2-android-clear-1-state; docker network rm s2_android_clear`.
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
| 1 | Every tool once against known screens (Launcher, Settings, F-Droid, Chrome `example.com`, Play Store signed-out): outline, `device_find`, stale refs, `device_type` Unicode/`clear`/`submit`, `device_wait_for`, `device_swipe`, `device_tap` on canvas, `device_open_url`, `device_app_*`, `device_logcat`, `device_shell`, screenshot PNG | [partly]: all 18 tools called once and behave; `device_open_url` + `wait_for "Example Domain"` pass. [done] in W3 (Unicode, canvas tap, Play Store screen; fixes 17-18, 20) |
| 2 | **Install APK** via `PUT /internal/in/...` then `device_install_apk`, deleted afterwards, non-APK fails cleanly | [done] (install works) |
| 3 | uiautomator2 assumptions: `app_list("-3")`, every `KEYS` name accepted by `d.press`, `install(..., nolaunch=True)`, `send_keys(clear=...)`, Chrome first-run dialogs, first `u2.connect` latency after restore | [partly]: install works; Chrome first run handled in init (stage 1 step 7). [done] in W3: every `KEYS` name checked (none trimmed), first call 0.9 s, F-Droid hint added |
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
| 1 | **Wrapper (W4: done, see "W4 results"):** slot names, overlay volumes, `volume inspect` lower/upper/work; kill a slot (restarted/recreated); change `ANDROID_SLOT_CPUS` (only changed slots recreated, upper wiped); `scale wrapper=0` removes all slots within the 120 s grace; wrapper has no network and is unreachable | [partly]: slots exist and run. [todo] kill/recreate, spec-hash change, scale-0, wrapper isolation check |
| 2 | **Networks/DNS:** gateway and nginx resolve the slots; nginx reaches `:6080`; firewall list from a stack slot incl. overlay peers and gateway | [partly]: gateways reach their slots; clear-slot overlay peers unreachable by ping (stage 2). [todo] full list, nginx -> `:6080` |
| 3 | **Tor** (see below) | [partly] |
| 4 | **Gateway start:** slot slice, no errors; reset-at-start resets a slot left busy by killing the gateway mid-session | [partly]: gateways start and use their slice. [todo] the kill-gateway-mid-session check |
| 5 | **MCP end to end through nginx** with a real client; script it | [done] in W5: `e2e_android.py` (both egresses), `e2e_android_view.py`, `e2e_android_lifecycle.py` |
| 6 | **Quarantine path:** make verify fail on purpose; slot quarantined, capacity shrinks, gateway restart reinstates | [done] in W5 |

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
13. **Wrapper scoped to its stack**, 14. **`/tmp` 4g** (see W2 results), 15. **slot recreated from scratch after every session** (see "Fix 15") and 16. **`verify` `/sdcard` live baseline** (see "Fix 16").
12. **`NET_ADMIN` removed from PID 1** (see W1 results): `ENTRYPOINT ["/app/entrypoint.sh"]`, which ends in `exec setpriv ... tini -- slotd.py`.
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
| `smoke_android.py` run and output recorded; an Android e2e through nginx that runs unattended on a KVM host | [done]: `e2e_android.py` / `_view.py` / `_lifecycle.py` (real Keycloak tokens on the dev stack; the stub IdP of `e2e_auth.py` cannot front the deployed gateways); smoke passes on the clear slot |
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
| `mcp-research/e2e_android.py`, `e2e_android_view.py`, `e2e_android_lifecycle.py`, `e2e_android_setup.sh` | new (W5); `e2e_auth.py` unchanged |
| `README.md`, `docs/architecture/android.md`, `docs/operations/deploy-and-configure.md` | measured numbers, corrected claims, new env vars |
| `docs/implemented-plans/android-hoarder.md` | replace "Not run" with the verification record |

## Docs to update

`docs/architecture/android.md` (numbers, tor route repair, any changed behaviour), `docs/operations/deploy-and-configure.md` (host sizing,
new env vars), `README.md` (Android section), and the implemented plan's notes. On completion (all stages done), move this file to
`docs/implemented-plans/` and link it from `docs/implemented-plans/index.md`; until then it stays in `docs/plans/`.
