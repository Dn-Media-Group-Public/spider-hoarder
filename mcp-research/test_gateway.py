"""Host-runnable checks: JWT verification, SSRF guard, browser_evaluate parsing,
CMP host-rules, owner scoping."""

import asyncio
import os
import shutil
import time
import types

import jwt
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
    assert app._read_out(1, "ok.txt") == b"hi"
    w("big", b"x" * 11)
    app.MAX_FILE_BYTES = 10
    for bad in ("big", "missing"):
        try:
            app._read_out(1, bad); assert False, bad
        except (ValueError, OSError):
            pass
    app.MAX_FILE_BYTES = 1000
    os.symlink("/etc/passwd", home + "/out/link")
    os.mkfifo(home + "/out/fifo")
    for bad in ("link", "fifo"):  # symlink refused, FIFO neither hangs nor is read
        try:
            app._read_out(1, bad); assert False, bad
        except (ValueError, OSError):
            pass
    shutil.move(home + "/out", home + "/real"); os.symlink(home + "/real", home + "/out")  # swapped out/ dir
    try:
        app._read_out(1, "ok.txt"); assert False
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
            text = '### Result\n{"remaining": %d}' % remaining[0] if name == "browser_end_session" else "### Result\n\"ok\""
            return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)])

    bs = types.SimpleNamespace(client=Client(), owner="a@b.c", id="v", uid=1, last_used=0)

    async def get_session(*a, **k):
        return bs

    async def close(b):
        closed.append(b)

    app._caller = lambda: (None, "a@b.c", None)
    sessions.get_session, sessions.close = get_session, close
    run = lambda n, **a: asyncio.run(app.call_tool(n, a))
    run("browser_end_session", browser="x")
    assert calls[-1] == ("browser_end_session", {"browser": "x"}) and not closed  # others remain
    remaining[0] = 0
    run("browser_end_session", browser="x")
    assert closed == [bs]  # last browser closed -> session ends
    run("browser_end_session")
    assert len(closed) == 2 and calls[-1][1] == {"browser": "x"}  # no id: no upstream call
    run("browser_request_human_help", browser="x", reason="r")
    assert calls[-1] == ("browser_bring_to_front", {"browser": "x"})
    assert app.FILE_TOOLS["browser_storage_state"] and "browser_run_code_unsafe" not in app.FILE_TOOLS


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
