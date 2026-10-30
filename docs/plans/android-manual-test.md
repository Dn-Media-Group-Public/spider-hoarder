# Plan: Android manual test (Google sign-in and real apps)

Status: **todo**, needs a human. Split out of [Plan: Android KVM bring-up](../implemented-plans/android-kvm-bringup.md) (its W6 / stage 5)
when that plan was closed on 2026-10-29: everything that can be run unattended was run there, this is what is left and cannot be.
Tags: **[done]**, **[partly]**, **[todo]**.

## Problem / Goal

The privacy guarantee of the Android servers is "no state survives a session": no Google account, no installed app, no file. The
unattended checks prove it for what a script can dirty (an APK, a created account, a Chrome login stand-in, files on `/sdcard`; see
the "W2 re-run 2" results in the bring-up plan). They cannot sign in to a real Google account, because that needs a person (CAPTCHA,
phone checks, a live Play Store). Goal: do it once by hand, on both egresses, record what happens, and judge how usable the
`device_snapshot` outline is on real apps. Done = the exit criteria at the end hold and the findings are in the README note and the
tool descriptions.

## Prerequisites

- A dev (or any) deployed stack with Android enabled and both slots `free` (`docker stack services`, or
  `docker exec spider-hoarder-android-clear-1 ...health`). The bring-up plan's results sections list the commands.
- A **throwaway Google account** (never a real one: its credentials end up typed into a shared emulator, and Google may lock it).
  For `android-tor` a second throwaway account, since a sign-in from a Tor exit is likely to be challenged and may lock the account.
- A browser logged in to the stack's SSO, and an MCP client (Claude Code with both servers added, or `tests/live/e2e_android.py`'s
  `Conn` class) to start sessions and call `device_request_human_help` for the live-view link.
- Two or three real apps to try (one native list app, one webview-heavy app, one game or canvas app), installable from Play Store or F-Droid.
- Do **not** reboot the host and do not redeploy during the run: a redeploy removes every slot and ends the session.

## Procedure

1. **Sign in and install, `android-clear` [todo].** Start a session, call `device_request_human_help`, open the link, sign in to the
   throwaway account in Settings or Play Store, open Play Store and install an app. Also log in to Chrome with the account (the one
   bring-up item that needed an account, see W2). End the session with `device_end_session`. Start another session on the same server:
   **no account, no installed app** (`device_snapshot` or the live view; `/internal/verify` is `ok`). The headline guarantee: do it **twice**.
2. **F-Droid install flow [todo].** Walk through installing an app from F-Droid by hand, including the "unknown sources" prompt
   (not walked through in W3: a script cannot answer it). Note whether the tool descriptions' "first steps" hint matches what a user sees.
3. **`android-tor` [todo].** The same sign-in on `android-tor`. Expected: Google often blocks or challenges Tor exits. Record exactly what
   happens (blocked at which screen, CAPTCHA, phone check) and whether Play Store works signed out. This feeds the README sentence
   "Play Store is best effort on `android-tor`".
4. **Real apps [todo].** Use two or three real apps through `device_snapshot`, `device_tap`/`device_click` and `device_take_screenshot`: a native
   list app, a webview-heavy app, a game or canvas app. Note where the outline is unusable (no refs, wrong bounds, huge text) and whether
   coordinates plus screenshots are enough. This feeds the "webview / Chrome automation is out of scope" decision.
5. **Handoff feel [todo].** While in the live view: mouse and keyboard, typing a password with special characters, screen size and
   scaling on a laptop and a phone browser, what the user sees when the session ends under them.

## Record

| Item | Result |
|---|---|
| Date, host, who ran it | |
| Step 1, round 1 and 2: state after the next session starts (account, apps, Chrome login) | |
| Step 2: unknown-sources flow, anything the hint misses | |
| Step 3: where Google stops a sign-in from a Tor exit | |
| Step 4: per app: snapshot usable (yes/partly/no), what broke | |
| Step 5: live-view problems | |
| Problems found (each with the fix or a follow-up) | |

Results go in a section "Results" at the bottom of this file while the plan is open, then with the move to
`docs/implemented-plans/`.

## Exit criteria

- After a session that signed in to Google and installed an app, the next session on the same server shows neither, twice, on `android-clear`.
- The `android-tor` behaviour is recorded and the README sentence says what was seen.
- Usability notes for the three kinds of app are recorded; anything that makes the outline unusable has a follow-up.

## Files to change

| File | Change |
|---|---|
| this file | results |
| `README.md` (Android section) | the real behaviour of Play Store / Google sign-in on `android-tor` |
| `android-toolserver/android_tools.py` (then `export_tools.py`) | tool descriptions, if the "first steps" or install hints are wrong |

## Docs to update

`docs/architecture/android.md` (limits seen on real apps), `README.md` (Android section). On completion move this file to
`docs/implemented-plans/` and link it from `docs/implemented-plans/index.md`; remove it from `docs/plans/index.md`.

See also: [Plan: Android KVM bring-up](../implemented-plans/android-kvm-bringup.md) (results, fixes, and the commands used to check a slot),
[Android emulators](../architecture/android.md).
