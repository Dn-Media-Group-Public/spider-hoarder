"""The parser's own logic, with no storage or wrapper: terse outlines, archives (unpacked in the parser, never recursively), binary
formats, limits. `run` (conftest) parses an input tree into a MemSink."""

import base64
import bz2
import gzip
import io
import json
import lzma
import plistlib
import tarfile
import zipfile

import bson
import cbor2
import msgpack
import py7zr

from spider_hoarder_parser import extract, parse

PAGE = (b"<html><body><article><h1>T</h1><p>" + b"Some long article text here. " * 20 +
        b"</p><a href='/x'>l</a></article></body></html>")


def test_terse():
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


def test_html_md_and_text_marks(run):
    s = run({"p.html": (PAGE.replace(b"<a href='/x'>l</a>", b"<h2>Part</h2>"), "text/html"), "n.md": (b"# H\n[l](u)", "text/markdown"),
             "t.txt": (b"hello", "text/plain"), "e.md": b"only prose", "shot.png": b"\x89PNG\0\0"})
    assert {"parsed/p.html.md", "simplified/p.html.md", "terse/p.html.md", "terse/n.md", "parsed/t.txt.md"} <= set(s.files), set(s.files)
    assert s.files["terse/p.html.md"] == b"# T\n## Part"  # terse comes from parsed/
    assert "terse/e.md" not in s.files and not any(p.startswith("parsed/n") or p.startswith("parsed/e") for p in s.files)
    assert {"original/n.md", "original/e.md"} <= s.text and "original/shot.png" not in s.text


def test_tree_absolutizes_only_with_base_url(run):
    files = {"response/body.html": (PAGE, "text/html"), "response/headers.json": (b'{"a": 1}', "application/json")}
    assert b"http://h.test/x" in run(files, "http://h.test/p").files["parsed/response/body.html.md"]
    assert b"http://h.test/x" not in run(files).files["parsed/response/body.html.md"]


def tar(mode):
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode=mode) as tf:
        for n, d in (("a/n.md", b"# H\n[l](u)"), ("in.tgz", b"x")):
            ti = tarfile.TarInfo(n)
            ti.size = len(d)
            tf.addfile(ti, io.BytesIO(d))
    return b.getvalue()


def test_archives(run):
    for name, mode in (("t.tar", "w"), ("t.tgz", "w:gz"), ("t.tar.bz2", "w:bz2"), ("t.txz", "w:xz")):
        p = set(run({name: tar(mode)}).files)
        assert {f"original/{name}.d/a/n.md", f"terse/{name}.d/a/n.md", f"original/{name}.d/in.tgz"} <= p, p  # members are not unpacked again
    b = io.BytesIO()
    with py7zr.SevenZipFile(b, "w") as z:
        z.writestr(b"# H\n[l](u)", "a/n.md")
    assert "terse/s.7z.d/a/n.md" in run({"s.7z": (b.getvalue(), "application/x-7z-compressed")}).files
    for name, comp in (("n.md.gz", gzip.compress), ("n.md.bz2", bz2.compress), ("n.md.xz", lzma.compress)):
        assert f"terse/{name}.d/n.md" in run({name: comp(b"# H\n[l](u)")}).files
    s = run({"fake.gz": (b"plain text", "application/gzip")})  # not really gzip: parsed as any file
    assert not any("fake.gz.d" in p for p in s.files)
    assert extract.archive_kind("application/x-7z-compressed", "x") == "7z" and extract.archive_kind("", "a.rar") == "rar"
    assert extract.archive_kind("application/gzip", "a.tar.gz") == "tar" and extract.archive_kind("text/html", "a.html") is None


def test_zip_members_and_rerun(run):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/b.html", PAGE)
        zf.writestr("n.md", "# H\n[l](u)")
        zf.writestr("in.zip", b"PK")
    s = run({"bundle.zip": (buf.getvalue(), "application/zip")}, "http://h.test/p")
    assert {"original/bundle.zip.d/a/b.html", "parsed/bundle.zip.d/a/b.html.md", "simplified/bundle.zip.d/a/b.html.md",
            "terse/bundle.zip.d/n.md", "original/bundle.zip.d/in.zip"} <= set(s.files), set(s.files)
    assert "parsed/bundle.zip.d/n.md.md" not in s.files and not s.notes
    assert b"http://h.test/x" in s.files["parsed/bundle.zip.d/a/b.html.md"]  # members get the base_url too
    # a retry finds the members next to the archive in original/: parsed, but an archive member of them is not unpacked
    s = run({"bundle.zip": (buf.getvalue(), "application/zip"), "bundle.zip.d/in.zip": b"PK", "bundle.zip.d/n.md": b"# H"})
    assert not any(p.startswith("original/bundle.zip.d/in.zip.d") for p in s.files)
    # one zip nested as a network response body is unpacked next to it
    assert "original/response/body.zip.d/a/b.html" in run({"response/body.zip": (buf.getvalue(), "application/zip")}).files


def test_archive_limits(run, monkeypatch):
    many = io.BytesIO()
    with zipfile.ZipFile(many, "w") as zf:
        for i in range(5):
            zf.writestr(f"m{i}.txt", "x" * 100)
    monkeypatch.setattr(parse, "MAX_ARCHIVE_MEMBERS", 4)
    s = run({"many.zip": many.getvalue()})
    assert s.notes == ["archive not unpacked: 5 members > MAX_ARCHIVE_MEMBERS"] and not any("many.zip.d" in p for p in s.files)
    monkeypatch.setattr(parse, "MAX_ARCHIVE_MEMBERS", 50_000)
    monkeypatch.setattr(parse, "MAX_ARCHIVE_BYTES", 250)
    s = run({"many.zip": many.getvalue()})
    assert "MAX_ARCHIVE_BYTES" in s.notes[0]
    assert sum(p.startswith("original/many.zip.d") for p in s.files) == 2  # written before the abort
    monkeypatch.setattr(parse, "MAX_PARSE_BYTES", 10)  # stored (no cap) but not parsed
    s = run({"big.html": (b"<p>" + b"x" * 50 + b"</p>", "text/html")})
    assert s.notes == ["big.html not parsed (57 bytes > MAX_PARSE_BYTES)"] and not s.files


def test_binary_formats(run):
    obj = {"a": [1, 2.5], "b": b"\x00\x01"}
    want = {"a": [1, 2.5], "b": {"$base64": "AAE="}}
    for name, data, ct in (("d.msgpack", msgpack.packb(obj), ""), ("d.cbor", cbor2.dumps(obj), ""),
                           ("d.bson", bson.dumps(obj), ""), ("d.plist", plistlib.dumps(obj, fmt=plistlib.FMT_BINARY), ""),
                           ("noext", plistlib.dumps(obj, fmt=plistlib.FMT_BINARY), "application/octet-stream"),
                           ("noext2", msgpack.packb(obj), "application/x-msgpack")):
        s = run({name: (data, ct)})
        assert json.loads(s.files[f"parsed/{name}.json"]) == want, name
    assert json.loads(extract.binary_to_json(cbor2.dumps({1: float("nan")}), "cbor")) == {"1": "nan"}
    pb = b"\x0a\x03abc\x10\x96\x01"
    j = json.loads(extract.binary_to_json(pb, "protobuf"))
    assert j["message"] == {"1": {"$base64": "YWJj"}, "2": 150} and j["typedef"]["2"]["type"] == "int", j
    framed = b"\x00" + len(pb).to_bytes(4, "big") + pb + b"\x80" + (9).to_bytes(4, "big") + b"grpc-status: 0"[:9]
    j = json.loads(extract.binary_to_json(framed, "protobuf", "application/grpc-web+proto"))
    assert j[0]["message"]["2"] == 150 and j[1] == {"trailers": "grpc-stat"}, j
    assert len(json.loads(extract.binary_to_json(base64.b64encode(framed), "protobuf", "application/grpc-web-text"))) == 2
    s = run({"bad.cbor": b"\xa1\x01"})  # undecodable: noted, no parsed/
    assert "bad.cbor not parsed" in s.notes[0] and "parsed/bad.cbor.json" not in s.files
    assert "parsed/x.json.json" not in run({"x.json": (b'{"a":1}', "application/json")}).files


def test_apk_is_not_parsed(run):
    s = run({"x.apk": (b"PK\3\4", "application/vnd.android.package-archive")})
    assert not [p for p in s.files if p.startswith("parsed/")]
