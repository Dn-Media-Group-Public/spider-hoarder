"""mcp-research gateway: per-user isolated headed browser sessions (proxied
toolserver) plus fetch/parse/blob tools. See
docs/implemented-plans/browser-sessions-plan.md.

Everything is served under /<egress>/ (nginx routes by that prefix and by the replica id `rid`
in ids and URLs), except /authz which nginx's auth_request calls on the internal network.
See docs/implemented-plans/research-replicas.md."""

import asyncio
import contextlib
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import stat
import time
import uuid
from typing import Any

import anyio

from mcp import types
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

import auth
import blobtoken
import extract
import fetch
import ports
import sessions
import storage
from toolserver.mediapolicy import ROUTED_UPLOAD_CAP, Policy
from toolserver.textcap import DEFAULT_TEXT_CAP

EGRESS = sessions.EGRESS
log = logging.getLogger("app")
DEFAULT_TEXT_LENGTH = 20000  # chars; keeps get_file_text results LLM-sized
PUBLIC_HOST = os.environ["PUBLIC_HOST"]
SCHEME = os.environ.get("PUBLIC_SCHEME", "https")  # TLS ends at the load balancer; dev uses http
OIDC_ISSUER = os.environ["OIDC_ISSUER"].rstrip("/")
BASE = f"{SCHEME}://{PUBLIC_HOST}/{EGRESS}"
MCP_PATH = f"/{EGRESS}/mcp"
UPLOAD_MARK = "@upload"  # blobtoken path marking an upload-link token (never a real file path)
INLINE_LIMIT = 50_000
MAX_PARSE_BYTES = int(os.environ.get("MAX_PARSE_BYTES", 50 * 1024 * 1024))  # stored files have no cap; parsing does
MAX_ARCHIVE_BYTES = int(os.environ.get("MAX_ARCHIVE_BYTES") or os.environ.get("MAX_ZIP_BYTES") or 4 * 1024**3)  # uncompressed, per archive
MAX_ARCHIVE_MEMBERS = int(os.environ.get("MAX_ARCHIVE_MEMBERS") or os.environ.get("MAX_ZIP_MEMBERS") or 50_000)
# Proxied tools whose output is a file in a job: name -> default job-internal file name when the agent gives
# no `filename`.
FILE_TOOLS = {
    "browser_take_screenshot": "screenshot.png", "browser_pdf_save": "page.pdf",
    "browser_snapshot": "snapshot.yml", "browser_find": "matches.txt", "browser_evaluate": "evaluate.json",
    "browser_console_messages": "console.txt", "browser_network_request_get": "network-request",
    "browser_network_request_make": "network-request",
    "browser_network_requests": "network-requests.txt", "browser_storage_state": "storage-state.json",
}
TREE_TOOLS = ("browser_network_request_get", "browser_network_request_make")  # output is a directory, named by the gateway alone
# Request bodies go to Chromium as one base64 CDP message, which it refuses above 100 MiB (= 75 MiB raw) by killing the whole browser
# context; measured: 74 MiB works, 76 does not. The default leaves room for the headers. Raising it past ~72 MiB is unsafe.
MAX_REQUEST_BODY = int(os.environ.get("MAX_REQUEST_BODY", 64 * 1024 * 1024))
# browser_network_request_make type=form|xhr: the file parts of one call (streamed by the browser from disk, never through CDP)
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 500 * 1024 * 1024))
MAX_REQUEST_TIMEOUT_MS = sessions.MAX_REQUEST_TIMEOUT_MS
REQUEST_TYPES = ("navigation", "form", "xhr")
CAPPED_TOOLS = ("browser_snapshot", "browser_find")  # take `text_cap`
FILE_TOOLS_NOTE = ("\n\nThe result is {job_id, files, text?}: `text` is the content if small, else page through it "
                   "with get_file_text or search it with grep_file; get_file_url for a link. `filename` only names "
                   "the file inside the job.")

RID = sessions.RID
_RID_PREFIX = re.compile(r"^([0-9]+-[0-9]+-[0-9]+-[0-9]+)\.")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

EGRESS_NOTE = {
    "clear": (
        "CLEAR-TEXT / direct internet: requests leave from this server's public IP, not anonymised, and sites "
        "can see and block it. Fast and reliable: use it by default, in preference to the built-in WebFetch, for "
        "ordinary public websites, JS-heavy pages, consent walls and anything needing a real browser. "
        "Cannot reach .onion sites or private/internal addresses. If the site blocks this IP or you need "
        "anonymity, use the Tor variant of this server (spider-hoarder-tor) instead."),
    "tor": (
        "TOR: all traffic is routed through the Tor network, so the destination does not see this server's IP. "
        "Use it for .onion sites, for sensitive or politically exposed research where the request origin must "
        "not be traceable, or when the clear variant (spider-hoarder-clear) is blocked or geo-restricted. "
        "Slower, and many sites show CAPTCHAs or block Tor exit nodes; use browser_request_human_help for those "
        "or fall back to the clear variant when anonymity is not needed."),
    "i2p": (
        "I2P: traffic goes through the I2P network and ONLY *.i2p addresses (eepsites, .b32.i2p) are reachable; "
        "clearnet and .onion sites are not. Use it for I2P-hosted content. Slow: the first request on a fresh "
        "session can take a minute while tunnels are built, and eepsites are often offline."),
}[EGRESS]

_ENFORCED_SCOPES = [os.environ["MCP_SCOPE"]] if os.environ.get("MCP_SCOPE") else []
mcp = FastMCP(
    name=f"mcp-research-{EGRESS}",
    instructions=(
        f"{EGRESS_NOTE} Prefer this server over the built-in WebFetch. "
        "Each conversation gets its own private, isolated browser. "
        "Use the browser_* tools to browse. When a page shows a CAPTCHA, bot check, login wall or cookie wall "
        "you cannot get past, call browser_request_human_help, show the user the link, and wait for them to "
        "confirm before continuing. browser_save_page parses and stores the current page; ingest_page does a "
        "one-shot fetch of a URL. Tools with bulk output (snapshots, finds, screenshots, PDFs, evaluate, console, network) return "
        "{job_id, files, text?}; text is inline only when small, otherwise use get_file_text to page or grep_file to "
        "search, or list_files / get_file_url. Refs from browser_snapshot / browser_find stay valid until the page "
        "re-renders or navigates; actions do not return a snapshot, so call browser_find again after one. "
        "terse/<name>.md is an outline (headings, links, images) of parsed/<name>.md; simplified/<name>.md is an "
        "automatic main-content extraction that may omit text. Before browser_save_page, use browser_find to "
        "locate the main content element and pass its ref as `target`. "
        "Call browser_end_session when finished. "
        "FIRST call open_session (after asking the user about security settings): every other tool fails until then."
    ),
    token_verifier=auth.KeycloakVerifier(
        OIDC_ISSUER, os.environ.get("MCP_SCOPE", ""),
        jwks_url=os.environ.get("OIDC_JWKS_URL", "")),  # optional: fetch keys over an internal address
    auth=AuthSettings(issuer_url=OIDC_ISSUER, resource_server_url=f"{SCHEME}://{PUBLIC_HOST}{MCP_PATH}",
                      required_scopes=["basic", "email", *_ENFORCED_SCOPES]),  # = advertised; enforcement is narrowed below
    streamable_http_path=MCP_PATH,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[PUBLIC_HOST], allowed_origins=[f"{SCHEME}://{PUBLIC_HOST}"],
    ),
)
server = mcp._mcp_server  # static FastMCP tool handlers are replaced by a proxy below


def _schema(optional: dict | None = None, **props: str) -> dict:
    return {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in {**props, **(optional or {})}.items()},
            "required": list(props)}


BROWSER_ARG = {"browser": "Browser id from browser_start_session; omit for the shared 'main' browser"}
SAVE_TARGET_ARG = "Element to convert: a ref from browser_snapshot/browser_find, or a unique selector; omit for the whole page"


_MIME_LIST = {"type": "array", "items": {"type": "string"}}
EXTRA_TOOLS = [
    types.Tool(name="open_session", inputSchema={"type": "object", "properties": {
                   "media": {"type": "boolean", "description": "true = load images, video, audio and other media; false = never request or download them. "
                             "Default: false on Tor and I2P, true on clear."},
                   "allow": {**_MIME_LIST, "description": "Extra content types allowed for direct navigation/downloads, e.g. ['image/svg+xml']"},
                   "deny": {**_MIME_LIST, "description": "Content types always blocked for direct navigation/downloads, e.g. ['text/geojson']"}}},
               description="Set this session's media policy. Must be the FIRST tool call of a conversation; every other tool fails until it has been called. "
               "Calling it again ENDS the existing session (all browsers, cookies, tabs) and starts a new one with the new policy, so call it only once, "
               "at the start, never in the middle of a task. Browsing the dark web can expose the user to legal "
               "risk if images, video or audio are even loaded, so BEFORE calling this, explicitly ask the user about their security "
               "settings. Call it before any browser call. Pass `allow` / `deny` only if the user asked for a specific security setup; "
               "otherwise call it with no arguments for the defaults. Returns the effective policy (same as get_session)."),
    types.Tool(name="get_session", inputSchema={"type": "object", "properties": {}},
               description="Read-only: the session's effective media policy (`opened` is false until open_session has been called, "
               "and the rest are then the defaults open_session() would apply). Subagents: call this to see what was decided; do not call open_session (it would end the session)."),
    types.Tool(name="browser_request_human_help", inputSchema=_schema(BROWSER_ARG, reason="What the user needs to do, e.g. 'solve the CAPTCHA'"),
               description="Get a link that lets the user see and control THIS conversation's browser in a new tab. "
               "Use it for a CAPTCHA, bot challenge, login, 2FA or anything else a human should do. Give the link to "
               "the user, explain what to do, then STOP until they confirm they are done."),
    types.Tool(name="browser_save_page", inputSchema=_schema({**BROWSER_ARG, "target": SAVE_TARGET_ARG}),
               description=f"[{EGRESS.upper()}] Convert the page currently open in the browser to Markdown and store it. "
               "Returns {job_id, files, text?}. With `target` (an element ref from browser_snapshot or browser_find, or a "
               "unique selector) only that element is converted; without it, the whole page, including navigation, teasers "
               "and footers. To extract an article, first call browser_find (e.g. for `article`, `main`, or the headline) "
               "to get the ref of the element that contains the content, then pass it as `target`. "
               "`simplified/` holds an automatic main-content guess that may omit text. "
               "Use after getting past consent walls/CAPTCHAs. "
               "Prefer this over browser_snapshot for extracting page content; use browser_snapshot only for "
               "navigation and interaction (finding refs to click or type into)."),
    types.Tool(name="browser_end_session", inputSchema=_schema(BROWSER_ARG),
               description="With `browser`: close just that browser (window and tabs). Without: close all browsers and end "
               "this conversation's session, discarding its state (cookies, tabs, downloads). Subagents: pass the id "
               "from browser_start_session when done. Closing the last browser ends the session."),
    types.Tool(name="ingest_page", inputSchema=_schema({"target": "Unique selector of the element to convert (refs do not work here: no snapshot exists); omit for the whole page"}, url="URL to fetch"),
               description=f"[{EGRESS.upper()}] {EGRESS_NOTE}\n\nOne-shot: fetch a URL (HTML via a throwaway browser, anything else directly) and parse it "
               "to Markdown. Returns {job_id, files, text?}. `target` (HTML only) converts just that element, and must be a selector. `simplified/` holds an automatic main-content guess that may omit text. If the text looks like a consent/CAPTCHA wall, use the "
               "browser_* tools and browser_request_human_help instead."),
    types.Tool(name="get_upload_link", inputSchema=_schema(),
               description="Mint a job id and a link for a human to upload files through a browser."),
    types.Tool(name="list_files", inputSchema=_schema(job_id="Job id"), description="Manifest of files stored under a job id. terse/<name>.md is an outline (headings, links, images) of parsed/<name>.md."),
    types.Tool(name="get_file_text", inputSchema={
                   "type": "object", "required": ["job_id", "path"],
                   "properties": {
                       "job_id": {"type": "string", "description": "Job id"},
                       "path": {"type": "string", "description": "File path from the manifest"},
                       "offset": {"type": "integer", "minimum": 0, "description": "Start character (default 0)"},
                       "length": {"type": ["integer", "null"], "minimum": 0, "default": DEFAULT_TEXT_LENGTH,
                                  "description": f"Max characters to return (default {DEFAULT_TEXT_LENGTH}); null = whole file"}}},
               description="Return one parsed file's Markdown text inline, or a slice of it "
               "(offset/length in characters). If truncated, a header gives the total size and the next offset. "
               "terse/<name>.md is an outline (headings, links, images) of parsed/<name>.md."),
    types.Tool(name="grep_file", inputSchema={
                   "type": "object", "required": ["job_id", "path", "pattern"],
                   "properties": {
                       "job_id": {"type": "string", "description": "Job id"},
                       "path": {"type": "string", "description": "Text file path from the manifest"},
                       "pattern": {"type": "string", "description": "Python regular expression, or /pattern/i for case-insensitive"},
                       "context": {"type": "integer", "minimum": 0, "default": 2, "description": "Lines of context around each match (default 2)"},
                       "max_matches": {"type": "integer", "minimum": 1, "default": 50, "description": "Max matches to show (default 50)"}}},
               description="Search one text file of a job (e.g. a stored snapshot, matches or parsed page) with a regular "
               "expression. Returns matching lines with context, line numbers and the character offset of each block "
               "(usable as get_file_text offset). Binary files are refused."),
    types.Tool(name="get_file_url", inputSchema=_schema(job_id="Job id", path="File path from the manifest"),
               description="Mint a short-lived opaque signed URL for one blob (original or parsed)."),
]
HIDDEN = {"browser_install", "browser_bring_to_front", "browser_set_policy"}  # the last is the gateway's own call at session start


def _caller() -> tuple[Any, str, str | None]:
    ctx = server.request_context
    token: AccessToken = ctx.request.scope["user"].access_token
    # the SDK sees the bare id: ReplicaSessions strips the `<rid>.` prefix from requests
    return ctx.session, token.claims["email"].lower(), ctx.request.headers.get("mcp-session-id")


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


async def _file_tool(bs: sessions.BrowserSession, name: str, arguments: dict) -> types.CallToolResult:
    """Run a file-producing upstream tool; the file ends up under a new job_id."""
    tree = name in TREE_TOOLS
    jname = FILE_TOOLS[name] if tree else _job_name(arguments.get("filename"), FILE_TOOLS[name])
    job_id = _new_job_id()
    private = f"{uuid.uuid4()}-{jname}"  # gateway-chosen, relative: lands in out/, never an agent-chosen path
    staged, given = [], arguments
    if name == "browser_network_request_make":
        pol = getattr(bs, "policy", None)
        arguments, staged = await asyncio.to_thread(_request_body, bs.owner, bs.uid, arguments, bool(pol and pol.active and not pol.media))
    try:
        res = await bs.client.call_tool(name, {**arguments, "filename": private})
    finally:
        for s_ in staged:
            await asyncio.to_thread(_drop_in, bs.uid, s_)
    up = sessions.result_text(res)
    if res.isError or "](out/" not in up:  # an error, or a plain text answer such as "No new requests."
        return res
    meta = {}
    if tree:  # the toolserver's request line: `<n>. [METHOD] url => [status] text`
        m = re.search(r"^\d+\. \[(\S+)\] (\S+) => \[(\w+)\]", up, re.M)
        if m:
            meta = {"url": m.group(2), "method": m.group(1), "status": int(m.group(3)) if m.group(3).isdigit() else None}
        if m and m.group(3) == "BLOCKED":
            meta["blocked"] = "media policy"
        if r := re.search(r"^Redirects: (.+)$", up, re.M):
            meta["redirects"] = json.loads(r.group(1))
        if name == "browser_network_request_make":
            meta["type"] = arguments.get("type") or "navigation"
            if r := re.search(r"^Origin: (.+)$", up, re.M):
                meta["origin"] = r.group(1)
            if isinstance(given.get("body"), dict):  # the argument as given, with job references: never a second copy of a big upload
                meta["body"] = given["body"]
    key = _key(bs.owner, job_id)
    try:
        result = await asyncio.to_thread(_ingest_out, bs.uid, private, key, jname, tree, meta.get("url", ""))
    except (OSError, ValueError) as e:
        return _text(f"{name} produced no readable output file ({e}). Upstream said: {up}", True)
    if name in CAPPED_TOOLS:
        m = re.search(r"^- Page URL: (.+)$", up, re.M)
        meta = {"url": m and m.group(1), "target": arguments.get("target"), "depth": arguments.get("depth"),
                "text_cap": arguments.get("text_cap", DEFAULT_TEXT_CAP)}
        if name == "browser_find":
            meta["query"] = {k: arguments[k] for k in ("text", "regex") if arguments.get(k)}
    storage.set_meta(key, keep_none=("text_cap",), **meta)
    if meta.get("type") == "form" and arguments.get("cookies"):
        result["notes"] = [*result.get("notes", []), "cookies were set as a Cookie header, which bypasses SameSite filtering of the session's own cookies"]
    if meta.get("blocked"):
        result["notes"] = [*result.get("notes", []), "blocked by the media policy: the response body was never read"]
    return _text(json.dumps({"job_id": job_id, **result}))


async def _ingest_page(owner: str, url: str, live: sessions.BrowserSession | None = None, target: str | None = None,
                       policy: Policy | None = None) -> dict:
    job_id = _new_job_id()
    kw = {}
    if live:  # preflight/download as the real browser session: its cookies, its Tor circuit / I2P destination
        kw = {"cookies": await sessions.cookies_for(live, url), "socks_port": ports.socks_port(live.uid)}
    ctype = await asyncio.to_thread(fetch.content_type, url, **kw)
    if "text/html" in ctype or ctype == "":
        html, _ = await sessions.fetch_html(owner, url, live, target, policy)
        raw, ctype = html.encode(), "text/html"
    else:
        raw, ctype = await asyncio.to_thread(fetch.download, url, policy, **kw)
    filename = os.path.basename(url.split("?")[0]) or "page"
    key = _key(owner, job_id)
    result = _store_and_parse(key, filename, raw, ctype, url)
    storage.set_meta(key, url=url, target=target if "html" in ctype else None)
    return {"job_id": job_id, **result, **_meta(key)}


def _meta(key: str) -> dict:
    return {k: v for k, v in storage.get_manifest(key).items() if k in ("url", "target")}


async def _save_page(bs: sessions.BrowserSession, browser: str | None = None, target: str | None = None) -> dict:
    job_id = _new_job_id()
    html, url = await sessions.page_html(bs, browser, target)
    key = _key(bs.owner, job_id)
    result = _store_and_parse(key, "page.html", html.encode(), "text/html", url)
    storage.set_meta(key, url=url, target=target)
    return {"job_id": job_id, **result, **_meta(key)}


def _text(msg: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], isError=error)


def _without_filename(t: types.Tool) -> types.Tool:
    """browser_network_request_get/_make fill a whole job: the agent has no filename to choose."""
    sch = {**t.inputSchema, "properties": {k: v for k, v in t.inputSchema.get("properties", {}).items() if k != "filename"}}
    desc = (t.description or "").replace(" `filename` only names the file inside the job.", "")
    return t.model_copy(update={"inputSchema": sch, "description": desc})


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    own = {t.name for t in EXTRA_TOOLS}  # the gateway's versions replace the upstream ones of the same name
    tools = [t for t in await sessions.upstream_tools() if t.name not in HIDDEN | own]
    tools = [t.model_copy(update={"description": (t.description or "") + FILE_TOOLS_NOTE}) if t.name in FILE_TOOLS else t
             for t in tools]
    tools = [_without_filename(t) if t.name in TREE_TOOLS else t for t in tools]
    tools = [t.model_copy(update={"description": f"[{EGRESS.upper()}] {EGRESS_NOTE}\n\n{t.description or ''}"}) if t.name == "browser_navigate" else t
             for t in tools]
    return tools + EXTRA_TOOLS


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
    mcp_session, owner, sid = _caller()
    args = arguments or {}
    if name in HIDDEN:
        return _text(f"{name} is disabled", True)
    try:
        if name == "open_session":
            if not sid:
                return _text("no MCP session", True)
            policy = Policy.make(EGRESS, args.get("media"), args.get("allow"), args.get("deny"))  # validate before ending anything
            if sid in sessions.policies:  # a client may reuse one MCP session across conversations: a repeat call starts over
                if bs := await sessions.get_session(mcp_session, owner, create=False):
                    await sessions.close(bs)
            sessions.policies[sid] = policy
            log.info("session %s opened: %s", sid[:8], sessions.policies[sid].summary(EGRESS, True))
            return _text(json.dumps(sessions.policies[sid].summary(EGRESS, True)))
        if name == "get_session":
            return _text(json.dumps((sessions.policies.get(sid or "") or Policy.make(EGRESS)).summary(EGRESS, sid in sessions.policies)))
        if sid not in sessions.policies:  # fail closed: the agent cannot skip the question
            return _text("session not opened: ask the user about security settings, then call open_session first", True)
        if name == "browser_end_session":
            bs = await sessions.get_session(mcp_session, owner, create=False)
            blocked = None
            if bs:
                res = await bs.client.call_tool("browser_end_session", {k: args[k] for k in ("browser",) if args.get(k)})
                if res.isError and args.get("browser"):
                    return res
                if not res.isError:
                    info = sessions.parse_result(sessions.result_text(res))
                    if args.get("browser") and info["remaining"] > 0:
                        return res
                    blocked = info["blocked"]
                await sessions.close(bs)  # no id, or the last browser was closed
            sessions.policies.pop(sid)  # the session is over: the next open_session asks the user again (clients reuse one MCP session across /clear)
            return _text("Browser session closed." + (f" Media policy blocked {blocked} request(s)." if blocked else ""))
        if name == "ingest_page":
            return _text(json.dumps(await _ingest_page(
                owner, args["url"], await sessions.get_session(mcp_session, owner, create=False), args.get("target"),
                sessions.policies[sid])))
        if name == "get_upload_link":
            job_id = _new_job_id()
            token = blobtoken.mint(_key(owner, job_id), UPLOAD_MARK, ttl=3600)
            return _text(json.dumps({"url": f"{BASE}/upload/{RID}/{token}", "job_id": job_id}))
        if name in ("list_files", "get_file_text", "get_file_url", "grep_file"):
            key = _key(owner, args["job_id"])
            if name == "list_files":
                return _text(json.dumps(storage.get_manifest(key)))
            path = _check_rel_path(args["path"])
            if not storage.exists(key, path):
                return _text("file not found", True)
            if name == "grep_file":
                raw = storage.read_bytes(key, path)
                if b"\0" in raw[:1024]:
                    return _text("binary file: grep_file only searches text files", True)
                return _text(extract.grep_text(
                    raw.decode("utf-8", errors="replace"), args["pattern"], int(args.get("context", 2)),
                    int(args.get("max_matches", 50)), limit=INLINE_LIMIT))
            if name == "get_file_text":
                text = storage.read_text(key, path)
                off = max(int(args.get("offset") or 0), 0)
                n = args.get("length", DEFAULT_TEXT_LENGTH)
                end = len(text) if n is None else min(off + max(int(n), 0), len(text))
                if off == 0 and end == len(text):
                    return _text(text)
                return _text(f"[Showing characters {off}-{end} of {len(text)}."
                             + (f" Truncated: call again with offset={end} to continue." if end < len(text) else "")
                             + f"]\n\n{text[off:end]}")
            return _text(json.dumps({"url": f"{BASE}/blob/{RID}/{blobtoken.mint(key, path)}"}))

        bs = await sessions.get_session(mcp_session, owner, sid)
        bs.last_used = time.time()
        if name == "browser_request_human_help":
            reason = args.get("reason", "help with the page")
            if args.get("browser"):  # raise that browser's window in the live view
                res = await bs.client.call_tool("browser_bring_to_front", {"browser": args["browser"]})
                if res.isError:
                    return res
            return _text(
                f"Live view of this conversation's browser: {BASE}/view/{RID}/{bs.id}/start\n"
                f"Ask the user to open it (they sign in with their newsroom account) and to: {reason}. "
                "Only the user who owns this conversation can open it. "
                "Do not take further browser actions until the user confirms they are done.")
        if name == "browser_save_page":
            return _text(json.dumps(await _save_page(bs, args.get("browser"), args.get("target"))))
        if name in FILE_TOOLS:
            return await _file_tool(bs, name, args)
        return await bs.client.call_tool(name, arguments)
    except Exception as e:  # noqa: BLE001
        return _text(f"{type(e).__name__}: {e}", True)


# ------------------------------------------------------ live view / web ----
VIEW_RE = re.compile(rf"^/{EGRESS}/view/{RID}/([A-Za-z0-9_-]+)(/|$)")  # a foreign rid never matches


def _owned_session(request: Request, view_id: str) -> sessions.BrowserSession | None:
    # X-Auth-Request-Email is set by nginx after oauth2-proxy (client values replaced);
    # the gateway port is only meant for nginx, and session uids are firewalled off loopback.
    email = request.headers.get("x-auth-request-email", "").lower()
    bs = sessions.by_view.get(view_id)
    if not bs or not email or bs.owner != email:
        return None
    bs.last_used = time.time()
    return bs


@mcp.custom_route("/authz", methods=["GET"])
async def authz(request: Request) -> Response:
    """nginx auth_request: is this SSO user the owner of the session in the URL?"""
    m = VIEW_RE.match(request.headers.get("x-forwarded-uri", ""))
    bs = _owned_session(request, m.group(1)) if m else None
    if not bs:
        return PlainTextResponse("forbidden", status_code=403)
    return Response(status_code=200, headers={"X-Upstream": f"{RID.replace('-', '.')}:{ports.ws_port(bs.uid)}"})  # this replica, not the service


def _own(request: Request) -> None:
    if request.path_params["rid"] != RID:
        raise HTTPException(404, "not found")  # nginx routes by rid, so this is only defence in depth


@mcp.custom_route(f"/{EGRESS}/view/{{rid}}/{{view_id}}/start", methods=["GET"])
async def view_start(request: Request) -> Response:
    """Landing link handed to the user; reveals the VNC password only to the owner."""
    _own(request)
    view_id = request.path_params["view_id"]
    bs = _owned_session(request, view_id)
    if not bs:
        return PlainTextResponse("This browser session does not exist or is not yours.", status_code=403)
    base = f"/{EGRESS}/view/{RID}/{view_id}"
    return RedirectResponse(
        f"{base}/vnc.html?path={base.lstrip('/')}/websockify&autoconnect=1&reconnect=1"
        f"&resize=scale&password={bs.vnc_password}", status_code=302)


def _upload_key(token: str) -> str:
    try:
        data = blobtoken.verify(token)
    except ValueError as e:
        raise HTTPException(403, str(e))
    if data["path"] != UPLOAD_MARK:
        raise HTTPException(403, "not an upload link")
    return data["job_id"]


@mcp.custom_route("/", methods=["GET"])
async def index_page(request: Request) -> Response:
    """Public landing page (no auth): how to add both servers; URLs come from PUBLIC_SCHEME/PUBLIC_HOST."""
    root = f"{SCHEME}://{PUBLIC_HOST}"
    cmds = "\n".join(
        f"claude mcp add --transport http spider-hoarder-{e} {root}/{e}/mcp" for e in ("clear", "tor", "i2p"))
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=utf-8><title>spider-hoarder</title></head><body>"
        "<h1>spider-hoarder</h1><p>Add the MCP servers to Claude Code:</p>"
        f"<pre>{html.escape(cmds)}</pre>"
        "<p>Then run <code>/mcp</code>, pick the server and choose Authenticate. "
        "<b>clear</b> browses directly; <b>tor</b> browses via Tor; <b>i2p</b> reaches *.i2p via I2P.</p></body></html>")


@mcp.custom_route(f"/{EGRESS}/upload/{{rid}}/{{token}}", methods=["GET"])
async def upload_page(request: Request) -> Response:
    _own(request)
    _upload_key(request.path_params["token"])
    return HTMLResponse(
        '<!doctype html><html><body><h1>Upload files</h1>'
        '<form method="post" action="" enctype="multipart/form-data">'
        '<input type="file" name="files" multiple><button type="submit">Upload</button></form></body></html>')


@mcp.custom_route(f"/{EGRESS}/upload/{{rid}}/{{token}}", methods=["POST"])
async def upload_submit(request: Request) -> Response:
    _own(request)
    key = _upload_key(request.path_params["token"])
    form = await request.form()
    previews = []
    for uf in form.getlist("files"):
        if not hasattr(uf, "filename"):
            continue
        data = await uf.read()
        filename = os.path.basename(uf.filename or "upload")
        ctype = uf.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        _store_and_parse(key, filename, data, ctype)
        previews.append((filename, storage.read_text(key, f"parsed/{filename}.md") if storage.exists(key, f"parsed/{filename}.md") else "(archive unpacked)"))
    body = "".join(f"<h2>{html.escape(n)}</h2><pre>{html.escape(t[:5000])}</pre>" for n, t in previews)
    return HTMLResponse(f"<!doctype html><html><body><h1>Uploaded</h1>{body}</body></html>")


@mcp.custom_route(f"/{EGRESS}/blob/{{rid}}/{{token}}", methods=["GET"])
async def get_blob(request: Request) -> Response:
    _own(request)
    try:
        data = blobtoken.verify(request.path_params["token"])
    except ValueError as e:
        raise HTTPException(403, str(e))
    if data["path"] == UPLOAD_MARK or not storage.exists(data["job_id"], data["path"]):
        raise HTTPException(404, "file not found")
    ctype = mimetypes.guess_type(data["path"])[0] or "application/octet-stream"
    return Response(content=storage.read_bytes(data["job_id"], data["path"]), media_type=ctype)


# ---------------------------------------------------------------- app ------
def _header(scope: dict, name: bytes) -> str | None:
    return next((v.decode("latin-1") for k, v in scope["headers"] if k == name), None)


class ReplicaSessions:
    """Wraps the MCP endpoint (inside auth, so the caller is known). Makes the MCP session id
    `<rid>.<sdk id>` for clients (nginx routes on it) while the SDK only ever sees the bare id,
    and reserves a browser uid at `initialize` so a full replica answers 503 (nginx tries the next)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        sid_hdr = _header(scope, b"mcp-session-id")
        reserve_uid = owner = None
        if sid_hdr is not None:
            rid, _, sid = sid_hdr.partition(".")
            if rid != RID:  # nginx routes by rid, so this is a stale or foreign id
                return await Response('{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Session not found"}}',
                                      status_code=404, media_type="application/json")(scope, receive, send)
            scope = {**scope, "headers": [(k, sid.encode() if k == b"mcp-session-id" else v) for k, v in scope["headers"]]}
        elif scope["method"] == "POST":  # ponytail: any POST without a session id is treated as initialize
            owner = scope["user"].access_token.claims["email"].lower()
            try:
                reserve_uid = sessions.reserve(owner)
            except RuntimeError as e:
                return await Response(json.dumps({"error": "full", "message": str(e)}), status_code=503,
                                      media_type="application/json")(scope, receive, send)
        new_sid = None

        async def send_(msg):
            nonlocal new_sid, reserve_uid
            if msg["type"] == "http.response.start":
                hs = []
                for k, v in msg["headers"]:
                    if k.lower() == b"mcp-session-id":
                        new_sid = v.decode()
                        v = f"{RID}.{new_sid}".encode()
                    hs.append((k, v))
                msg = {**msg, "headers": hs}
                if reserve_uid is not None and new_sid and msg["status"] < 300:
                    sessions.register(new_sid, owner, reserve_uid)
                    reserve_uid = None
            await send(msg)

        if sid_hdr is not None:
            sessions.conn_open(sid)
        done = False
        try:
            await self.inner(scope, receive, send_)
            done = True
        finally:
            with anyio.CancelScope(shield=True):  # the client may have aborted: cleanup must still finish
                if sid_hdr is not None:
                    sessions.conn_close(sid)
                if owner is not None and not done and new_sid:  # initialize died after its session was registered
                    await sessions.unreserve(new_sid)
                    sessions.conn.pop(new_sid, None)
                if reserve_uid is not None:  # not an initialize after all, or it failed
                    await sessions.pool.release(reserve_uid)
                if sid_hdr is not None and scope["method"] == "DELETE":
                    await sessions.end_mcp_session(sid)


app = mcp.streamable_http_app()
# The SDK both advertises and enforces required_scopes. We advertise "basic" too (a realm-default scope that
# carries email, as Ariadne/Demeter do: Claude registers its client with exactly the advertised scopes, and
# without it the token has no email claim) but enforce only MCP_SCOPE.
for _r in app.routes:
    if getattr(_r, "path", None) == MCP_PATH:  # RequireAuthMiddleware: wrap what is inside it so unauthenticated calls get its 401
        _r.app.required_scopes = _ENFORCED_SCOPES
        _r.app.app = ReplicaSessions(_r.app.app)
_inner_lifespan = app.router.lifespan_context


@contextlib.asynccontextmanager
async def lifespan(a):
    await sessions.pool.reset()
    async with _inner_lifespan(a):
        task = asyncio.create_task(sessions.reaper())
        try:
            yield
        finally:
            task.cancel()
            for bs in list(sessions.by_mcp.values()):
                await sessions.close(bs)


app.router.lifespan_context = lifespan
