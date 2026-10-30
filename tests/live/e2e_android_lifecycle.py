"""Session lifecycle and failure paths of the Android servers through nginx (companion of e2e_android.py; same env and users).
One phase per run: python e2e_android_lifecycle.py <phase> [android-clear|android-tor]

  idle        session with no tool call is reaped after ANDROID_IDLE_TIMEOUT (+ the reaper's 60 s tick), slot wiped
  help        a pending device_request_human_help suspends the idle timer; a live-view page load refreshes it
  disconnect  a client that vanished without ending the session loses it after DISCONNECT_TIMEOUT_MINUTES
  killslot / killemu / fillout / quarantine
              the failure paths: the script asks the host to break something (marker file in $SHOTS, see inject()) and checks
              what a user sees and that the slot recovers

The short timeouts are set by the caller on the gateway service, e.g.
  docker service update --env-add ANDROID_IDLE_TIMEOUT=60 --env-add DISCONNECT_TIMEOUT_MINUTES=1 spider-hoarder_research-android-clear
and removed afterwards (`--env-rm`, or ./deploy.sh --dev)."""

import asyncio
import json
import os
import re
import sys
import time

import httpx

import e2e_android as e
from e2e_android import check

SHOTS = os.environ.get("SHOTS", "/tmp")
IDLE = int(os.environ.get("IDLE", "60"))
DISCONNECT = float(os.environ.get("DISCONNECT_MIN", "1")) * 60


async def inject(name, timeout=300):
    """Ask the host to do `name` (a script/shell step the test container cannot do) and wait until it says done."""
    open(f"{SHOTS}/INJECT_{name}", "w").write("")
    print(f"INJECT {name}", flush=True)
    deadline = time.time() + timeout
    while not os.path.exists(f"{SHOTS}/DONE_{name}"):
        if time.time() > deadline:
            raise TimeoutError(f"host did not do {name}")
        await asyncio.sleep(1)


async def mark(c):
    """Plant a marker in the device: it is still there only in the same session (a reaped session is replaced by a fresh slot)."""
    await c.call("device_shell", command="settings put global device_name E2EMARK")


async def marked(c):
    try:
        err, text = await asyncio.wait_for(c.call("device_shell", command="settings get global device_name"), 180)
    except BaseException as ex:  # noqa: BLE001
        return False, repr(ex)[:100]
    return (not err and "E2EMARK" in text), text[:100].replace("\n", " ")


async def session_alive(c):
    try:
        err, text = await asyncio.wait_for(c.call("device_current_app"), 60)
        return not err, text
    except BaseException as ex:  # noqa: BLE001
        return False, repr(ex)[:160]


async def idle():
    await e.wait_capacity()
    async with e.Conn("alice", owns_session=False) as c:
        ok, _ = await session_alive(c)
        check("session works", ok)
        await mark(c)
        await asyncio.sleep(IDLE + 130)  # idle + one reaper tick (60 s) + margin
        # the next call is served by a NEW session on a recreated slot (get_session books one), so look for the marker
        ok, text = await marked(c)
        check(f"idle session reaped after {IDLE} s (+ reaper tick): marker gone, device wiped", not ok, text)
    check("slot is back (wiped, recreated) for the next session", await e.wait_capacity())


async def help_phase():
    await e.wait_capacity()
    async with e.Conn("alice") as c:
        err, text = await c.call("device_request_human_help", reason="idle test")
        check("help link given", not err and "/view/" in text, text[:80])
        await mark(c)  # (this call ends the help wait; ask again)
        await c.call("device_request_human_help", reason="idle test")
        await asyncio.sleep(IDLE + 130)
        ok, text = await marked(c)  # this call ends the help wait again
        check("pending human help: session survives > idle timeout + reaper tick (same device)", ok, text)
        await asyncio.sleep(IDLE + 130)
        ok, text = await marked(c)
        check("after the help wait ended by a tool call, idle reaping applies again", not ok, text)
    await e.wait_capacity()


async def disconnect():
    await e.wait_capacity()
    # a client that opens a session, uses it once and vanishes (no DELETE, no end_session)
    r = e.raw_initialize("alice")
    sid = r.headers["mcp-session-id"]
    h = {**e.headers("alice"), "Accept": "application/json, text/event-stream", "mcp-session-id": sid}
    httpx.post(e.URL, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = httpx.post(e.URL, headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "device_current_app", "arguments": {}}}, timeout=120)
    check("session used once", r.status_code == 200 and "isError\":true" not in r.text.replace(" ", ""), r.text[:100].replace("\n", " "))
    t0 = time.time()
    check("slot busy while the vanished client holds it", e.raw_initialize("bob").status_code == 503)
    # the reaper looks every 60 s: gone after DISCONNECT (+ tick) plus the reset
    while time.time() - t0 < DISCONNECT + 60 + e.RESET_WAIT:
        await asyncio.sleep(5)
        if e.raw_initialize("bob").status_code != 503:
            sid2 = None
            break
    dt = time.time() - t0
    check(f"vanished client reaped, slot free again after {dt:.0f} s", dt < DISCONNECT + 60 + 150, f"limit {DISCONNECT + 60 + 150:.0f}")
    await e.wait_capacity()


async def killslot():
    await e.wait_capacity()
    async with e.Conn("alice", owns_session=False) as c:
        ok, _ = await session_alive(c)
        check("session works", ok)
        await inject("killslot")  # docker kill the slot container
        t0 = time.time()
        err, text = None, ""
        try:
            err, text = await asyncio.wait_for(c.call("device_current_app"), 90)
        except BaseException as ex:  # noqa: BLE001
            err, text = True, repr(ex)[:200]
        check("tool call after the slot died: an error, not a hang", err, f"{text[:160]} ({time.time() - t0:.0f}s)")
        await asyncio.sleep(5)
    t0 = time.time()
    check("slot recovered and usable for a new session", await e.wait_capacity(), f"after {time.time() - t0:.0f}s")


async def killemu():
    await e.wait_capacity()
    async with e.Conn("alice", owns_session=False) as c:
        ok, _ = await session_alive(c)
        check("session works", ok)
        await inject("killemu")  # kill only qemu inside the slot
        await asyncio.sleep(8)
        t0 = time.time()
        try:
            err, text = await asyncio.wait_for(c.call("device_current_app"), 90)
        except BaseException as ex:  # noqa: BLE001
            err, text = True, repr(ex)[:200]
        check("tool call after the emulator died: an error, not a hang", err, f"{text[:160]} ({time.time() - t0:.0f}s)")
    await e.wait_capacity()
    async with e.Conn("alice") as c2:
        ok, text = await session_alive(c2)
        check("a new session works after recovery", ok, text[:100])


async def fillout():
    await e.wait_capacity()
    async with e.Conn("alice") as c:
        await inject("fillout")  # root: fill the slot's /out tmpfs
        err, text = await c.call("device_snapshot")
        check("snapshot with /out full: a clear error or success, not a hang", True, f"err={err} {text[:200]}")
        err2, text2 = await c.call("device_current_app")
        check("session still usable afterwards", not err2, text2[:100])
        await inject("fillout-clean")


async def quarantine():
    await e.wait_capacity()
    await inject("break-baseline")  # host: add a bogus line to the golden baseline-packages.txt (restored later)
    async with e.Conn("alice") as c:
        ok, _ = await session_alive(c)
        check("session works on the already running slot", ok)
    # the end of the session recreates the slot from the changed golden volume: verify must fail -> quarantine
    await asyncio.sleep(150)
    r = e.raw_initialize("alice")
    check("quarantined slot is not handed out (503 'capacity')", r.status_code == 503 and "capacity" in r.text, f"{r.status_code} {r.text[:100]}")
    await inject("restore-baseline")  # host: restore the file, restart the gateway
    check("after the restore and a gateway restart the slot is reinstated", await e.wait_capacity())


PHASES = {"idle": idle, "help": help_phase, "disconnect": disconnect, "killslot": killslot, "killemu": killemu,
          "fillout": fillout, "quarantine": quarantine}


async def wrapper():
    phase = sys.argv[1]
    if len(sys.argv) > 2:
        e.EGRESS = sys.argv[2]
    try:
        await PHASES[phase]()
    except BaseException as ex:  # noqa: BLE001
        import traceback
        traceback.print_exception(ex)
        e.FAILED.append(repr(ex)[:80])
    print("\nFAILED:" if e.FAILED else "\nALL PASSED", e.FAILED or "")
    sys.exit(1 if e.FAILED else 0)


asyncio.run(wrapper())
