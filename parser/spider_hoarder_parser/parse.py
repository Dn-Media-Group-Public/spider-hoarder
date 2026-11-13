"""The parse of one job: every file under `original/` of the input tree is run through the one rule below, archives are unpacked
(here, in the sandboxed process: members are parsed too, but never unpacked again) and every artefact goes to a `Sink`.
No storage access, no network: the wrapper moves the bytes (docs/plans/parser-wrapper.md)."""

import mimetypes
import os
import shutil

from spider_hoarder_parser import extract, metadata

MAX_PARSE_BYTES = int(os.environ.get("MAX_PARSE_BYTES") or 50 * 1024 * 1024)  # stored files have no cap; parsing does
MAX_ARCHIVE_BYTES = int(os.environ.get("MAX_ARCHIVE_BYTES") or os.environ.get("MAX_ZIP_BYTES") or 4 * 1024**3)  # uncompressed, per archive
MAX_ARCHIVE_MEMBERS = int(os.environ.get("MAX_ARCHIVE_MEMBERS") or os.environ.get("MAX_ZIP_MEMBERS") or 50_000)


class Sink:
    """Where artefacts go. `rel` is a job-relative path (`parsed/…`, `original/<archive>.d/…`)."""

    def put_bytes(self, rel: str, data: bytes, ctype: str) -> None: ...
    def put_file(self, rel: str, src: str, ctype: str) -> None: ...  # moves `src` (a file in the work dir) into the output
    def mark_text(self, rel: str) -> None: ...  # the manifest entry of `rel` is plain text the gateway may return inline
    def note(self, text: str) -> None: ...


def _ctype(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _terse(sink: Sink, name: str, md: str) -> None:
    if t := extract.terse(md):
        sink.put_bytes(f"terse/{name}", t.encode(), "text/markdown")


def _convert(sink: Sink, path: str, raw: bytes, ctype: str, base_url: str) -> None:
    if path.lower().endswith((".md", ".markdown")):  # already Markdown: no parsed/, terse from the original
        sink.mark_text(f"original/{path}")
        return _terse(sink, path, raw.decode("utf-8", errors="replace"))
    if bkind := extract.binary_kind(ctype, path, raw):
        try:
            text = extract.binary_to_json(raw, bkind, ctype)
        except Exception as e:  # noqa: BLE001  undecodable: noted, never fails the job
            return sink.note(f"{path} not parsed ({type(e).__name__}: {e})")
        return sink.put_bytes(f"parsed/{path}.json", text.encode(), "application/json")
    if path.lower().endswith(".apk"):  # nothing to parse: stored for device_install_apk
        return
    is_html = "html" in ctype or path.lower().endswith((".html", ".htm"))
    if is_html and base_url:
        raw = extract.absolutize(raw, base_url)  # parsed/ and simplified/ get absolute links; original/ is stored untouched
    try:
        text = extract.html_to_markdown(raw) if is_html else extract.file_to_markdown(raw, path)
    except Exception as e:  # noqa: BLE001  one unparseable file never fails the job
        sink.note(f"{path} not parsed ({type(e).__name__}: {e})")
        text = ""
    if not text.strip():
        if not is_html and b"\0" not in raw[:1024]:  # ponytail: NUL sniff; text nobody converted is still returned inline from original/
            sink.mark_text(f"original/{path}")
        return
    sink.put_bytes(f"parsed/{path}.md", text.encode(), "text/markdown")
    if is_html and (simple := extract.html_to_simplified(raw)):
        sink.put_bytes(f"simplified/{path}.md", simple.encode(), "text/markdown")
    _terse(sink, f"{path}.md", text)


def parse_file(sink: Sink, src: str, path: str, ctype: str, base_url: str = "", unzip: bool = True,
               work: str = "/tmp") -> None:
    """Parse `src` (the on-disk original `original/<path>`); an unpacked member goes to the sink as `original/<path>.d/…`."""
    size = os.path.getsize(src)
    meta_notes: list = []
    meta = metadata.extract_metadata(src, ctype, path, meta_notes, tmp=work)  # before the size check: headers, not whole files
    for n in meta_notes:
        sink.note(n)
    if meta:
        text, truncated = metadata.dump_yaml(meta)
        if truncated:
            sink.note(f"{path}: metadata truncated (> METADATA_MAX_BYTES)")
        sink.put_bytes(f"metadata/{path}.yaml", text.encode(), "application/yaml")
    if size > MAX_PARSE_BYTES:
        return sink.note(f"{path} not parsed ({size} bytes > MAX_PARSE_BYTES)")
    if unzip and extract.archive_kind(ctype, path):
        try:
            with open(src, "rb") as af:
                members = extract.unpack_archive(af, path, ctype, MAX_ARCHIVE_MEMBERS, MAX_ARCHIVE_BYTES, [])
                for name, f in members:
                    _member(sink, f"{path}.d/{name}", f, base_url, work)
            return
        except extract.ArchiveLimit as e:
            return sink.note(str(e))
        except extract.BadArchive:
            pass  # named like an archive but is not: parse it as any file
    with open(src, "rb") as f:
        _convert(sink, path, f.read(), ctype, base_url)


def _member(sink: Sink, path: str, f, base_url: str, work: str) -> None:
    """Stage a member in the work dir, parse it (no unpacking), then hand it to the sink as original/<path>."""
    ctype = _ctype(path)
    staged = os.path.join(work, "member" + os.path.splitext(path.lower())[1][:12])
    with open(staged, "wb") as d:
        shutil.copyfileobj(f, d)
    parse_file(sink, staged, path, ctype, base_url, unzip=False, work=work)
    sink.put_file(f"original/{path}", staged, ctype)


def parse_job(sink: Sink, indir: str, job: dict, work: str) -> None:
    """Everything under `<indir>/original/`. job: {base_url, ctypes: {path: content type}}.
    A file inside `<x>.d/` next to an original `<x>` is a member from an earlier run: parsed, not unpacked again."""
    root = os.path.join(indir, "original")
    paths = sorted(os.path.relpath(os.path.join(d, n), root) for d, _, names in os.walk(root) for n in names)
    ctypes = job.get("ctypes") or {}
    for path in paths:
        parts = path.split("/")
        derived = any(parts[i].endswith(".d") and os.path.isfile(os.path.join(root, *parts[:i], parts[i][:-2]))
                      for i in range(len(parts) - 1))
        parse_file(sink, os.path.join(root, path), path, ctypes.get(path) or _ctype(path), job.get("base_url") or "",
                   unzip=not derived, work=work)
