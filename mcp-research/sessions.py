"""Browser sessions: one uid + one process group (Xvfb, x11vnc, websockify,
toolserver) each, started by sessiond. The gateway proxies MCP tool calls
to the session's toolserver. All paths that end a session (idle reaper,
browser_end_session, MCP session close, crash) go through close()."""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client

import cmp_rules
import fetch
import ports
from pool import Pool, Sessiond
from toolserver.mediapolicy import Policy

MAX_REQUEST_TIMEOUT_MS = int(os.environ.get("MAX_REQUEST_TIMEOUT_MS", 300_000))  # browser_network_request_make

log = logging.getLogger("sessions")

EGRESS = os.environ.get("EGRESS", "clear")  # "tor" | "clear" | "i2p"
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "1800"))
DISCONNECT_TIMEOUT = float(os.environ.get("DISCONNECT_TIMEOUT_MINUTES", "5")) * 60  # no open request this long = client gone
MAX_PER_USER = int(os.environ.get("MAX_SESSIONS_PER_USER", "3"))
UPSTREAM_HOST = "mcp-upstream"  # must match the allow-list in toolserver/server.py



def _own_rid() -> str:
    """This replica's IP on the network it shares with nginx, dashes for dots (ids and URLs carry it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((socket.gethostbyname("nginx"), 9))  # UDP connect: picks the route, sends nothing
        return s.getsockname()[0].replace(".", "-")


RID = os.environ.get("REPLICA_RID") or _own_rid()  # REPLICA_RID: tests / single-host runs without nginx

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
    sid: str | None = None  # SDK MCP session id (no replica prefix)
    policy: Policy | None = None  # the MCP session's no-media policy, installed in the toolserver at start


@dataclass
class Reservation:
    """A uid set aside at MCP `initialize`, so capacity is decided before any tool call."""
    owner: str
    uid: int
    created: float = field(default_factory=time.time)


by_mcp: dict[int, BrowserSession] = {}   # id(ServerSession) -> session
by_view: dict[str, BrowserSession] = {}  # view id -> session
policies: dict[str, Policy] = {}         # MCP session id -> policy chosen by open_session (immutable; dropped by browser_end_session)
reserved: dict[str, Reservation] = {}    # MCP session id -> uid held for its first browser
conn: dict[str, list] = {}               # MCP session id -> [requests in flight (open SSE stream counts), last activity]
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
        proxied = EGRESS in ("tor", "i2p")
        env = {"UPSTREAM_SOCKS": f"{ports.SOCKS_HOST}:{ports.socks_port(bs.uid)}"} if proxied else {}
        if EGRESS == "i2p":
            env["ALLOW_SUFFIX"] = ".i2p"
            if not await asyncio.to_thread(fetch.i2p_ready, ports.socks_port(bs.uid)):
                raise RuntimeError("I2P router/tunnels not ready yet; retry in a minute")
        # clear: Chromium --host-rules; tor/i2p: the filter relay (host-rules don't apply when a proxy resolves names)
        files = {"vncpass": bs.vnc_password, "hostrules": "" if proxied else _HOST_RULES}
        await pool.start(bs.uid, env, files)
        bs.running = True
        url = f"http://127.0.0.1:{ports.mcp_port(bs.uid)}/mcp"
        await _wait_http(url)
        async with streamablehttp_client(
                url, headers={"Host": UPSTREAM_HOST}, sse_read_timeout=MAX_REQUEST_TIMEOUT_MS / 1000 + 60) as (r, w, _):
            async with ClientSession(r, w) as cs:
                await cs.initialize()
                if bs.policy:
                    res = await cs.call_tool("browser_set_policy", {"policy": bs.policy.to_json()})
                    if res.isError:
                        raise RuntimeError(result_text(res))
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


def _count(owner: str | None = None) -> int:
    return sum(1 for o in [s.owner for s in by_mcp.values()] + [r.owner for r in reserved.values()]
               if owner is None or o == owner)


def reserve(owner: str) -> int:
    """Hold a uid for a new MCP session (not yet keyed: see register). RuntimeError = full."""
    if _count(owner) >= MAX_PER_USER:
        raise RuntimeError(f"you already have {MAX_PER_USER} browser sessions; end one first")
    if _count() >= ports.MAX_SESSIONS_TOTAL:
        raise RuntimeError("browser capacity reached; try again later")
    return pool.take()


def register(sid: str, owner: str, uid: int) -> None:
    reserved[sid] = Reservation(owner, uid)
    conn[sid] = [0, time.time()]


def conn_open(sid: str) -> None:
    conn.setdefault(sid, [0, time.time()])[0] += 1


def conn_close(sid: str) -> None:
    if c := conn.get(sid):
        c[0] -= 1
        c[1] = time.time()


async def unreserve(sid: str) -> None:
    if r := reserved.pop(sid, None):
        await pool.release(r.uid)  # the browser was never started: stop() just wipes the uid


async def end_mcp_session(sid: str) -> None:
    """MCP session closed (DELETE): free its reservation and any browser."""
    conn.pop(sid, None)
    policies.pop(sid, None)
    await unreserve(sid)
    for bs in [b for b in by_mcp.values() if b.sid == sid]:
        await close(bs)


async def new_session(owner: str, mcp_session: Any = None, uid: int | None = None, sid: str | None = None,
                      policy: Policy | None = None) -> BrowserSession:
    """Start a session on a uid (taken here unless reserved); registered only if mcp_session given."""
    uid = pool.take() if uid is None else uid
    bs = BrowserSession(owner=owner, uid=uid, mcp_session=mcp_session, sid=sid, policy=policy)
    if mcp_session is not None:
        by_mcp[id(mcp_session)] = bs
        by_view[bs.id] = bs
    try:
        return await _start(bs)
    except Exception:
        await close(bs)
        raise


async def get_session(mcp_session: Any, owner: str, sid: str | None = None, create: bool = True) -> BrowserSession | None:
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
        # normally the uid was reserved at initialize; otherwise (browser ended earlier, reservation reaped)
        # reserve now, which can fail with "capacity reached"
        r = reserved.pop(sid or "", None)
        uid = r.uid if r else reserve(owner)
        return await new_session(owner, mcp_session, uid, sid, policies.get(sid or ""))


async def upstream_tools() -> list[types.Tool]:
    """Tool list of the toolserver, fetched once from a throwaway session."""
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


def parse_result(text: str):
    """Unwrap a toolserver reply: '### Result\\n<json>' (up to the next section)."""
    body = text.split("### Result", 1)[-1].lstrip("\n")
    return json.loads(body.split("\n### ", 1)[0].strip())


def parse_eval_result(text: str) -> tuple[str, str]:
    value = parse_result(text)
    if not isinstance(value, dict) or not isinstance(value.get("html"), str):
        raise ValueError("browser_evaluate did not return {html, url}")
    return value["html"], value.get("url", "")


# Scroll to trigger lazy content, then wait until the DOM has been quiet for 1s (max 10s) so
# client-side rendered pages are complete before we read them.
# The function gets the `target` element as its argument (none = whole document).
SETTLE_JS = """async (el) => {
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
  return {html: (el || document.documentElement).outerHTML, url: location.href};
}"""


async def page_html(bs: BrowserSession, browser: str | None = None, target: str | None = None) -> tuple[str, str]:
    """(outerHTML of target or the whole page, location.href)."""
    res = await bs.client.call_tool("browser_evaluate", {"function": SETTLE_JS, **({"target": target} if target else {}), **_on(browser)})
    if res.isError:
        raise RuntimeError(result_text(res))
    return parse_eval_result(result_text(res))


async def cookies_for(bs: BrowserSession, url: str) -> dict:
    """The session's browser cookies applicable to url, as a name->value dict for `requests`."""
    res = await bs.client.call_tool("browser_cookie_list", {"url": url})
    if res.isError:
        raise RuntimeError(result_text(res))
    return {c["name"]: c["value"] for c in parse_result(result_text(res))}


async def fetch_html(owner: str, url: str, live: BrowserSession | None = None, target: str | None = None,
                     policy: Policy | None = None) -> tuple[str, str]:
    """One-shot: navigate, read HTML. Without `live`: ephemeral session (own uid), destroyed after.
    With `live`: a temporary browser (window) in that session, so its cookies apply but nobody's page is clobbered."""
    if live:
        res = await live.client.call_tool("browser_start_session", {})
        if res.isError:
            raise RuntimeError(result_text(res))
        browser = parse_result(result_text(res))
        try:
            return await _navigate_read(live, url, browser, target)
        finally:
            await live.client.call_tool("browser_end_session", {"browser": browser})
    bs = await new_session(owner, policy=policy)
    try:
        return await _navigate_read(bs, url, None, target)
    finally:
        await close(bs)


def _on(browser: str | None) -> dict:
    return {"browser": browser} if browser else {}


async def _navigate_read(bs: BrowserSession, url: str, browser: str | None = None, target: str | None = None) -> tuple[str, str]:
    for tool, args in (("browser_navigate", {"url": url}), ("browser_wait_for", {"time": 2})):
        res = await bs.client.call_tool(tool, {**args, **_on(browser)})
        if res.isError:
            raise RuntimeError(result_text(res))
    return await page_html(bs, browser, target)


async def reaper() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for bs in list(by_mcp.values()):
            if now - bs.last_used > IDLE_TIMEOUT:
                log.info("reaping idle session %s", bs.id[:8])
                await close(bs)
        for sid, (inflight, last) in list(conn.items()):
            if inflight <= 0 and now - last > DISCONNECT_TIMEOUT:
                log.info("reaping disconnected MCP session %s", sid[:8])
                await end_mcp_session(sid)
        for sid, r in list(reserved.items()):
            if now - r.created > IDLE_TIMEOUT:
                log.info("reaping unused reservation for %s", r.owner)
                await unreserve(sid)
