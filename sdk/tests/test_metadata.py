"""File metadata: the extractors, the child-process limits, and the job integration (metadata/<path>.yaml)."""

import io
import shutil
import sys
import tempfile
import zipfile

import pytest
import yaml

from spider_hoarder_sdk import testing

testing.gateway_env()

from spider_hoarder_sdk.content import metadata, storage
from spider_hoarder_sdk.gateway import jobs

pytestmark = pytest.mark.metadata

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
GEOJSON = b'{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},"geometry":{"type":"Point","coordinates":[10.5,59.9]}}]}'


def docx(document: str, comments: str = "", member_bytes: bytes = b"") -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", f'<w:document {W}><w:body>{document}</w:body></w:document>')
        if comments:
            z.writestr("word/comments.xml", f'<w:comments {W}>{comments}</w:comments>')
        if member_bytes:
            z.writestr("word/big.xml", member_bytes)
        z.writestr("docProps/core.xml", '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/"><cp:lastModifiedBy>Bob</cp:lastModifiedBy></cp:coreProperties>')
    return b.getvalue()


def extract(name: str, data: bytes, ctype: str = "application/octet-stream"):
    """(metadata, notes) of `data` staged as `name`, ignoring a missing exiftool/ffprobe on the dev machine."""
    notes = []
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/f{name[name.rfind('.'):]}"
        open(path, "wb").write(data)
        meta = metadata.extract_metadata(path, ctype, name, notes)
    return meta, [n for n in notes if "exiftool" not in n and "media" not in n]


def job(monkeypatch):
    testing.fresh_store()
    return testing.new_job("a@x.y")[1]


def test_office_revisions_comments_and_hostile_xml():
    doc = (f'<!DOCTYPE d [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><w:document {W}><w:body><w:p>&b;'
           '<w:ins w:author="Jane" w:date="2024-03-01T10:12:00Z"><w:r><w:t>new</w:t></w:r></w:ins>'
           '<w:del w:author="Jim"><w:r><w:delText>old</w:delText></w:r></w:del></w:p></w:body></w:document>')
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/comments.xml", f'<w:comments {W}><w:comment w:author="Ann" w:date="d"><w:p><w:r><w:t>hm</w:t></w:r></w:p></w:comment></w:comments>')
    meta, notes = extract("a.docx", b.getvalue())
    o = meta["SpiderHoarder-Office"]
    assert not notes and o["RevisionCount"] == 2
    assert o["Revisions"][0] == {"Author": "Jane", "Date": "2024-03-01T10:12:00Z", "Type": "insert", "Text": "new"}
    assert o["Revisions"][1]["Type"] == "delete" and o["Comments"] == [{"Author": "Ann", "Date": "d", "Text": "hm"}]


def test_office_zip_bomb_is_refused_and_noted():
    meta, notes = extract("a.docx", docx("", member_bytes=b"0" * (200 << 20)))  # part itself is fine, the ratio is not read: big.xml is unused
    assert not notes  # unused parts are never inflated
    big = f'<w:document {W}><w:body>' + "<w:p/>" * 3_000_000 + "</w:body></w:document>"
    meta, notes = extract("b.docx", docx(big))
    assert any("office failed" in n for n in notes) and "SpiderHoarder-Office" not in meta


def test_raster_vector():
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    with tempfile.TemporaryDirectory() as tmp:
        with rasterio.open(f"{tmp}/g.tif", "w", driver="GTiff", width=4, height=4, count=1, dtype="uint8", crs="EPSG:25833",
                           transform=from_origin(500000, 6600000, 10, 10), nodata=0) as r:
            r.write(np.ones((4, 4), "uint8"), 1)
        meta, notes = extract("g.tif", open(f"{tmp}/g.tif", "rb").read())
    g = meta["SpiderHoarder-Raster"]
    assert not notes and g["CRS"]["EPSG"] == 25833 and g["BBox"] == [500000, 6599960, 500040, 6600000]
    assert 14 < g["BBoxWGS84"][0] < 16 and g["Size"] == {"Width": 4, "Height": 4} and g["NoData"] == [0.0]
    meta, notes = extract("p.geojson", GEOJSON)
    v = meta["SpiderHoarder-Vector"]
    assert not notes and v["BBox"] == [10.5, 59.9, 10.5, 59.9] and v["Layers"][0]["Features"] == 1


def test_pdf_incremental_update():
    from pypdf import PdfWriter
    with tempfile.TemporaryDirectory() as tmp:
        w = PdfWriter()
        w.add_blank_page(100, 100)
        w.add_metadata({"/Producer": "P1", "/Creator": "C1"})
        w.write(f"{tmp}/a.pdf")
        data = io.BytesIO(open(f"{tmp}/a.pdf", "rb").read())
        w2 = PdfWriter(fileobj=data, incremental=True)
        w2.add_metadata({"/Title": "t"})
        w2.write(data)
        open(f"{tmp}/a.pdf", "wb").write(data.getvalue())
        meta, notes = extract("a.pdf", open(f"{tmp}/a.pdf", "rb").read(), "application/pdf")
    p = meta["SpiderHoarder-PDF"]
    assert not notes and p["IncrementalUpdates"] >= 1 and p["Creator"] == "C1"


def test_media_postprocess():
    m = metadata._media({"format": {"duration": "3.5", "tags": {"title": "T"}},
                         "streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 480, "avg_frame_rate": "30000/1001"},
                                     {"codec_type": "audio", "codec_name": "aac", "channels": 2}],
                         "chapters": [{"start_time": "0", "end_time": "1", "tags": {"title": "c"}}]})
    assert m["Streams"][0]["FPS"] == 29.97 and m["Streams"][1]["Channels"] == 2 and m["Chapters"][0]["Title"] == "c"


@pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool not installed")
def test_exiftool_drops_staging_noise():
    meta, _ = extract("a.png", b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR\0\0\0\x01\0\0\0\x01\x08\x02\0\0\0\x90wS\xde\0\0\0\x00IEND\xaeB`\x82", "image/png")
    assert "System" not in meta and "Directory" not in meta.get("File", {}) and meta["File"]["FileType"] == "PNG"
    assert extract("a.txt", b"hello")[0] == {}  # only the file type: no metadata, no file


def test_limits_contain_children(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(metadata, "TIMEOUT", 1)
    with pytest.raises(Exception, match="timed out"):
        metadata._run(["sleep", "10"], tmp)
    monkeypatch.setattr(metadata, "MAX_MEM", 256 << 20)
    with pytest.raises(RuntimeError):  # MemoryError in the child: traceback on stderr, nothing on stdout
        metadata._run([sys.executable, "-I", "-c", "x = bytearray(1 << 30); print(1)"], tmp)
    with pytest.raises(RuntimeError):
        metadata._run(["false"], tmp)
    assert metadata._run(["echo", "ok"], tmp).strip() == b"ok"
    if metadata._sandbox():
        assert metadata._run([sys.executable, "-I", "-c", "import socket; print(len(socket.if_nameindex()))"], tmp).strip() == b"1"  # only lo
    assert metadata._run(["env"], tmp).count(b"\n") <= 5  # scrubbed environment


def test_dump_truncates(monkeypatch):
    monkeypatch.setattr(metadata, "MAX_BYTES", 2000)
    meta = {"File": {"FileType": "DOCX"}, "SpiderHoarder-Office": {"Revisions": [{"Author": "x" * 20, "Text": str(i)} for i in range(500)]}}
    text, truncated = metadata.dump_yaml(meta)
    assert truncated and len(text) <= 2000 and yaml.safe_load(text)["SpiderHoarder-Office"]["Truncated"] is True


def test_jobs_write_metadata_yaml(monkeypatch):
    key = job(monkeypatch)
    paths = lambda r: {f["path"] for f in r["files"]}
    r = jobs._store_and_parse(key, "p.geojson", GEOJSON, "application/geo+json")
    assert "metadata/p.geojson.yaml" in paths(r)
    assert yaml.safe_load(storage.read_text(key, "metadata/p.geojson.yaml"))["SpiderHoarder-Vector"]["Layers"]
    assert next(f for f in r["files"] if f["path"] == "metadata/p.geojson.yaml")["content_type"] == "application/yaml"
    monkeypatch.setattr(jobs, "MAX_PARSE_BYTES", 10)  # oversize: not parsed, still gets metadata
    r = jobs._store_and_parse(key, "big.geojson", GEOJSON, "application/geo+json")
    assert "metadata/big.geojson.yaml" in paths(r) and "parsed/big.geojson.md" not in paths(r) and any("not parsed" in n for n in r["notes"])
    monkeypatch.setattr(jobs, "MAX_PARSE_BYTES", 50 << 20)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("m.geojson", GEOJSON)
    assert "metadata/z.zip.d/m.geojson.yaml" in paths(jobs._store_and_parse(key, "z.zip", b.getvalue(), "application/zip"))
    assert "metadata/n.txt.yaml" not in paths(jobs._store_and_parse(key, "n.txt", b"hello", "text/plain"))
    monkeypatch.setattr(metadata, "MAX_BYTES", 400)
    revs = "".join('<w:ins w:author="A" w:date="d"><w:r><w:t>t</w:t></w:r></w:ins>' for _ in range(100))
    r = jobs._store_and_parse(key, "t.docx", docx(f"<w:p>{revs}</w:p>"), "application/octet-stream")
    assert any("metadata truncated" in n for n in r["notes"])
    assert yaml.safe_load(storage.read_text(key, "metadata/t.docx.yaml"))["SpiderHoarder-Office"]["Truncated"] is True
