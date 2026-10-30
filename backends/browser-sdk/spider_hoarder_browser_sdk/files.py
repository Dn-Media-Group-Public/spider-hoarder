"""Browser session files: the session's out/ and in/ directories as the gateway reads and writes them without trusting
anything the session controls, and the request-body staging for browser_network_request_make."""

import os
import re
import shutil
import stat
import uuid
from typing import Any

from spider_hoarder_browser_sdk import ports
from spider_hoarder_browser_sdk.mediapolicy import ROUTED_UPLOAD_CAP
from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway.jobs import (MAX_PARSE_BYTES, _check_rel_path, _ctype, _describe_file, _job_name, _job_result, _key,
                                             _store_tree, _with_notes)

# Request bodies go to Chromium as one base64 CDP message, which it refuses above 100 MiB (= 75 MiB raw) by killing the whole browser
# context; measured: 74 MiB works, 76 does not. The default leaves room for the headers. Raising it past ~72 MiB is unsafe.
MAX_REQUEST_BODY = int(os.environ.get("MAX_REQUEST_BODY", 64 * 1024 * 1024))
# browser_network_request_make type=form|xhr: the file parts of one call (streamed by the browser from disk, never through CDP)
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 500 * 1024 * 1024))
MAX_REQUEST_TIMEOUT_MS = int(os.environ.get("MAX_REQUEST_TIMEOUT_MS", 300_000))  # browser_network_request_make
REQUEST_TYPES = ("navigation", "form", "xhr")


_NET_FILE = re.compile(r"headers\.json|body\.[A-Za-z0-9]{1,10}")  # the only names read from a network request dir


def _out_files(uid: int, name: str, tree: bool = False):
    """Yield (relative_path, file) for <home>/out/<name> from a session without trusting anything it controls:
    every path component is opened O_NOFOLLOW relative to the previous fd (a session owns out/ and could swap
    it for a symlink), O_NONBLOCK so a planted FIFO cannot hang us, regular files only. A file yields ("", f).
    With `tree`, name is a network request dir: only request|response/<_NET_FILE> are read, the rest ignored.
    Each file is closed when the next one is asked for."""
    fds = []

    def open_(n, flags, dir_fd):
        fds.append(os.open(n, flags | os.O_NOFOLLOW, dir_fd=dir_fd))
        return fds[-1]

    def regular(n, dir_fd):
        try:
            fd = open_(n, os.O_RDONLY | os.O_NONBLOCK, dir_fd)
        except OSError:  # symlink or vanished: skipped like any other junk
            return None
        return fd if stat.S_ISREG(os.fstat(fd).st_mode) else None

    try:  # O_PATH: the session home is 0o710 (traverse only), which O_RDONLY cannot open
        fds.append(os.open(ports.home(uid), os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW))
        out = open_("out", os.O_RDONLY | os.O_DIRECTORY, fds[-1])
        if not tree:
            fd = open_(name, os.O_RDONLY | os.O_NONBLOCK, out)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("output is not a regular file")
            with os.fdopen(os.dup(fd), "rb") as f:
                yield "", f
            return
        root = open_(name, os.O_RDONLY | os.O_DIRECTORY, out)
        for sub in ("request", "response"):
            try:
                d = open_(sub, os.O_RDONLY | os.O_DIRECTORY, root)
            except OSError:
                continue
            for n in sorted(os.listdir(d)):
                if _NET_FILE.fullmatch(n) and (fd := regular(n, d)) is not None:
                    with os.fdopen(os.dup(fd), "rb") as f:
                        yield f"{sub}/{n}", f
    finally:
        for fd in fds:
            os.close(fd)


def _stage(uid: int, key: str, path: str, filename: str) -> str:
    """Leave a job file for the session as <home>/in/<random>/<filename> (the mirror image of reading out/: every component
    O_NOFOLLOW from the home fd, the file O_EXCL so a planted name or symlink is never written through). One directory per file,
    so equal filenames never collide and the name stays exactly the one the browser will upload. Returns "<random>/<filename>"."""
    d = uuid.uuid4().hex
    fds = [os.open(ports.home(uid), os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        fds.append(os.open("in", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[0]))
        os.mkdir(d, 0o700, dir_fd=fds[1])
        try:
            fds.append(os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[1]))
            os.fchmod(fds[2], 0o777)  # the session user is neither owner nor in our group: it must read and delete; in/ keeps others out
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fds[2])
            with os.fdopen(fd, "wb") as f, storage.open_read(key, path) as src:
                os.fchmod(f.fileno(), 0o644)
                shutil.copyfileobj(src, f, 1 << 20)
        except BaseException:
            _drop_in(uid, d)
            raise
        return f"{d}/{filename}"
    finally:
        for fd in fds:
            os.close(fd)


def _drop_in(uid: int, name: str) -> None:
    """Backstop for staged files the toolserver never got to delete; name is "<random>" or "<random>/<filename>"."""
    try:
        fd = os.open(ports.home(uid), os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            d = os.open("in", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                shutil.rmtree(name.split("/")[0], dir_fd=d)
            finally:
                os.close(d)
        finally:
            os.close(fd)
    except OSError:
        pass


_REF_KEYS = {"job_id", "path", "filename", "content_type"}


def _request_body(owner: str, uid: int, arguments: dict, routed: bool = False) -> tuple[dict, list[str]]:
    """browser_network_request_make: validate type/body, resolve every {job_id, path} (owner-scoped) and stage it in the session's
    in/. Returns the arguments for the toolserver (job references replaced by staged names; the hidden `body_file` is ours alone)
    and the staged names. Everything is checked, sizes included, before the first byte is copied. `routed`: the session's
    layer A pauses every request, which kills Chromium on big uploads."""
    for k in ("body_job_id", "body_path"):
        if k in arguments:
            raise ValueError("body_job_id/body_path are gone: use body={job_id, path}")
    a = {k: v for k, v in arguments.items() if k != "body_file"}
    typ, body = a.get("type") or "navigation", a.get("body")
    if typ not in REQUEST_TYPES:
        raise ValueError(f"type must be one of {', '.join(REQUEST_TYPES)}")
    if a.get("timeout_ms") is not None:
        a["timeout_ms"] = min(int(a["timeout_ms"]), MAX_REQUEST_TIMEOUT_MS)
    refs: list[dict] = []  # every file reference, in order
    text = 0  # bytes of text (raw body and field values)
    if isinstance(body, str):
        text, kind = len(body.encode()), "text"
    elif isinstance(body, dict) and body.keys() & {"job_id", "path"}:
        if body.keys() != {"job_id", "path"}:
            raise ValueError("a job file body is exactly {job_id, path}; file parts go in a fields object")
        kind, refs = "file", [body]
    elif isinstance(body, dict):
        kind = "fields"
        for name, v in body.items():
            for item in v if isinstance(v, list) else [v]:
                if isinstance(item, dict):
                    if not {"job_id", "path"} <= item.keys() <= _REF_KEYS or not all(isinstance(x, str) for x in item.values()):
                        raise ValueError(f"field {name!r}: a file part is {{job_id, path, filename?, content_type?}}")
                    refs.append(item)
                elif isinstance(item, (str, int, float)):  # bool is an int
                    text += len(str(item).encode())
                else:
                    raise ValueError(f"field {name!r}: a value is text, a number, a boolean, a file part or a list of those")
    elif body is None:
        kind = "none"
    else:
        raise ValueError("body must be text, {job_id, path} or a fields object")
    method = str(a.get("method") or "GET").upper()
    if typ == "form" and kind in ("text", "file"):
        raise ValueError("type=form sends fields only: use a fields object, or type=xhr/navigation for a raw body")
    if typ == "xhr" and method in ("GET", "HEAD") and kind in ("text", "file"):
        raise ValueError("fetch cannot send a body with GET/HEAD")
    sizes = {}
    for r in refs:
        key, path = _key(owner, r["job_id"]), _check_rel_path(r["path"])
        if not storage.exists(key, path):
            raise ValueError("file not found")
        sizes[id(r)] = next(f["size"] for f in storage.get_manifest(key)["files"] if f["path"] == path)
    files = sum(sizes.values())
    if typ == "navigation":  # one CDP message: everything counts
        if files + text > MAX_REQUEST_BODY:
            raise ValueError(f"request body too large (max {MAX_REQUEST_BODY} bytes for type=navigation)")
    else:
        cap = ROUTED_UPLOAD_CAP if routed else MAX_UPLOAD_BYTES
        if text > MAX_REQUEST_BODY:
            raise ValueError(f"request body too large (max {MAX_REQUEST_BODY} bytes of text)")
        if files > cap:
            raise ValueError(f"file parts too large: {files} bytes > {cap}" + (
                " (this session's media policy filters every request, which the browser cannot do for larger uploads)" if routed else ""))
    staged: list[str] = []
    try:
        def stage(r: dict) -> str:
            name = _job_name(r.get("filename"), os.path.basename(r["path"]))
            staged.append(_stage(uid, _key(owner, r["job_id"]), _check_rel_path(r["path"]), name))
            return staged[-1]
        if kind == "file":
            del a["body"]
            a["body_file"] = stage(body)
        elif kind == "fields":
            def part(v):
                return {"file": stage(v), "filename": _job_name(v.get("filename"), os.path.basename(v["path"])),
                        **({"content_type": v["content_type"]} if "content_type" in v else {})}
            a["body"] = {n: [part(i) if isinstance(i, dict) else i for i in v] if isinstance(v, list) else part(v) if isinstance(v, dict) else v
                         for n, v in body.items()}
    except BaseException:
        for s_ in staged:
            _drop_in(uid, s_)
        raise
    return a, staged


def _ingest_out(uid: int, private: str, key: str, jname: str, tree: bool, base_url: str = "") -> dict:
    """Copy a tool's output (file, or request dir if `tree`) from the session into the job; blocking."""
    notes = []
    if tree:
        infos = _store_tree(key, ((rel, f, _ctype(rel)) for rel, f in _out_files(uid, private, True)), base_url, notes)
        body = next((i for p, i in infos.items() if p.startswith("response/body.")), None)
        return _with_notes(_job_result(key, *(body or ())), notes)
    for _, f in _out_files(uid, private):
        size = storage.write_stream(key, f"original/{jname}", f, _ctype(jname))
    if size > MAX_PARSE_BYTES:
        notes.append(f"original/{jname} not parsed ({size} bytes > MAX_PARSE_BYTES)")
        return _with_notes(_job_result(key), notes)
    return _describe_file(key, jname, storage.read_bytes(key, f"original/{jname}"))
