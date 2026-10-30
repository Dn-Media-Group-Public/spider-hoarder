"""Host-runnable checks for running several gateway replicas: replica-prefixed
MCP session ids, reserve-at-initialize, foreign job ids. See docs/implemented-plans/research-replicas.md."""

import asyncio
import json
import os
import types

from spider_hoarder_sdk import testing

testing.gateway_env()

from spider_hoarder_sdk.gateway import app, jobs
from spider_hoarder_sdk.gateway import sessions

UUID = "123e4567-e89b-12d3-a456-426614174000"


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
    sessions.driver.max_total = total


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
    ok = jobs._key("a@x.y", f"{jobs.RID}.{UUID}")
    assert ok == f"a@x.y/{UUID}"
    for bad, msg in ((f"10-0-0-9.{UUID}", "another replica"), (UUID, "invalid"), (f"{jobs.RID}.nope", "invalid")):
        try:
            jobs._key("a@x.y", bad); assert False, bad
        except ValueError as e:
            assert msg in str(e), (bad, e)


def _mcp_route():
    return next(r for r in app.app.routes if getattr(r, "path", None) == app.MCP_PATH)


def test_unauthenticated_mcp_post_is_401_not_500():
    """ReplicaSessions must sit inside RequireAuthMiddleware, else anonymous callers crash it (500)."""
    from starlette.testclient import TestClient
    r = TestClient(app.app, base_url=f"https://{app.PUBLIC_HOST}").post(app.MCP_PATH, json={})
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
    assert isinstance(_mcp_route().app.app, app.ReplicaSessions)


def test_metadata_advertises_basic_and_enforced_scope():
    from starlette.testclient import TestClient
    r = TestClient(app.app, base_url=f"https://{app.PUBLIC_HOST}").get("/.well-known/oauth-protected-resource" + app.MCP_PATH)
    assert {"basic", "email"} <= set(r.json()["scopes_supported"]) and set(app._ENFORCED_SCOPES) <= set(r.json()["scopes_supported"])
    assert _mcp_route().app.required_scopes == app._ENFORCED_SCOPES  # basic is advertised, not enforced


def test_disconnected_session_is_reaped():
    _setup(total=1, per_user=1)
    sessions.conn.clear()
    _call(_initialize)  # reserves + tracks "abc"
    sessions.conn_open("abc")  # an open SSE stream keeps it alive
    sessions.conn["abc"][1] -= 10 * sessions.DISCONNECT_TIMEOUT

    async def reap_once():
        real, calls = asyncio.sleep, []

        async def fake_sleep(_):
            calls.append(1)
            if len(calls) > 1:
                raise asyncio.CancelledError
        sessions.asyncio.sleep = fake_sleep
        try:
            await sessions.reaper()
        except asyncio.CancelledError:
            pass
        finally:
            sessions.asyncio.sleep = real
    asyncio.run(reap_once())
    assert "abc" in sessions.conn and sessions.reserved  # still connected
    sessions.conn_close("abc")
    sessions.conn["abc"][1] -= 10 * sessions.DISCONNECT_TIMEOUT
    asyncio.run(reap_once())
    assert "abc" not in sessions.conn and not sessions.reserved and sessions.pool.free == [0]


def test_failed_initialize_frees_reservation_immediately():
    _setup(total=1, per_user=1)
    sessions.conn.clear()

    async def die_after_start(scope, receive, send):  # session id minted, then the request is cut
        await send({"type": "http.response.start", "status": 200, "headers": [(b"mcp-session-id", b"zzz")]})
        raise RuntimeError("client went away")

    try:
        _call(die_after_start)
    except RuntimeError:
        pass
    assert not sessions.reserved and not sessions.conn and sessions.pool.free == [0]

    async def cancelled(scope, receive, send):  # aborted before any response: uid must not leak
        raise asyncio.CancelledError

    try:
        _call(cancelled)
    except BaseException:
        pass
    assert sessions.pool.free == [0]
