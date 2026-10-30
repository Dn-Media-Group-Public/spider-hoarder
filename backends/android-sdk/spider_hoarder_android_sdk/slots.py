"""Gateway side of the Android slots: the slot table of this replica's slice and the backend that plugs into pool.Pool.
Slot k is the container <backend>-<k>, e.g. android-tor-3 (made by the backend's own android-wrapper);
replica `REPLICA_SLOT=s` owns slots (s-1)*P+1 .. s*P, P = ANDROID_SLOTS_PER_REPLICA, so there is no shared
allocator. A slot is free, busy or quarantined (pool.Pool keeps that state). See docs/implemented-plans/android-hoarder.md."""

import asyncio
import json
import logging
import os

import httpx

log = logging.getLogger("slots")

BACKEND = os.environ.get("BACKEND", "android_clear").replace("_", "-")  # the hyphenated backend name: slot names and aliases
PER_REPLICA = int(os.environ.get("ANDROID_SLOTS_PER_REPLICA", "2"))
REPLICA = int(os.environ.get("REPLICA_SLOT", "1"))
SECRET = os.environ.get("SLOT_SECRET", "")
SLOTS = range((REPLICA - 1) * PER_REPLICA + 1, REPLICA * PER_REPLICA + 1)
HOST_FMT = os.environ.get("SLOT_HOST_FORMAT", "{backend}-{k}")  # tests / single-host runs
MCP_PORT, CONTROL_PORT, WS_PORT = 9000, 9001, 6080
READY_TIMEOUT = float(os.environ.get("SLOT_READY_TIMEOUT", "60"))
RESET_TIMEOUT = float(os.environ.get("SLOT_RESET_TIMEOUT", "300"))  # snapshot restore + checks
TOOLS_FILE = os.path.join(os.path.dirname(__file__), "android-tools.json")


def host(k: int) -> str:
    return HOST_FMT.format(backend=BACKEND, k=k)


def mcp_url(k: int) -> str:
    return f"http://{host(k)}:{MCP_PORT}/mcp"


def view_upstream(k: int) -> str:
    """What nginx proxies the noVNC page and websocket to (answered by /authz)."""
    return f"{host(k)}:{WS_PORT}"


def mcp_headers() -> dict:
    return {"Host": "mcp-upstream", "X-Slot-Secret": SECRET}


def static_tools() -> list:
    from mcp import types
    return [types.Tool(**t) for t in json.load(open(TOOLS_FILE))]


class SlotBackend:
    """pool.Pool backend: `uid` is the slot number. start = begin a session on the slot; stop = reset it to the
    snapshot and verify it; a slot that cannot be proven clean raises, and the pool quarantines it."""

    def __init__(self, client_factory=None):
        self._factory = client_factory or (lambda: httpx.AsyncClient(headers={"X-Slot-Secret": SECRET}))
        self.dirty: set[int] = set()  # slots a session may have touched since their last verified reset

    async def _call(self, k: int, method: str, path: str, timeout: float, **kw) -> httpx.Response:
        async with self._factory() as c:
            r = await c.request(method, f"http://{host(k)}:{CONTROL_PORT}{path}", timeout=timeout, **kw)
        return r

    async def _ok(self, k: int, method: str, path: str, timeout: float, **kw) -> dict:
        r = await self._call(k, method, path, timeout, **kw)
        if r.status_code != 200:
            raise RuntimeError(f"slot {k} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    async def health(self, k: int) -> dict:
        return await self._ok(k, "GET", "/internal/health", 5)

    async def _wait_free(self, k: int, timeout: float | None = None) -> None:
        deadline = asyncio.get_running_loop().time() + (READY_TIMEOUT if timeout is None else timeout)
        last = "unreachable"
        while asyncio.get_running_loop().time() < deadline:
            try:
                last = (await self.health(k))["state"]
                if last == "free":
                    return
            except (httpx.HTTPError, RuntimeError) as e:
                last = repr(e)
            await asyncio.sleep(1)
        raise RuntimeError(f"slot {k} is not ready ({last})")

    async def start(self, uid: int, env: dict, files: dict) -> None:
        self.dirty.add(uid)  # even a failed start: stop() then resets (and heals or quarantines) the slot
        await self._wait_free(uid)
        await self._ok(uid, "POST", "/internal/start", 30, json={"vnc_password": files["vncpass"]})

    async def stop(self, uid: int) -> None:
        if uid not in self.dirty:
            return  # never started: a reservation that came to nothing needs no reset
        await self._reset_and_verify(uid)
        self.dirty.discard(uid)

    async def _reset_and_verify(self, k: int) -> None:
        """The slot throws itself away (slotd exits, the wrapper recreates the container from scratch): wait for the new
        one to be free, then check it. The old one never reports free again, so this cannot see it by mistake."""
        try:
            await self._ok(k, "POST", "/internal/reset", 30)
        except httpx.HTTPError as e:  # the container is already gone (killed, OOM): its replacement is fresh by construction
            log.warning("slot %s unreachable at reset (%r): waiting for its replacement", k, e)
        await self._wait_free(k, RESET_TIMEOUT)
        v = await self._ok(k, "GET", "/internal/verify", 60)
        if not v.get("ok"):
            raise RuntimeError(f"slot {k} not clean after reset: {v.get('problems')}")

    async def reset(self) -> None:
        """Gateway (re)start: no session survives. Reset every slot of the slice that is not a clean, free one
        (a crashed gateway leaves busy slots behind); slots that do not answer yet are booting, hence clean."""
        async def one(k):
            try:
                h = await self.health(k)
            except (httpx.HTTPError, RuntimeError):
                return
            self.dirty.discard(k)
            if h.get("state") not in ("free", "booting"):
                try:
                    await self._reset_and_verify(k)
                except Exception as e:  # noqa: BLE001
                    log.error("slot %s could not be reset at start: %s", k, e)
                    self.dirty.add(k)  # the next release tries again, and quarantines if it fails again
        await asyncio.gather(*(one(k) for k in SLOTS))

    async def fetch_out(self, k: int, name: str, max_bytes: int) -> bytes:
        r = await self._call(k, "GET", f"/internal/out/{name}", 60)
        if r.status_code != 200:
            raise ValueError(f"slot has no output file {name!r}")
        if len(r.content) > max_bytes:
            raise ValueError(f"output file too large ({len(r.content)} > {max_bytes} bytes)")
        return r.content

    async def push_in(self, k: int, name: str, data: bytes) -> None:
        r = await self._call(k, "PUT", f"/internal/in/{name}", 120, content=data)
        if r.status_code != 200:
            raise RuntimeError(f"could not push {name} to the slot: HTTP {r.status_code}")


backend = SlotBackend()
