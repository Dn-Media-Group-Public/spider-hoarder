"""Host-runnable checks: JWT verification, SSRF guard, browser_evaluate parsing,
CMP host-rules, owner scoping."""

import asyncio
import os
import shutil
import time
import types

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

os.environ.setdefault("BLOB_TOKEN_SECRET", "test-secret")
os.environ.setdefault("PUBLIC_HOST", "h.example")
os.environ.setdefault("OIDC_ISSUER", "https://kc.example/realms/r")
os.environ.setdefault("REPLICA_RID", "10-0-0-5")

import auth
import cmp_rules
import netguard
import sessions

ISS = "https://kc.example/realms/r"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
verifier = auth.KeycloakVerifier(ISS, "mcp", signing_key=lambda t: types.SimpleNamespace(key=KEY.public_key()))


def _tok(**over):
    claims = {"iss": ISS, "aud": "whatever", "sub": "u", "exp": int(time.time()) + 60, "email": "a@b.c", "scope": "openid mcp", **over}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


def test_jwt():
    ok = lambda t: asyncio.run(verifier.verify_token(t))
    assert ok(_tok()).claims["email"] == "a@b.c"
    assert ok(_tok(aud="anything")) and ok(_tok(aud=None))
    assert ok(_tok(scope="openid")) is None
    assert ok(_tok(iss="https://evil")) is None
    assert ok(_tok(exp=int(time.time()) - 5)) is None
    assert ok(_tok(email=None)) is None


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
    assert sessions.parse_eval_result(text) == ("<html>\n<body>x</body></html>", "http://u/")
    assert sessions.parse_result('### Result\n[{"name": "a"}]') == [{"name": "a"}]


def test_host_rules(tmp_path="/tmp/cmp_test.txt"):
    open(tmp_path, "w").write("! c\n||cmp.example^\n@@||ok.example^\n||x.example^$third-party\n##.banner\n")
    assert cmp_rules.hosts(tmp_path) == ["cmp.example", "x.example"]  # $options kept, @@ and ## dropped
    assert cmp_rules.host_rules(tmp_path).startswith("MAP cmp.example ~NOTFOUND, MAP *.cmp.example ~NOTFOUND")


def test_filter_proxy():
    import filter_proxy as f
    B = frozenset({"cookielaw.org"})
    assert f.is_blocked("cdn.cookielaw.org", B) and f.is_blocked("cookielaw.org", B)
    assert not f.is_blocked("notcookielaw.org", B) and not f.is_blocked("example.org", B)


def test_owner_scoping():
    import app
    j = f"{app.RID}.123e4567-e89b-12d3-a456-426614174000"
    assert app._key("A@x.y", j) != app._key("b@x.y", j)
    for bad in ("..", "x", ""):
        try:
            app._key(bad, j)
            assert False, bad
        except ValueError:
            pass


def test_file_outputs():
    import app, storage, tempfile
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    home = tempfile.mkdtemp()
    app.ports.home = lambda uid: home
    os.mkdir(home + "/out")
    w = lambda n, b: open(f"{home}/out/{n}", "wb").write(b)
    w("ok.txt", b"hi")
    read = lambda n, tree=False: [(r, f.read()) for r, f in app._out_files(1, n, tree)]
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
        n = app._job_name(given, "d")
        assert "/" not in n and "\\" not in n and n not in ("", ".", ".."), (given, n)
    assert app._job_name("../../x.png", "d") == "x.png"
    j = f"{app.RID}.123e4567-e89b-12d3-a456-426614174000"
    key = app._key("a@x.y", j)
    r = app._store_file(key, "shot.png", b"\x89PNG\0\0")
    assert "text" not in r and any(f["path"] == "original/shot.png" for f in r["files"]), r
    r = app._store_file(key, "snap.md", b"# page")
    assert r["text"] == "# page"
    app.INLINE_LIMIT = 3
    assert "text" not in app._store_file(key, "snap2.md", b"# page")


def test_tree_reader():
    import app, tempfile
    home = tempfile.mkdtemp()
    app.ports.home = lambda uid: home
    d = home + "/out/net"
    os.makedirs(d + "/request"); os.makedirs(d + "/response"); os.makedirs(d + "/other")
    w = lambda n, b: open(f"{d}/{n}", "wb").write(b)
    w("request/headers.json", b"{}"); w("response/headers.json", b"{}"); w("response/body.png", b"\0\1")
    w("response/evil.txt", b"x"); w("other/headers.json", b"x"); w("response/body.", b"x")
    os.symlink("/etc/passwd", d + "/response/body.txt")  # symlink at the file level: ignored
    os.mkfifo(d + "/request/body.bin")  # non-regular: ignored
    got = lambda n="net": {r: f.read() for r, f in app._out_files(1, n, True)}
    assert got() == {"request/headers.json": b"{}", "response/headers.json": b"{}", "response/body.png": b"\0\1"}, got()
    shutil.move(d + "/response", home + "/elsewhere"); os.symlink(home + "/elsewhere", d + "/response")  # symlinked subdir
    assert set(got()) == {"request/headers.json"}
    shutil.move(d, home + "/out/real"); os.symlink(home + "/out/real", d)  # symlinked tree root
    try:
        got(); assert False
    except OSError:
        pass
    try:
        list(app._out_files(1, "real", False)); assert False  # a directory is not a file
    except ValueError:
        pass


def test_tree_store_and_zip_limits():
    import app, extract, storage, tempfile, io, zipfile
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    app.INLINE_LIMIT = 50_000
    key = app._key("a@x.y", f"{app.RID}.123e4567-e89b-12d3-a456-426614174010")
    page = b"<html><body><article><h1>T</h1><p>" + b"Some long article text here. " * 20 + b"</p><a href='/x'>l</a></article></body></html>"
    # tree: HTML gets absolutized only with a base_url, nested path kept, JSON parsed by the generic rule
    infos = app._store_tree(key, [("response/body.html", io.BytesIO(page), "text/html"),
                                  ("response/headers.json", io.BytesIO(b'{"a": 1}'), "application/json")], "http://h.test/p")
    paths = {f["path"] for f in storage.get_manifest(key)["files"]}
    assert {"original/response/body.html", "parsed/response/body.html.md", "simplified/response/body.html.md"} <= paths, paths
    assert "http://h.test/x" in storage.read_text(key, "parsed/response/body.html.md")
    assert infos["response/body.html"][1] == "parsed/response/body.html.md"
    # zip: members under <zip>.d/, HTML member gets simplified/ but no absolutizing, md member only terse/
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/b.html", page); zf.writestr("n.md", "# H\n[l](u)"); zf.writestr("in.zip", b"PK")
    r = app._store_and_parse(key, "bundle.zip", buf.getvalue(), "application/zip", "http://h.test/p")
    paths = {f["path"] for f in r["files"]}
    assert {"original/bundle.zip", "original/bundle.zip.d/a/b.html", "parsed/bundle.zip.d/a/b.html.md",
            "simplified/bundle.zip.d/a/b.html.md", "terse/bundle.zip.d/n.md", "original/bundle.zip.d/in.zip"} <= paths, paths
    assert "parsed/bundle.zip.d/n.md.md" not in paths and "notes" not in r and "text" not in r
    assert "http://h.test/x" not in storage.read_text(key, "parsed/bundle.zip.d/a/b.html.md")
    # a zip as a network response body is unpacked next to it
    r = app._store_and_parse(key, "response/body.zip", buf.getvalue(), "application/zip")
    assert "original/response/body.zip.d/a/b.html" in {f["path"] for f in r["files"]}
    # limits: member count (up front) and the running uncompressed total (each member alone is under the cap)
    many = io.BytesIO()
    with zipfile.ZipFile(many, "w") as zf:
        for i in range(5):
            zf.writestr(f"m{i}.txt", "x" * 100)
    app.MAX_ARCHIVE_MEMBERS = 4
    r = app._store_and_parse(key, "many.zip", many.getvalue(), "application/zip")
    assert r["notes"] == ["archive not unpacked: 5 members > MAX_ARCHIVE_MEMBERS"] and not any("many.zip.d" in f["path"] for f in r["files"]), r
    app.MAX_ARCHIVE_MEMBERS, app.MAX_ARCHIVE_BYTES = 50_000, 250
    r = app._store_and_parse(key, "many.zip", many.getvalue(), "application/zip")
    assert "MAX_ARCHIVE_BYTES" in r["notes"][0] and any(f["path"] == "original/many.zip" for f in r["files"]), r
    assert sum("many.zip.d" in f["path"] and f["path"].startswith("original") for f in r["files"]) == 2  # written before the abort
    app.MAX_ARCHIVE_BYTES = 4 * 1024**3
    # parse cap: stored (no cap) but not parsed
    app.MAX_PARSE_BYTES = 10
    r = app._store_and_parse(key, "big.html", b"<p>" + b"x" * 50 + b"</p>", "text/html")
    assert "text" not in r and r["notes"] == ["big.html not parsed (57 bytes > MAX_PARSE_BYTES)"], r
    assert any(f["path"] == "original/big.html" and f["size"] == 57 for f in r["files"]) and "parsed/big.html.md" not in storage.get_manifest(key)["files"].__str__()
    app.MAX_PARSE_BYTES = 50 * 1024 * 1024


def test_write_stream():
    import storage, tempfile, io
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    data = b"abc" * (1 << 20)  # 3 MiB: several chunks
    assert storage.write_stream("j", "original/big.bin", io.BytesIO(data), "x/y") == len(data)
    assert storage.read_bytes("j", "original/big.bin") == data
    assert storage.get_manifest("j")["files"] == [{"path": "original/big.bin", "size": len(data), "content_type": "x/y"}]


def test_text_cap_and_grep():
    from toolserver.textcap import cap_snapshot as cs, cap_found_line as cf
    long = "x" * 200
    assert cs(f"- text: {long}", 10) == "- text: " + "x" * 10 + "..."
    assert cs(f"- paragraph [ref=e2]: {long}", 10).endswith("x" * 10 + "...")
    assert cs(f'- link "{long}" [ref=e3]\n  - /url: {long}', 10) == f'- link "{long}" [ref=e3]\n  - /url: {long}'
    assert cs(f"- text: {long}", None) == f"- text: {long}" and cs("- text: abc", 128) == "- text: abc"
    assert cs("- text: abcdef", 1) == "- text: a..."
    t = "a" * 100 + "NEEDLE" + "b" * 100
    line = f"- paragraph [ref=e1]: {t}"
    s = line.index("NEEDLE")
    out = cf(line, (s, s + 6), 20)
    assert "NEEDLE" in out and out.startswith("- paragraph [ref=e1]: ...") and out.endswith("...")
    assert cf(line, (0, 3), 20) == "- paragraph [ref=e1]: " + "a" * 20 + "..."  # match in the ref: cut from the start
    assert cf(line, (s, s + 6), 300) == line and cf("- text: hi", None, 5) == "- text: hi"
    assert cf(line, (len(line) - 3, len(line)), 20).startswith("- paragraph [ref=e1]: ...") and cf(line, (len(line) - 3, len(line)), 20).endswith("b" * 17 + "")

    import extract
    text = "\n".join(f"line {i}" for i in range(100)) + "\nTAIL " + "z" * 500
    g = extract.grep_text(text, "line 5$", context=1)
    assert g.startswith("-- line 5, offset ") and "5- line 4" in g and "6: line 5" in g and "7- line 6" in g and "line 7" not in g
    assert extract.grep_text(text, "/LINE 9$/i", 0).splitlines()[1] == "10: line 9"
    assert extract.grep_text(text, "line 1.$", 0, max_matches=2).endswith("[showing 2 of 10 matches]")
    assert extract.grep_text(text, "nope") == "No matches for nope."
    assert "..." in extract.grep_text(text, "TAIL").splitlines()[-1] and len(extract.grep_text(text, "TAIL").splitlines()[-1]) < 320
    off = int(extract.grep_text(text, "line 50$", 0).split("offset ")[1].split(" ")[0])
    assert text[off:].startswith("line 50")
    try:
        extract.grep_text(text, "("); assert False
    except ValueError:
        pass


def test_file_tools_are_jobs():
    import app, storage, tempfile, json
    from mcp import types as mt
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    home = tempfile.mkdtemp()
    os.mkdir(home + "/out")
    app.ports.home = lambda uid: home
    snap = "- generic [ref=e1]:\n  - text: hello"

    class Client:
        async def call_tool(self, name, args):
            open(f"{home}/out/{args['filename']}", "w").write(snap)
            return mt.CallToolResult(content=[mt.TextContent(type="text", text="### Page\n- Page URL: http://p/\n### Snapshot\n- [Snapshot](out/x)\n")])

    bs = types.SimpleNamespace(client=Client(), owner="a@b.c", id="v", uid=1, last_used=0)

    async def get_session(*a, **k):
        return bs
    app._caller = lambda: (None, "a@b.c", "sid1")
    sessions.policies["sid1"] = app.Policy.make("clear")
    sessions.get_session = get_session
    app.INLINE_LIMIT = 50_000
    run = lambda name, **a: asyncio.run(app.call_tool(name, a))

    r = run("browser_snapshot", target="e5", depth=2)
    out = json.loads(r.content[0].text)
    assert out["text"] == snap and out["files"][0]["path"] == "original/snapshot.yml", out
    m = storage.get_manifest(app._key("a@b.c", out["job_id"]))
    assert m["url"] == "http://p/" and m["target"] == "e5" and m["depth"] == 2 and m["text_cap"] == 128 and "query" not in m, m
    r = run("browser_find", regex="/x/i", text_cap=None, filename="m.txt")
    out = json.loads(r.content[0].text)
    m = storage.get_manifest(app._key("a@b.c", out["job_id"]))
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
    m = storage.get_manifest(app._key("a@b.c", net["job_id"]))
    assert (m["url"], m["method"], m["status"]) == ("http://n.test/p", "GET", 200), m
    assert "filename" not in app._without_filename(mt.Tool(name="browser_network_request_get", inputSchema={"type": "object", "properties": {"index": {}, "filename": {}}})).inputSchema["properties"]
    bs.client = Client()

    key = app._key("a@b.c", out["job_id"])
    storage.write_bytes(key, "original/b.bin", b"\0\1", "application/octet-stream")
    g = lambda **a: asyncio.run(app.call_tool("grep_file", {"job_id": out["job_id"], **a}))
    ok = g(path="original/m.txt", pattern="ref=e1")
    assert not ok.isError and "ref=e1" in ok.content[0].text and "text: hello" in ok.content[0].text
    assert g(path="original/b.bin", pattern="x").isError and g(path="original/m.txt", pattern="(").isError
    assert g(path="original/none", pattern="x").content[0].text == "file not found"
    app._caller = lambda: (None, "other@b.c", None)
    assert g(path="original/m.txt", pattern="x").isError  # another owner's job is not reachable
    app._caller = lambda: (None, "a@b.c", "sid1")
    sessions.policies["sid1"] = app.Policy.make("clear")


def test_request_make_body_and_job():
    import app, storage, tempfile, json
    from mcp import types as mt
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    home = tempfile.mkdtemp()
    os.mkdir(home + "/out"); os.mkdir(home + "/in")
    app.ports.home = lambda uid: home
    jid = f"{app.RID}.123e4567-e89b-12d3-a456-426614174000"
    storage.write_bytes(app._key("a@b.c", jid), "original/d.json", b'{"k":1}', "application/json")
    ref = {"job_id": jid, "path": "original/d.json"}
    rb = lambda owner="a@b.c", routed=False, **a: app._request_body(owner, 1, a, routed)
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

    app.MAX_REQUEST_BODY = 6  # the file is 7 bytes; text counts too
    for bad in ({"body": ref}, {"body": "1234567"}, {"body": {"a": "1234567"}}, {"type": "xhr", "body": "1234567", "method": "POST"}):
        try:
            rb(**bad); assert False, bad
        except ValueError as e:
            assert "too large" in str(e), e
    assert staged() == []
    app.MAX_REQUEST_BODY = 64 * 1024 * 1024
    app.MAX_UPLOAD_BYTES = 6
    try:
        rb(type="xhr", method="PUT", body=ref); assert False
    except ValueError as e:
        assert "file parts too large" in str(e)
    app.MAX_UPLOAD_BYTES = 500 * 1024 * 1024
    assert rb(type="xhr", method="PUT", body=ref)[0]["body_file"] and len(staged()) == 1  # raw job file: hidden body_file
    app._drop_in(1, staged()[0])
    app.ROUTED_UPLOAD_CAP = 6  # a session whose media policy routes every request
    try:
        rb(routed=True, type="xhr", method="PUT", body=ref); assert False
    except ValueError as e:
        assert "media policy" in str(e)
    assert rb(routed=True, type="navigation", method="POST", body=ref)[0]["body_file"]  # navigation is not affected
    app._drop_in(1, staged()[0])
    app.ROUTED_UPLOAD_CAP = 64 * 1024 * 1024

    a, names = rb(type="form", method="POST", body_file="agent-chosen",
                  body={"k": "v", "f": {**ref, "filename": "../up.json", "content_type": "text/x"}, "l": [ref, "t"]})
    assert "body_file" not in a and len(names) == 2 and a["body"]["k"] == "v" and a["body"]["l"][1] == "t"
    f, l0 = a["body"]["f"], a["body"]["l"][0]
    assert f["filename"] == "up.json" and f["content_type"] == "text/x" and l0["filename"] == "d.json" and "content_type" not in l0  # basename, default from path
    for part, n in ((f, "up.json"), (l0, "d.json")):
        d = part["file"].split("/")[0]
        assert part["file"] == f"{d}/{n}" and open(f"{home}/in/{d}/{n}", "rb").read() == b'{"k":1}'
        assert oct(os.stat(f"{home}/in/{d}/{n}").st_mode & 0o777) == "0o644" and oct(os.stat(f"{home}/in/{d}").st_mode & 0o777) == "0o777"
    for n in names:
        app._drop_in(1, n); app._drop_in(1, n)  # idempotent
    assert staged() == []
    os.symlink("/etc/passwd", home + "/in/trap")  # a session-planted name is never written through
    shutil.move(home + "/in", home + "/real"); os.symlink(home + "/real", home + "/in")  # swapped in/ dir
    try:
        app._stage(1, app._key("a@b.c", jid), "original/d.json", "x"); assert False
    except OSError:
        pass
    os.unlink(home + "/in"); shutil.move(home + "/real", home + "/in"); os.unlink(home + "/in/trap")
    orig = app._stage  # a failure while staging the second file removes the first
    calls = []
    def flaky(uid, key, path, name):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return orig(uid, key, path, name)
    app._stage = flaky
    try:
        rb(type="xhr", method="POST", body={"a": ref, "b": ref}); assert False
    except OSError:
        pass
    finally:
        app._stage = orig
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

    bs = types.SimpleNamespace(client=Client(), owner="a@b.c", id="v", uid=1, last_used=0)
    given = {"url": "http://n.test/a", "type": "form", "method": "POST", "cookies": {"a": "b"}, "body": {"f": ref}, "body_file": "x", "timeout_ms": 10**9}
    r = asyncio.run(app._file_tool(bs, "browser_network_request_make", given))
    out = json.loads(r.content[0].text)
    assert "body_file" not in seen[0] and seen[0]["body"]["f"]["file"].endswith("/d.json") and staged() == [], seen  # leftovers dropped
    assert seen[0]["timeout_ms"] == app.MAX_REQUEST_TIMEOUT_MS
    m = storage.get_manifest(app._key("a@b.c", out["job_id"]))
    assert (m["url"], m["method"], m.get("status"), m["blocked"], m["redirects"][0]["status"]) == ("http://n.test/b", "POST", None, "media policy", 307), m
    assert (m["type"], m["origin"], m["body"]) == ("form", "http://o.test/page", {"f": ref}), m  # the argument as given, not the staged names
    assert any("media policy" in n for n in out["notes"]) and any("SameSite" in n for n in out["notes"]), out
    try:  # call_tool turns this into an error result; nothing reaches the toolserver
        asyncio.run(app._file_tool(bs, "browser_network_request_make", {"url": "u", "type": "form", "body": "x"})); assert False
    except ValueError:
        assert len(seen) == 1 and staged() == []
    assert "filename" not in app._without_filename(mt.Tool(name="browser_network_request_make", inputSchema={"type": "object", "properties": {"url": {}, "filename": {}}})).inputSchema["properties"]


def test_terse():
    import extract
    t = extract.terse
    md = """Intro [a](http://a "title") text ![i](http://i.png)

Setext
======

## Sec [**bold** `c`](http://h) x

[![alt](img)](url) and [**b** t](u2) <https://auto.x> https://lin.kify/x [r][ref]

| c |
|---|
| [tl](tu) |

- [li](lu)

> [q](qu)

```
# not a heading
[no](link)
```

    [no](indented)

`[no](code)`

<div><a href="x">no</a></div>

[a](http://a)

[ref]: http://ref
"""
    assert t(md).split("\n") == [
        "* [a](http://a)", "* ![i](http://i.png)", "# Setext", "## Sec [bold c](http://h) x",
        "* [![alt](img)](url)", "* [b t](u2)", "* <https://auto.x>", "* <https://lin.kify/x>", "* [r](http://ref)",
        "* [tl](tu)", "* [li](lu)", "* [q](qu)", "* [a](http://a)"], t(md)
    assert t("just prose\n\n```\n# x\n```") == ""


def test_store_terse():
    import app, storage, tempfile
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    app.INLINE_LIMIT = 50_000
    key = app._key("a@x.y", f"{app.RID}.123e4567-e89b-12d3-a456-426614174001")
    paths = lambda r: {f["path"] for f in r["files"]}
    r = app._store_and_parse(key, "p.html", b"<html><body><article><h1>Title</h1><p>" + b"Some long article text here. " * 20 + b"</p><h2>Part</h2><p>" + b"More article text. " * 20 + b"</p></article></body></html>", "text/html")
    assert {"original/p.html", "parsed/p.html.md", "simplified/p.html.md", "terse/p.html.md"} <= paths(r), paths(r)
    assert storage.read_text(key, "terse/p.html.md") == "# Title\n## Part"  # terse comes from parsed/
    page = b"<html><body><p>" + b"Filler paragraph text. " * 40 + b"</p><div>Prose in a div with <a href='http://x'>a link</a> kept.</div></body></html>"
    r = app._store_and_parse(key, "d.html", page, "text/html")
    assert "Prose in a div with [a link](http://x) kept." in storage.read_text(key, "parsed/d.html.md")
    storage.set_meta(key, url="http://u", target="main")
    storage.write_bytes(key, "x.txt", b"x", "text/plain")  # a later manifest update keeps the meta
    m = storage.get_manifest(key)
    assert m["url"] == "http://u" and m["target"] == "main" and any(f["path"] == "d.html" or f["path"] == "x.txt" for f in m["files"]), m
    k2 = app._key("a@x.y", f"{app.RID}.123e4567-e89b-12d3-a456-426614174002")
    storage.write_bytes(k2, "a", b"a", "text/plain")
    assert "url" not in storage.get_manifest(k2) and app._meta(k2) == {}
    r = app._store_and_parse(key, "n.md", b"# H\n[l](u)", "text/markdown")
    assert "terse/n.md" in paths(r) and not any(p.startswith("parsed/n") for p in paths(r))
    assert r["text_path"] == "original/n.md"
    r = app._store_and_parse(key, "t.txt", b"hello", "text/plain")
    assert not any(p.startswith("terse/t") for p in paths(r))
    r = app._store_and_parse(key, "e.md", b"only prose", "text/markdown")
    assert "terse/e.md" not in paths(r)
    app.INLINE_LIMIT = 15  # parsed large, terse small -> outline inline
    r = app._store_and_parse(key, "big.md", b"# H\n[l](u)\n" + b"prose " * 10, "text/markdown")
    assert r["text"] == "# H\n* [l](u)" and r["text_path"] == "terse/big.md", r
    app.INLINE_LIMIT = 3  # both large
    assert "text" not in app._store_and_parse(key, "big2.md", b"# Heading\n[l](u)\n" + b"prose " * 10, "text/markdown")
    app.INLINE_LIMIT = 50_000


def test_multi_browser_routing():
    import app
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

    bs = types.SimpleNamespace(client=Client(), owner="a@b.c", id="v", uid=1, last_used=0)

    async def get_session(*a, **k):
        return bs

    async def close(b):
        closed.append(b)

    app._caller = lambda: (None, "a@b.c", "sid1")
    sessions.policies["sid1"] = app.Policy.make("clear")
    sessions.get_session, sessions.close = get_session, close
    run = lambda n, **a: asyncio.run(app.call_tool(n, a))
    run("browser_end_session", browser="x")
    assert calls[-1] == ("browser_end_session", {"browser": "x"}) and not closed  # others remain
    remaining[0] = 0
    run("browser_end_session", browser="x")
    assert closed == [bs]  # last browser closed -> session ends
    run("browser_end_session")
    assert len(closed) == 2 and calls[-1] == ("browser_end_session", {})  # no id: ends them all (and reports the blocked count)
    run("browser_request_human_help", browser="x", reason="r")
    assert calls[-1] == ("browser_bring_to_front", {"browser": "x"})
    assert app.FILE_TOOLS["browser_storage_state"] and "browser_run_code_unsafe" not in app.FILE_TOOLS


def test_archives_and_binary_formats():
    import app, extract, storage, tempfile, io, tarfile, gzip, bz2, lzma, json, msgpack, cbor2, bson, plistlib, base64, py7zr
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    key = app._key("a@x.y", f"{app.RID}.123e4567-e89b-12d3-a456-426614174011")

    def paths(r):
        return {f["path"] for f in r["files"]}

    def tar(mode):
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode=mode) as tf:
            for n, d in (("a/n.md", b"# H\n[l](u)"), ("in.tgz", b"x")):
                ti = tarfile.TarInfo(n); ti.size = len(d); tf.addfile(ti, io.BytesIO(d))
        return b.getvalue()
    for name, mode in (("t.tar", "w"), ("t.tgz", "w:gz"), ("t.tar.bz2", "w:bz2"), ("t.txz", "w:xz")):
        p = paths(app._store_and_parse(key, name, tar(mode), "application/octet-stream"))
        assert {f"original/{name}.d/a/n.md", f"terse/{name}.d/a/n.md", f"original/{name}.d/in.tgz"} <= p, p  # no recursion
    b = io.BytesIO()
    with py7zr.SevenZipFile(b, "w") as z:
        z.writestr(b"# H\n[l](u)", "a/n.md")
    assert "terse/s.7z.d/a/n.md" in paths(app._store_and_parse(key, "s.7z", b.getvalue(), "application/x-7z-compressed"))
    for name, comp in (("n.md.gz", gzip.compress), ("n.md.bz2", bz2.compress), ("n.md.xz", lzma.compress)):
        assert f"terse/{name}.d/n.md" in paths(app._store_and_parse(key, name, comp(b"# H\n[l](u)"), "application/octet-stream"))
    r = app._store_and_parse(key, "fake.gz", b"plain text", "application/gzip")  # not really gzip: parsed as any file
    assert "original/fake.gz" in paths(r) and not any("fake.gz.d" in p for p in paths(r))
    assert extract.archive_kind("application/x-7z-compressed", "x") == "7z" and extract.archive_kind("", "a.rar") == "rar"
    assert extract.archive_kind("application/gzip", "a.tar.gz") == "tar" and extract.archive_kind("text/html", "a.html") is None
    app.MAX_ARCHIVE_MEMBERS = 1
    r = app._store_and_parse(key, "lim.tgz", tar("w:gz"), "application/gzip")
    assert "MAX_ARCHIVE_MEMBERS" in r["notes"][0], r
    app.MAX_ARCHIVE_MEMBERS, app.MAX_ARCHIVE_BYTES = 50_000, 5
    r = app._store_and_parse(key, "lim.txz", tar("w:xz"), "application/octet-stream")
    assert "MAX_ARCHIVE_BYTES" in r["notes"][0], r
    app.MAX_ARCHIVE_BYTES = 4 * 1024**3

    # binary structured data -> parsed/<path>.json; the original is untouched
    obj = {"a": [1, 2.5], "b": b"\x00\x01"}
    want = {"a": [1, 2.5], "b": {"$base64": "AAE="}}
    for name, data, ct in (("d.msgpack", msgpack.packb(obj), ""), ("d.cbor", cbor2.dumps(obj), ""),
                           ("d.bson", bson.dumps(obj), ""), ("d.plist", plistlib.dumps(obj, fmt=plistlib.FMT_BINARY), ""),
                           ("noext", plistlib.dumps(obj, fmt=plistlib.FMT_BINARY), "application/octet-stream"),
                           ("noext2", msgpack.packb(obj), "application/x-msgpack")):
        r = app._store_and_parse(key, name, data, ct)
        assert "parsed/%s.json" % name in paths(r), (name, paths(r))
        assert json.loads(storage.read_text(key, f"parsed/{name}.json")) == want, name
        assert storage.read_bytes(key, f"original/{name}") == data
    assert json.loads(extract.binary_to_json(cbor2.dumps({1: float("nan")}), "cbor")) == {"1": "nan"}
    pb = b"\x0a\x03abc\x10\x96\x01"
    j = json.loads(extract.binary_to_json(pb, "protobuf"))
    assert j["message"] == {"1": {"$base64": "YWJj"}, "2": 150} and j["typedef"]["2"]["type"] == "int", j
    framed = b"\x00" + len(pb).to_bytes(4, "big") + pb + b"\x80" + (9).to_bytes(4, "big") + b"grpc-status: 0"[:9]
    j = json.loads(extract.binary_to_json(framed, "protobuf", "application/grpc-web+proto"))
    assert j[0]["message"]["2"] == 150 and j[1] == {"trailers": "grpc-stat"}, j
    j = json.loads(extract.binary_to_json(base64.b64encode(framed), "protobuf", "application/grpc-web-text"))
    assert len(j) == 2
    r = app._store_and_parse(key, "bad.cbor", b"\xa1\x01", "")  # undecodable: noted, job not failed, no parsed/
    assert "bad.cbor not parsed" in r["notes"][0] and "parsed/bad.cbor.json" not in paths(r), r
    r = app._store_and_parse(key, "x.json", b'{"a":1}', "application/json")
    assert storage.read_bytes(key, "original/x.json") == b'{"a":1}' and "parsed/x.json.json" not in paths(r)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")


def test_open_session_gate_and_get_session():
    import json
    import app
    from mcp import types as mt
    app._caller = lambda: (None, "a@b.c", "sid-gate")
    sessions.policies.clear()
    run = lambda n, **a: asyncio.run(app.call_tool(n, a))
    text = lambda r: r.content[0].text
    for name, args in (("browser_navigate", {"url": "x"}), ("ingest_page", {"url": "x"}), ("list_files", {"job_id": "j"}),
                       ("browser_set_policy", {"policy": {}})):
        r = run(name, **args)
        assert r.isError, name  # fail closed before open_session; browser_set_policy is never client-callable
    # get_session works before open_session and equals the no-argument defaults
    before = json.loads(text(run("get_session")))
    assert before["opened"] is False and before["egress"] == app.EGRESS
    r = run("open_session")
    after = json.loads(text(r))
    assert after == {**before, "opened": True} and not r.isError
    assert json.loads(text(run("get_session"))) == after
    r = run("open_session", media=True)  # the policy cannot change
    assert r.isError and json.loads(text(run("get_session"))) == after
    assert run("open_session", allow=["nonsense"]).isError
    # allow / deny land in the effective lists
    app._caller = lambda: (None, "a@b.c", "sid-2")
    s = json.loads(text(run("open_session", media=False, allow=["image/svg+xml"], deny=["text/csv"])))
    assert s["media"] is False and "image/svg+xml" in s["allowed_content_types"] and s["denied_content_types"] == ["text/csv"]
    # tools exposed to clients: open_session / get_session take no way to change policy via get_session; set_policy is hidden
    sessions._tools_cache = [mt.Tool(name=n, inputSchema={"type": "object"}) for n in ("browser_navigate", "browser_set_policy")]
    names = [t.name for t in asyncio.run(app.list_tools())]
    assert {"open_session", "get_session"} <= set(names) and "browser_set_policy" not in names
    assert next(t for t in asyncio.run(app.list_tools()) if t.name == "get_session").inputSchema["properties"] == {}
    sessions.policies.clear()


def test_ingest_page_blocks_media_on_nomedia_session(monkeypatch):
    import app
    import fetch
    from toolserver.mediapolicy import Policy

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
        asyncio.run(app._ingest_page("a@b.c", "http://x/a.png", None, None, Policy.make("tor")))
    assert resp.closed
    with pytest.raises(ValueError, match="blocked"):
        fetch.download("http://x/a.png", Policy.make("clear", deny=["image/png"]))
