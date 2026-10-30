"""Turn raw bytes into Markdown: markitdown for everything (trafilatura additionally gives a
simplified main-content guess for HTML), archive unpacking (preserving the internal tree) and binary structured data -> JSON."""

import base64
import bz2
import datetime
import gzip
import io
import json
import lzma
import math
import os
import plistlib
import re
import tarfile
import zipfile

import blackboxprotobuf
import bson
import cbor2
import lxml.html
import msgpack
import py7zr
import py7zr.io
import rarfile
import trafilatura
from markdown_it import MarkdownIt
from markitdown import MarkItDown, StreamInfo

_md = MarkItDown()
_mdit = MarkdownIt("commonmark", {"linkify": True}).enable(["table", "linkify"])


def absolutize(html_bytes: bytes, base_url: str) -> bytes:
    """Resolve relative href/src against the page URL (honouring <base href>) so saved Markdown links work."""
    if not base_url or not html_bytes.strip():
        return html_bytes
    try:
        doc = lxml.html.fromstring(html_bytes.decode("utf-8", errors="replace"), base_url=base_url)
        doc.make_links_absolute(base_url)
        return lxml.html.tostring(doc, encoding="utf-8")
    except Exception:  # ponytail: malformed HTML falls back to unresolved links
        return html_bytes


def html_to_markdown(html_bytes: bytes) -> str:
    """Lossless conversion of everything in the HTML (the client picks the container via `target`)."""
    info = StreamInfo(extension=".html", charset="utf-8")  # always UTF-8 from the browser: no detection
    return _md.convert_stream(io.BytesIO(html_bytes), stream_info=info).text_content


def html_to_simplified(html_bytes: bytes) -> str:
    text = trafilatura.extract(html_bytes.decode("utf-8", errors="replace"), output_format="markdown")
    return text or ""


def file_to_markdown(data: bytes, filename: str) -> str:
    result = _md.convert_stream(io.BytesIO(data), file_extension=os.path.splitext(filename)[1])
    return result.text_content


def safe_name(name: str) -> bool:
    """Zip-slip check: no absolute paths, no '..' segments."""
    norm = os.path.normpath(name)
    return not (norm.startswith("..") or norm.startswith("/") or os.path.isabs(norm))


def safe_zip_members(zf: zipfile.ZipFile) -> list[str]:
    return [n for n in zf.namelist() if not n.endswith("/") and safe_name(n)]


class ArchiveLimit(ValueError):
    pass


class BadArchive(ValueError):
    """Not (or not readable as) the archive its name says: the caller parses it as any other file."""


class _Counted:
    """Member reader charging every byte actually read against a shared budget (declared sizes can lie)."""

    def __init__(self, f, left: list, max_bytes: int):
        self.f, self.left, self.max = f, left, max_bytes

    def read(self, n=-1):
        data = self.f.read(n)
        self.left[0] -= len(data)
        if self.left[0] < 0:
            raise ArchiveLimit(f"archive not unpacked: more than {self.max} uncompressed bytes > MAX_ARCHIVE_BYTES")
        return data


_TAR = (".tar", ".tgz", ".tar.gz", ".tbz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz")
_SINGLE = {".gz": "gz", ".bz2": "bz2", ".xz": "xz"}
_BY_CTYPE = {"application/zip": "zip", "application/x-tar": "tar", "application/x-7z-compressed": "7z",
             "application/vnd.rar": "rar", "application/x-rar-compressed": "rar", "application/gzip": "gz",
             "application/x-gzip": "gz", "application/x-bzip2": "bz2", "application/x-xz": "xz"}


def archive_kind(content_type: str, filename: str) -> str | None:
    """zip | tar | 7z | rar | gz | bz2 | xz, by extension (wins) or content type only; None if not an archive."""
    n = filename.lower()
    if n.endswith(_TAR):
        return "tar"
    if n.endswith(".zip"):
        return "zip"
    if n.endswith(".7z"):
        return "7z"
    if n.endswith(".rar"):
        return "rar"
    for ext, kind in _SINGLE.items():
        if n.endswith(ext):
            return kind
    return _BY_CTYPE.get(content_type.split(";")[0].strip().lower())


def _unpack(f, name, kind, max_members, max_bytes, notes):
    left = [max_bytes]
    count = [0]

    def member(n):  # -> True if this regular file with a safe name should be extracted
        if not safe_name(n):
            return False
        count[0] += 1
        if count[0] > max_members:
            raise ArchiveLimit(f"archive not unpacked: {count[0]} members > MAX_ARCHIVE_MEMBERS")
        return True

    if kind == "zip":
        with zipfile.ZipFile(f) as zf:
            if len(zf.namelist()) > max_members:
                raise ArchiveLimit(f"archive not unpacked: {len(zf.namelist())} members > MAX_ARCHIVE_MEMBERS")
            for n in safe_zip_members(zf):
                member(n)
                with zf.open(n) as m:
                    yield n, _Counted(m, left, max_bytes)
    elif kind == "tar":
        with tarfile.open(fileobj=f, mode="r|*") as tf:  # streaming: no seeking, any compression
            for m in tf:
                if m.isdir():
                    continue
                if not m.isfile():
                    kind_ = "symlink" if m.issym() else "hardlink" if m.islnk() else "special file"
                    notes.append(f"{m.name} skipped ({kind_})")
                elif member(m.name):
                    yield m.name, _Counted(tf.extractfile(m), left, max_bytes)
    elif kind == "7z":
        with py7zr.SevenZipFile(f) as sz:
            infos = [i for i in sz.list() if not i.is_directory]
            for i in infos:
                if i.is_symlink:
                    notes.append(f"{i.filename} skipped (symlink)")
            infos = [i for i in infos if not i.is_symlink and member(i.filename)]
            # ponytail: extracted into memory a batch at a time, sized by the declared sizes (a lying header can
            # exceed _BATCH); a streaming WriterFactory would remove the ceiling
            while infos:
                batch, size = [], 0
                while infos and (not batch or size + infos[0].uncompressed <= _7Z_BATCH):
                    batch.append(infos.pop(0))
                    size += batch[-1].uncompressed
                sz.reset()
                fac = py7zr.io.BytesIOFactory(_7Z_BATCH)
                sz.extract(targets=[i.filename for i in batch], factory=fac)
                for i in batch:
                    out = fac.products[i.filename]
                    out.seek(0)
                    yield i.filename, _Counted(out, left, max_bytes)
    elif kind == "rar":
        with rarfile.RarFile(f) as rf:
            for i in rf.infolist():
                if i.is_dir():
                    continue
                if i.is_symlink():
                    notes.append(f"{i.filename} skipped (symlink)")
                elif member(i.filename):
                    with rf.open(i) as m:
                        yield i.filename, _Counted(m, left, max_bytes)
    else:  # gz | bz2 | xz: one member, named without the suffix
        opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[kind]
        stem = os.path.basename(name)
        stem = stem[:stem.rfind(".")] if "." in stem else stem
        with opener(f) as m:
            m.peek(1)  # validates the header now, so a mislabelled file is BadArchive and not a half-stored one
            yield stem or "data", _Counted(m, left, max_bytes)


_7Z_BATCH = 64 * 1024**2
_BAD = (zipfile.BadZipFile, tarfile.TarError, py7zr.Bad7zFile, rarfile.Error, OSError, EOFError, lzma.LZMAError)


def unpack_archive(f, name: str, content_type: str, max_members: int, max_bytes: int, notes: list | None = None):
    """Yields (relative_path, file) for every safe regular member of the archive `f` (seekable, except tar);
    each file must be consumed before the next one is asked for. Symlinks/hardlinks/devices are skipped and
    appended to `notes`. Raises ArchiveLimit over the member count or uncompressed bytes, BadArchive if unreadable."""
    notes = [] if notes is None else notes
    try:
        yield from _unpack(f, name, archive_kind(content_type, name), max_members, max_bytes, notes)
    except _BAD as e:
        raise BadArchive(str(e)) from e


def grep_text(text: str, pattern: str, context: int = 2, max_matches: int = 50, width: int = 300, limit: int = 50_000) -> str:
    """Matching lines with `context` lines around them, as `line N (offset O)` blocks; overlapping blocks merge.
    `pattern` is a Python regex, or /pattern/i. Lines are trimmed to ~`width` chars around the match."""
    m = re.fullmatch(r"/(.*)/([a-z]*)", pattern, re.S)
    try:
        rx = re.compile(m.group(1) if m else pattern, re.I if m and "i" in m.group(2) else 0)
    except re.error as e:
        raise ValueError(f"invalid regex: {e}")
    lines = text.split("\n")
    offsets, o = [], 0
    for l in lines:
        offsets.append(o)
        o += len(l) + 1
    spans = {i: s.span() for i, l in enumerate(lines) if (s := rx.search(l))}
    if not spans:
        return f"No matches for {pattern}."
    shown = list(spans)[:max_matches]
    context = max(context, 0)
    blocks: list[list[int]] = []
    for h in shown:
        lo, hi = max(0, h - context), min(len(lines) - 1, h + context)
        if blocks and lo <= blocks[-1][1] + 1:
            blocks[-1][1] = hi
        else:
            blocks.append([lo, hi])

    def trim(i: int) -> str:
        l = lines[i]
        s, e = spans.get(i, (0, 0))
        if len(l) <= width:
            return l
        lo = min(max(0, (s + e) // 2 - width // 2), len(l) - width)
        return ("..." if lo else "") + l[lo:lo + width] + ("..." if lo + width < len(l) else "")

    out, size = [], 0
    for lo, hi in blocks:
        block = "\n".join(f"{i + 1}{':' if i in spans else '-'} {trim(i)}" for i in range(lo, hi + 1))
        block = f"-- line {lo + 1}, offset {offsets[lo]} --\n{block}"
        if size + len(block) > limit:
            break
        out.append(block)
        size += len(block)
    cut = len(shown) < len(spans) or len(out) < len(blocks)
    note = f"\n\n[showing {sum(h <= b[1] for b in blocks[:len(out)] for h in shown if h >= b[0])} of {len(spans)} matches]" if cut else ""
    return "\n\n".join(out) + note


def _pieces(children) -> list[tuple[bool, str]]:
    """Inline tokens -> [(is_link_or_image, text)]; styling flattened, links/images as Markdown."""
    out, i = [], 0
    while i < len(children):
        t = children[i]
        if t.type in ("text", "code_inline"):
            out.append((False, t.content))
        elif t.type in ("softbreak", "hardbreak"):
            out.append((False, " "))
        elif t.type == "image":
            alt = "".join(s for _, s in _pieces(t.children or []))
            out.append((True, f"![{alt}]({t.attrGet('src')})"))
        elif t.type == "link_open":
            j = i + 1
            while children[j].type != "link_close":  # links cannot nest
                j += 1
            text = "".join(s for _, s in _pieces(children[i + 1:j]))
            out.append((True, f"<{text}>" if t.markup in ("autolink", "linkify") else f"[{text}]({t.attrGet('href')})"))
            i = j
        i += 1
    return out


def terse(markdown: str) -> str:
    """Outline of a Markdown document: headings, plus one `* ` bullet per link/image outside headings."""
    lines, level = [], 0
    for tok in _mdit.parse(markdown):
        if tok.type == "heading_open":
            level = int(tok.tag[1:])
        elif tok.type == "heading_close":
            level = 0
        elif tok.type == "inline":
            pieces = _pieces(tok.children or [])
            if level:
                lines.append("#" * level + " " + "".join(s for _, s in pieces))
            else:
                lines += [f"* {s}" for is_link, s in pieces if is_link]
    return "\n".join(lines)


_BINARY = {"msgpack": (".msgpack", ".mp"), "cbor": (".cbor",), "bson": (".bson",), "plist": (".plist",), "protobuf": (".pb",)}
_BINARY_CTYPE = {"application/msgpack": "msgpack", "application/x-msgpack": "msgpack", "application/cbor": "cbor",
                 "application/bson": "bson", "application/x-plist": "plist",
                 "application/x-protobuf": "protobuf", "application/protobuf": "protobuf"}


def binary_kind(content_type: str, filename: str, data: bytes = b"") -> str | None:
    """msgpack | cbor | bson | plist | protobuf, by extension or content type; only plist is also sniffed (bplist00)."""
    n, ct = filename.lower(), content_type.split(";")[0].strip().lower()
    for kind, exts in _BINARY.items():
        if n.endswith(exts):
            return kind
    if ct.startswith("application/grpc-web"):
        return "protobuf"
    if data.startswith(b"bplist00"):
        return "plist"
    return _BINARY_CTYPE.get(ct)


def _jsonable(o):
    """Everything JSON cannot say: bytes -> {"$base64"}, datetimes -> ISO 8601, NaN/Inf -> string, sets -> lists,
    non-string keys -> str(), anything unknown -> str()."""
    if o is None or isinstance(o, (bool, int, str)):
        return o
    if isinstance(o, float):
        return o if math.isfinite(o) else str(o)
    if isinstance(o, (bytes, bytearray, memoryview)):
        return {"$base64": base64.b64encode(bytes(o)).decode()}
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    if isinstance(o, dict):
        return {k if isinstance(k, str) else str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in o]
    return str(o)


def _grpc_web(data: bytes, text: bool) -> list:
    if text:  # application/grpc-web-text: base64 of the same frames
        data = base64.b64decode(data)
    out, i = [], 0
    while i < len(data):
        if i + 5 > len(data):
            raise ValueError("truncated gRPC-web frame header")
        flag, n = data[i], int.from_bytes(data[i + 1:i + 5], "big")
        body = data[i + 5:i + 5 + n]
        if len(body) < n:
            raise ValueError("truncated gRPC-web frame")
        if flag & 0x80:  # trailers: HTTP/1-style header text
            out.append({"trailers": body.decode("utf-8", errors="replace")})
        else:
            msg, typedef = blackboxprotobuf.decode_message(body)
            out.append({"message": msg, "typedef": typedef})
        i += 5 + n
    return out


def _bson_docs(data: bytes):
    docs, i = [], 0  # a .bson file may be a concatenation of documents (mongodump)
    while i < len(data):
        n = int.from_bytes(data[i:i + 4], "little")
        if n < 5 or i + n > len(data):
            raise ValueError("bad BSON document length")
        docs.append(bson.loads(data[i:i + n]))  # the PyPI `bson` package (not pymongo's, which has no loads)
        i += n
    return docs[0] if len(docs) == 1 else docs


def binary_to_json(data: bytes, kind: str, content_type: str = "") -> str:
    """Decode a binary structured format to pretty-printed JSON. `content_type` only selects gRPC-web framing."""
    if kind == "msgpack":
        v = msgpack.unpackb(data, raw=False, strict_map_key=False)
    elif kind == "cbor":
        v = cbor2.loads(data)
    elif kind == "bson":
        v = _bson_docs(data)
    elif kind == "plist":
        v = plistlib.loads(data)
    elif kind == "protobuf":
        if content_type.lower().startswith("application/grpc-web"):
            v = _grpc_web(data, "text" in content_type.lower())
        else:
            msg, typedef = blackboxprotobuf.decode_message(data)  # schema-less: types are guesses
            v = {"message": msg, "typedef": typedef}
    else:
        raise ValueError(f"unknown binary kind {kind}")
    return json.dumps(_jsonable(v), indent=2, ensure_ascii=False)
