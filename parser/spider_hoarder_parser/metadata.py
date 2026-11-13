"""File metadata (EXIF/GPS, Office revisions, GeoTIFF CRS, media streams, PDF history ...) as one YAML mapping per file.

ExifTool's own groups come first; our extensions are further groups named `SpiderHoarder-<Format>`. Every extractor runs
in its own child process on a staged copy of the file (wall-clock timeout, memory/CPU/output limits, scrubbed environment; the parser container has no egress); a failing extractor is reported in `notes` and never fails the job.
Python extractors are the functions below, run as `python -I -m spider_hoarder_parser.metadata <extractor> <path>`."""

import json
import math
import mimetypes
import os
import shutil
import signal
import subprocess
import sys

TIMEOUT = int(os.environ.get("METADATA_TIMEOUT") or 30)
MAX_MEM = int(os.environ.get("METADATA_MAX_MEM") or 1 << 30)
MAX_BYTES = int(os.environ.get("METADATA_MAX_BYTES") or 1 << 20)  # size of the written YAML
MAX_RAW = 16 << 20  # size of one extractor's JSON output (RLIMIT_FSIZE)
MAX_ITEMS = 5000  # per list inside an extractor (revisions, comments, history)
MAX_PART = 64 << 20  # per OOXML part, uncompressed

RASTER = {".tif", ".tiff", ".geotiff", ".img", ".asc", ".jp2", ".nc", ".hgt", ".dem"}
VECTOR = {".gpx", ".kml", ".geojson", ".gpkg", ".zip"}  # a zip only if it holds a shapefile
OFFICE = {".docx", ".xlsx", ".pptx"}
MEDIA = {".mp3", ".mp4", ".m4a", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".flac", ".ogg", ".opus", ".wav", ".aac", ".wmv", ".flv"}
FILE_NOISE = {"FileName", "Directory", "FileSize", "FileModifyDate", "FileAccessDate", "FileInodeChangeDate", "FilePermissions"}


# ---- python extractors (child process) ------------------------------------------------------------------------

def _wgs84(crs, bounds):
    from rasterio.warp import transform_bounds
    return list(transform_bounds(crs, "EPSG:4326", *bounds))


def _raster(path):
    import rasterio
    with rasterio.open(path) as r:
        out = {"Size": {"Width": r.width, "Height": r.height}, "Resolution": list(r.res), "NoData": list(r.nodatavals),
               "Bands": [{"DataType": d, "Description": desc} for d, desc in zip(r.dtypes, r.descriptions)],
               "Tags": r.tags()}
        if r.crs:  # without a CRS the bounds are just pixel coordinates
            out |= {"CRS": {"EPSG": r.crs.to_epsg(), "WKT": r.crs.to_wkt()}, "BBox": list(r.bounds),
                    "BBoxWGS84": _wgs84(r.crs, r.bounds)}
        return out


def _vector(path):
    import pyogrio
    import zipfile
    if path.lower().endswith(".zip"):
        if not any(n.lower().endswith(".shp") for n in zipfile.ZipFile(path).namelist()):
            return {}
        path = "zip://" + path
    layers, crs, boxes = [], None, []
    for name, _ in pyogrio.list_layers(path):
        info = pyogrio.read_info(path, layer=name)
        layers.append({"Name": name, "GeometryType": info["geometry_type"], "Features": info["features"]})
        crs = crs or info["crs"]
        if info["total_bounds"] is not None and all(map(math.isfinite, info["total_bounds"])):
            boxes.append(info["total_bounds"])
    out = {"Layers": layers}
    if crs:
        out["CRS"] = crs
    if boxes:
        out["BBox"] = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
        if crs:
            out["BBoxWGS84"] = _wgs84(crs, out["BBox"])
    return out


def _capped(items):
    """(items cut to MAX_ITEMS, True if cut)."""
    return items[:MAX_ITEMS], len(items) > MAX_ITEMS


def _office(path):
    import re
    import zipfile
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)  # no entity bombs, no XXE
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    revs, comments, out = [], [], {}
    with zipfile.ZipFile(path) as z:
        total = 0

        def part(name):
            nonlocal total
            info = z.getinfo(name)
            total += info.file_size
            if info.file_size > MAX_PART or total > 2 * MAX_PART or info.file_size > 200 * max(info.compress_size, 1000):
                raise ValueError(f"{name}: too large / zip bomb")
            with z.open(name) as f:
                data = f.read(MAX_PART + 1)  # the header size can lie
            if len(data) > MAX_PART:
                raise ValueError(f"{name}: too large")
            return etree.fromstring(data, parser)

        names = set(z.namelist())
        if "word/document.xml" in names:
            for el in part("word/document.xml").iter(W + "ins", W + "del"):
                revs.append({"Author": el.get(W + "author"), "Date": el.get(W + "date"),
                             "Type": "insert" if el.tag == W + "ins" else "delete",
                             "Text": "".join(t.text or "" for t in el.iter(W + "t", W + "delText"))})
        authors = {}  # xlsx: authors/author by position; pptx: cmAuthor by id
        for n in sorted(names):
            if re.fullmatch(r"ppt/commentAuthors\.xml", n):
                authors |= {a.get("id"): a.get("name") for a in part(n).iter("{*}cmAuthor")}
        for n in sorted(names):
            if re.fullmatch(r"word/comments\.xml|xl/comments\d*\.xml|ppt/comments/[^/]+\.xml", n):
                root = part(n)
                xl_authors = [a.text for a in root.iter("{*}author")]
                for c in root.iter("{*}comment", "{*}cm"):
                    aid = c.get("authorId")
                    author = c.get(W + "author") or authors.get(aid) or (xl_authors[int(aid)] if aid is not None and aid.isdigit() and int(aid) < len(xl_authors) else None)
                    comments.append({"Author": author, "Date": c.get(W + "date") or c.get("dt"), "Text": "".join(c.itertext()).strip()})
        if "docProps/core.xml" in names:
            for el in part("docProps/core.xml").iter("{*}lastModifiedBy"):
                out["LastModifiedBy"] = el.text
    out["RevisionCount"] = len(revs)
    (out["Revisions"], t1), (out["Comments"], t2) = _capped(revs), _capped(comments)
    if t1 or t2:
        out["Truncated"] = True
    return out


def _pdf(path):
    import mmap
    import re
    from lxml import etree
    from pypdf import PdfReader
    out = {}
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        # ponytail: a linearized PDF also has two %%EOF, so it reads as one update too many
        out["IncrementalUpdates"] = max(len(re.findall(rb"%%EOF", m)) - 1, 0)
        s = m.find(b"<x:xmpmeta")
        e = m.find(b"</x:xmpmeta>", s) if s >= 0 else -1
        if e >= 0:
            root = etree.fromstring(m[s:e + 12], etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
            hist = []
            for li in root.iter("{http://ns.adobe.com/xap/1.0/mm/}History"):
                for ev in li.iter("{*}li"):
                    hist.append({etree.QName(c).localname: c.text for c in ev if c.text}
                                | {etree.QName(k).localname: v for k, v in ev.attrib.items()})
            out["History"], truncated = _capped([h for h in hist if h])
            if truncated:
                out["Truncated"] = True
            for el in root.iter("{http://ns.adobe.com/xap/1.0/}CreatorTool"):
                out["CreatorTool"] = el.text
    info = PdfReader(path).metadata
    if info:
        out["Producer"], out["Creator"] = info.producer, info.creator
    return out


EXTRACTORS = {"raster": _raster, "vector": _vector, "office": _office, "pdf": _pdf}


# ---- parent side ----------------------------------------------------------------------------------------------

def _fps(rate):
    n, _, d = (rate or "0/1").partition("/")
    return round(float(n) / float(d or 1), 3) if float(d or 1) else None


def _media(j):
    """ffprobe JSON -> the SpiderHoarder-Media group."""
    fmt = j.get("format", {})
    streams = [{"Type": s.get("codec_type"), "Codec": s.get("codec_name"), "Bitrate": s.get("bit_rate"),
                "Width": s.get("width"), "Height": s.get("height"),
                "FPS": _fps(s.get("avg_frame_rate")) if s.get("codec_type") == "video" else None,
                "Channels": s.get("channels"), "SampleRate": s.get("sample_rate"), "Tags": s.get("tags")}
               for s in j.get("streams", [])]
    chapters = [{"Start": c.get("start_time"), "End": c.get("end_time"), "Title": c.get("tags", {}).get("title")}
                for c in j.get("chapters", [])]
    return {"Duration": fmt.get("duration"), "Streams": streams, "Chapters": chapters, "Tags": fmt.get("tags")}


def _exiftool(data):
    d = data[0] if data else {}
    for g in ("SourceFile", "System", "ExifTool"):  # filesystem facts of the staged copy / tool noise
        d.pop(g, None)
    if isinstance(d.get("File"), dict):
        d["File"] = {k: v for k, v in d["File"].items() if k not in FILE_NOISE}
    return d


def _run(cmd: list, tmp: str) -> bytes:
    """stdout of `cmd` in `tmp`, under the limits; raises on timeout, crash or no output."""
    limits = [f"--as={MAX_MEM}", f"--cpu={TIMEOUT}", f"--fsize={MAX_RAW}"]
    prefix = ["prlimit", *limits, "--"] if shutil.which("prlimit") else []
    out = os.path.join(tmp, "stdout.tmp")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp, "LANG": "C.UTF-8",
           "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}  # thread arenas blow RLIMIT_AS at import of numpy
    with open(out, "wb") as o:
        p = subprocess.Popen([*prefix, *cmd], stdout=o, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             cwd=tmp, env=env, start_new_session=True)
        try:
            p.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            raise
    data = open(out, "rb").read()
    if not data:
        raise RuntimeError(f"exit {p.returncode}")
    return data


def _clean(v):
    """YAML-safe and without empty values: bytes/NaN/odd types become strings."""
    if isinstance(v, dict):
        v = {str(k): _clean(x) for k, x in v.items()}
        return {k: x for k, x in v.items() if x not in (None, "", [], {})}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v if x is not None]
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    return v if v is None or isinstance(v, (bool, int, float, str)) else str(v)


def _kinds(ctype: str, name: str) -> list:
    ext = os.path.splitext(name.lower())[1]
    ctype = ctype or mimetypes.guess_type(name)[0] or ""
    kinds = ["exiftool"]
    if ext in RASTER:
        kinds.append("raster")
    if ext in VECTOR:
        kinds.append("vector")
    if ext in OFFICE:
        kinds.append("office")
    if ext == ".pdf" or ctype == "application/pdf":
        kinds.append("pdf")
    if ctype.startswith(("audio/", "video/")) or ext in MEDIA:
        kinds.append("media")
    return kinds


def extract_metadata(path: str, ctype: str, name: str, notes: list, tmp: str | None = None) -> dict:
    """Merged metadata of the file `path` ({} if there is none worth writing); `name` is the job path; `tmp` is a scratch dir."""
    tmp, meta = tmp or os.path.dirname(path), {}
    for kind in _kinds(ctype, name):
        try:
            if kind == "exiftool":
                meta |= _exiftool(json.loads(_run(["exiftool", "-json", "-g1", "-struct", "-api", "LargeFileSupport=1", "--", path], tmp)))
            elif kind == "media":
                probe = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", "-protocol_whitelist", "file", path]
                meta["SpiderHoarder-Media"] = _media(json.loads(_run(probe, tmp)))
            else:
                group = "SpiderHoarder-" + {"raster": "Raster", "vector": "Vector", "office": "Office", "pdf": "PDF"}[kind]
                meta[group] = json.loads(_run([sys.executable, "-I", "-m", __name__, kind, path], tmp))
        except Exception as e:  # noqa: BLE001  never fails the job
            notes.append(f"{name}: metadata {kind} failed ({type(e).__name__})")
    meta = {g: v for g, v in _clean(meta).items() if v}
    return {} if set(meta) <= {"File"} else meta  # file type alone is not metadata


def dump_yaml(meta: dict) -> tuple:
    """(YAML text <= MAX_BYTES, truncated?): halves the longest list of an extension group, then drops whole groups."""
    import yaml

    def dump():
        return yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)

    text, truncated = dump(), False
    while len(text.encode()) > MAX_BYTES:
        truncated = True
        lists = [(g, k) for g, v in meta.items() if g.startswith("SpiderHoarder-") for k, x in v.items()
                 if isinstance(x, list) and len(x) > 1]
        if lists:  # ponytail: longest by count, not bytes
            g, k = max(lists, key=lambda gk: len(meta[gk[0]][gk[1]]))
            meta[g][k] = meta[g][k][:len(meta[g][k]) // 2]
            meta[g]["Truncated"] = True
        else:
            del meta[max(meta, key=lambda g: len(yaml.safe_dump(meta[g])))]
        text = dump()
    return text, truncated


if __name__ == "__main__":
    json.dump(EXTRACTORS[sys.argv[1]](sys.argv[2]), sys.stdout, default=str)
