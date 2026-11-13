"""Job store and gateway-generic checks: JWT verification, owner scoping, the job result from the manifest, tool dispatch errors (parsing itself is tested in parser/tests)."""

import asyncio
import os
import time
import types

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from spider_hoarder_sdk import testing

testing.gateway_env()

from spider_hoarder_sdk.gateway import auth, jobs

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


def test_write_stream():
    from spider_hoarder_sdk.content import storage
    import io, tempfile
    testing.fresh_store()
    data = b"abc" * (1 << 20)  # 3 MiB: several chunks
    assert storage.write_stream("j", "original/big.bin", io.BytesIO(data), "x/y") == len(data)
    assert storage.read_bytes("j", "original/big.bin") == data
    assert storage.get_manifest("j")["files"] == [{"path": "original/big.bin", "size": len(data), "content_type": "x/y"}]


def test_text_cap_and_grep():
    from spider_hoarder_sdk.textcap import cap_snapshot as cs, cap_found_line as cf
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

    from spider_hoarder_sdk.content import grep as extract
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


def test_manifest_meta_and_job_result():
    """set_meta survives later manifest updates; _job_result picks inline text from the manifest by size (the parser marks text files)."""
    from spider_hoarder_sdk.content import storage
    testing.fresh_store()
    key = testing.new_job("a@x.y")[1]
    storage.write_bytes(key, "original/d.html", b"<p>x</p>", "text/html")
    storage.set_meta(key, url="http://u", target="main")
    storage.write_bytes(key, "x.txt", b"x", "text/plain")  # a later manifest update keeps the meta
    m = storage.get_manifest(key)
    assert m["url"] == "http://u" and m["target"] == "main" and any(f["path"] == "x.txt" for f in m["files"]), m
    k2 = testing.new_job("a@x.y")[1]
    storage.write_bytes(k2, "a", b"a", "text/plain")
    assert "url" not in storage.get_manifest(k2) and jobs._meta(k2) == {}
    # text selection: parsed/ first, then a text-marked original; terse when the text is too big; nothing for an unparsed binary
    storage.write_bytes(k2, "original/n.md", b"# H\n[l](u)\n" + b"prose " * 10, "text/markdown")
    storage.write_bytes(k2, "terse/n.md", b"# H\n* [l](u)", "text/markdown")
    assert jobs._job_result(k2, "n.md")["text_path"] == "terse/n.md"  # original/n.md is not marked as text: the outline
    m = storage.get_manifest(k2)
    m["files"] = [{**f, "text": True} if f["path"] == "original/n.md" else f for f in m["files"]]
    storage.set_meta(k2, files=m["files"])
    r = jobs._job_result(k2, "n.md", ["a note"])
    assert r["text"].startswith("# H\n[l](u)") and r["text_path"] == "original/n.md" and r["notes"] == ["a note"]
    old, jobs.INLINE_LIMIT = jobs.INLINE_LIMIT, 20
    try:
        assert jobs._job_result(k2, "n.md")["text_path"] == "terse/n.md"
        jobs.INLINE_LIMIT = 3
        assert "text" not in jobs._job_result(k2, "n.md")
    finally:
        jobs.INLINE_LIMIT = old


def test_tool_exception_is_logged(caplog):
    """An unexpected exception in a tool reaches the client as a one-liner AND the server log with its traceback."""
    from spider_hoarder_sdk.gateway import app
    app._caller = lambda: (None, "a@b.c", "sid1")
    with caplog.at_level("ERROR", logger="app"):
        res = asyncio.run(app.call_tool("get_file_text", {"job_id": "j"}))   # invalid job_id: ValueError
    assert res.isError and any(r.exc_info and r.exc_info[0] is ValueError for r in caplog.records)


def test_job_names():
    from spider_hoarder_sdk.content import storage
    import tempfile
    testing.fresh_store()
    for given in ("../../etc/passwd", "/etc/passwd", "a\\..\\b.png", "", "..", None):
        n = jobs._job_name(given, "d")
        assert "/" not in n and "\\" not in n and n not in ("", ".", ".."), (given, n)
    assert jobs._job_name("../../x.png", "d") == "x.png"


def test_job_tools_through_the_gateway():
    """list_files / get_file_text / grep_file / get_file_url need no session and are scoped to the caller."""
    import json
    import tempfile
    from spider_hoarder_sdk.content import storage
    from spider_hoarder_sdk.gateway import app
    testing.fresh_store()
    job, key = testing.new_job("a@b.c")
    storage.write_bytes(key, "original/m.txt", b"- generic [ref=e1]:\n  - text: hello", "text/plain")
    storage.write_bytes(key, "original/b.bin", b"\0\1", "application/octet-stream")
    app._caller = lambda: (None, "a@b.c", "sid1")
    run = lambda name, **a: asyncio.run(app.call_tool(name, a))
    assert json.loads(run("list_files", job_id=job).content[0].text)["files"]
    assert "hello" in run("get_file_text", job_id=job, path="original/m.txt").content[0].text
    ok = run("grep_file", job_id=job, path="original/m.txt", pattern="ref=e1")
    assert not ok.isError and "ref=e1" in ok.content[0].text and "text: hello" in ok.content[0].text
    assert run("grep_file", job_id=job, path="original/b.bin", pattern="x").isError
    assert run("grep_file", job_id=job, path="original/m.txt", pattern="(").isError
    assert run("grep_file", job_id=job, path="original/none", pattern="x").content[0].text == "file not found"
    assert "/blob/" in json.loads(run("get_file_url", job_id=job, path="original/m.txt").content[0].text)["url"]
    app._caller = lambda: (None, "other@b.c", None)
    assert run("grep_file", job_id=job, path="original/m.txt", pattern="x").isError  # another owner's job is not reachable


def test_fake_driver_tools_and_unknown_tool_proxy():
    """The framework lists the driver's tools plus the job tools and the instructions carry the driver's note."""
    from spider_hoarder_sdk.gateway import app
    tools = asyncio.run(app.list_tools())
    names = [t.name for t in tools]
    assert "fake_tool" in names and {"list_files", "get_file_text", "grep_file", "get_file_url", "get_upload_link"} <= set(names)
    assert app.mcp.name == "spider-hoarder-fake-backend" and app.mcp.instructions.startswith("A fake backend.")
