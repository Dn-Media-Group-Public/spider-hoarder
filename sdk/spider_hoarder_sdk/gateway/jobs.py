"""Job store helpers shared by every backend: job ids (`<proj>/<session>/<job>`, project-scoped storage keys), storing files and
having the parser service parse them (parse_client), the {files, text?} job result. Nothing here knows what produced the file."""

import json
import mimetypes
import os
import re
import uuid
from typing import Any

from mcp import types

from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway import ident, parse_client, projects
from spider_hoarder_sdk.gateway import policy as lv

RID = ident.RID  # this replica: the MCP session id and live-view URLs carry it, jobs and links do not
DEFAULT_TEXT_LENGTH = 20000  # chars; keeps get_file_text results LLM-sized
UPLOAD_MARK = "@upload"  # blobtoken path marking an upload-link token (never a real file path)
INLINE_LIMIT = 50_000
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", 50 * 1024 * 1024))  # files pulled through the gateway (APKs, device output)

FILE_TOOLS_NOTE = ("\n\nThe result is {job_id, files, text?}: `files` maps each stored file (its path is original/<name>) to its size, its `outputs` "
                   "(parsed/, terse/ ... paths, usable as `path`) and, for archives, its unpacked `files`; `text` is the content if small, else page through it "
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
    key = storage.job_key(proj, session, job)
    job_policy[key] = effective(proj, sid)  # ponytail: dropped by _parse_job; a job that is never parsed leaks one small dict until restart
    return f"{proj}/{session}/{job}", key


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


def _parse_job(key: str, base_url: str = "", main: str | None = None, policy: dict | None = None) -> dict:
    """Have the parser service parse everything stored in the job (archives are unpacked there), then build the job result;
    `main` is the original whose text is returned inline. `policy` is the resolved policy (an upload link's: its own chains); by default
    the one _new_job resolved for the job's session, else admin + project only (what is still ASK is off)."""
    policy = policy or job_policy.pop(key, None) or effective(key.split("/")[1])
    return _job_result(key, main, parse_client.parse(key, base_url, policy))


def _store_and_parse(key: str, filename: str, raw: bytes, content_type: str, base_url: str = "") -> dict:
    _store_original(key, filename, raw, content_type)
    return _parse_job(key, base_url, filename)


# ---- policy levels (docs/implemented-plans/policy-levels.md): the pure rules are in policy.py; this reads this gateway's admin env, the
# project's access.json and the session's choices
job_policy: dict[str, dict] = {}  # job key -> the resolved policy of the session that created it, until its parse


def _admin() -> tuple[dict, dict]:
    names = lv.profile_names(os.environ)
    return lv.admin_level(os.environ, names), names


def _backend() -> str:
    return os.environ.get("BACKEND", "")


def effective(proj: str | None, sid: str | None = None) -> dict:
    """The resolved policy: admin env, the project's level for this backend, and the session's choices (none: still ASK means unresolved)."""
    admin, names = _admin()
    return lv.resolve([("admin", admin), ("project", projects.policy_of(proj, _backend()) if proj else {}),
                       ("session", projects.session_policy.get(sid or "", {}))], names)


def check_project_policy(policy) -> dict:
    """Validate a project's `{backend: {setting: value | ASK}}` for storing. This backend's section is checked against this gateway's admin level;
    a gateway only sees its own admin env, so another backend's section is checked for shape only (its stale values are dropped when it resolves)."""
    if not isinstance(policy, dict):
        raise ValueError("policy is an object {backend: {setting: value}}")
    admin, names = _admin()
    out = {}
    for backend, level in policy.items():
        if not isinstance(backend, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", backend):
            raise ValueError("policy: invalid backend name")
        if backend == _backend():
            out[backend] = lv.check_level(level, lv.resolve([("admin", admin)], names), names)
        else:
            out[backend] = lv.check_level(level, lv.resolve([("admin", {s: lv.ASK for s in lv.SETTINGS})]), None)
    return out


def bounds(proj: str | None) -> dict:
    """What a project policy (or, without a project, the admin level) lets the level below choose, per setting and source: for get_project."""
    admin, names = _admin()
    r = lv.resolve([("admin", admin), ("project", projects.policy_of(proj, _backend()) if proj else {})], names)
    return {s: {"bound": "any" if x["bound"] is None else x["bound"], "source": x["bound_source"]} for s, x in r.items()}


def check_session_policy(owner: str, project: str | None, args: dict, settings, defaults: dict) -> tuple[dict, dict]:
    """(session level, resolved policy) for open_session's arguments: each validated against the bound above it (rejected, never clipped); refuses
    while a setting that applies is still ASK. Needs the write role in `project` (so this runs before anything is ended)."""
    admin, names = _admin()
    level = {}
    if project:
        projects.require(owner, project, "write")
    above = lv.resolve([("admin", admin), ("project", projects.policy_of(project, _backend()) if project else {})], names, settings)
    for s in settings:
        if args.get(s) is not None:
            if args[s] == lv.ASK:
                raise ValueError(f"{s}: ASK is not a choice")
            level[s] = lv.check(s, args[s], lv.bound_of(above, s), names)
    resolved = lv.with_defaults(lv.resolve([("admin", admin), ("project", projects.policy_of(project, _backend()) if project else {}), ("session", level)],
                                           names, settings), defaults)
    if ask := lv.unresolved(resolved):
        raise ValueError("open_session needs a choice for: " + "; ".join(f"{s} (any of {b if b is not None else 'the available profiles'})"
                                                                           for s, b in ask.items()))
    return level, resolved


def policy_view(sid: str | None, settings, defaults: dict) -> dict:
    """get_session's / open_session's `policy` and `ask`: the effective policy of the MCP session, before or after open_session."""
    admin, names = _admin()
    proj = projects.bound.get(sid or "")
    r = lv.with_defaults(lv.resolve([("admin", admin), ("project", projects.policy_of(proj, _backend()) if proj else {}),
                                     ("session", projects.session_policy.get(sid or "", {}))], names, settings), defaults)
    return lv.view(r)


def _entry_notes(files: dict):
    for e in files.values():
        if e.get("notes"):
            yield from e["notes"].split("\n")
        yield from _entry_notes(e.get("files") or {})


def _job_result(key: str, main: str | None = None, notes: list | None = None) -> dict:
    """{files, text?, text_path?, notes?} from the parser's index.json (files: per input file, relative to original/: size, outputs by job-relative
    path, archive members): the one place that decides whether text is small enough to return inline; falls back to the terse outline when the
    full text is too big. `main` is an input's path, as stored under original/."""
    index = storage.get_index(key)
    files = index["files"]
    result = {"files": files}
    if main and (entry := files.get(main)):
        outputs = entry.get("outputs") or {}
        # parsed text, else the input itself when the parser marked it as text
        text = next((p for p in (f"parsed/{main}.md", f"parsed/{main}.json") if p in outputs), None)
        sizes = {p: o["size"] for p, o in outputs.items()}
        if text is None and (entry.get("meta") or {}).get("text"):
            text, sizes[f"original/{main}"] = f"original/{main}", entry["size"]
        terse = next((p for p in (f"terse/{main}.md", f"terse/{main}") if p in outputs), None)
        small = lambda p: p is not None and sizes[p] < INLINE_LIMIT  # noqa: E731
        if small(text) or small(terse):
            path = text if small(text) else terse
            result["text"], result["text_path"] = storage.read_bytes(key, path).decode("utf-8", errors="replace"), path
    if all_notes := [n for n in dict.fromkeys([*(index.get("notes") or "").split("\n"), *_entry_notes(files), *(notes or [])]) if n]:
        result["notes"] = all_notes
    return result


def _job_name(filename: Any, default: str) -> str:
    """Agent-supplied filename -> a bare job-internal name (never a path)."""
    name = os.path.basename(str(filename or "").replace("\\", "/"))
    return default if name in ("", ".", "..") else name


def _ctype(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _meta(key: str) -> dict:
    return {k: v for k, v in storage.get_meta(key).items() if k in ("url", "target")}


def _text(msg: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], isError=error)


def _schema(optional: dict | None = None, **props: str) -> dict:
    """JSON schema of a tool whose arguments are strings: `props` are required, `optional` are not. A value that is a dict is the property's own schema."""
    return {"type": "object", "properties": {k: v if isinstance(v, dict) else {"type": "string", "description": v} for k, v in {**props, **(optional or {})}.items()},
            "required": list(props)}


def sum_usage(calls: list[dict]) -> list[dict]:
    """One row per (feature, profile, model, outcome, unit), input / output summed; units are never mixed."""
    rows: dict = {}
    for c in calls:
        k = tuple(c.get(f) for f in ("feature", "profile", "model", "outcome", "unit"))
        r = rows.setdefault(k, {"calls": 0, "input": 0, "output": 0})
        r["calls"] += 1
        r["input"] += c.get("input") or 0
        r["output"] += c.get("output") or 0
    return [{**dict(zip(("feature", "profile", "model", "outcome", "unit"), k)), **r} for k, r in sorted(rows.items(), key=lambda i: tuple(map(str, i[0])))]


def _status(key: str) -> dict:
    raw = storage.read_object(f"{key}/status.json")
    return json.loads(raw) if raw else {}


def usage(owner: str, project: str, job_id: str | None = None) -> dict:
    """What the sidecars reported (status.json `usage`) for one job, or summed over every job of the project; needs the read role."""
    if job_id:
        if _parse_ref(job_id)[0] != project:
            raise projects.NotFound()
        st = _status(_key(owner, job_id, "read"))
        u = st.get("usage") or {}
        calls = u.get("calls") or []
        return {"job_id": job_id, "status": st.get("status"), "calls": calls, "truncated": bool(u.get("truncated")), **({"error": u["error"]} if u.get("error") else {}),
                "totals": sum_usage(calls)}
    if not projects.UUID_RE.match(project or ""):
        raise projects.NotFound()
    projects.require(owner, project, "read")
    calls, jobs, with_usage, truncated, errors = [], 0, 0, 0, []
    base = f"projects/{project}/data"
    for session in storage.list_names(base):
        for job in storage.list_names(f"{base}/{session}"):
            st = _status(f"{base}/{session}/{job}")
            if not st:
                continue
            jobs += 1
            u = st.get("usage") or {}
            if u.get("error"):
                errors.append({"job_id": f"{project}/{session}/{job}", "error": u["error"]})
            if u.get("calls") is not None and not u.get("error"):
                with_usage += 1
                truncated += bool(u.get("truncated"))
            calls += u.get("calls") or []
    return {"project": project, "jobs": jobs, "jobs_with_usage": with_usage, "truncated_jobs": truncated, "errors": errors, "totals": sum_usage(calls)}  # ponytail: one read per job; a tally in index.json if projects get large
