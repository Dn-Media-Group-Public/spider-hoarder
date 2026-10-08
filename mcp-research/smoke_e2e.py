"""Live smoke test, run inside the container as uid 1000 (see README): real
sessions, tool list, HTML round trip, consent blocklist, isolation from inside a session."""

import asyncio
import sys

import sessions

URL = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"


async def main():
    await sessions.pool.reset()
    tools = await sessions.upstream_tools()
    names = {t.name for t in tools}
    print("tools:", len(names), "browser_evaluate" in names, "browser_install" in names)
    html = await sessions.fetch_html("smoke@example.org", URL)
    print("html:", len(html), html[:80].replace("\n", " "))
    bs = await sessions.new_session("smoke@example.org")
    try:
        # blocklisted host must not load; a normal one must
        for u in ("https://cdn.cookielaw.org/", "https://example.org/"):
            r = await bs.client.call_tool("browser_navigate", {"url": u})
            print(u, "->", sessions.result_text(r)[:160].replace("\n", " | "))
        print("free/busy:", len(sessions.pool.free), len(sessions.pool.busy))
    finally:
        await sessions.close(bs)
    print("after close free/busy/lost:", len(sessions.pool.free), len(sessions.pool.busy), len(sessions.pool.lost))


asyncio.run(main())
