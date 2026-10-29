"""Soak test for one Android egress THROUGH NGINX on a deployed (dev) stack: N sessions back to back, each with a realistic
mix of tool calls, then end the session and time how long until the replacement slot serves again.
docs/plans/android-kvm-bringup.md stage 6 step 1. Run (same setup as e2e_android.py; one run per egress, both at once is
the "two slots at once" test):

    docker run --rm --network host -v $PWD:/app -w /app -e KC_ALICE_PASSWORD=... -e KC_BOB_PASSWORD=... \
        --entrypoint python mcp-research:local soak_android.py android-clear 50

Prints one line per session (TSV: n, start s, calls s, end-to-ready s, container-visible errors) and a summary. Resource
numbers (memory, overlay, processes, volumes) come from soak_sample.sh on the host, which sees the containers."""

import asyncio
import json
import statistics
import sys
import time

import e2e_android as e2e  # the EGRESS argument is read from sys.argv there

N = int(next((a for a in sys.argv[1:] if a.isdigit()), "50"))
USER = "alice" if e2e.EGRESS.endswith("clear") else "bob"  # one user per egress, so two soaks do not contend for a limit
PAGES = ["https://example.com", "https://en.wikipedia.org/wiki/Android_(operating_system)", "https://www.openstreetmap.org"]


async def one_session(i):
    t0 = time.time()
    errors = []
    async with e2e.Conn(USER) as c:
        start = time.time() - t0
        t1 = time.time()
        for url in PAGES[i % len(PAGES):] + PAGES[:i % len(PAGES)]:
            err, text = await c.call("device_open_url", url=url)
            if err:
                errors.append("open_url: " + text[:60])
            await asyncio.sleep(4)
            err, text = await c.call("device_snapshot")
            if err:
                errors.append("snapshot: " + text[:60])
            for _ in range(2):
                err, text = await c.call("device_swipe", x1=400, y1=800, x2=400, y2=300)
                if err:
                    errors.append("swipe: " + text[:60])
        err, text = await c.call("device_app_start", package="org.fdroid.fdroid")
        if err:
            errors.append("app_start: " + text[:60])
        err, text = await c.call("device_take_screenshot", filename="soak.png")
        if err:
            errors.append("screenshot: " + text[:60])
        calls = time.time() - t1
        end0 = time.time()
    # Conn.__aexit__ called device_end_session; the slot is recreated now
    ok = await e2e.wait_capacity()
    return start, calls, time.time() - end0, errors, ok


async def main():
    await e2e.wait_capacity()
    rows = []
    for i in range(N):
        try:
            start, calls, ready, errors, ok = await one_session(i)
        except Exception as e:  # noqa: BLE001
            print(f"{i + 1}\tEXCEPTION {e!r}"[:300], flush=True)
            rows.append(None)
            await asyncio.sleep(30)
            continue
        rows.append((start, calls, ready, errors, ok))
        print(f"{i + 1}\t{start:.1f}\t{calls:.1f}\t{ready:.1f}\t{'READY' if ok else 'NEVER-READY'}\t{'; '.join(errors)}", flush=True)
        if not ok:
            break
    good = [r for r in rows if r]
    if good:
        for name, k in (("session start", 0), ("tool calls", 1), ("end to ready", 2)):
            v = [r[k] for r in good]
            h = len(v) // 2
            print(f"{name}: min {min(v):.1f} median {statistics.median(v):.1f} max {max(v):.1f}; first half median "
                  f"{statistics.median(v[:h] or v):.1f}, second half {statistics.median(v[h:] or v):.1f}")
        bad = [i + 1 for i, r in enumerate(rows) if not r or r[3] or not r[4]]
        print(f"{len(good)}/{N} sessions ran; sessions with errors or not ready: {bad or 'none'}")


if __name__ == "__main__":
    asyncio.run(main())
