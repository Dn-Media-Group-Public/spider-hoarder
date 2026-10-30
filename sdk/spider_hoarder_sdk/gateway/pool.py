"""Gateway side of the pool: a free set of uids (browser uids, emulator slots, ...) and the backend that starts, stops and
resets them. A uid is returned to the free set only when the backend's stop succeeds; a failed stop means the backend
could not prove it clean, so it is quarantined (dropped)."""

import asyncio
import logging

log = logging.getLogger("pool")


class Pool:
    def __init__(self, backend, uids):
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
            raise RuntimeError("capacity reached; try again later")
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
        """Stop (kill, wipe, verify) then free; quarantine if stop fails. The whole thing, bookkeeping included, runs as
        its own task: a caller that is cancelled while waiting (sessions.close gives up after a timeout) must not leave the
        uid busy once the stop has finished."""
        async def work() -> None:
            try:
                await self.backend.stop(uid)
            except Exception as e:  # noqa: BLE001
                self.busy.discard(uid)
                self.lost.add(uid)
                log.error("uid %s QUARANTINED: %s", uid, e)
                return
            self.busy.discard(uid)
            self.free.add(uid)
        await asyncio.shield(asyncio.ensure_future(work()))
