"""Job store helpers shared by every backend: job ids (`<rid>.<uuid>`, owner-scoped storage keys), storing a file
and parsing it into Markdown, the {files, text?} job result. Nothing here knows what produced the file."""

import mimetypes
import os
import re
import uuid
import json
from typing import Any

from mcp import types

from spider_hoarder_sdk.content import extract
from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway import ident

RID = ident.RID
DEFAULT_TEXT_LENGTH = 20000  # chars; keeps get_file_text results LLM-sized
UPLOAD_MARK = "@upload"  # blobtoken path marking an upload-link token (never a real file path)
INLINE_LIMIT = 50_000
MAX_PARSE_BYTES = int(os.environ.get("MAX_PARSE_BYTES", 50 * 1024 * 1024))  # stored files have no cap; parsing does
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", 50 * 1024 * 1024))  # files pulled through the gateway (APKs, device output)
MAX_ARCHIVE_BYTES = int(os.environ.get("MAX_ARCHIVE_BYTES") or os.environ.get("MAX_ZIP_BYTES") or 4 * 1024**3)  # uncompressed, per archive
MAX_ARCHIVE_MEMBERS = int(os.environ.get("MAX_ARCHIVE_MEMBERS") or os.environ.get("MAX_ZIP_MEMBERS") or 50_000)

FILE_TOOLS_NOTE = ("\n\nThe result is {job_id, files, text?}: `text` is the content if small, else page through it "
                   "with get_file_text or search it with grep_file; get_file_url for a link. `filename` only names "
                   "the file inside the job.")

_RID_PREFIX = re.compile(r"^([0-9]+-[0-9]+-[0-9]+-[0-9]+)\.")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _new_job_id() -> str:
    return f"{RID}.{uuid.uuid4()}"


def _key(owner: str, job_id: str) -> str:
    """Storage key: jobs live under <owner>/<uuid>, so callers only see their own. The job_id
    is `<rid>.<uuid>`; storage is per replica, so another replica's job is not reachable."""
    rid, _, job_id = job_id.rpartition(".")
    if rid != RID and _RID_PREFIX.match(f"{rid}."):
        raise ValueError("job lives on another replica and is not reachable from this session")
    if rid != RID or not _UUID_RE.match(job_id):
        raise ValueError("invalid job_id")
    safe = re.sub(r"[^a-z0-9._@-]", "_", owner)
    if "@" not in safe:  # also keeps '..' from being an owner dir
        raise ValueError("invalid owner")
    return f"{safe}/{job_id}"


def _check_rel_path(path: str) -> str:
    norm = os.path.normpath(path)
    if norm.startswith("..") or os.path.isabs(norm):
        raise ValueError("invalid path")
    return path


def _parse_one(key: str, path: str, raw: bytes, ctype: str, base_url: str, notes: list) -> tuple | None:
    """Parse one stored file: (text, text_path, terse, terse_path) for _job_result, or None if nothing was made."""
    if path.lower().endswith((".md", ".markdown")):  # already Markdown: no parsed/, terse from the original
        text = raw.decode("utf-8", errors="replace")
        return text, f"original/{path}", _write_terse(key, path, text), f"terse/{path}"
    if bkind := extract.binary_kind(ctype, path, raw):
        try:
            text = extract.binary_to_json(raw, bkind, ctype)
        except Exception as e:  # noqa: BLE001  undecodable: noted, never fails the job
            notes.append(f"{path} not parsed ({type(e).__name__}: {e})")
            return None
        storage.write_bytes(key, f"parsed/{path}.json", text.encode(), "application/json")
        return text, f"parsed/{path}.json", "", ""
    if path.lower().endswith(".apk"):  # nothing to parse: stored for device_install_apk
        return None
    is_html = "html" in ctype or path.lower().endswith((".html", ".htm"))
    if is_html and base_url:
        raw = extract.absolutize(raw, base_url)  # parsed/ and simplified/ get absolute links; original/ is stored untouched
    try:
        text = extract.html_to_markdown(raw) if is_html else extract.file_to_markdown(raw, path)
    except Exception as e:  # noqa: BLE001  one unparseable file never fails the job
        notes.append(f"{path} not parsed ({type(e).__name__}: {e})")
        return None
    if not is_html and not text.strip():
        return None
    storage.write_bytes(key, f"parsed/{path}.md", text.encode(), "text/markdown")
    if is_html and (simple := extract.html_to_simplified(raw)):
        storage.write_bytes(key, f"simplified/{path}.md", simple.encode(), "text/markdown")
    return text, f"parsed/{path}.md", _write_terse(key, f"{path}.md", text), f"terse/{path}.md"


def _store_one(key: str, path: str, src, ctype: str, base_url: str = "", notes: list | None = None,
               unzip: bool = True) -> tuple | None:
    """Store `src` (bytes or a file object) as original/<path> and parse it by the one rule for every file;
    an archive is unpacked to <path>.d/ (once: members that are archives are only stored)."""
    notes = [] if notes is None else notes
    if isinstance(src, bytes):
        storage.write_bytes(key, f"original/{path}", src, ctype)
        size = len(src)
    else:
        size = storage.write_stream(key, f"original/{path}", src, ctype)
    if size > MAX_PARSE_BYTES:
        notes.append(f"{path} not parsed ({size} bytes > MAX_PARSE_BYTES)")
        return None
    if unzip and extract.archive_kind(ctype, path):
        try:
            with storage.open_read(key, f"original/{path}") as af:
                members = ((n, f, mimetypes.guess_type(n)[0] or "application/octet-stream")
                           for n, f in extract.unpack_archive(af, path, ctype, MAX_ARCHIVE_MEMBERS, MAX_ARCHIVE_BYTES, notes))
                _store_tree(key, members, notes=notes, prefix=f"{path}.d/", unzip=False)
            return None
        except extract.ArchiveLimit as e:
            notes.append(str(e))
            return None
        except extract.BadArchive:
            pass  # named like an archive but is not: parse it as any file
    raw = src if isinstance(src, bytes) else storage.read_bytes(key, f"original/{path}")
    return _parse_one(key, path, raw, ctype, base_url, notes)


def _store_tree(key: str, files, base_url: str = "", notes: list | None = None, prefix: str = "",
                unzip: bool = True) -> dict:
    """_store_one for each (path, fileobj, ctype); returns {path: parse result}."""
    notes = [] if notes is None else notes
    return {path: _store_one(key, prefix + path, f, ctype, base_url, notes, unzip) for path, f, ctype in files}


def _store_and_parse(key: str, filename: str, raw: bytes, content_type: str, base_url: str = "") -> dict:
    notes = []
    info = _store_one(key, filename, raw, content_type, base_url, notes)
    return _with_notes(_job_result(key, *(info or ())), notes)


def _with_notes(result: dict, notes: list) -> dict:
    return {**result, "notes": notes} if notes else result


def _write_terse(key: str, name: str, md: str) -> str:
    """Store terse/<name> if the outline is non-empty; returns the outline text."""
    t = extract.terse(md)
    if t:
        storage.write_bytes(key, f"terse/{name}", t.encode(), "text/markdown")
    return t


def _job_result(key: str, text: str | None = None, path: str | None = None,
                terse: str = "", terse_path: str = "") -> dict:
    """{files, text?, text_path?}: the one place that decides whether text is small enough to return inline;
    falls back to the terse outline when the full text is too big."""
    result = {"files": storage.get_manifest(key)["files"]}
    if text is not None and len(text) < INLINE_LIMIT:
        result["text"], result["text_path"] = text, path
    elif terse and len(terse) < INLINE_LIMIT:
        result["text"], result["text_path"] = terse, terse_path
    return result


def _job_name(filename: Any, default: str) -> str:
    """Agent-supplied filename -> a bare job-internal name (never a path)."""
    name = os.path.basename(str(filename or "").replace("\\", "/"))
    return default if name in ("", ".", "..") else name


def _store_file(key: str, name: str, raw: bytes) -> dict:
    storage.write_bytes(key, f"original/{name}", raw, _ctype(name))
    return _describe_file(key, name, raw)


def _ctype(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _describe_file(key: str, name: str, raw: bytes) -> dict:
    """Job result for a tool's stored output file: PDFs are parsed, text is returned as is."""
    if name.lower().endswith(".pdf"):
        text = extract.file_to_markdown(raw, name)
        storage.write_bytes(key, f"parsed/{name}.md", text.encode(), "text/markdown")
        return _job_result(key, text, f"parsed/{name}.md", _write_terse(key, f"{name}.md", text), f"terse/{name}.md")
    if b"\0" not in raw[:1024]:  # ponytail: NUL sniff for text vs binary
        text = raw.decode("utf-8", errors="replace")
        if name.lower().endswith((".md", ".markdown")):
            return _job_result(key, text, f"original/{name}", _write_terse(key, name, text), f"terse/{name}")
        return _job_result(key, text, f"original/{name}")
    return _job_result(key)


def _meta(key: str) -> dict:
    return {k: v for k, v in storage.get_manifest(key).items() if k in ("url", "target")}


def _text(msg: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], isError=error)


def _schema(optional: dict | None = None, **props: str) -> dict:
    """JSON schema of a tool whose arguments are all strings: `props` are required, `optional` are not."""
    return {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in {**props, **(optional or {})}.items()},
            "required": list(props)}
