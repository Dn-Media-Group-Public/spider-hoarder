"""Job store helpers shared by every backend: job ids (`<proj>/<session>/<job>`, project-scoped storage keys), storing files and
having the parser service parse them (parse_client), the {files, text?} job result. Nothing here knows what produced the file."""

import mimetypes
import os
import uuid
from typing import Any

from mcp import types

from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway import ident, parse_client, projects

RID = ident.RID  # this replica: the MCP session id and live-view URLs carry it, jobs and links do not
DEFAULT_TEXT_LENGTH = 20000  # chars; keeps get_file_text results LLM-sized
UPLOAD_MARK = "@upload"  # blobtoken path marking an upload-link token (never a real file path)
INLINE_LIMIT = 50_000
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", 50 * 1024 * 1024))  # files pulled through the gateway (APKs, device output)

FILE_TOOLS_NOTE = ("\n\nThe result is {job_id, files, text?}: `text` is the content if small, else page through it "
                   "with get_file_text or search it with grep_file; get_file_url for a link. `filename` only names "
                   "the file inside the job.")

_UUID_RE = projects.UUID_RE


def _parse_ref(job_id: str) -> tuple[str, str, str]:
    """`<proj>/<session>/<job>`, three uuids: no path component can hold `/` or `..`."""
    parts = str(job_id).split("/")
    if len(parts) != 3 or not all(_UUID_RE.match(p) for p in parts):
        raise ValueError("invalid job_id")
    return tuple(parts)


def _key(owner: str, job_id: str, need: str = "read") -> str:
    """Storage key of a job the caller has at least role `need` in (the gateway's authorisation point for jobs)."""
    proj, session, job = _parse_ref(job_id)
    projects.require(owner, proj, need)
    return storage.job_key(proj, session, job)


def job_key(job_id: str) -> str:
    """Storage key of a job reference with no authorisation: for signed blob / upload tokens, which are the authority."""
    return storage.job_key(*_parse_ref(job_id))


def _new_job(owner: str, sid: str | None, project: str | None = None) -> tuple[str, str]:
    """(job_id, key) of a new job in `project` (else the MCP session's bound project); needs the write role."""
    proj = projects.resolve(sid, project)
    projects.require(owner, proj, "write")
    session, job = projects.session_dir(sid), str(uuid.uuid4())
    return f"{proj}/{session}/{job}", storage.job_key(proj, session, job)


def _check_rel_path(path: str) -> str:
    norm = os.path.normpath(path)
    if norm.startswith("..") or os.path.isabs(norm):
        raise ValueError("invalid path")
    return path


def _store_original(key: str, path: str, src, ctype: str) -> int:
    """Store `src` (bytes or a file object) as original/<path>; parsing is the parser service's job (_parse_job)."""
    if isinstance(src, bytes):
        storage.write_bytes(key, f"original/{path}", src, ctype)
        return len(src)
    return storage.write_stream(key, f"original/{path}", src, ctype)


def _store_tree(key: str, files, prefix: str = "") -> None:
    """_store_original for each (path, fileobj, ctype)."""
    for path, f, ctype in files:
        _store_original(key, prefix + path, f, ctype)


def _parse_job(key: str, base_url: str = "", main: str | None = None) -> dict:
    """Have the parser service parse everything stored in the job (archives are unpacked there), then build the job result;
    `main` is the original whose text is returned inline."""
    return _job_result(key, main, parse_client.parse(key, base_url))


def _store_and_parse(key: str, filename: str, raw: bytes, content_type: str, base_url: str = "") -> dict:
    _store_original(key, filename, raw, content_type)
    return _parse_job(key, base_url, filename)


def _job_result(key: str, main: str | None = None, notes: list | None = None) -> dict:
    """{files, text?, text_path?, notes?} from index.json: the one place that decides whether text is small enough to return inline;
    falls back to the terse outline when the full text is too big. `main` is an original's path, as stored under original/."""
    manifest = storage.get_manifest(key)
    files = {e["path"]: e for e in manifest["files"]}
    result = {"files": manifest["files"]}

    def small(p: str) -> bool:
        return p in files and files[p]["size"] < INLINE_LIMIT

    if main:
        text = next((p for p in (f"parsed/{main}.md", f"parsed/{main}.json", f"original/{main}")
                     if p in files and (p.startswith("parsed/") or files[p].get("text"))), None)
        terse = next((p for p in (f"terse/{main}.md", f"terse/{main}") if p in files), None)
        if text and small(text) or terse and small(terse):
            path = text if text and small(text) else terse
            result["text"], result["text_path"] = storage.read_bytes(key, path).decode("utf-8", errors="replace"), path
    if all_notes := list(dict.fromkeys([*manifest.get("notes", []), *(notes or [])])):
        result["notes"] = all_notes
    return result


def _job_name(filename: Any, default: str) -> str:
    """Agent-supplied filename -> a bare job-internal name (never a path)."""
    name = os.path.basename(str(filename or "").replace("\\", "/"))
    return default if name in ("", ".", "..") else name


def _ctype(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _meta(key: str) -> dict:
    return {k: v for k, v in storage.get_manifest(key).items() if k in ("url", "target")}


def _text(msg: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], isError=error)


def _schema(optional: dict | None = None, **props: str) -> dict:
    """JSON schema of a tool whose arguments are all strings: `props` are required, `optional` are not."""
    return {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in {**props, **(optional or {})}.items()},
            "required": list(props)}
