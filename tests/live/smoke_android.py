"""Live smoke test for the Android backends (BACKEND=android_clear or android_tor), run inside an android-<egress>-gateway container as uid 1000 on a KVM host
(see README): a real slot, a session, the tool list, screen reading and input, a file output, human-help
isolation, reset + verify after the session. Not part of the host tests: it needs a booted emulator."""

import asyncio
import os
import sys

os.environ.setdefault("BACKEND", "android_clear")

from spider_hoarder_sdk.gateway import sessions
from spider_hoarder_android_sdk import slots


async def main():
    assert sessions.driver.noun == "device", "run with BACKEND=android_clear|android_tor (inside android-<egress>-gateway)"
    await sessions.pool.reset()
    print("slots:", list(slots.SLOTS), "free:", sorted(sessions.pool.free))
    names = {t.name for t in await sessions.upstream_tools()}
    print("tools:", len(names), "device_snapshot" in names, "browser_navigate" in names)
    bs = await sessions.new_session("smoke@example.org")
    old_view = bs.id
    try:
        text = sessions.result_text(await bs.client.call_tool("device_snapshot", {}))
        print("snapshot:", text[:300].replace("\n", " | "))
        print("home screen has F-Droid:", "F-Droid" in text)
        await bs.client.call_tool("device_press_key", {"key": "home"})
        r = await bs.client.call_tool("device_open_url", {"url": sys.argv[1] if len(sys.argv) > 1 else "https://example.com"})
        print("open_url:", sessions.result_text(r).replace("\n", " | ")[:160])
        r = await bs.client.call_tool("device_wait_for", {"text": "Example Domain", "timeout": 60})
        print("wait_for:", sessions.result_text(r).replace("\n", " | ")[:160])
        name = "smoke.png"
        r = await bs.client.call_tool("device_take_screenshot", {"filename": name})
        png = await slots.backend.fetch_out(bs.uid, name, 50 * 1024 * 1024)
        print("screenshot bytes:", len(png), png[:4] == b"\x89PNG")
        # a second session on the same slot must be refused by the slot itself
        try:
            await slots.backend.start(bs.uid, {}, {"vncpass": "abcd1234"})
            print("ERROR: slot accepted a second session")
        except RuntimeError as e:
            print("second start refused:", e)
    finally:
        await sessions.close(bs)
    print("old view id gone:", old_view not in sessions.by_view)
    print("after close free/busy/lost:", len(sessions.pool.free), len(sessions.pool.busy), len(sessions.pool.lost))
    print("verify:", await slots.backend._ok(bs.uid, "GET", "/internal/verify", 60))


asyncio.run(main())
