"""One persistent Chromium context per session; a "browser" is an OS window plus its tabs.

Pages join a browser through page.opener() (popups, window.open) or an explicit claim
(windows/tabs this module creates). No locking between callers: two callers on one id clobber
each other by design (see docs/implemented-plans/python-browser-tool-server.md)."""

import asyncio
import re
import secrets

from playwright.async_api import BrowserContext, Page, async_playwright

from .mediapolicy import Policy

MAIN = "main"
WINDOW = {"left": 0, "top": 0, "width": 1440, "height": 900}


class ToolError(Exception):
    pass


class PageState:
    """Per-page logs the tools read: console, network, a pending dialog / file chooser."""

    def __init__(self, page: Page):
        self.console: list[tuple[str, str, str]] = []  # (level, text, location)
        self.network: list[dict] = []  # {"req", "resp"} in completion order
        self.net_mark = 0  # network[:net_mark] was already shown by browser_network_requests(new=true)
        self.dialog = None
        self.chooser = None
        self.console_mark = 0  # console[console_mark:] = since the last navigation
        page.on("console", lambda m: self.console.append((m.type, m.text, f"{m.location.get('url', '')}:{m.location.get('lineNumber', 0)}")))
        page.on("pageerror", lambda e: self.console.append(("error", str(e), "")))
        page.on("response", lambda r: self.network.append({"req": r.request, "resp": r}))
        page.on("requestfailed", lambda r: self.network.append({"req": r, "resp": None}))
        page.on("dialog", lambda d: setattr(self, "dialog", d))
        page.on("filechooser", lambda c: setattr(self, "chooser", c))
        page.on("framenavigated", lambda f: f == page.main_frame and setattr(self, "console_mark", len(self.console)))


class Registry:
    def __init__(self, ctx: BrowserContext):
        self.ctx = ctx
        self.tabs: dict[str, list[Page]] = {}  # browser id -> its pages, in creation order
        self.cur: dict[str, Page] = {}  # browser id -> current tab
        self.owner: dict[Page, str] = {}
        self.state: dict[Page, PageState] = {}
        self._claim: str | None = None
        self._ignore = False  # pages created for internal use (storage state) stay unattributed
        self._tasks: set[asyncio.Task] = set()
        self._new = asyncio.Lock()  # only serialises window creation (claims), not tool calls
        self.policy: Policy | None = None  # set once by set_policy; until then every tool but that one fails
        self.blocked: list[dict] = []  # {url, kind, layer}: requests the media policy stopped
        self.blocked_urls: set[str] = set()
        ctx.on("page", self._on_page)
        for p in ctx.pages:  # the initial page is main's first tab
            self._add(MAIN, p)

    def _on_page(self, page: Page) -> None:
        t = asyncio.create_task(self._attach(page))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _attach(self, page: Page) -> None:
        if self._ignore:
            return
        bid = self._claim
        if bid is None:
            opener = await page.opener()
            bid = self.owner.get(opener, MAIN)
        self._add(bid, page)

    def _add(self, bid: str, page: Page) -> None:
        self.tabs.setdefault(bid, []).append(page)
        self.owner[page] = bid
        self.state[page] = PageState(page)
        self.cur[bid] = page
        page.on("close", lambda p: self._removed(p))

    def _removed(self, page: Page) -> None:
        bid = self.owner.pop(page, None)
        self.state.pop(page, None)
        if bid is None or page not in self.tabs.get(bid, []):
            return
        self.tabs[bid].remove(page)
        if not self.tabs[bid]:
            del self.tabs[bid]
            self.cur.pop(bid, None)
        elif self.cur.get(bid) is page:
            self.cur[bid] = self.tabs[bid][-1]

    async def set_policy(self, policy: Policy) -> None:
        """One-shot: install layer A (route on resource type) and layer D (CDP Fetch on Content-Type of documents)."""
        if self.policy:
            raise ToolError("policy already set")
        self.policy = policy
        if policy.active:
            if not policy.media:
                await self.ctx.route("**/*", self._route)
            self.ctx.on("page", lambda p: self._spawn(self._hook(p)))
            for p in self.ctx.pages:
                await self._hook(p)

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _block(self, url: str, kind: str, layer: str) -> None:
        self.blocked.append({"url": url, "kind": kind, "layer": layer})
        self.blocked_urls.add(url)

    async def _route(self, route) -> None:
        r = route.request
        if self.policy.resource_blocked(r.resource_type):
            self._block(r.url, r.resource_type, "A")
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    async def _hook(self, page: Page) -> None:
        try:
            cdp = await self.ctx.new_cdp_session(page)
            cdp.on("Fetch.requestPaused", lambda ev: self._spawn(self._paused(cdp, page, ev)))
            await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Response"}]})
        except Exception:  # noqa: BLE001  the page closed under us
            pass

    async def _paused(self, cdp, page: Page, ev: dict) -> None:
        status = ev.get("responseStatusCode")
        ctype = next((h["value"] for h in ev.get("responseHeaders", []) if h["name"].lower() == "content-type"), None)
        bodyless = status is None or 300 <= status < 400 or status in (204, 205)  # redirects and empty replies carry no media
        blocked = not bodyless and self.policy.content_type_blocked(ctype)
        if blocked:
            self._block(ev["request"]["url"], ctype or "", "D")
        try:
            if blocked:
                await cdp.send("Fetch.failRequest", {"requestId": ev["requestId"], "errorReason": "BlockedByClient"})
            else:
                await cdp.send("Fetch.continueRequest", {"requestId": ev["requestId"]})
        except Exception:  # noqa: BLE001  the page closed or navigated away
            pass

    async def settle(self) -> None:
        """Wait for popups opened by the last action to be attributed."""
        await asyncio.sleep(0)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _window(self, bid: str) -> str:
        url = "about:blank"
        async with self._new:
            self._claim = bid
            try:
                cdp = await self.ctx.browser.new_browser_cdp_session()
                t = await cdp.send("Target.createTarget", {"url": url, "newWindow": True})
                for _ in range(100):
                    if bid in self.tabs:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise ToolError("browser window did not open")
                win = await cdp.send("Browser.getWindowForTarget", {"targetId": t["targetId"]})
                await cdp.send("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": WINDOW})
                await cdp.detach()
            finally:
                self._claim = None
        return bid

    async def start(self) -> str:
        return await self._window(secrets.token_urlsafe(12))

    async def resolve(self, bid: str | None) -> str:
        """Validate an id; `main` is created on demand. Unknown ids never reveal other ids."""
        bid = bid or MAIN
        if bid == MAIN and MAIN not in self.tabs:
            await self._window_as(MAIN)
        if bid not in self.tabs:
            raise ToolError("unknown browser id")
        return bid

    async def _window_as(self, bid: str) -> None:
        if not self.ctx.pages:
            self._claim = bid
            try:
                await self.ctx.new_page()
                await self.settle()
            finally:
                self._claim = None
            return
        await self._window(bid)

    def page(self, bid: str) -> Page:
        return self.cur[bid]

    async def new_tab(self, bid: str, url: str | None = None) -> Page:
        await self.cur[bid].bring_to_front()  # a new tab lands in the last-active window
        self._claim = bid
        try:
            page = await self.ctx.new_page()
            await self.settle()
        finally:
            self._claim = None
        if url:
            await page.goto(url)
        return page

    async def end(self, bid: str | None) -> int:
        """Close one browser (all its tabs), or all of them. Returns how many browsers remain."""
        for b in list(self.tabs) if bid is None else [await self.resolve_existing(bid)]:
            for p in list(self.tabs.get(b, [])):
                await p.close()
        return len(self.tabs)

    async def resolve_existing(self, bid: str) -> str:
        if bid not in self.tabs:
            raise ToolError("unknown browser id")
        return bid

    async def window_id(self, bid: str) -> tuple:
        cdp = await self.ctx.new_cdp_session(self.cur[bid])
        return cdp, (await cdp.send("Browser.getWindowForTarget"))["windowId"]


_ref = re.compile(r"^(f\d+)?e\d+$")


def locator(page: Page, target: str):
    """aria ref from the last snapshot (e3, f1e2) or a plain selector."""
    return page.locator(f"aria-ref={target}" if _ref.match(target) else target)


async def launch(cfg: dict):
    """Headed persistent context, as configured by session-group.sh. Returns (playwright, Registry)."""
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        cfg["userDataDir"], headless=cfg.get("headless", False), chromium_sandbox=False, no_viewport=True,
        # --enable-automation is what sets navigator.webdriver and the "controlled by automated software" bar
        ignore_default_args=["--enable-automation"], args=[*cfg["args"], "--disable-blink-features=AutomationControlled"])
    ctx.set_default_navigation_timeout(cfg.get("navTimeout", 30000))
    if cfg.get("initScript"):
        await ctx.add_init_script(path=cfg["initScript"])
    return pw, Registry(ctx)
