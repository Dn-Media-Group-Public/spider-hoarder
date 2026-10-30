"""Job store and gateway-generic checks: JWT verification, owner scoping, storing and parsing files into jobs,
archives and binary formats, tool dispatch errors."""

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


def test_owner_scoping():
    from spider_hoarder_sdk.gateway import jobs
    j = f"{jobs.RID}.123e4567-e89b-12d3-a456-426614174000"
    assert jobs._key("A@x.y", j) != jobs._key("b@x.y", j)
    for bad in ("..", "x", ""):
        try:
            jobs._key(bad, j)
            assert False, bad
        except ValueError:
            pass


def test_write_stream():
    from spider_hoarder_sdk.content import storage
    import io, tempfile
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
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

    from spider_hoarder_sdk.content import extract
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


def test_terse():
    from spider_hoarder_sdk.content import extract
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
    from spider_hoarder_sdk.gateway import jobs
    from spider_hoarder_sdk.content import storage
    import tempfile
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    jobs.INLINE_LIMIT = 50_000
    key = jobs._key("a@x.y", f"{jobs.RID}.123e4567-e89b-12d3-a456-426614174001")
    paths = lambda r: {f["path"] for f in r["files"]}
    r = jobs._store_and_parse(key, "p.html", b"<html><body><article><h1>Title</h1><p>" + b"Some long article text here. " * 20 + b"</p><h2>Part</h2><p>" + b"More article text. " * 20 + b"</p></article></body></html>", "text/html")
    assert {"original/p.html", "parsed/p.html.md", "simplified/p.html.md", "terse/p.html.md"} <= paths(r), paths(r)
    assert storage.read_text(key, "terse/p.html.md") == "# Title\n## Part"  # terse comes from parsed/
    page = b"<html><body><p>" + b"Filler paragraph text. " * 40 + b"</p><div>Prose in a div with <a href='http://x'>a link</a> kept.</div></body></html>"
    r = jobs._store_and_parse(key, "d.html", page, "text/html")
    assert "Prose in a div with [a link](http://x) kept." in storage.read_text(key, "parsed/d.html.md")
    storage.set_meta(key, url="http://u", target="main")
    storage.write_bytes(key, "x.txt", b"x", "text/plain")  # a later manifest update keeps the meta
    m = storage.get_manifest(key)
    assert m["url"] == "http://u" and m["target"] == "main" and any(f["path"] == "d.html" or f["path"] == "x.txt" for f in m["files"]), m
    k2 = jobs._key("a@x.y", f"{jobs.RID}.123e4567-e89b-12d3-a456-426614174002")
    storage.write_bytes(k2, "a", b"a", "text/plain")
    assert "url" not in storage.get_manifest(k2) and jobs._meta(k2) == {}
    r = jobs._store_and_parse(key, "n.md", b"# H\n[l](u)", "text/markdown")
    assert "terse/n.md" in paths(r) and not any(p.startswith("parsed/n") for p in paths(r))
    assert r["text_path"] == "original/n.md"
    r = jobs._store_and_parse(key, "t.txt", b"hello", "text/plain")
    assert not any(p.startswith("terse/t") for p in paths(r))
    r = jobs._store_and_parse(key, "e.md", b"only prose", "text/markdown")
    assert "terse/e.md" not in paths(r)
    jobs.INLINE_LIMIT = 15  # parsed large, terse small -> outline inline
    r = jobs._store_and_parse(key, "big.md", b"# H\n[l](u)\n" + b"prose " * 10, "text/markdown")
    assert r["text"] == "# H\n* [l](u)" and r["text_path"] == "terse/big.md", r
    jobs.INLINE_LIMIT = 3  # both large
    assert "text" not in jobs._store_and_parse(key, "big2.md", b"# Heading\n[l](u)\n" + b"prose " * 10, "text/markdown")
    jobs.INLINE_LIMIT = 50_000


def test_archives_and_binary_formats():
    from spider_hoarder_sdk.gateway import jobs
    from spider_hoarder_sdk.content import extract, storage
    import tempfile, io, tarfile, gzip, bz2, lzma, json, msgpack, cbor2, bson, plistlib, base64, py7zr
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    key = jobs._key("a@x.y", f"{jobs.RID}.123e4567-e89b-12d3-a456-426614174011")

    def paths(r):
        return {f["path"] for f in r["files"]}

    def tar(mode):
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode=mode) as tf:
            for n, d in (("a/n.md", b"# H\n[l](u)"), ("in.tgz", b"x")):
                ti = tarfile.TarInfo(n); ti.size = len(d); tf.addfile(ti, io.BytesIO(d))
        return b.getvalue()
    for name, mode in (("t.tar", "w"), ("t.tgz", "w:gz"), ("t.tar.bz2", "w:bz2"), ("t.txz", "w:xz")):
        p = paths(jobs._store_and_parse(key, name, tar(mode), "application/octet-stream"))
        assert {f"original/{name}.d/a/n.md", f"terse/{name}.d/a/n.md", f"original/{name}.d/in.tgz"} <= p, p  # no recursion
    b = io.BytesIO()
    with py7zr.SevenZipFile(b, "w") as z:
        z.writestr(b"# H\n[l](u)", "a/n.md")
    assert "terse/s.7z.d/a/n.md" in paths(jobs._store_and_parse(key, "s.7z", b.getvalue(), "application/x-7z-compressed"))
    for name, comp in (("n.md.gz", gzip.compress), ("n.md.bz2", bz2.compress), ("n.md.xz", lzma.compress)):
        assert f"terse/{name}.d/n.md" in paths(jobs._store_and_parse(key, name, comp(b"# H\n[l](u)"), "application/octet-stream"))
    r = jobs._store_and_parse(key, "fake.gz", b"plain text", "application/gzip")  # not really gzip: parsed as any file
    assert "original/fake.gz" in paths(r) and not any("fake.gz.d" in p for p in paths(r))
    assert extract.archive_kind("application/x-7z-compressed", "x") == "7z" and extract.archive_kind("", "a.rar") == "rar"
    assert extract.archive_kind("application/gzip", "a.tar.gz") == "tar" and extract.archive_kind("text/html", "a.html") is None
    jobs.MAX_ARCHIVE_MEMBERS = 1
    r = jobs._store_and_parse(key, "lim.tgz", tar("w:gz"), "application/gzip")
    assert "MAX_ARCHIVE_MEMBERS" in r["notes"][0], r
    jobs.MAX_ARCHIVE_MEMBERS, jobs.MAX_ARCHIVE_BYTES = 50_000, 5
    r = jobs._store_and_parse(key, "lim.txz", tar("w:xz"), "application/octet-stream")
    assert "MAX_ARCHIVE_BYTES" in r["notes"][0], r
    jobs.MAX_ARCHIVE_BYTES = 4 * 1024**3

    # binary structured data -> parsed/<path>.json; the original is untouched
    obj = {"a": [1, 2.5], "b": b"\x00\x01"}
    want = {"a": [1, 2.5], "b": {"$base64": "AAE="}}
    for name, data, ct in (("d.msgpack", msgpack.packb(obj), ""), ("d.cbor", cbor2.dumps(obj), ""),
                           ("d.bson", bson.dumps(obj), ""), ("d.plist", plistlib.dumps(obj, fmt=plistlib.FMT_BINARY), ""),
                           ("noext", plistlib.dumps(obj, fmt=plistlib.FMT_BINARY), "application/octet-stream"),
                           ("noext2", msgpack.packb(obj), "application/x-msgpack")):
        r = jobs._store_and_parse(key, name, data, ct)
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
    r = jobs._store_and_parse(key, "bad.cbor", b"\xa1\x01", "")  # undecodable: noted, job not failed, no parsed/
    assert "bad.cbor not parsed" in r["notes"][0] and "parsed/bad.cbor.json" not in paths(r), r
    r = jobs._store_and_parse(key, "x.json", b'{"a":1}', "application/json")
    assert storage.read_bytes(key, "original/x.json") == b'{"a":1}' and "parsed/x.json.json" not in paths(r)


def test_tool_exception_is_logged(caplog):
    """An unexpected exception in a tool reaches the client as a one-liner AND the server log with its traceback."""
    from spider_hoarder_sdk.gateway import app
    app._caller = lambda: (None, "a@b.c", "sid1")
    with caplog.at_level("ERROR", logger="app"):
        res = asyncio.run(app.call_tool("get_file_text", {"job_id": "j"}))   # invalid job_id: ValueError
    assert res.isError and any(r.exc_info and r.exc_info[0] is ValueError for r in caplog.records)


def test_job_names_and_stored_files():
    from spider_hoarder_sdk.content import storage
    import tempfile
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    for given in ("../../etc/passwd", "/etc/passwd", "a\\..\\b.png", "", "..", None):
        n = jobs._job_name(given, "d")
        assert "/" not in n and "\\" not in n and n not in ("", ".", ".."), (given, n)
    assert jobs._job_name("../../x.png", "d") == "x.png"
    key = jobs._key("a@x.y", f"{jobs.RID}.123e4567-e89b-12d3-a456-426614174000")
    r = jobs._store_file(key, "shot.png", b"\x89PNG\0\0")
    assert "text" not in r and any(f["path"] == "original/shot.png" for f in r["files"]), r
    r = jobs._store_file(key, "snap.md", b"# page")
    assert r["text"] == "# page"
    jobs.INLINE_LIMIT = 3
    assert "text" not in jobs._store_file(key, "snap2.md", b"# page")
    jobs.INLINE_LIMIT = 50_000


def test_job_tools_through_the_gateway():
    """list_files / get_file_text / grep_file / get_file_url need no session and are scoped to the caller."""
    import json
    import tempfile
    from spider_hoarder_sdk.content import storage
    from spider_hoarder_sdk.gateway import app
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    job = f"{jobs.RID}.123e4567-e89b-12d3-a456-426614174077"
    key = jobs._key("a@b.c", job)
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


def test_tree_store_and_zip_limits():
    from spider_hoarder_sdk.gateway import jobs
    from spider_hoarder_sdk.content import extract, storage
    import tempfile, io, zipfile
    storage.BLOB_URL = "file://" + tempfile.mkdtemp()
    jobs.INLINE_LIMIT = 50_000
    key = jobs._key("a@x.y", f"{jobs.RID}.123e4567-e89b-12d3-a456-426614174010")
    page = b"<html><body><article><h1>T</h1><p>" + b"Some long article text here. " * 20 + b"</p><a href='/x'>l</a></article></body></html>"
    # tree: HTML gets absolutized only with a base_url, nested path kept, JSON parsed by the generic rule
    infos = jobs._store_tree(key, [("response/body.html", io.BytesIO(page), "text/html"),
                                  ("response/headers.json", io.BytesIO(b'{"a": 1}'), "application/json")], "http://h.test/p")
    paths = {f["path"] for f in storage.get_manifest(key)["files"]}
    assert {"original/response/body.html", "parsed/response/body.html.md", "simplified/response/body.html.md"} <= paths, paths
    assert "http://h.test/x" in storage.read_text(key, "parsed/response/body.html.md")
    assert infos["response/body.html"][1] == "parsed/response/body.html.md"
    # zip: members under <zip>.d/, HTML member gets simplified/ but no absolutizing, md member only terse/
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/b.html", page); zf.writestr("n.md", "# H\n[l](u)"); zf.writestr("in.zip", b"PK")
    r = jobs._store_and_parse(key, "bundle.zip", buf.getvalue(), "application/zip", "http://h.test/p")
    paths = {f["path"] for f in r["files"]}
    assert {"original/bundle.zip", "original/bundle.zip.d/a/b.html", "parsed/bundle.zip.d/a/b.html.md",
            "simplified/bundle.zip.d/a/b.html.md", "terse/bundle.zip.d/n.md", "original/bundle.zip.d/in.zip"} <= paths, paths
    assert "parsed/bundle.zip.d/n.md.md" not in paths and "notes" not in r and "text" not in r
    assert "http://h.test/x" not in storage.read_text(key, "parsed/bundle.zip.d/a/b.html.md")
    # a zip as a network response body is unpacked next to it
    r = jobs._store_and_parse(key, "response/body.zip", buf.getvalue(), "application/zip")
    assert "original/response/body.zip.d/a/b.html" in {f["path"] for f in r["files"]}
    # limits: member count (up front) and the running uncompressed total (each member alone is under the cap)
    many = io.BytesIO()
    with zipfile.ZipFile(many, "w") as zf:
        for i in range(5):
            zf.writestr(f"m{i}.txt", "x" * 100)
    jobs.MAX_ARCHIVE_MEMBERS = 4
    r = jobs._store_and_parse(key, "many.zip", many.getvalue(), "application/zip")
    assert r["notes"] == ["archive not unpacked: 5 members > MAX_ARCHIVE_MEMBERS"] and not any("many.zip.d" in f["path"] for f in r["files"]), r
    jobs.MAX_ARCHIVE_MEMBERS, jobs.MAX_ARCHIVE_BYTES = 50_000, 250
    r = jobs._store_and_parse(key, "many.zip", many.getvalue(), "application/zip")
    assert "MAX_ARCHIVE_BYTES" in r["notes"][0] and any(f["path"] == "original/many.zip" for f in r["files"]), r
    assert sum("many.zip.d" in f["path"] and f["path"].startswith("original") for f in r["files"]) == 2  # written before the abort
    jobs.MAX_ARCHIVE_BYTES = 4 * 1024**3
    # parse cap: stored (no cap) but not parsed
    jobs.MAX_PARSE_BYTES = 10
    r = jobs._store_and_parse(key, "big.html", b"<p>" + b"x" * 50 + b"</p>", "text/html")
    assert "text" not in r and r["notes"] == ["big.html not parsed (57 bytes > MAX_PARSE_BYTES)"], r
    assert any(f["path"] == "original/big.html" and f["size"] == 57 for f in r["files"]) and "parsed/big.html.md" not in storage.get_manifest(key)["files"].__str__()
    jobs.MAX_PARSE_BYTES = 50 * 1024 * 1024
