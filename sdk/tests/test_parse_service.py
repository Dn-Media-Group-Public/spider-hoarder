"""The gateway against a real parser service (cadmus-parser-server in a thread, its real worker and the `files` processor) and a real object store
(scripts/test.sh starts a SeaweedFS): this project's job layout (input = <job>/original, output = <job>), its scoped credentials, the index.json
it reads back and the notes it reports. The wrapper's own behaviour (hostile parsers, limits, renewal codes ...) is tested in the library."""

import io
import json
import sys
import time
import zipfile

import pytest

from spider_hoarder_sdk import testing

testing.gateway_env()

from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway import creds, jobs, parse_client

PROFILES = {"remote": {"url": "http://ocr-remote", "token": "tok-remote"}, "lan": {"url": "http://ocr-lan", "token": "tok-lan"}}


def script(code: str) -> list:
    """A stand-in parser: `code` runs with fd 1 = the protocol pipe, jobdir = sys.argv[1]."""
    return [sys.executable, "-I", "-u", "-c", "import os, sys, json, time\njobdir = sys.argv[1]\n" + code]


@pytest.fixture
def parser():
    made = []

    def make(**cfg):
        made.append(testing.start_parser(**cfg))
        return made[-1]
    yield make
    for p in made:
        p.stop()


def test_end_to_end_through_the_gateway_client(parser, monkeypatch):
    parser()
    monkeypatch.setattr(jobs, "INLINE_LIMIT", 50_000)
    job_id, key = testing.new_job()
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("a/n.md", "# H\n[l](u)")
        z.writestr("in.zip", b"PK")
    page = b"<html><body><article><h1>Title</h1><p>" + b"Some long article text here. " * 20 + b"</p><h2>Part</h2></article></body></html>"
    jobs._store_original(key, "page.html", page, "text/html")
    jobs._store_original(key, "b.zip", zb.getvalue(), "application/zip")
    storage.set_meta(key, url="http://u", target="main")
    r = jobs._parse_job(key, "http://h.test/", "page.html")
    files = r["files"]
    assert set(files) == {"page.html", "b.zip"}  # per input file, relative to original/
    assert set(files["page.html"]["outputs"]) == {"parsed/page.html.md", "simplified/page.html.md", "terse/page.html.md"}
    assert files["page.html"]["content_type"] == "text/html"  # the type the originals were stored with reached the processor
    members = files["b.zip"]["files"]
    assert set(members) == {"a/n.md", "in.zip"} and "original/b.zip.d/a/n.md" in members["a/n.md"]["outputs"] and "terse/b.zip.d/a/n.md" in members["a/n.md"]["outputs"]
    assert members["a/n.md"]["meta"] == {"text": True} and "files" not in members["in.zip"]
    assert "notes" not in r and r["text_path"] == "parsed/page.html.md" and "Title" in r["text"]
    m = storage.get_manifest(key)
    assert m["url"] == "http://u" and m["target"] == "main" and set(m["files"]) == {"page.html", "b.zip"}  # the gateway's own keys live in meta.json, beside the index
    assert "content_types" not in m and storage.get_meta(key)["content_types"]["page.html"] == "text/html"
    assert json.loads(storage.read_bytes(key, "status.json"))["status"] == "done"
    assert storage.read_bytes(key, "terse/page.html.md") == b"# Title\n## Part"
    monkeypatch.setattr(jobs, "INLINE_LIMIT", 16)  # parsed too big for inline, terse fits
    assert jobs._job_result(key, "page.html")["text_path"] == "terse/page.html.md"
    # a second parse of the same, done job is allowed (an upload link adds files over time): a fresh index of everything stored, archives not re-unpacked
    jobs._store_original(key, "second.md", b"# Two", "text/markdown")
    r2 = jobs._parse_job(key, "http://h.test/", "second.md")
    f2 = r2["files"]
    assert {"page.html", "b.zip", "second.md", "b.zip.d/a/n.md", "b.zip.d/in.zip"} == set(f2) and r2.get("text_path") == "original/second.md", r2  # stored members are inputs now
    assert "files" not in f2["b.zip"] and "files" not in f2["b.zip.d/in.zip"] and not creds.active  # not unpacked again; nothing minted is left registered


def test_gateway_client_reports_problems_as_notes(monkeypatch):
    testing.fresh_store()
    key = testing.new_job()[1]
    jobs._store_original(key, "x.md", b"# x", "text/markdown")
    monkeypatch.delenv("PARSE_URL", raising=False)
    r = jobs._parse_job(key, "", "x.md")
    assert "no parser service configured" in r["notes"][0] and set(r["files"]) == {"x.md"}  # the original is never lost, also before any parse
    monkeypatch.setenv("PARSE_URL", "http://127.0.0.1:9")
    r = jobs._parse_job(key, "", "x.md")
    assert "parser unavailable" in r["notes"][0] and set(r["files"]) == {"x.md"}
    assert not creds.active  # the credential of the failed attempt was revoked


def test_a_failed_parse_says_why_and_keeps_the_originals(parser):
    parser(parser_cmd=script("sys.exit(3)"))
    key = testing.new_job()[1]
    jobs._store_original(key, "x.md", b"# x", "text/markdown")
    r = jobs._parse_job(key, "", "x.md")
    assert r["notes"] == ["not parsed (parser exited with 3)"] and set(r["files"]) == {"x.md"} and "text" not in r


def test_ocr_profile_reaches_the_parser_only_when_asked(parser, monkeypatch):
    parser(ocr_profiles=PROFILES, parser_cmd=script("os.write(1, json.dumps({'note': json.dumps({k: v for k, v in os.environ.items() if k.startswith('OCR')})}).encode() + b'\\n')"))
    key = testing.new_job()[1]
    jobs._store_original(key, "x.md", b"# x", "text/markdown")
    monkeypatch.setenv("PARSE_OCR", "lan")
    assert "ocr-lan" in jobs._parse_job(key, "", "x.md")["notes"][0] and "tok-remote" not in storage.read_bytes(key, "index.json").decode()
    monkeypatch.delenv("PARSE_OCR")


def test_caption_feature_only_when_set(monkeypatch):
    seen = []
    class C:
        def __init__(self, *a): pass
        def run(self, *a, features=None, **k): seen.append(features); raise RuntimeError
    monkeypatch.setattr(parse_client, "Client", C)
    monkeypatch.setenv("PARSE_URL", "http://x")
    monkeypatch.delenv("PARSE_CAPTION", raising=False)
    monkeypatch.setenv("PARSE_CAPTION_FALLBACK", "b")
    monkeypatch.setattr(parse_client.creds, "urls", lambda k: (None, "i", "o"))
    monkeypatch.setattr(parse_client.storage, "get_meta", lambda k: {})
    key = "projects/p/data/s/j"
    for cap in (None, "a"):
        if cap: monkeypatch.setenv("PARSE_CAPTION", cap)
        try: parse_client.parse(key)
        except Exception: pass
    assert seen == [{"caption-fallback": "b"}, {"caption": "a", "caption-fallback": "b"}]


def test_credentials_are_renewed_while_a_long_job_runs_and_all_revoked_after(parser, monkeypatch):
    parser(parser_cmd=script("os.makedirs(jobdir + '/out/parsed', exist_ok=True)\ntime.sleep(3)\nopen(jobdir + '/out/parsed/late.md', 'wb').write(b'late')\n"
                             "os.write(1, json.dumps({'in': ['x.md'], 'path': 'parsed/late.md', 'content_type': 'text/markdown'}).encode() + b'\\n')"))
    monkeypatch.setattr(parse_client, "RENEW_EVERY", 0.5)
    minted = []
    real = creds.track
    monkeypatch.setattr(creds, "track", lambda proj, h: (minted.append(h), real(proj, h))[1])
    key = testing.new_job()[1]
    jobs._store_original(key, "x.md", b"# x", "text/markdown")
    r = jobs._parse_job(key, "", "x.md")
    assert "notes" not in r and "parsed/late.md" in r["files"]["x.md"]["outputs"]  # the upload after several renewals still worked
    assert len(minted) >= 4 and all(h.revoked for h in minted) and not creds.active  # the first, every renewal: revoked, none left registered
