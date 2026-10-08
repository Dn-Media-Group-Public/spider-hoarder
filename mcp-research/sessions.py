"""Browser sessions: one uid + one process group (Xvfb, x11vnc, websockify,
@playwright/mcp) each, started by sessiond. The gateway proxies MCP tool calls
to the session's playwright-mcp. All paths that end a session (idle reaper,
browser_end_session, MCP session close, crash) go through close()."""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client

import cmp_rules
import ports
from pool import Pool, Sessiond

log = logging.getLogger("sessions")

EGRESS = os.environ.get("EGRESS", "clear")  # "tor" | "clear"
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "1800"))
MAX_PER_USER = int(os.environ.get("MAX_SESSIONS_PER_USER", "3"))
UPSTREAM_HOST = "mcp-upstream"  # must match allowedHosts in session-group.sh

pool = Pool(Sessiond(os.environ.get("SESSIOND_SOCKET", "/run/sessiond.sock")))
_HOST_RULES = cmp_rules.host_rules()


@dataclass
class BrowserSession:
    owner: str
    uid: int
    mcp_session: Any = None
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    vnc_password: str = field(default_factory=lambda: secrets.token_urlsafe(6))  # VNC auth caps at 8 chars
    client: ClientSession | None = None
    error: BaseException | None = None
    running: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    last_used: float = field(default_factory=time.time)


by_mcp: dict[int, BrowserSession] = {}   # id(ServerSession) -> session
by_view: dict[str, BrowserSession] = {}  # view id -> session
_lock = asyncio.Lock()
_tools_cache: list[types.Tool] | None = None
_tools_lock = asyncio.Lock()


async def _wait_http(url: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    async with httpx.AsyncClient() as h:
        while time.time() < deadline:
            try:
                await h.get(url, headers={"Host": UPSTREAM_HOST}, timeout=2)
                return
            except httpx.HTTPError:
                await asyncio.sleep(0.5)
    raise TimeoutError(f"browser session did not come up: {url}")


async def _run_client(bs: BrowserSession) -> None:
    """Owns the session's uid and its upstream MCP connection for its lifetime."""
    try:
        env = {"UPSTREAM_SOCKS": f"tor:{ports.socks_port(bs.uid)}"} if EGRESS == "tor" else {}
        # clear: Chromium --host-rules; tor: the filter relay (host-rules don't apply when a proxy resolves names)
        files = {"vncpass": bs.vnc_password, "hostrules": "" if EGRESS == "tor" else _HOST_RULES}
        await pool.start(bs.uid, env, files)
        bs.running = True
        url = f"http://127.0.0.1:{ports.mcp_port(bs.uid)}/mcp"
        await _wait_http(url)
        async with streamablehttp_client(url, headers={"Host": UPSTREAM_HOST}) as (r, w, _):
            async with ClientSession(r, w) as cs:
                await cs.initialize()
                bs.client = cs
                bs.ready.set()
                log.info("session %s started for %s on uid %s", bs.id[:8], bs.owner, bs.uid)
                await bs.closing.wait()
    except BaseException as e:  # noqa: BLE001
        bs.error = e
        log.warning("session %s failed: %r", bs.id[:8], e)
    finally:
        bs.ready.set()
        if bs.running:
            await pool.release(bs.uid)
        else:
            pool.busy.discard(bs.uid)  # never started; pool.start already released it


async def _start(bs: BrowserSession) -> BrowserSession:
    bs.task = asyncio.create_task(_run_client(bs))
    await bs.ready.wait()
    if bs.error or not bs.client:
        raise RuntimeError(f"could not start browser: {bs.error!r}")
    return bs


async def close(bs: BrowserSession) -> None:
    if by_mcp.get(id(bs.mcp_session)) is bs:
        del by_mcp[id(bs.mcp_session)]
    by_view.pop(bs.id, None)
    bs.closing.set()
    if bs.task:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(bs.task, 60)
    log.info("session %s closed", bs.id[:8])


async def new_session(owner: str, mcp_session: Any = None) -> BrowserSession:
    """Take a uid and start a session on it (registered only if mcp_session given)."""
    uid = pool.take()
    bs = BrowserSession(owner=owner, uid=uid, mcp_session=mcp_session)
    if mcp_session is not None:
        by_mcp[id(mcp_session)] = bs
        by_view[bs.id] = bs
    try:
        return await _start(bs)
    except Exception:
        await close(bs)
        raise


async def get_session(mcp_session: Any, owner: str, create: bool = True) -> BrowserSession | None:
    async with _lock:
        bs = by_mcp.get(id(mcp_session))
        if bs and bs.mcp_session is mcp_session:
            if bs.owner != owner:
                raise PermissionError("session owner mismatch")
            if bs.error or (bs.task and bs.task.done()):
                await close(bs)
            else:
                return bs
        if not create:
            return None
        if sum(1 for s in by_mcp.values() if s.owner == owner) >= MAX_PER_USER:
            raise RuntimeError(f"you already have {MAX_PER_USER} browser sessions; end one first")
        if len(by_mcp) >= ports.MAX_SESSIONS_TOTAL:
            raise RuntimeError("browser capacity reached; try again later")
        return await new_session(owner, mcp_session)


async def upstream_tools() -> list[types.Tool]:
    """Tool list of @playwright/mcp, fetched once from a throwaway session."""
    global _tools_cache
    async with _tools_lock:
        if _tools_cache is None:
            probe = await new_session("probe")
            try:
                _tools_cache = (await probe.client.list_tools()).tools
            finally:
                await close(probe)
    return _tools_cache


def result_text(res: types.CallToolResult) -> str:
    return "".join(c.text for c in res.content if isinstance(c, types.TextContent))


def parse_eval_result(text: str) -> str:
    """Unwrap browser_evaluate's reply: '### Result\\n<json>\\n### Ran Playwright code...'"""
    body = text.split("### Result", 1)[-1].lstrip("\n")
    body = body.split("\n### ", 1)[0].strip()
    value = json.loads(body)
    if not isinstance(value, str):
        raise ValueError("browser_evaluate did not return a string")
    return value


# Scroll to trigger lazy content, then wait until the DOM has been quiet for 1s (max 10s) so
# client-side rendered pages are complete before we read them.
SETTLE_JS = """async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let last = Date.now();
  const obs = new MutationObserver(() => { last = Date.now(); });
  obs.observe(document.documentElement, {subtree: true, childList: true, characterData: true});
  const y0 = window.scrollY;
  for (let y = 0, i = 0; y < document.body.scrollHeight && i < 30; y += window.innerHeight, i++) {
    window.scrollTo(0, y);
    await sleep(150);
  }
  window.scrollTo(0, y0);
  const t0 = Date.now();
  while (Date.now() - last < 1000 && Date.now() - t0 < 10000) await sleep(100);
  obs.disconnect();
  return document.documentElement.outerHTML;
}"""


async def page_html(bs: BrowserSession) -> str:
    res = await bs.client.call_tool("browser_evaluate", {"function": SETTLE_JS})
    if res.isError:
        raise RuntimeError(result_text(res))
    return parse_eval_result(result_text(res))


async def cookies_for(bs: BrowserSession, url: str) -> dict:
    """The session's browser cookies applicable to url, as a name->value dict for `requests`."""
    code = "async (page) => JSON.stringify(await page.context().cookies(%s))" % json.dumps(url)
    res = await bs.client.call_tool("browser_run_code_unsafe", {"code": code})
    if res.isError:
        raise RuntimeError(result_text(res))
    return {c["name"]: c["value"] for c in json.loads(parse_eval_result(result_text(res)))}


async def fetch_html(owner: str, url: str, live: BrowserSession | None = None) -> str:
    """One-shot: navigate, read HTML. Without `live`: ephemeral session (own uid), destroyed after.
    With `live`: a temporary tab in that session, so its cookies apply."""
    if live:
        await live.client.call_tool("browser_tabs", {"action": "new"})
        try:
            return await _navigate_read(live, url)
        finally:
            await live.client.call_tool("browser_tabs", {"action": "close"})
    bs = await new_session(owner)
    try:
        return await _navigate_read(bs, url)
    finally:
        await close(bs)


async def _navigate_read(bs: BrowserSession, url: str) -> str:
    for tool, args in (("browser_navigate", {"url": url}), ("browser_wait_for", {"time": 2})):
        res = await bs.client.call_tool(tool, args)
        if res.isError:
            raise RuntimeError(result_text(res))
    return await page_html(bs)


async def reaper() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for bs in list(by_mcp.values()):
            if now - bs.last_used > IDLE_TIMEOUT:
                log.info("reaping idle session %s", bs.id[:8])
                await close(bs)
