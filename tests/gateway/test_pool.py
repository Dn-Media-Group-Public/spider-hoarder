"""Pool invariant with a fake sessiond: random start/stop/crash sequences never
put two live sessions on one uid, and a failed stop quarantines instead of freeing."""

import asyncio
import random

from mcp_research.gateway.pool import Pool


class Fake:
    def __init__(self, fail_stop=()):
        self.live, self.fail_stop, self.fail_start = set(), set(fail_stop), set()

    async def start(self, uid, env, files):
        assert uid not in self.live, "two live sessions on one uid"
        if uid in self.fail_start:
            raise RuntimeError("start failed")
        self.live.add(uid)

    async def stop(self, uid):
        if uid in self.fail_stop:
            raise RuntimeError("stop failed")
        self.live.discard(uid)

    async def reset(self):
        self.live.clear()


async def _random_run(seed):
    rnd = random.Random(seed)
    fake = Fake(fail_stop={3} if seed % 2 else ())
    pool = Pool(fake, uids=range(20000, 20006))
    held = {}
    for _ in range(300):
        op = rnd.choice(["take", "take", "stop", "crash"])
        if op == "take":
            try:
                uid = pool.take()
            except RuntimeError:
                continue
            fake.fail_start = {4} if rnd.random() < 0.2 else set()
            try:
                await pool.start(uid, {}, {})
                held[uid] = True
            except RuntimeError:
                pass  # pool.start already released it
        elif held:
            uid = rnd.choice(list(held))
            if op == "crash":  # supervisor died: process gone, gateway still releases via stop
                fake.live.discard(uid)
            del held[uid]
            await pool.release(uid)
        assert pool.free.isdisjoint(pool.busy) and pool.free.isdisjoint(pool.lost)
        assert fake.live <= set(held), "live session on a uid the pool thinks is free"
        assert pool.free | pool.busy | pool.lost == pool.all
    return pool


def test_invariant():
    for seed in range(20):
        asyncio.run(_random_run(seed))


def test_failed_stop_quarantines():
    async def go():
        pool = Pool(Fake(fail_stop={20000}), uids=range(20000, 20002))
        uid = pool.take()
        await pool.start(uid, {}, {})
        await pool.release(uid)
        assert uid in pool.lost and uid not in pool.free
        assert pool.take() == 20001  # quarantined uid is never handed out again
    asyncio.run(go())


if __name__ == "__main__":
    test_invariant()
    test_failed_stop_quarantines()
    print("ok")
