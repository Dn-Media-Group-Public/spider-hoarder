"""Host-runnable checks for running several research replicas: SocksPort slices, replica-prefixed
MCP session ids, reserve-at-initialize, foreign job ids. See docs/implemented-plans/research-replicas.md."""

import asyncio
import json
import os
import subprocess
import sys
import types

for k, v in dict(BLOB_TOKEN_SECRET="test-secret", PUBLIC_HOST="h.example", OIDC_ISSUER="https://kc.example/realms/r",
                 MCP_AUDIENCE="aud", REPLICA_RID="10-0-0-5").items():
    os.environ.setdefault(k, v)

import app
import sessions

UUID = "123e4567-e89b-12d3-a456-426614174000"


def _ports(slot, max_replicas=4, total=18):
    code = "import ports; print(ports.socks_port(ports.UIDS[0]), ports.socks_port(ports.UIDS[-1]), ports.GATEWAY_SOCKS_PORT)"
    env = {**os.environ, "REPLICA_SLOT": str(slot), "MAX_REPLICAS": str(max_replicas), "MAX_SESSIONS_TOTAL": str(total)}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=os.path.dirname(__file__) or ".")
    return r.returncode, [int(x) for x in r.stdout.split()]


def test_socks_slices():
    rc, (first, last, gw) = _ports(1)
    assert rc == 0 and first == 9050  # slot 1 = the single-container layout
    seen = set()
    for slot in range(1, 5):
        rc, (first, last, gw) = _ports(slot)
        r = set(range(first, last + 1)) | {gw}
        assert not (r & seen), slot
        seen |= r
    assert max(seen) < 9050 + 4 * 21  # tor opens MAX_REPLICAS * (POOL_SIZE + 1) ports
    assert _ports(5)[0] != 0  # SLOT > MAX_REPLICAS refuses to start


class FakePool:
    def __init__(self, n):
        self.free, self.released = list(range(n)), []

    def take(self):
        if not self.free:
            raise RuntimeError("browser capacity reached; try again later")
        return self.free.pop(0)

    async def release(self, uid):
        self.released.append(uid)
        self.free.append(uid)


def _setup(total=2, per_user=1):
    sessions.pool = FakePool(total)
    sessions.reserved.clear()
    sessions.by_mcp.clear()
    sessions.MAX_PER_USER = per_user
    sessions.ports.MAX_SESSIONS_TOTAL = total


def test_reserve_limits():
    _setup(total=2, per_user=1)
    sessions.register("s1", "a@x.y", sessions.reserve("a@x.y"))
    try:
        sessions.reserve("a@x.y"); assert False  # per-user
    except RuntimeError:
        pass
    sessions.register("s2", "b@x.y", sessions.reserve("b@x.y"))
    try:
        sessions.reserve("c@x.y"); assert False  # full
    except RuntimeError:
        pass
    asyncio.run(sessions.end_mcp_session("s1"))  # DELETE frees the reservation
    sessions.reserve("c@x.y")


def _call(inner, method="POST", sid=None, owner="a@x.y"):
    """Run ReplicaSessions around `inner`; returns (status, response headers, scope the SDK saw)."""
    seen = {}
    headers = [(b"mcp-session-id", sid.encode())] if sid else []
    scope = {"type": "http", "method": method, "headers": headers,
             "user": types.SimpleNamespace(access_token=types.SimpleNamespace(claims={"email": owner}))}
    out = {}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"], out["headers"] = m["status"], dict(m["headers"])

    async def wrapped(sc, rc, sd):
        seen.update(sc)
        await inner(sc, rc, sd)

    asyncio.run(app.ReplicaSessions(wrapped)(scope, None, send))
    return out["status"], out["headers"], seen


async def _initialize(scope, receive, send):  # what the SDK does: mint an id, echo it
    await send({"type": "http.response.start", "status": 200, "headers": [(b"mcp-session-id", b"abc")]})


async def _fail(scope, receive, send):
    await send({"type": "http.response.start", "status": 400, "headers": []})


def test_session_id_prefix_and_reserve():
    _setup(total=1, per_user=1)
    status, hdrs, _ = _call(_initialize)
    assert status == 200 and hdrs[b"mcp-session-id"] == b"10-0-0-5.abc"
    assert sessions.reserved["abc"].owner == "a@x.y"
    status, _, _ = _call(_initialize, owner="b@x.y")  # full -> 503, inner never called
    assert status == 503
    status, _, seen = _call(_initialize, sid="10-0-0-5.abc")  # SDK sees the bare id
    assert dict(seen["headers"])[b"mcp-session-id"] == b"abc"
    assert _call(_initialize, sid="10-0-0-9.abc")[0] == 404  # stale / foreign replica
    _call(_initialize, method="DELETE", sid="10-0-0-5.abc")
    assert not sessions.reserved and sessions.pool.free == [0]
    _call(_fail)  # POST that is not a successful initialize gives its uid back
    assert sessions.pool.free == [0]


def test_foreign_job_id():
    ok = app._key("a@x.y", f"{app.RID}.{UUID}")
    assert ok == f"a@x.y/{UUID}"
    for bad, msg in ((f"10-0-0-9.{UUID}", "another replica"), (UUID, "invalid"), (f"{app.RID}.nope", "invalid")):
        try:
            app._key("a@x.y", bad); assert False, bad
        except ValueError as e:
            assert msg in str(e), (bad, e)
