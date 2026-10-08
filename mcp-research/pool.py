"""Gateway side of the uid pool: a free set under one lock, and the sessiond
client. A uid is returned to the free set only when sessiond's stop succeeds;
a failed stop means sessiond has quarantined it, so it is dropped."""

import asyncio
import json
import logging

import ports

log = logging.getLogger("pool")


class Sessiond:
    def __init__(self, path: str = "/run/sessiond.sock"):
        self.path = path

    async def call(self, **req) -> dict:
        r, w = await asyncio.open_unix_connection(self.path)
        w.write(json.dumps(req).encode() + b"\n")
        await w.drain()
        resp = json.loads(await r.readline())
        w.close()
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "sessiond error"))
        return resp

    async def start(self, uid, env, files):
        await self.call(op="start", uid=uid, env=env, files=files)

    async def reset(self):
        await self.call(op="reset")

    async def stop(self, uid):
        await self.call(op="stop", uid=uid)


class Pool:
    def __init__(self, backend, uids=ports.UIDS):
        self.backend = backend
        self.all = set(uids)
        self.free = set(uids)
        self.busy: set[int] = set()
        self.lost: set[int] = set()  # quarantined

    async def reset(self) -> None:
        """Gateway (re)start: no session survives, so wipe every uid."""
        await self.backend.reset()
        self.free, self.busy, self.lost = set(self.all), set(), set()

    def take(self) -> int:
        if not self.free:
            raise RuntimeError("browser capacity reached; try again later")
        uid = min(self.free)
        self.free.remove(uid)
        self.busy.add(uid)
        return uid

    async def start(self, uid: int, env: dict, files: dict) -> None:
        """Start the session group on a taken uid; on failure the uid is stopped and returned."""
        try:
            await self.backend.start(uid, env, files)
        except BaseException:
            await self.release(uid)
            raise

    async def release(self, uid: int) -> None:
        """Stop (kill, wipe, verify) then free; quarantine if stop fails."""
        try:
            await asyncio.shield(self.backend.stop(uid))
        except Exception as e:  # noqa: BLE001
            self.busy.discard(uid)
            self.lost.add(uid)
            log.error("uid %s QUARANTINED: %s", uid, e)
            return
        self.busy.discard(uid)
        self.free.add(uid)
