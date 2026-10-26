"""Host-runnable checks for MODE=android: the replica's slot slice, the slot backend against a fake slotd
(lifecycle, verify, quarantine), tool list, /authz upstream, file tools and APK installs through the slot.
Run on its own (it sets the mode before importing the gateway)."""

import asyncio
import httpx
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for k, v in dict(BLOB_TOKEN_SECRET="test-secret", PUBLIC_HOST="h.example", OIDC_ISSUER="https://kc.example/realms/r",
                 REPLICA_RID="10-0-0-5", MODE="android", EGRESS="clear", ANDROID_SLOTS_PER_REPLICA="2", REPLICA_SLOT="2",
                 SLOT_SECRET="s3", SLOT_HOST_FORMAT="127.0.0.1", SLOT_READY_TIMEOUT="3").items():
    os.environ[k] = v

import app
import pool
import sessions
import slots
import storage

HERE = os.path.dirname(os.path.abspath(__file__))
UUID = "123e4567-e89b-12d3-a456-426614174000"


class FakeSlotd(BaseHTTPRequestHandler):
    """Just enough of android-slot/slotd.py: state machine, secret, verify result, files."""
    state = "free"
    verify_ok = True
    files_out: dict = {}
    files_in: dict = {}
    log: list = []

    def log_message(self, *a): pass

    def _send(self, code, body, raw=False):
        b = body if raw else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _go(self, method):
        if self.headers.get("X-Slot-Secret") != "s3":
            return self._send(403, {"error": "forbidden"})
        FakeSlotd.log.append((method, self.path))
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        c = FakeSlotd
        if self.path == "/internal/health":
            return self._send(200, {"state": c.state})
        if self.path == "/internal/start":
            if c.state != "free":
                return self._send(409, {"error": "busy"})
            c.state = "busy"; c.vnc = json.loads(body)["vnc_password"]
            return self._send(200, {"ok": True})
        if self.path == "/internal/reset":
            # the real slot exits and the wrapper replaces it: busy slot -> "resetting" for a moment -> a fresh free one
            c.state = "resetting"
            threading.Timer(0.3, lambda: setattr(c, "state", "free")).start()
            return self._send(200, {"ok": True})
        if self.path == "/internal/verify":
            return self._send(200, {"ok": c.verify_ok, "problems": [] if c.verify_ok else ["an account exists"]})
        if self.path.startswith("/internal/out/"):
            name = self.path.rsplit("/", 1)[1]
            return self._send(200, c.files_out.pop(name), raw=True) if name in c.files_out else self._send(404, {})
        if self.path.startswith("/internal/in/"):
            c.files_in[self.path.rsplit("/", 1)[1]] = body
            return self._send(200, {"ok": True})
        self._send(404, {})

    def do_GET(self): self._go("GET")
    def do_POST(self): self._go("POST")
    def do_PUT(self): self._go("PUT")


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSlotd)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    slots.CONTROL_PORT = srv.server_address[1]
    FakeSlotd.state, FakeSlotd.verify_ok, FakeSlotd.log = "free", True, []
    return srv


def test_slot_slice_per_replica():
    code = "import slots; print(list(slots.SLOTS))"
    for slot, want in ((1, [1, 2, 3]), (2, [4, 5, 6]), (3, [7, 8, 9])):
        env = {**os.environ, "REPLICA_SLOT": str(slot), "ANDROID_SLOTS_PER_REPLICA": "3"}
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=HERE).stdout
        assert json.loads(out) == want, (slot, out)
    assert list(slots.SLOTS) == [3, 4]  # REPLICA_SLOT=2, 2 per replica
    assert sessions.pool.free == set(slots.SLOTS)


def test_backend_lifecycle_and_quarantine():
    srv = serve()
    try:
        async def go():
            be = slots.SlotBackend()
            p = pool.Pool(be, uids=slots.SLOTS)
            uid = p.take()
            assert uid == 3
            await p.start(uid, {}, {"vncpass": "abcd1234"})
            assert FakeSlotd.state == "busy" and FakeSlotd.vnc == "abcd1234" and uid in be.dirty
            # a second start on a busy slot is refused by the slot itself (e.g. after a gateway restart lost its table)
            FakeSlotd.state = "busy"
            slots.READY_TIMEOUT = 1.5
            try:
                await be.start(uid, {}, {"vncpass": "x"}); assert False
            except RuntimeError as e:
                assert "not ready (busy)" in str(e)
            await p.release(uid)  # reset + verify
            assert uid in p.free and FakeSlotd.state == "free" and uid not in be.dirty
            assert ("POST", "/internal/reset") in FakeSlotd.log and ("GET", "/internal/verify") in FakeSlotd.log
            # an unused reservation needs no reset
            FakeSlotd.log.clear()
            u2 = p.take(); await p.release(u2)
            assert FakeSlotd.log == []
            # failed verification: quarantined, never handed out again
            uid = p.take()
            await p.start(uid, {}, {"vncpass": "abcd1234"})
            FakeSlotd.verify_ok = False
            await p.release(uid)
            assert uid in p.lost and uid not in p.free
            assert p.take() != uid
        asyncio.run(go())
    finally:
        srv.shutdown()


def test_backend_unreachable_slot_and_wrong_secret():
    srv = serve()
    try:
        async def go():
            be = slots.SlotBackend()
            bad = slots.SlotBackend(lambda: __import__("httpx").AsyncClient(headers={"X-Slot-Secret": "nope"}))
            try:
                await bad.health(3); assert False
            except RuntimeError as e:
                assert "403" in str(e)
            slots.CONTROL_PORT = 1  # nothing listens: start fails after the ready timeout, slot stays dirty for the reset
            slots.READY_TIMEOUT = 0.5
            try:
                await be.start(3, {}, {"vncpass": "x"}); assert False
            except RuntimeError as e:
                assert "not ready" in str(e)
            assert 3 in be.dirty
        asyncio.run(go())
    finally:
        srv.shutdown()


def test_gateway_start_resets_leftover_busy_slots():
    srv = serve()
    try:
        async def go():
            be = slots.SlotBackend()
            FakeSlotd.state = "busy"  # a crashed gateway left it
            await be.reset()
            assert FakeSlotd.state == "free" and not be.dirty
            FakeSlotd.log.clear()
            await be.reset()  # free slots are left alone
            assert [m for m, p in FakeSlotd.log if p == "/internal/reset"] == []
            FakeSlotd.state, FakeSlotd.verify_ok = "busy", False
            await be.reset()  # cannot be proven clean: the next release retries and quarantines
            assert be.dirty == set(slots.SLOTS)
        asyncio.run(go())
    finally:
        srv.shutdown()


def test_a_call_in_flight_fails_when_the_session_task_dies():
    """The slot container was killed mid-session: the upstream task ends with an error. A tool call already waiting for its answer
    must fail then (it hung forever: the SDK does not wake it)."""
    class Hangs:
        async def call_tool(self, *a, **kw):
            await asyncio.sleep(3600)

    async def go():
        bs = sessions.BrowserSession(owner="a@x.y", uid=1)
        bs.error = ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("Name or service not known")])
        g = sessions._Guarded(Hangs(), bs)
        asyncio.get_running_loop().call_later(0.05, bs.dead.set)
        try:
            await asyncio.wait_for(g.call_tool("device_snapshot", {}), 5)
            raise AssertionError("the call should have failed")
        except RuntimeError as e:
            assert "ended unexpectedly" in str(e) and "ConnectionError: Name or service not known" in str(e), str(e)
    asyncio.run(go())


def test_reset_of_a_vanished_slot_waits_for_the_replacement_instead_of_failing():
    srv = serve()
    try:
        class Gone(slots.SlotBackend):
            async def _call(self, k, method, path, timeout, **kw):
                if path == "/internal/reset":
                    raise httpx.ConnectError("[Errno -2] Name or service not known")
                return await super()._call(k, method, path, timeout, **kw)

        async def go():
            be = Gone()
            FakeSlotd.state, FakeSlotd.verify_ok = "free", True  # the replacement is already up (the old one cannot be reached)
            await be._reset_and_verify(slots.SLOTS[0])
            FakeSlotd.verify_ok = False  # ... and still has to pass verify
            try:
                await be._reset_and_verify(slots.SLOTS[0])
                raise AssertionError("verify failure must still raise")
            except RuntimeError as e:
                assert "not clean" in str(e)
            FakeSlotd.verify_ok = True
        asyncio.run(go())
    finally:
        srv.shutdown()


def test_tool_list():
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert {"device_snapshot", "device_click", "device_take_screenshot", "device_request_human_help", "device_end_session",
            "device_install_apk", "get_upload_link", "list_files", "get_file_text", "grep_file", "get_file_url"} <= names
    assert not [n for n in names if n.startswith("browser_")] and not names & {"ingest_page", "browser_save_page"}
    by = {t.name: t for t in tools}
    assert set(by["device_install_apk"].inputSchema["properties"]) == {"job_id", "path"}  # the gateway's schema, not the slot's
    assert "{job_id, files, text?}" in by["device_snapshot"].description
    assert "ANDROID" in by["device_open_url"].description and "CLEAR" in by["device_open_url"].description
    assert "NO pre-logged-in account" in app.mcp.instructions and app.MCP_PATH == "/android-clear/mcp"
    assert app.BASE == "http://h.example/android-clear" or app.BASE == "https://h.example/android-clear"


def test_authz_points_nginx_at_the_slot():
    from starlette.testclient import TestClient
    sessions.by_view.clear()
    bs = sessions.BrowserSession(owner="a@b.c", uid=4)
    sessions.by_view[bs.id] = bs
    c = TestClient(app.app, base_url="https://h.example")
    uri = f"/android-clear/view/{app.RID}/{bs.id}/websockify"
    r = c.get("/authz", headers={"x-forwarded-uri": uri, "x-auth-request-email": "A@b.c"})
    assert r.status_code == 200 and r.headers["x-upstream"] == "127.0.0.1:6080"
    assert c.get("/authz", headers={"x-forwarded-uri": uri, "x-auth-request-email": "x@b.c"}).status_code == 403  # not the owner
    assert c.get("/authz", headers={"x-forwarded-uri": f"/clear/view/{app.RID}/{bs.id}/x", "x-auth-request-email": "a@b.c"}).status_code == 403
    sessions.by_view.pop(bs.id)  # session ended: an old link is dead
    assert c.get("/authz", headers={"x-forwarded-uri": uri, "x-auth-request-email": "a@b.c"}).status_code == 403
    s = c.get(f"/android-clear/view/{app.RID}/{bs.id}/start", headers={"x-auth-request-email": "a@b.c"})
    assert s.status_code == 403


def _fake_session(client, owner="a@b.c"):
    sessions.by_view.clear()
    bs = types.SimpleNamespace(client=client, owner=owner, id="vid", uid=3, last_used=0, waiting_human=None)

    async def get_session(*a, **k):
        return bs
    sessions.get_session = get_session
    app._caller = lambda: (None, owner, None)
    return bs


def test_file_tool_fetches_from_slot_into_a_job():
    from mcp import types as mt
    srv = serve()
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    try:
        class Client:
            calls = []

            async def call_tool(self, name, args):
                Client.calls.append((name, args))
                assert "/" not in args["filename"] and " " not in args["filename"]  # a plain, gateway-chosen name
                if name == "device_take_screenshot":
                    assert args["filename"].endswith(".png")
                    FakeSlotd.files_out[args["filename"]] = b"\x89PNG\0\0"
                return mt.CallToolResult(content=[mt.TextContent(type="text", text="### Result\nSaved")])

        _fake_session(Client())
        r = asyncio.run(app.call_tool("device_take_screenshot", {"filename": "../../my shot.png"}))
        out = json.loads(r.content[0].text)
        assert out["files"][0]["path"] == "original/my shot.png" and "text" not in out, out
        r = asyncio.run(app.call_tool("device_snapshot", {}))
        assert "produced no readable output file" in r.content[0].text and r.isError  # slot had nothing
    finally:
        srv.shutdown()


def test_human_help_suspends_idle_and_next_call_resumes():
    bs = _fake_session(types.SimpleNamespace())
    r = asyncio.run(app.call_tool("device_request_human_help", {"reason": "sign in to Google"}))
    assert f"/android-clear/view/{app.RID}/vid/start" in r.content[0].text and "sign in to Google" in r.content[0].text
    assert bs.waiting_human is not None

    async def reap_once():
        sessions.by_mcp.clear()
        sessions.by_mcp[1] = bs
        bs.mcp_session = 1
        closed = []

        async def close(b): closed.append(b)
        real_close, real_sleep, calls = sessions.close, sessions.asyncio.sleep, []

        async def fake_sleep(_):
            calls.append(1)
            if len(calls) > 1:
                raise asyncio.CancelledError
        sessions.close, sessions.asyncio.sleep = close, fake_sleep
        try:
            await sessions.reaper()
        except asyncio.CancelledError:
            pass
        finally:
            sessions.close, sessions.asyncio.sleep = real_close, real_sleep
            sessions.by_mcp.clear()
        return closed

    bs.last_used = 0  # long idle, but waiting for the human
    assert asyncio.run(reap_once()) == []
    bs.waiting_human -= 2 * sessions.HELP_TIMEOUT  # ... though not forever
    assert asyncio.run(reap_once()) == [bs]
    bs.waiting_human = None
    assert asyncio.run(reap_once()) == [bs]


def test_unknown_tool_books_no_device():
    called = []

    async def get_session(*a, **k):
        called.append(1)
    sessions.get_session = get_session
    app._caller = lambda: (None, "a@b.c", None)
    for name in ("browser_navigate", "ingest_page", "nope"):
        r = asyncio.run(app.call_tool(name, {}))
        assert r.isError and not called, name


def test_install_apk_pushes_the_job_file_to_the_slot():
    from mcp import types as mt
    srv = serve()
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    try:
        key = app._key("a@b.c", f"{app.RID}.{UUID}")
        storage.write_bytes(key, "original/app.apk", b"PK\3\4apk", "application/vnd.android.package-archive")
        storage.write_bytes(key, "original/notes.txt", b"hello", "text/plain")

        class Client:
            async def call_tool(self, name, args):
                self.seen = (name, args)
                return mt.CallToolResult(content=[mt.TextContent(type="text", text="### Result\nInstalled")])

        c = Client()
        _fake_session(c)
        r = asyncio.run(app.call_tool("device_install_apk", {"job_id": f"{app.RID}.{UUID}", "path": "original/app.apk"}))
        assert not r.isError and c.seen[0] == "device_install_apk"
        pushed = c.seen[1]["file"]
        assert pushed.endswith(".apk") and "/" not in pushed and FakeSlotd.files_in[pushed] == b"PK\3\4apk"
        for args, msg in (({"job_id": f"{app.RID}.{UUID}", "path": "original/notes.txt"}, "not an APK"),
                          ({"job_id": f"{app.RID}.{UUID}", "path": "original/none.apk"}, "not found"),
                          ({"job_id": f"{app.RID}.{UUID}", "path": "../../x"}, "invalid path")):
            r = asyncio.run(app.call_tool("device_install_apk", args))
            assert r.isError and msg in r.content[0].text, (args, r)
        # another user's job is not reachable
        _fake_session(c, owner="other@b.c")
        r = asyncio.run(app.call_tool("device_install_apk", {"job_id": f"{app.RID}.{UUID}", "path": "original/app.apk"}))
        assert r.isError
    finally:
        srv.shutdown()


def test_apk_upload_is_stored_not_parsed():
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    key = app._key("a@b.c", f"{app.RID}.{UUID}")
    r = app._store_and_parse(key, "x.apk", b"PK\3\4", "application/vnd.android.package-archive")
    assert [f["path"] for f in r["files"]] == ["original/x.apk"]


def test_reserve_android_is_per_slot_not_per_total():
    sessions.reserved.clear(); sessions.by_mcp.clear()
    sessions.pool = pool.Pool(slots.SlotBackend(), uids=slots.SLOTS)
    assert sessions.MAX_PER_USER == 1
    u = sessions.reserve("a@x.y")
    sessions.register("s1", "a@x.y", u)
    for owner in ("a@x.y",):  # per user
        try:
            sessions.reserve(owner); assert False
        except RuntimeError as e:
            assert "device sessions" in str(e)
    sessions.register("s2", "b@x.y", sessions.reserve("b@x.y"))
    try:
        sessions.reserve("c@x.y"); assert False  # both slots taken: busy, immediately
    except RuntimeError as e:
        assert "capacity" in str(e)


def test_gateway_to_slot_connections_detect_a_dead_peer():
    """A slot container that dies leaves the gateway's connection with no FIN/RST: keepalive and TCP_USER_TIMEOUT must be on
    (they make the kernel give up within about a minute instead of the retransmission limit)."""
    import socket
    client = sessions._stack_client()
    opts = client._transport._pool._socket_options
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in opts
    assert (socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 60_000) in opts
    assert sessions.STACK_SOCKET_OPTIONS == opts
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    c = socket.create_connection(srv.getsockname())  # the kernel accepts every option on a real connection
    for level, name, value in opts:
        c.setsockopt(level, name, value)
        assert c.getsockopt(level, name) == value
    c.close(); srv.close()
    assert client.follow_redirects and client.timeout.read == 30.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
