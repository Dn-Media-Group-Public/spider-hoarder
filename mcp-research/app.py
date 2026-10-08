"""mcp-research gateway: per-user isolated headed browser sessions (proxied
@playwright/mcp) plus fetch/parse/blob tools. See
docs/implemented-plans/browser-sessions-plan.md.

Everything is served under /<egress>/ (Caddy routes by that prefix), except
/authz which Caddy's forward_auth calls on the internal network."""

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
    "browser_network_request": None, "browser_network_requests": None, "browser_run_code_unsafe": None,
}
FILE_TOOLS_NOTE = ("\n\nWhen this writes a file, the result is {job_id, files, text?}; fetch with get_file_text / "
                   "get_file_url. `filename` only names the file inside the job.")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

mcp = FastMCP(
    name=f"mcp-research-{EGRESS}",
    instructions=(
        f"Each conversation gets its own private, isolated browser ({'egress via Tor' if EGRESS == 'tor' else 'direct egress'}). "
        "Use the browser_* tools to browse. When a page shows a CAPTCHA, bot check, login wall or cookie wall "
        "you cannot get past, call browser_request_human_help, show the user the link, and wait for them to "
        "confirm before continuing. browser_save_page parses and stores the current page; ingest_page does a "
        "one-shot fetch of a URL. Tools that write files (screenshots, PDFs, anything given a `filename`) return "
        "{job_id, files, text?} instead; fetch them with list_files / get_file_text / get_file_url. "
        "Call browser_end_session when finished."
    ),
    token_verifier=auth.KeycloakVerifier(
        OIDC_ISSUER, os.environ["MCP_AUDIENCE"], os.environ.get("ALLOWED_GROUP", ""),
        jwks_url=os.environ.get("OIDC_JWKS_URL", "")),  # optional: fetch keys over an internal address
    auth=AuthSettings(issuer_url=OIDC_ISSUER, resource_server_url=f"{SCHEME}://{PUBLIC_HOST}{MCP_PATH}"),
    streamable_http_path=MCP_PATH,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[PUBLIC_HOST], allowed_origins=[f"{SCHEME}://{PUBLIC_HOST}"],
    ),
)
server = mcp._mcp_server  # static FastMCP tool handlers are replaced by a proxy below


def _schema(**props: str) -> dict:
    return {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
            "required": list(props)}


EXTRA_TOOLS = [
    types.Tool(name="browser_request_human_help", inputSchema=_schema(reason="What the user needs to do, e.g. 'solve the CAPTCHA'"),
               description="Get a link that lets the user see and control THIS conversation's browser in a new tab. "
               "Use it for a CAPTCHA, bot challenge, login, 2FA or anything else a human should do. Give the link to "
               "the user, explain what to do, then STOP until they confirm they are done."),
    types.Tool(name="browser_save_page", inputSchema=_schema(),
               description="Parse the page currently open in the browser to Markdown and store it. "
               "Returns {job_id, files, text?}. Use after getting past consent walls/CAPTCHAs. "
               "Prefer this over browser_snapshot for extracting page content; use browser_snapshot only for "
               "navigation and interaction (finding refs to click or type into)."),
    types.Tool(name="browser_end_session", inputSchema=_schema(),
               description="Close this conversation's browser and discard its state (cookies, tabs, downloads)."),
    types.Tool(name="ingest_page", inputSchema=_schema(url="URL to fetch"),
               description="One-shot: fetch a URL (HTML via a throwaway browser, anything else directly) and parse it "
               "to Markdown. Returns {job_id, files, text?}. If the text looks like a consent/CAPTCHA wall, use the "
               "browser_* tools and browser_request_human_help instead."),
    types.Tool(name="get_upload_link", inputSchema=_schema(),
               description="Mint a job id and a link for a human to upload files through a browser."),
    types.Tool(name="list_files", inputSchema=_schema(job_id="Job id"), description="Manifest of files stored under a job id."),
    types.Tool(name="get_file_text", inputSchema={
                   "type": "object", "required": ["job_id", "path"],
                   "properties": {
                       "job_id": {"type": "string", "description": "Job id"},
                       "path": {"type": "string", "description": "File path from the manifest"},
                       "offset": {"type": "integer", "minimum": 0, "description": "Start character (default 0)"},
                       "length": {"type": ["integer", "null"], "minimum": 0, "default": DEFAULT_TEXT_LENGTH,
                                  "description": f"Max characters to return (default {DEFAULT_TEXT_LENGTH}); null = whole file"}}},
               description="Return one parsed file's Markdown text inline, or a slice of it "
               "(offset/length in characters). If truncated, a header gives the total size and the next offset."),
    types.Tool(name="get_file_url", inputSchema=_schema(job_id="Job id", path="File path from the manifest"),
               description="Mint a short-lived opaque signed URL for one blob (original or parsed)."),
]
HIDDEN = {"browser_install"}


def _caller() -> tuple[Any, str]:
    ctx = server.request_context
    token: AccessToken = ctx.request.scope["user"].access_token
    return ctx.session, token.claims["email"].lower()


def _key(owner: str, job_id: str) -> str:
    """Storage key: jobs live under <owner>/<job_id>, so callers only see their own."""
    if not _UUID_RE.match(job_id):
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
    else:
        text = extract.html_to_markdown(raw) if "html" in content_type else extract.file_to_markdown(raw, filename)
        storage.write_bytes(key, f"parsed/{filename}.md", text.encode(), "text/markdown")
    return _job_result(key, text)


def _job_result(key: str, text: str | None) -> dict:
    """{files, text?}: the one place that decides whether text is small enough to return inline."""
    result = {"files": storage.get_manifest(key)["files"]}
    if text is not None and len(text) < INLINE_LIMIT:
        result["text"] = text
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
    text = None
    if name.lower().endswith(".pdf"):
        text = extract.file_to_markdown(raw, name)
        storage.write_bytes(key, f"parsed/{name}.md", text.encode(), "text/markdown")
    elif b"\0" not in raw[:1024]:  # ponytail: NUL sniff for text vs binary
        text = raw.decode("utf-8", errors="replace")
    return _job_result(key, text)


async def _file_tool(bs: sessions.BrowserSession, name: str, arguments: dict) -> types.CallToolResult | None:
    """Run a file-writing upstream tool; the file ends up under a new job_id. None = not a file call."""
    default = FILE_TOOLS[name]
    if default is None and not arguments.get("filename"):
        return None
    jname = _job_name(arguments.get("filename"), default or "output")
    job_id = str(uuid.uuid4())
    private = f"{uuid.uuid4()}-{jname}"  # gateway-chosen, relative: lands in out/, never an agent-chosen path
    res = await bs.client.call_tool(name, {**arguments, "filename": private})
    if res.isError:
        return res
    try:
        raw = await asyncio.to_thread(_read_out, bs.uid, private)
    except (OSError, ValueError) as e:
        return _text(f"{name} produced no readable output file ({e}). Upstream said: {sessions.result_text(res)}", True)
    return _text(json.dumps({"job_id": job_id, **_store_file(_key(bs.owner, job_id), jname, raw)}))


async def _ingest_page(owner: str, url: str, live: sessions.BrowserSession | None = None) -> dict:
    job_id = str(uuid.uuid4())
    kw = {}
    if live:  # preflight/download as the real browser session: its cookies, its Tor circuit
        kw = {"cookies": await sessions.cookies_for(live, url), "socks_port": ports.socks_port(live.uid)}
    ctype = await asyncio.to_thread(fetch.content_type, url, **kw)
    if "text/html" in ctype or ctype == "":
        raw, ctype = (await sessions.fetch_html(owner, url, live)).encode(), "text/html"
    else:
        raw, ctype = await asyncio.to_thread(fetch.download, url, **kw)
    filename = os.path.basename(url.split("?")[0]) or "page"
    return {"job_id": job_id, **_store_and_parse(_key(owner, job_id), filename, raw, ctype)}


async def _save_page(bs: sessions.BrowserSession) -> dict:
    job_id = str(uuid.uuid4())
    raw = (await sessions.page_html(bs)).encode()
    return {"job_id": job_id, **_store_and_parse(_key(bs.owner, job_id), "page.html", raw, "text/html")}


def _text(msg: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], isError=error)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    tools = [t for t in await sessions.upstream_tools() if t.name not in HIDDEN]
    tools = [t.model_copy(update={"description": (t.description or "") + FILE_TOOLS_NOTE}) if t.name in FILE_TOOLS else t
             for t in tools]
    return tools + EXTRA_TOOLS


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
    mcp_session, owner = _caller()
    args = arguments or {}
    if name in HIDDEN:
        return _text(f"{name} is disabled", True)
    try:
        if name == "browser_end_session":
            bs = await sessions.get_session(mcp_session, owner, create=False)
            if bs:
                await sessions.close(bs)
            return _text("Browser session closed.")
        if name == "ingest_page":
            return _text(json.dumps(await _ingest_page(
                owner, args["url"], await sessions.get_session(mcp_session, owner, create=False))))
        if name == "get_upload_link":
            job_id = str(uuid.uuid4())
            token = blobtoken.mint(_key(owner, job_id), UPLOAD_MARK, ttl=3600)
            return _text(json.dumps({"url": f"{BASE}/upload/{token}", "job_id": job_id}))
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
            return _text(json.dumps({"url": f"{BASE}/blob/{blobtoken.mint(key, path)}"}))

        bs = await sessions.get_session(mcp_session, owner)
        bs.last_used = time.time()
        if name == "browser_request_human_help":
            reason = args.get("reason", "help with the page")
            return _text(
                f"Live view of this conversation's browser: {BASE}/view/{bs.id}/start\n"
                f"Ask the user to open it (they sign in with their newsroom account) and to: {reason}. "
                "Only the user who owns this conversation can open it. "
                "Do not take further browser actions until the user confirms they are done.")
        if name == "browser_save_page":
            return _text(json.dumps(await _save_page(bs)))
        if name in FILE_TOOLS and (res := await _file_tool(bs, name, args)):
            return res
        return await bs.client.call_tool(name, arguments)
    except Exception as e:  # noqa: BLE001
        return _text(f"{type(e).__name__}: {e}", True)


# ------------------------------------------------------ live view / web ----
VIEW_RE = re.compile(rf"^/{EGRESS}/view/([A-Za-z0-9_-]+)(/|$)")


def _owned_session(request: Request, view_id: str) -> sessions.BrowserSession | None:
    # X-Auth-Request-Email is set by Caddy after oauth2-proxy (client values stripped);
    # the gateway port is only reachable from Caddy, and session uids are firewalled off loopback.
    email = request.headers.get("x-auth-request-email", "").lower()
    bs = sessions.by_view.get(view_id)
    if not bs or not email or bs.owner != email:
        return None
    bs.last_used = time.time()
    return bs


@mcp.custom_route("/authz", methods=["GET"])
async def authz(request: Request) -> Response:
    """Caddy forward_auth: is this SSO user the owner of the session in the URL?"""
    m = VIEW_RE.match(request.headers.get("x-forwarded-uri", ""))
    bs = _owned_session(request, m.group(1)) if m else None
    if not bs:
        return PlainTextResponse("forbidden", status_code=403)
    return Response(status_code=200, headers={"X-Upstream": f"research-{EGRESS}:{ports.ws_port(bs.uid)}"})


@mcp.custom_route(f"/{EGRESS}/view/{{view_id}}/start", methods=["GET"])
async def view_start(request: Request) -> Response:
    """Landing link handed to the user; reveals the VNC password only to the owner."""
    view_id = request.path_params["view_id"]
    bs = _owned_session(request, view_id)
    if not bs:
        return PlainTextResponse("This browser session does not exist or is not yours.", status_code=403)
    base = f"/{EGRESS}/view/{view_id}"
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


@mcp.custom_route(f"/{EGRESS}/upload/{{token}}", methods=["GET"])
async def upload_page(request: Request) -> Response:
    _upload_key(request.path_params["token"])
    return HTMLResponse(
        '<!doctype html><html><body><h1>Upload files</h1>'
        '<form method="post" action="" enctype="multipart/form-data">'
        '<input type="file" name="files" multiple><button type="submit">Upload</button></form></body></html>')


@mcp.custom_route(f"/{EGRESS}/upload/{{token}}", methods=["POST"])
async def upload_submit(request: Request) -> Response:
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


@mcp.custom_route(f"/{EGRESS}/blob/{{token}}", methods=["GET"])
async def get_blob(request: Request) -> Response:
    try:
        data = blobtoken.verify(request.path_params["token"])
    except ValueError as e:
        raise HTTPException(403, str(e))
    if data["path"] == UPLOAD_MARK or not storage.exists(data["job_id"], data["path"]):
        raise HTTPException(404, "file not found")
    ctype = mimetypes.guess_type(data["path"])[0] or "application/octet-stream"
    return Response(content=storage.read_bytes(data["job_id"], data["path"]), media_type=ctype)


# ---------------------------------------------------------------- app ------
app = mcp.streamable_http_app()
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
