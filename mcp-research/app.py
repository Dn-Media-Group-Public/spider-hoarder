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
import mimetypes
import os
import re
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

EGRESS = sessions.EGRESS
DEFAULT_TEXT_LENGTH = 20000  # chars; keeps get_file_text results LLM-sized
PUBLIC_HOST = os.environ["PUBLIC_HOST"]
SCHEME = os.environ.get("PUBLIC_SCHEME", "https")  # TLS ends at the load balancer; dev uses http
OIDC_ISSUER = os.environ["OIDC_ISSUER"].rstrip("/")
BASE = f"{SCHEME}://{PUBLIC_HOST}/{EGRESS}"
MCP_PATH = f"/{EGRESS}/mcp"
UPLOAD_MARK = "@upload"  # blobtoken path marking an upload-link token (never a real file path)
INLINE_LIMIT = 50_000
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", 50 * 1024 * 1024))
# Proxied tools whose upstream writes a file: name -> default job-internal file name when the agent gives no
# `filename`; None = only a file when the agent passes `filename`, else it returns inline as upstream does.
FILE_TOOLS = {
    "browser_take_screenshot": "screenshot.png", "browser_pdf_save": "page.pdf",
    "browser_snapshot": None, "browser_evaluate": None, "browser_console_messages": None,
    "browser_network_request": None, "browser_network_requests": None, "browser_storage_state": "storage-state.json",
}
FILE_TOOLS_NOTE = ("\n\nWhen this writes a file, the result is {job_id, files, text?}; fetch with get_file_text / "
                   "get_file_url. `filename` only names the file inside the job.")

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
        "one-shot fetch of a URL. Tools that write files (screenshots, PDFs, anything given a `filename`) return "
        "{job_id, files, text?} instead; fetch them with list_files / get_file_text / get_file_url. "
        "terse/<name>.md is an outline (headings, links, images) of parsed/<name>.md; simplified/<name>.md is an "
        "automatic main-content extraction that may omit text. Before browser_save_page, use browser_find to "
        "locate the main content element and pass its ref as `target`. "
        "Call browser_end_session when finished."
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


EXTRA_TOOLS = [
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
    types.Tool(name="get_file_url", inputSchema=_schema(job_id="Job id", path="File path from the manifest"),
               description="Mint a short-lived opaque signed URL for one blob (original or parsed)."),
]
HIDDEN = {"browser_install", "browser_bring_to_front"}


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


def _store_and_parse(key: str, filename: str, raw: bytes, content_type: str) -> dict:
    storage.write_bytes(key, f"original/{filename}", raw, content_type)
    text = None
    if extract.is_zip(content_type, filename):
        for rel_path, data in extract.unpack_zip(raw).items():
            storage.write_bytes(key, f"original/{rel_path}", data, "application/octet-stream")
            md = extract.file_to_markdown(data, rel_path)
            storage.write_bytes(key, f"parsed/{rel_path}.md", md.encode(), "text/markdown")
            _write_terse(key, f"{rel_path}.md", md)
        return _job_result(key)
    if filename.lower().endswith((".md", ".markdown")):  # already Markdown: no parsed/, terse from the original
        text = raw.decode("utf-8", errors="replace")
        return _job_result(key, text, f"original/{filename}", _write_terse(key, filename, text), f"terse/{filename}")
    is_html = "html" in content_type
    text = extract.html_to_markdown(raw) if is_html else extract.file_to_markdown(raw, filename)
    storage.write_bytes(key, f"parsed/{filename}.md", text.encode(), "text/markdown")
    if is_html and (simple := extract.html_to_simplified(raw)):
        storage.write_bytes(key, f"simplified/{filename}.md", simple.encode(), "text/markdown")
    return _job_result(key, text, f"parsed/{filename}.md", _write_terse(key, f"{filename}.md", text), f"terse/{filename}.md")


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


def _read_out(uid: int, name: str) -> bytes:
    """Read <home>/out/<name> from a session without trusting anything the session controls: every path
    component is opened O_NOFOLLOW relative to the previous fd (a session owns out/ and could swap it for
    a symlink), O_NONBLOCK so a planted FIFO cannot hang us, regular files only, size-capped."""
    fds = []
    try:
        fds.append(os.open(ports.home(uid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        fds.append(os.open("out", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
        fds.append(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1]))
        st = os.fstat(fds[-1])
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("output is not a regular file")
        if st.st_size > MAX_FILE_BYTES:
            raise ValueError(f"output file too large ({st.st_size} > {MAX_FILE_BYTES} bytes)")
        with os.fdopen(os.dup(fds[-1]), "rb") as f:
            data = f.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("output file too large")
        return data
    finally:
        for fd in fds:
            os.close(fd)


def _job_name(filename: Any, default: str) -> str:
    """Agent-supplied filename -> a bare job-internal name (never a path)."""
    name = os.path.basename(str(filename or "").replace("\\", "/"))
    return default if name in ("", ".", "..") else name


def _store_file(key: str, name: str, raw: bytes) -> dict:
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    storage.write_bytes(key, f"original/{name}", raw, ctype)
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


async def _file_tool(bs: sessions.BrowserSession, name: str, arguments: dict) -> types.CallToolResult | None:
    """Run a file-writing upstream tool; the file ends up under a new job_id. None = not a file call."""
    default = FILE_TOOLS[name]
    if default is None and not arguments.get("filename"):
        return None
    jname = _job_name(arguments.get("filename"), default or "output")
    job_id = _new_job_id()
    private = f"{uuid.uuid4()}-{jname}"  # gateway-chosen, relative: lands in out/, never an agent-chosen path
    res = await bs.client.call_tool(name, {**arguments, "filename": private})
    if res.isError:
        return res
    try:
        raw = await asyncio.to_thread(_read_out, bs.uid, private)
    except (OSError, ValueError) as e:
        return _text(f"{name} produced no readable output file ({e}). Upstream said: {sessions.result_text(res)}", True)
    return _text(json.dumps({"job_id": job_id, **_store_file(_key(bs.owner, job_id), jname, raw)}))


async def _ingest_page(owner: str, url: str, live: sessions.BrowserSession | None = None, target: str | None = None) -> dict:
    job_id = _new_job_id()
    kw = {}
    if live:  # preflight/download as the real browser session: its cookies, its Tor circuit
        kw = {"cookies": await sessions.cookies_for(live, url), "socks_port": ports.socks_port(live.uid)}
    ctype = await asyncio.to_thread(fetch.content_type, url, **kw)
    if "text/html" in ctype or ctype == "":
        html, _ = await sessions.fetch_html(owner, url, live, target)
        raw, ctype = html.encode(), "text/html"
    else:
        raw, ctype = await asyncio.to_thread(fetch.download, url, **kw)
    filename = os.path.basename(url.split("?")[0]) or "page"
    key = _key(owner, job_id)
    result = _store_and_parse(key, filename, raw, ctype)
    storage.set_meta(key, url=url, target=target if "html" in ctype else None)
    return {"job_id": job_id, **result, **_meta(key)}


def _meta(key: str) -> dict:
    return {k: v for k, v in storage.get_manifest(key).items() if k in ("url", "target")}


async def _save_page(bs: sessions.BrowserSession, browser: str | None = None, target: str | None = None) -> dict:
    job_id = _new_job_id()
    html, url = await sessions.page_html(bs, browser, target)
    key = _key(bs.owner, job_id)
    result = _store_and_parse(key, "page.html", html.encode(), "text/html")
    storage.set_meta(key, url=url, target=target)
    return {"job_id": job_id, **result, **_meta(key)}


def _text(msg: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], isError=error)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    own = {t.name for t in EXTRA_TOOLS}  # the gateway's versions replace the upstream ones of the same name
    tools = [t for t in await sessions.upstream_tools() if t.name not in HIDDEN | own]
    tools = [t.model_copy(update={"description": (t.description or "") + FILE_TOOLS_NOTE}) if t.name in FILE_TOOLS else t
             for t in tools]
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
        if name == "browser_end_session":
            bs = await sessions.get_session(mcp_session, owner, create=False)
            if bs and args.get("browser"):
                res = await bs.client.call_tool("browser_end_session", {"browser": args["browser"]})
                if res.isError or sessions.parse_result(sessions.result_text(res))["remaining"] > 0:
                    return res
            if bs:
                await sessions.close(bs)  # no id, or the last browser was closed
            return _text("Browser session closed.")
        if name == "ingest_page":
            return _text(json.dumps(await _ingest_page(
                owner, args["url"], await sessions.get_session(mcp_session, owner, create=False), args.get("target"))))
        if name == "get_upload_link":
            job_id = _new_job_id()
            token = blobtoken.mint(_key(owner, job_id), UPLOAD_MARK, ttl=3600)
            return _text(json.dumps({"url": f"{BASE}/upload/{RID}/{token}", "job_id": job_id}))
        if name in ("list_files", "get_file_text", "get_file_url"):
            key = _key(owner, args["job_id"])
            if name == "list_files":
                return _text(json.dumps(storage.get_manifest(key)))
            path = _check_rel_path(args["path"])
            if not storage.exists(key, path):
                return _text("file not found", True)
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
        if name in FILE_TOOLS and (res := await _file_tool(bs, name, args)):
            return res
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
        f"claude mcp add --transport http spider-hoarder-{e} {root}/{e}/mcp" for e in ("clear", "tor"))
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=utf-8><title>spider-hoarder</title></head><body>"
        "<h1>spider-hoarder</h1><p>Add the MCP servers to Claude Code:</p>"
        f"<pre>{html.escape(cmds)}</pre>"
        "<p>Then run <code>/mcp</code>, pick the server and choose Authenticate. "
        "<b>clear</b> browses directly; <b>tor</b> browses via Tor.</p></body></html>")


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
