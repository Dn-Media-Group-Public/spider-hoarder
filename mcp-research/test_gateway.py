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
os.environ.setdefault("MCP_AUDIENCE", "aud")

import auth
import cmp_rules
import netguard
import sessions

ISS, AUD = "https://kc.example/realms/r", "aud"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
verifier = auth.KeycloakVerifier(ISS, AUD, "/inv", signing_key=lambda t: types.SimpleNamespace(key=KEY.public_key()))


def _tok(**over):
    claims = {"iss": ISS, "aud": AUD, "sub": "u", "exp": int(time.time()) + 60, "email": "a@b.c", "groups": ["/inv"], **over}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


def test_jwt():
    ok = lambda t: asyncio.run(verifier.verify_token(t))
    assert ok(_tok()).claims["email"] == "a@b.c"
    assert ok(_tok(aud="other")) is None
    assert ok(_tok(iss="https://evil")) is None
    assert ok(_tok(exp=int(time.time()) - 5)) is None
    assert ok(_tok(groups=[])) is None
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
    text = '### Result\n"<html>\\n<body>x</body></html>"\n### Ran Playwright code\n```js\nawait page.evaluate()\n```'
    assert sessions.parse_eval_result(text) == "<html>\n<body>x</body></html>"


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
    j = "123e4567-e89b-12d3-a456-426614174000"
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
    j = "123e4567-e89b-12d3-a456-426614174000"
    key = app._key("a@x.y", j)
    r = app._store_file(key, "shot.png", b"\x89PNG\0\0")
    assert "text" not in r and any(f["path"] == "original/shot.png" for f in r["files"]), r
    r = app._store_file(key, "snap.md", b"# page")
    assert r["text"] == "# page"
    app.INLINE_LIMIT = 3
    assert "text" not in app._store_file(key, "snap2.md", b"# page")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
