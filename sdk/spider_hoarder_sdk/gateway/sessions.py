"""Sessions: one per MCP conversation, each on one uid of the driver's pool (a browser uid, an emulator slot, ...) with
its own upstream MCP connection. The gateway proxies tool calls to it. Everything backend-specific (what starts it, where
its tool server listens, how long it may idle) comes from the driver (driver.py). All paths that end a session (idle
reaper, the driver's end-session tool, MCP session close, crash) go through close()."""

import asyncio
import contextlib
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client

from spider_hoarder_sdk.gateway import ident
from spider_hoarder_sdk.gateway.driver import driver
from spider_hoarder_sdk.gateway.pool import Pool

MAX_REQUEST_TIMEOUT_MS = int(os.environ.get("MAX_REQUEST_TIMEOUT_MS", 300_000))  # the longest a proxied call may take

log = logging.getLogger("sessions")

PREFIX = driver.prefix  # URL / service prefix: /<PREFIX>/mcp, /<PREFIX>/view/...
IDLE_TIMEOUT = driver.idle_timeout
HELP_TIMEOUT = driver.help_timeout  # a pending human-help request suspends the idle timer, up to this
DISCONNECT_TIMEOUT = float(os.environ.get("DISCONNECT_TIMEOUT_MINUTES", "5")) * 60  # no open request this long = client gone
MAX_PER_USER = driver.max_per_user
RID = ident.RID

pool: Pool = driver.make_pool()


@dataclass
class Session:
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
    dead: asyncio.Event = field(default_factory=asyncio.Event)  # set when the session's upstream task has ended, however
    task: asyncio.Task | None = None
    last_used: float = field(default_factory=time.time)
    sid: str | None = None  # SDK MCP session id (no replica prefix)
    waiting_human: float | None = None  # when a human-help request was made; cleared by the next tool call
    opts: Any = None  # what the driver's open-session step chose for this MCP session (browser: the media Policy)


@dataclass
class Reservation:
    """A uid set aside at MCP `initialize`, so capacity is decided before any tool call."""
    owner: str
    uid: int
    created: float = field(default_factory=time.time)


by_mcp: dict[int, Session] = {}   # id(ServerSession) -> session
by_view: dict[str, Session] = {}  # view id -> session
opened: dict[str, Any] = {}       # MCP session id -> what the driver's open-session step chose (immutable; dropped when the session ends)
reserved: dict[str, Reservation] = {}    # MCP session id -> uid held for its first session
conn: dict[str, list] = {}               # MCP session id -> [requests in flight (open SSE stream counts), last activity]
_lock = asyncio.Lock()
_tools_cache: list[types.Tool] | None = None
_tools_lock = asyncio.Lock()


async def wait_http(url: str, timeout: float = 60, headers: dict | None = None, host: str = "mcp-upstream") -> None:
    deadline = time.time() + timeout
    async with httpx.AsyncClient() as h:
        while time.time() < deadline:
            try:
                await h.get(url, headers=headers or {"Host": host}, timeout=2)
                return
            except httpx.HTTPError:
                await asyncio.sleep(0.5)
    raise TimeoutError(f"session did not come up: {url}")


class _Guarded:
    """The session's upstream MCP client, except that a call also ends when the session's task does. When the upstream dies
    (the slot container is killed, say) the task ends with an error, but the SDK never wakes a call already waiting for its
    answer: the user's call hung until their own timeout. Everything but call_tool is the wrapped session."""

    def __init__(self, cs: ClientSession, bs: "Session"):
        self._cs, self._bs = cs, bs

    def __getattr__(self, name):
        return getattr(self._cs, name)

    async def call_tool(self, *args, **kw):
        call = asyncio.ensure_future(self._cs.call_tool(*args, **kw))
        dead = asyncio.ensure_future(self._bs.dead.wait())
        try:
            await asyncio.wait({call, dead}, return_when=asyncio.FIRST_COMPLETED)
            if call.done():
                return call.result()
            err = self._bs.error
            while isinstance(err, BaseExceptionGroup) and err.exceptions:  # the task group's wrapper says nothing
                err = err.exceptions[0]
            why = f"{type(err).__name__}: {err}" if err else "it was closed"
            raise RuntimeError(f"the {driver.noun} session ended unexpectedly ({why[:200]}); start a new one")
        finally:
            call.cancel()
            dead.cancel()


async def _run_client(bs: Session) -> None:
    """Owns the session's uid and its upstream MCP connection for its lifetime."""
    try:
        env, files = await driver.prepare(bs)
        await pool.start(bs.uid, env, files)
        url, headers, kw = driver.upstream_mcp(bs.uid)
        bs.running = True
        await wait_http(url, headers=headers)
        async with streamablehttp_client(url, headers=headers, sse_read_timeout=MAX_REQUEST_TIMEOUT_MS / 1000 + 60, **kw) as (r, w, _):
            async with ClientSession(r, w) as cs:
                await cs.initialize()
                await driver.after_initialize(cs, bs)
                bs.client = _Guarded(cs, bs)
                bs.ready.set()
                log.info("session %s started for %s on uid %s", bs.id[:8], bs.owner, bs.uid)
                await bs.closing.wait()
    except BaseException as e:  # noqa: BLE001
        bs.error = e
        log.warning("session %s failed: %r", bs.id[:8], e)
    finally:
        bs.dead.set()  # in-flight calls fail now, not after the reset below
        bs.ready.set()
        if bs.running:
            await pool.release(bs.uid)
        else:
            pool.busy.discard(bs.uid)  # never started; pool.start already released it


async def _start(bs: Session) -> Session:
    bs.task = asyncio.create_task(_run_client(bs))
    await bs.ready.wait()
    if bs.error or not bs.client:
        raise RuntimeError(f"could not start {driver.noun}: {bs.error!r}")
    return bs


async def close(bs: Session) -> None:
    if by_mcp.get(id(bs.mcp_session)) is bs:
        del by_mcp[id(bs.mcp_session)]
    by_view.pop(bs.id, None)
    opened.pop(bs.sid, None)  # every way a session ends (idle reaper included) makes the next open-session step ask the user again
    bs.closing.set()
    if bs.task:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(bs.task, driver.close_timeout)
    log.info("session %s closed", bs.id[:8])


def _count(owner: str | None = None) -> int:
    return sum(1 for o in [s.owner for s in by_mcp.values()] + [r.owner for r in reserved.values()]
               if owner is None or o == owner)


def reserve(owner: str) -> int:
    """Hold a uid for a new MCP session (not yet keyed: see register). RuntimeError = full."""
    if _count(owner) >= MAX_PER_USER:
        raise RuntimeError(f"you already have {MAX_PER_USER} {driver.noun} sessions; end one first")
    if driver.max_total is not None and _count() >= driver.max_total:
        raise RuntimeError(f"{driver.noun} capacity reached; try again later")
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
        await pool.release(r.uid)  # nothing was started: stop() just wipes the uid


async def end_mcp_session(sid: str) -> None:
    """MCP session closed (DELETE): free its reservation and any session."""
    conn.pop(sid, None)
    opened.pop(sid, None)
    await unreserve(sid)
    for bs in [b for b in by_mcp.values() if b.sid == sid]:
        await close(bs)


async def new_session(owner: str, mcp_session: Any = None, uid: int | None = None, sid: str | None = None,
                      opts: Any = None) -> Session:
    """Start a session on a uid (taken here unless reserved); registered only if mcp_session given."""
    uid = pool.take() if uid is None else uid
    bs = Session(owner=owner, uid=uid, mcp_session=mcp_session, sid=sid, opts=opts)
    if mcp_session is not None:
        by_mcp[id(mcp_session)] = bs
        by_view[bs.id] = bs
    try:
        return await _start(bs)
    except Exception:
        await close(bs)
        raise


async def get_session(mcp_session: Any, owner: str, sid: str | None = None, create: bool = True) -> Session | None:
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
        # normally the uid was reserved at initialize; otherwise (session ended earlier, reservation reaped)
        # reserve now, which can fail with "capacity reached"
        r = reserved.pop(sid or "", None)
        uid = r.uid if r else reserve(owner)
        return await new_session(owner, mcp_session, uid, sid, opened.get(sid or ""))


async def upstream_tools() -> list[types.Tool]:
    """The tool server's tool list, as the driver provides it (a static list, or fetched once from a throwaway session)."""
    global _tools_cache
    async with _tools_lock:
        if _tools_cache is None:
            _tools_cache = await driver.upstream_tools()
    return _tools_cache


def result_text(res: types.CallToolResult) -> str:
    return "".join(c.text for c in res.content if isinstance(c, types.TextContent))


async def reaper() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for bs in list(by_mcp.values()):
            if bs.waiting_human is not None and now - bs.waiting_human < HELP_TIMEOUT:
                continue  # the user is at the device: not idle
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
