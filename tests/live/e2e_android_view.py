"""The human handoff of an Android session in a real (headless) Chromium, through nginx + oauth2-proxy + Keycloak.
Companion of e2e_android.py (same env, same users; run it the same way with --network host, see there). Covers:
the link -> SSO -> noVNC page loads and connects, the device screen is visible and controllable with mouse and keyboard,
the owner sees it and another user gets 403, and after the session ends the link is dead and the connection dropped."""

import asyncio
import os
import re
import sys
import time

from playwright.async_api import async_playwright

import e2e_android as e
from e2e_android import BASE, EGRESS, HOSTHDR, PUBLIC, check, local

OUT = os.environ.get("SHOTS", "/tmp")
RULES = "MAP localhost:7297 127.0.0.1:7297, MAP localhost:8085 127.0.0.1:8085"  # bare "MAP localhost ..." is ignored; localhost resolves to ::1 first here


async def login(ctx, user):
    page = await ctx.new_page()
    return page


async def sso(page, url, user):
    """Open url; if the SSO sends us to Keycloak, log in as user. Returns the last response of the final navigation."""
    resp = await page.goto(url, wait_until="domcontentloaded")
    if "/realms/" in page.url and await page.locator("#username").count():
        await page.fill("#username", user)
        await page.fill("#password", os.environ[f"KC_{user.upper()}_PASSWORD"])
        async with page.expect_navigation(wait_until="domcontentloaded"):
            await page.click("#kc-login")
        resp = None
    if "OAUTH_GRANT" in page.url:  # first login of a user with this client: Keycloak asks for consent
        async with page.expect_navigation(wait_until="domcontentloaded"):
            await page.click("button[name=accept]")
        resp = None
    return resp


async def novnc_state(page):
    return await page.evaluate("() => { const c = document.querySelector('canvas'); return c ? {w: c.width, h: c.height, vis: c.offsetParent !== null} : null }")


async def main():
    await e.wait_capacity()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=[f"--host-resolver-rules={RULES}"])
        async with e.Conn("alice") as a:
            await a.call("device_open_url", url="https://example.com")
            await a.call("device_wait_for", text="Example Domain", timeout=60)
            err, text = await a.call("device_request_human_help", reason="e2e test")
            link = re.search(r"(https?://\S+/view/\S+?/start)", text).group(1)
            print("link:", link)
            check("human-help link is under the public host and the android prefix", link.startswith(f"{PUBLIC}/{EGRESS}/view/"), link)

            # owner
            ctx = await browser.new_context(viewport={"width": 1280, "height": 1100})
            page = await ctx.new_page()
            ws_closed = asyncio.Event()
            page.on("websocket", lambda ws: ws.on("close", lambda *_: ws_closed.set()))
            await sso(page, link, "alice")
            try:
                await page.wait_for_url(re.compile(r"/view/.*/vnc\.html"), timeout=30000)
            except Exception:  # noqa: BLE001
                await page.screenshot(path=f"{OUT}/view-stuck.png")
                print("stuck at", page.url, (await page.inner_text("body"))[:300].replace("\n", " | "))
                raise
            check("owner: redirected to vnc.html with the password in the URL", "password=" in page.url and "autoconnect=1" in page.url, page.url[:120])
            password1 = re.search(r"password=([^&]+)", page.url).group(1)
            try:
                await page.wait_for_function("() => { const c = document.querySelector('canvas'); return c && c.width > 100 && c.offsetParent !== null }", timeout=30000)
                st = await novnc_state(page)
                check("owner: noVNC connected and the canvas has the device screen", True, str(st))
            except Exception as ex:  # noqa: BLE001
                check("owner: noVNC connected and the canvas has the device screen", False, repr(ex)[:200])
            await asyncio.sleep(2)
            await page.screenshot(path=f"{OUT}/view-owner.png")
            st = await novnc_state(page)
            box = await page.locator("canvas").bounding_box()
            check("owner: screen sized sensibly (fits the viewport, aspect kept)", box and 300 < box["width"] <= 1280 and 300 < box["height"] <= 1100, f"canvas {st}, box {box}")

            # mouse + keyboard: tap the Chrome address bar, type, read it back through the tool API
            await a.call("device_press_key", key="back")
            await a.call("device_open_url", url="https://example.com")
            await a.call("device_wait_for", text="Example Domain", timeout=60)
            # Coordinates are page pixels of a 1280x1100 viewport with resize=scale (see view-owner.png): the emulator window is
            # laid out the same every time; Chrome's address bar is at (480, 183). (There is no emulator dialog on top since fix 25.)
            await page.screenshot(path=f"{OUT}/view-before-click.png")
            await page.mouse.click(480, 183)
            await asyncio.sleep(1.5)
            # Chrome's address bar re-takes focus while its search view opens, which can eat the keys typed right after the click:
            # wait, type slowly, and retry once (select all first); the number of attempts is reported.
            found, attempts = "", 0
            await asyncio.sleep(2)
            for attempts in (1, 2):
                if attempts == 2:
                    await page.keyboard.press("Control+a")
                await page.keyboard.type("hello vnc", delay=200)
                await asyncio.sleep(2)
                err, found = await a.call("device_find", text="hello vnc")
                if "hello vnc" in found:
                    break
            await page.screenshot(path=f"{OUT}/view-typed.png")
            check(f"keyboard + mouse in noVNC reach the device (typed text found via the tool API, attempt {attempts})", "hello vnc" in found,
                  found[:160].replace("\n", " | "))

            # another user: 403, both on the start link and on the page itself
            ctx2 = await browser.new_context()
            p2 = await ctx2.new_page()
            await sso(p2, link, "bob")
            body = (await p2.content())[:200]
            check("other user: start link is 403", "not yours" in body or "403" in body or "forbidden" in body.lower(), f"{p2.url[:100]} {body[:100]!r}")
            vnc_uri = re.sub(r"/start$", "/vnc.html", link)
            r2 = await p2.goto(vnc_uri, wait_until="domcontentloaded")
            check("other user: vnc.html is 403", r2 is not None and r2.status == 403, str(r2.status if r2 else None))
            r3 = await p2.goto(vnc_uri.replace("vnc.html", "websockify"), wait_until="domcontentloaded")
            check("other user: websockify path is 403", r3 is not None and r3.status == 403, str(r3.status if r3 else None))
            # unauthenticated: SSO redirect, not the page
            ctx3 = await browser.new_context()
            p3 = await ctx3.new_page()
            await p3.goto(vnc_uri, wait_until="domcontentloaded")
            check("no login: sent to the SSO login", "/realms/" in p3.url or "/oauth2/" in p3.url, p3.url[:100])

            # end the session: link dead, connection dropped
            err, text = await a.call("device_end_session")
            check("device_end_session", not err, text[:100])
            try:
                await asyncio.wait_for(ws_closed.wait(), 30)
                check("old VNC connection dropped at reset", True)
            except asyncio.TimeoutError:
                check("old VNC connection dropped at reset", False, "websocket still open after 30 s")
            p4 = await ctx.new_page()
            r4 = await p4.goto(link, wait_until="domcontentloaded")
            check("old link after the session: 403", r4 is not None and r4.status == 403, f"{r4.status if r4 else None} {p4.url[:90]}")
            r5 = await p4.goto(vnc_uri, wait_until="domcontentloaded")
            check("old vnc.html after the session: 403", r5 is not None and r5.status == 403, str(r5.status if r5 else None))
            a.owns_session = False

        # a new session on the same slot: new password
        await e.wait_capacity()
        async with e.Conn("alice") as a2:
            err, text = await a2.call("device_request_human_help", reason="again")
            link2 = re.search(r"(https?://\S+/view/\S+?/start)", text).group(1)
            check("new session has another link", link2 != link, link2)
            p5 = await ctx.new_page()
            await p5.goto(link2, wait_until="domcontentloaded")
            await p5.wait_for_url(re.compile(r"/view/.*/vnc\.html"), timeout=30000)
            password2 = re.search(r"password=([^&]+)", p5.url).group(1)
            check("new session has a new VNC password", password2 != password1, f"{password1[:3]}.. -> {password2[:3]}..")
        await browser.close()


async def wrapper():
    try:
        await main()
    except BaseException as ex:  # noqa: BLE001
        import traceback; traceback.print_exception(ex)
        print("EXCEPTION", repr(ex)[:400])
        e.FAILED.append(repr(ex)[:80])
    print("\nFAILED:" if e.FAILED else "\nALL PASSED", e.FAILED or "")
    sys.exit(1 if e.FAILED else 0)


asyncio.run(wrapper())
