"""Gateway side of sessiond: the pool.Pool backend of the browser backends (one uid + process group per session)."""

import asyncio
import json


class Sessiond:
    """pool.Pool backend: start/stop/reset a session uid by asking sessiond over its unix socket."""

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
