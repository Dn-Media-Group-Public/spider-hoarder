"""Live smoke test, run inside the container as uid 1000 (see README; BACKEND=browser_clear): real
sessions, tool list, HTML round trip, consent blocklist, isolation from inside a session."""

import asyncio
import sys

from spider_hoarder_browser_sdk import pages
from spider_hoarder_sdk.gateway import sessions

URL = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"


async def main():
    await sessions.pool.reset()
    tools = await sessions.upstream_tools()
    names = {t.name for t in tools}
    print("tools:", len(names), "browser_evaluate" in names, "browser_install" in names)
    html = (await pages.fetch_html("smoke@example.org", URL))[0]
    print("html:", len(html), html[:80].replace("\n", " "))
    bs = await sessions.new_session("smoke@example.org")
    try:
        # blocklisted host must not load; a normal one must
        for u in ("https://cdn.cookielaw.org/", "https://example.org/"):
            r = await bs.client.call_tool("browser_navigate", {"url": u})
            print(u, "->", sessions.result_text(r)[:160].replace("\n", " | "))
        # two browsers in parallel, one login (cookie) shared, ids isolated, last one closed ends the session
        a = pages.parse_result(sessions.result_text(await bs.client.call_tool("browser_start_session", {})))
        await bs.client.call_tool("browser_navigate", {"url": "https://example.org/", "browser": a})
        await bs.client.call_tool("browser_navigate", {"url": "https://example.com/"})
        for b in ({"browser": a}, {}):
            r = await bs.client.call_tool("browser_tabs", {"action": "list", **b})
            print("tabs", b, "->", sessions.result_text(r).replace("\n", " | "))
        await bs.client.call_tool("browser_cookie_set", {"name": "smoke", "value": "1", "domain": "example.org", "path": "/"})
        print("cookie via other browser:", await pages.cookies_for(bs, "https://example.org/"))
        r = await bs.client.call_tool("browser_tabs", {"action": "list", "browser": "bogus"})
        print("bogus id ->", sessions.result_text(r).replace("\n", " | "))
        print("end a:", sessions.result_text(await bs.client.call_tool("browser_end_session", {"browser": a})))
        print("free/busy:", len(sessions.pool.free), len(sessions.pool.busy))
    finally:
        await sessions.close(bs)
    print("after close free/busy/lost:", len(sessions.pool.free), len(sessions.pool.busy), len(sessions.pool.lost))


asyncio.run(main())
