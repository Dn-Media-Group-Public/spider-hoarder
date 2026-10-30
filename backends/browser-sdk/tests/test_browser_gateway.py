"""Browser backend behaviour at the gateway: out/ and in/ session files, file tools as jobs, network-request bodies, the
open_session gate, multi-browser routing, media-blocked ingest. The gateway runs on the browser_clear driver."""

import asyncio
import os
import shutil
import types

import pytest

from spider_hoarder_browser_sdk import cmp_rules, files, netguard, pages, ports, fetch
from spider_hoarder_browser_sdk import driver as bdriver
from spider_hoarder_browser_sdk.mediapolicy import Policy
from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway import app, jobs, sessions

driver = app.driver
egress = driver.egress


def test_ssrf():
    for host in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.0.1", "::1", "localhost"):
        try:
            netguard.check_host(host)
            assert False, host
        except ValueError:
            pass
    try:
        netguard.guarded_request("GET", "http://127.0.0.1:1/")
        assert False
    except ValueError:
        pass


def test_parse_eval_result():
    text = '### Result\n{"html": "<html>\\n<body>x</body></html>", "url": "http://u/"}\n### Page\n- Page URL: about:blank\n'
    assert pages.parse_eval_result(text) == ("<html>\n<body>x</body></html>", "http://u/")
    assert pages.parse_result('### Result\n[{"name": "a"}]') == [{"name": "a"}]


def test_host_rules(tmp_path="/tmp/cmp_test.txt"):
    open(tmp_path, "w").write("! c\n||cmp.example^\n@@||ok.example^\n||x.example^$third-party\n##.banner\n")
    assert cmp_rules.hosts(tmp_path) == ["cmp.example", "x.example"]  # $options kept, @@ and ## dropped
    assert cmp_rules.host_rules(tmp_path).startswith("MAP cmp.example ~NOTFOUND, MAP *.cmp.example ~NOTFOUND")


def test_filter_proxy():
    from spider_hoarder_browser_sdk.session_server.session import filter_proxy as f
    B = frozenset({"cookielaw.org"})
    assert f.is_blocked("cdn.cookielaw.org", B) and f.is_blocked("cookielaw.org", B)
    assert not f.is_blocked("notcookielaw.org", B) and not f.is_blocked("example.org", B)


def test_file_outputs():
    import tempfile
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    home = tempfile.mkdtemp()
    ports.home = lambda uid: home
    os.mkdir(home + "/out")
    w = lambda n, b: open(f"{home}/out/{n}", "wb").write(b)
    w("ok.txt", b"hi")
    read = lambda n, tree=False: [(r, f.read()) for r, f in files._out_files(1, n, tree)]
    assert read("ok.txt") == [("", b"hi")]
    for bad in ("missing",):
        try:
            read(bad); assert False, bad
        except OSError:
            pass
    os.symlink("/etc/passwd", home + "/out/link")
    os.mkfifo(home + "/out/fifo")
    for bad in ("link", "fifo"):  # symlink refused, FIFO neither hangs nor is read
        try:
            read(bad); assert False, bad
        except (ValueError, OSError):
            pass
    os.chmod(home, 0o100)  # sessiond's home is traverse-only for the gateway: still readable via O_PATH
    assert read("ok.txt") == [("", b"hi")]
    os.chmod(home, 0o700)
    shutil.move(home + "/out", home + "/real"); os.symlink(home + "/real", home + "/out")  # swapped out/ dir
    try:
        read("ok.txt"); assert False
    except OSError:
        pass
    for given in ("../../etc/passwd", "/etc/passwd", "a\\..\\b.png", "", "..", None):
        n = jobs._job_name(given, "d")
        assert "/" not in n and "\\" not in n and n not in ("", ".", ".."), (given, n)
    assert jobs._job_name("../../x.png", "d") == "x.png"
    j = f"{jobs.RID}.123e4567-e89b-12d3-a456-426614174000"
    key = jobs._key("a@x.y", j)
    r = jobs._store_file(key, "shot.png", b"\x89PNG\0\0")
    assert "text" not in r and any(f["path"] == "original/shot.png" for f in r["files"]), r
    r = jobs._store_file(key, "snap.md", b"# page")
    assert r["text"] == "# page"
    jobs.INLINE_LIMIT = 3
    assert "text" not in jobs._store_file(key, "snap2.md", b"# page")


def test_tree_reader():
    import tempfile
    home = tempfile.mkdtemp()
    ports.home = lambda uid: home
    d = home + "/out/net"
    os.makedirs(d + "/request"); os.makedirs(d + "/response"); os.makedirs(d + "/other")
    w = lambda n, b: open(f"{d}/{n}", "wb").write(b)
    w("request/headers.json", b"{}"); w("response/headers.json", b"{}"); w("response/body.png", b"\0\1")
    w("response/evil.txt", b"x"); w("other/headers.json", b"x"); w("response/body.", b"x")
    os.symlink("/etc/passwd", d + "/response/body.txt")  # symlink at the file level: ignored
    os.mkfifo(d + "/request/body.bin")  # non-regular: ignored
    got = lambda n="net": {r: f.read() for r, f in files._out_files(1, n, True)}
    assert got() == {"request/headers.json": b"{}", "response/headers.json": b"{}", "response/body.png": b"\0\1"}, got()
    shutil.move(d + "/response", home + "/elsewhere"); os.symlink(home + "/elsewhere", d + "/response")  # symlinked subdir
    assert set(got()) == {"request/headers.json"}
    shutil.move(d, home + "/out/real"); os.symlink(home + "/out/real", d)  # symlinked tree root
    try:
        got(); assert False
    except OSError:
        pass
    try:
        list(files._out_files(1, "real", False)); assert False  # a directory is not a file
    except ValueError:
        pass


def test_file_tools_are_jobs():
    import tempfile, json
    from mcp import types as mt
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    home = tempfile.mkdtemp()
    os.mkdir(home + "/out")
    ports.home = lambda uid: home
    snap = "- generic [ref=e1]:\n  - text: hello"

    class Client:
        async def call_tool(self, name, args):
            open(f"{home}/out/{args['filename']}", "w").write(snap)
            return mt.CallToolResult(content=[mt.TextContent(type="text", text="### Page\n- Page URL: http://p/\n### Snapshot\n- [Snapshot](out/x)\n")])

    bs = types.SimpleNamespace(client=Client(), owner="a@b.c", id="v", uid=1, last_used=0, opts=None)

    async def get_session(*a, **k):
        return bs
    app._caller = lambda: (None, "a@b.c", "sid1")
    sessions.opened["sid1"] = Policy.make(True)
    sessions.get_session = get_session
    jobs.INLINE_LIMIT = 50_000
    run = lambda name, **a: asyncio.run(app.call_tool(name, a))

    r = run("browser_snapshot", target="e5", depth=2)
    out = json.loads(r.content[0].text)
    assert out["text"] == snap and out["files"][0]["path"] == "original/snapshot.yml", out
    m = storage.get_manifest(jobs._key("a@b.c", out["job_id"]))
    assert m["url"] == "http://p/" and m["target"] == "e5" and m["depth"] == 2 and m["text_cap"] == 128 and "query" not in m, m
    r = run("browser_find", regex="/x/i", text_cap=None, filename="m.txt")
    out = json.loads(r.content[0].text)
    m = storage.get_manifest(jobs._key("a@b.c", out["job_id"]))
    assert out["files"][0]["path"] == "original/m.txt" and m["text_cap"] is None and m["query"] == {"regex": "/x/i"} and "target" not in m, m
    assert json.loads(run("browser_evaluate", function="() => 1").content[0].text)["files"][0]["path"] == "original/evaluate.json"

    class NetClient:
        async def call_tool(self, name, args):
            if name == "browser_network_requests":
                return mt.CallToolResult(content=[mt.TextContent(type="text", text="### Result\nNo new requests.")])
            d = f"{home}/out/{args['filename']}"
            os.makedirs(d + "/response")
            open(d + "/response/headers.json", "w").write("{}")
            open(d + "/response/body.html", "w").write("<html><body><a href='/x'>l</a></body></html>")
            open(d + "/response/body.png", "wb").write(b"\x89PNG\0")
            return mt.CallToolResult(content=[mt.TextContent(type="text", text="### Result\n- [Network request](out/x)\n3. [GET] http://n.test/p => [200] OK")])

    bs.client = NetClient()
    r = run("browser_network_requests", new=True)
    assert r.content[0].text == "### Result\nNo new requests." and not r.isError  # no file: passed through
    net = json.loads(run("browser_network_request_get", index=3, filename="ignored").content[0].text)
    paths = {f["path"] for f in net["files"]}
    assert {"original/response/headers.json", "original/response/body.html", "parsed/response/body.html.md",
            "original/response/body.png"} <= paths and not any("ignored" in p for p in paths), paths
    assert "http://n.test/x" in net["text"] and net["text_path"] == "parsed/response/body.html.md", net
    m = storage.get_manifest(jobs._key("a@b.c", net["job_id"]))
    assert (m["url"], m["method"], m["status"]) == ("http://n.test/p", "GET", 200), m
    assert "filename" not in bdriver._without_filename(mt.Tool(name="browser_network_request_get", inputSchema={"type": "object", "properties": {"index": {}, "filename": {}}})).inputSchema["properties"]
    bs.client = Client()

    key = jobs._key("a@b.c", out["job_id"])
    storage.write_bytes(key, "original/b.bin", b"\0\1", "application/octet-stream")
    g = lambda **a: asyncio.run(app.call_tool("grep_file", {"job_id": out["job_id"], **a}))
    ok = g(path="original/m.txt", pattern="ref=e1")
    assert not ok.isError and "ref=e1" in ok.content[0].text and "text: hello" in ok.content[0].text
    assert g(path="original/b.bin", pattern="x").isError and g(path="original/m.txt", pattern="(").isError
    assert g(path="original/none", pattern="x").content[0].text == "file not found"
    app._caller = lambda: (None, "other@b.c", None)
    assert g(path="original/m.txt", pattern="x").isError  # another owner's job is not reachable
    app._caller = lambda: (None, "a@b.c", "sid1")
    sessions.opened["sid1"] = Policy.make(True)


def test_request_make_body_and_job():
    import tempfile, json
    from mcp import types as mt
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    home = tempfile.mkdtemp()
    os.mkdir(home + "/out"); os.mkdir(home + "/in")
    ports.home = lambda uid: home
    jid = f"{jobs.RID}.123e4567-e89b-12d3-a456-426614174000"
    storage.write_bytes(jobs._key("a@b.c", jid), "original/d.json", b'{"k":1}', "application/json")
    ref = {"job_id": jid, "path": "original/d.json"}
    rb = lambda owner="a@b.c", routed=False, **a: files._request_body(owner, 1, a, routed)
    staged = lambda: sorted(os.listdir(home + "/in"))

    assert rb(url="u", body="x") == ({"url": "u", "body": "x"}, [])
    assert rb(url="u", body={"a": 1, "b": ["x", 2.5, True]})[0]["body"] == {"a": 1, "b": ["x", 2.5, True]} and staged() == []
    for bad in ({"body": {"job_id": jid}}, {"body": {"path": "original/d.json"}}, {"body": {**ref, "x": 1}},
                {"body": {"job_id": jid, "path": "../x"}}, {"body": {"job_id": jid, "path": "/etc/passwd"}},
                {"body": {"job_id": jid, "path": "original/missing"}}, {"body": {"job_id": "junk", "path": "original/d.json"}},
                {"body": {"f": {"path": "original/d.json"}}}, {"body": {"f": {**ref, "x": 1}}}, {"body": {"f": None}}, {"body": {"f": [["nested"]]}},
                {"body": {"f": {**ref, "filename": 1}}}, {"body": 5}, {"body": ["a"]}, {"type": "nope"},
                {"body_job_id": jid, "body_path": "original/d.json"}, {"type": "form", "body": "text"}, {"type": "form", "body": ref},
                {"type": "xhr", "body": ref}, {"type": "xhr", "body": "x", "method": "head"}):
        try:
            rb(**bad); assert False, bad
        except ValueError:
            pass
    try:
        rb("other@b.c", body=ref); assert False  # another owner's job
    except ValueError as e:
        assert str(e) == "file not found"
    assert staged() == []  # nothing is copied for a refused call

    files.MAX_REQUEST_BODY = 6  # the file is 7 bytes; text counts too
    for bad in ({"body": ref}, {"body": "1234567"}, {"body": {"a": "1234567"}}, {"type": "xhr", "body": "1234567", "method": "POST"}):
        try:
            rb(**bad); assert False, bad
        except ValueError as e:
            assert "too large" in str(e), e
    assert staged() == []
    files.MAX_REQUEST_BODY = 64 * 1024 * 1024
    files.MAX_UPLOAD_BYTES = 6
    try:
        rb(type="xhr", method="PUT", body=ref); assert False
    except ValueError as e:
        assert "file parts too large" in str(e)
    files.MAX_UPLOAD_BYTES = 500 * 1024 * 1024
    assert rb(type="xhr", method="PUT", body=ref)[0]["body_file"] and len(staged()) == 1  # raw job file: hidden body_file
    files._drop_in(1, staged()[0])
    files.ROUTED_UPLOAD_CAP = 6  # a session whose media policy routes every request
    try:
        rb(routed=True, type="xhr", method="PUT", body=ref); assert False
    except ValueError as e:
        assert "media policy" in str(e)
    assert rb(routed=True, type="navigation", method="POST", body=ref)[0]["body_file"]  # navigation is not affected
    files._drop_in(1, staged()[0])
    files.ROUTED_UPLOAD_CAP = 64 * 1024 * 1024

    a, names = rb(type="form", method="POST", body_file="client-chosen",
                  body={"k": "v", "f": {**ref, "filename": "../up.json", "content_type": "text/x"}, "l": [ref, "t"]})
    assert "body_file" not in a and len(names) == 2 and a["body"]["k"] == "v" and a["body"]["l"][1] == "t"
    f, l0 = a["body"]["f"], a["body"]["l"][0]
    assert f["filename"] == "up.json" and f["content_type"] == "text/x" and l0["filename"] == "d.json" and "content_type" not in l0  # basename, default from path
    for part, n in ((f, "up.json"), (l0, "d.json")):
        d = part["file"].split("/")[0]
        assert part["file"] == f"{d}/{n}" and open(f"{home}/in/{d}/{n}", "rb").read() == b'{"k":1}'
        assert oct(os.stat(f"{home}/in/{d}/{n}").st_mode & 0o777) == "0o644" and oct(os.stat(f"{home}/in/{d}").st_mode & 0o777) == "0o777"
    for n in names:
        files._drop_in(1, n); files._drop_in(1, n)  # idempotent
    assert staged() == []
    os.symlink("/etc/passwd", home + "/in/trap")  # a session-planted name is never written through
    shutil.move(home + "/in", home + "/real"); os.symlink(home + "/real", home + "/in")  # swapped in/ dir
    try:
        files._stage(1, jobs._key("a@b.c", jid), "original/d.json", "x"); assert False
    except OSError:
        pass
    os.unlink(home + "/in"); shutil.move(home + "/real", home + "/in"); os.unlink(home + "/in/trap")
    orig = files._stage  # a failure while staging the second file removes the first
    calls = []
    def flaky(uid, key, path, name):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return orig(uid, key, path, name)
    files._stage = flaky
    try:
        rb(type="xhr", method="POST", body={"a": ref, "b": ref}); assert False
    except OSError:
        pass
    finally:
        files._stage = orig
    assert staged() == []

    seen = []

    class Client:
        async def call_tool(self, name, args):
            seen.append(args)
            d = f"{home}/out/{args['filename']}"
            os.makedirs(d + "/request"); os.makedirs(d + "/response")
            open(d + "/request/headers.json", "w").write("{}")
            open(d + "/response/headers.json", "w").write("{}")
            return mt.CallToolResult(content=[mt.TextContent(type="text", text=(
                "### Result\n- [Network request](out/x)\n1. [POST] http://n.test/b => [BLOCKED] blocked: media policy\n"
                "Origin: http://o.test/page\n"
                'Redirects: [{"url": "http://n.test/a", "status": 307}]'))])

    bs = types.SimpleNamespace(client=Client(), owner="a@b.c", id="v", uid=1, last_used=0, opts=None)
    given = {"url": "http://n.test/a", "type": "form", "method": "POST", "cookies": {"a": "b"}, "body": {"f": ref}, "body_file": "x", "timeout_ms": 10**9}
    r = asyncio.run(driver.file_tool(bs, "browser_network_request_make", given))
    out = json.loads(r.content[0].text)
    assert "body_file" not in seen[0] and seen[0]["body"]["f"]["file"].endswith("/d.json") and staged() == [], seen  # leftovers dropped
    assert seen[0]["timeout_ms"] == files.MAX_REQUEST_TIMEOUT_MS
    m = storage.get_manifest(jobs._key("a@b.c", out["job_id"]))
    assert (m["url"], m["method"], m.get("status"), m["blocked"], m["redirects"][0]["status"]) == ("http://n.test/b", "POST", None, "media policy", 307), m
    assert (m["type"], m["origin"], m["body"]) == ("form", "http://o.test/page", {"f": ref}), m  # the argument as given, not the staged names
    assert any("media policy" in n for n in out["notes"]) and any("SameSite" in n for n in out["notes"]), out
    try:  # call_tool turns this into an error result; nothing reaches the toolserver
        asyncio.run(driver.file_tool(bs, "browser_network_request_make", {"url": "u", "type": "form", "body": "x"})); assert False
    except ValueError:
        assert len(seen) == 1 and staged() == []
    assert "filename" not in bdriver._without_filename(mt.Tool(name="browser_network_request_make", inputSchema={"type": "object", "properties": {"url": {}, "filename": {}}})).inputSchema["properties"]


def test_multi_browser_routing():
    from mcp import types as mt
    sessions._tools_cache = [mt.Tool(name=n, inputSchema={"type": "object"}) for n in
                             ("browser_navigate", "browser_end_session", "browser_bring_to_front", "browser_start_session")]
    names = [t.name for t in asyncio.run(app.list_tools())]
    assert names.count("browser_end_session") == 1 and "browser_bring_to_front" not in names
    assert "browser_start_session" in names

    calls, remaining, closed = [], [1], []

    class Client:
        async def call_tool(self, name, args):
            calls.append((name, args))
            text = '### Result\n{"remaining": %d, "blocked": 0}' % remaining[0] if name == "browser_end_session" else "### Result\n\"ok\""
            return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)])

    bs = types.SimpleNamespace(client=Client(), owner="a@b.c", id="v", uid=1, last_used=0, opts=None)

    async def get_session(*a, **k):
        return bs

    async def close(b):
        closed.append(b)
        sessions.opened.pop("sid1", None)  # like the real close(): it drops the policy itself

    app._caller = lambda: (None, "a@b.c", "sid1")
    sessions.opened["sid1"] = Policy.make(True)
    sessions.get_session, sessions.close = get_session, close
    run = lambda n, **a: asyncio.run(app.call_tool(n, a))
    run("browser_end_session", browser="x")
    assert calls[-1] == ("browser_end_session", {"browser": "x"}) and not closed  # others remain
    remaining[0] = 0
    run("browser_end_session", browser="x")
    assert closed == [bs] and "sid1" not in sessions.opened  # last browser closed -> session ends, policy must be asked again
    sessions.opened["sid1"] = Policy.make(True)
    run("browser_end_session")
    assert len(closed) == 2 and calls[-1] == ("browser_end_session", {})  # no id: ends them all (and reports the blocked count)
    sessions.opened["sid1"] = Policy.make(True)
    run("browser_request_human_help", browser="x", reason="r")
    assert calls[-1] == ("browser_bring_to_front", {"browser": "x"})
    assert bdriver.FILE_TOOLS["browser_storage_state"] and "browser_run_code_unsafe" not in bdriver.FILE_TOOLS


def test_open_session_gate_and_get_session():
    import json
    from mcp import types as mt
    app._caller = lambda: (None, "a@b.c", "sid-gate")
    sessions.opened.clear()
    run = lambda n, **a: asyncio.run(app.call_tool(n, a))
    text = lambda r: r.content[0].text
    for name, args in (("browser_navigate", {"url": "x"}), ("ingest_page", {"url": "x"}), ("list_files", {"job_id": "j"}),
                       ("browser_set_policy", {"policy": {}})):
        r = run(name, **args)
        assert r.isError, name  # fail closed before open_session; browser_set_policy is never client-callable
    # get_session works before open_session and equals the no-argument defaults
    before = json.loads(text(run("get_session")))
    assert before["opened"] is False and before["egress"] == "clear"
    r = run("open_session")
    after = json.loads(text(r))
    assert after == {**before, "opened": True} and not r.isError
    assert json.loads(text(run("get_session"))) == after
    assert run("open_session", allow=["nonsense"]).isError and json.loads(text(run("get_session"))) == after  # invalid: nothing ends
    closed = []

    async def get_session(*a, **k):
        return types.SimpleNamespace()

    async def close(b):
        closed.append(b)

    sessions.get_session, sessions.close = get_session, close
    r = run("open_session", media=True)  # a repeat call ends the old session and starts over
    assert not r.isError and len(closed) == 1 and json.loads(text(run("get_session")))["media"] is True
    # allow / deny land in the effective lists
    app._caller = lambda: (None, "a@b.c", "sid-2")
    s = json.loads(text(run("open_session", media=False, allow=["image/svg+xml"], deny=["text/csv"])))
    assert s["media"] is False and "image/svg+xml" in s["allowed_content_types"] and s["denied_content_types"] == ["text/csv"]
    # tools exposed to clients: open_session / get_session take no way to change policy via get_session; set_policy is hidden
    sessions._tools_cache = [mt.Tool(name=n, inputSchema={"type": "object"}) for n in ("browser_navigate", "browser_set_policy")]
    names = [t.name for t in asyncio.run(app.list_tools())]
    assert {"open_session", "get_session"} <= set(names) and "browser_set_policy" not in names
    assert next(t for t in asyncio.run(app.list_tools()) if t.name == "get_session").inputSchema["properties"] == {}
    sessions.opened.clear()


def test_ingest_page_blocks_media_on_nomedia_session(monkeypatch):

    class Resp:
        headers = {"Content-Type": "image/png"}
        closed = False

        @property
        def content(self):
            raise AssertionError("body must not be read")

        def raise_for_status(self):
            pass

        def close(self):
            self.closed = True

    resp = Resp()
    monkeypatch.setattr(fetch, "_request", lambda *a, **k: resp)
    monkeypatch.setattr(fetch, "content_type", lambda *a, **k: "image/png")
    with pytest.raises(ValueError, match="blocked: media disabled"):
        asyncio.run(pages.ingest_page(egress, "a@b.c", "http://x/a.png", None, None, Policy.make(False)))
    assert resp.closed
    with pytest.raises(ValueError, match="blocked"):
        fetch.download(egress, "http://x/a.png", Policy.make(True, deny=["image/png"]))
