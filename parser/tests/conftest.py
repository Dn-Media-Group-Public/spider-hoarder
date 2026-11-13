import os
import shutil
import tempfile

import pytest

from spider_hoarder_parser import parse


class MemSink(parse.Sink):
    """Collects what the parser would hand to the wrapper."""

    def __init__(self):
        self.files, self.text, self.notes = {}, set(), []

    def put_bytes(self, rel, data, ctype):
        self.files[rel] = data

    def put_file(self, rel, src, ctype):
        self.files[rel] = open(src, "rb").read()
        os.unlink(src)

    def mark_text(self, rel):
        self.text.add(rel)

    def note(self, text):
        self.notes.append(text)


@pytest.fixture
def run():
    """run({name: bytes | (bytes, ctype)}, base_url="") -> MemSink: parse_job over a fresh input tree."""
    dirs = []

    def go(files: dict, base_url: str = "") -> MemSink:
        d = tempfile.mkdtemp()
        dirs.append(d)
        ctypes = {}
        for name, v in files.items():
            data, ct = v if isinstance(v, tuple) else (v, "application/octet-stream")
            ctypes[name] = ct
            os.makedirs(os.path.dirname(f"{d}/in/original/{name}"), exist_ok=True)
            open(f"{d}/in/original/{name}", "wb").write(data)
        os.makedirs(f"{d}/work")
        sink = MemSink()
        parse.parse_job(sink, f"{d}/in", {"base_url": base_url, "ctypes": ctypes}, f"{d}/work")
        return sink

    yield go
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_metadata(request, monkeypatch):
    """Metadata extraction needs exiftool/ffprobe and adds files and notes: only tests marked `metadata` run it."""
    if "metadata" not in request.keywords:
        from spider_hoarder_parser import metadata
        monkeypatch.setattr(metadata, "extract_metadata", lambda *a, **k: {})
