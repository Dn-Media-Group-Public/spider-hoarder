"""Host-runnable checks: JWT verification, SSRF guard, browser_evaluate parsing,
CMP host-rules, owner scoping."""

import asyncio
import os
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


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
