"""spider-hoarder gateway framework: auth, the job store and its file tools, the live-view routes, the session registry
and the MCP plumbing with a dynamic tool list. Everything backend-specific (browser sessions, Android emulator slots,
...) is the driver's (driver.py): the backend named by $BACKEND.

Everything is served under /<prefix>/ (prefix = the backend name with `_` as `-`; nginx routes by that prefix and by
the replica id `rid` in ids and URLs), except /authz which nginx's auth_request calls on the internal network.
See docs/architecture/backend-plugins.md and docs/implemented-plans/research-replicas.md."""

import asyncio
import contextlib
import html
import json
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass
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

from spider_hoarder_sdk.content import extract
from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway import auth
from spider_hoarder_sdk.gateway import blobtoken
from spider_hoarder_sdk.gateway import sessions
from spider_hoarder_sdk.gateway.driver import driver, hyphen
from spider_hoarder_sdk.gateway.jobs import (DEFAULT_TEXT_LENGTH, FILE_TOOLS_NOTE, INLINE_LIMIT, RID, UPLOAD_MARK, _check_rel_path,
                                             _key, _new_job_id, _schema, _text)

PREFIX = sessions.PREFIX  # the URL prefix nginx routes on
logging.basicConfig(level="INFO", format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")
PUBLIC_HOST = os.environ["PUBLIC_HOST"]
SCHEME = os.environ.get("PUBLIC_SCHEME", "https")  # TLS ends at the load balancer; dev uses http
OIDC_ISSUER = os.environ["OIDC_ISSUER"].rstrip("/")
BASE = f"{SCHEME}://{PUBLIC_HOST}/{PREFIX}"
MCP_PATH = f"/{PREFIX}/mcp"
_ENFORCED_SCOPES = [os.environ["MCP_SCOPE"]] if os.environ.get("MCP_SCOPE") else []


def other_servers() -> str:
    """One entry per other enabled backend (BACKENDS), the text from the descriptors (BACKEND_SERVERS, a JSON list that
    deploy.sh builds: this container only has its own backend installed). No lead-in label."""
    info = {e["name"]: e for e in json.loads(os.environ.get("BACKEND_SERVERS") or "[]")}
    lines = []
    for n in os.environ.get("BACKENDS", "").split():
        if n == driver.name:
            continue
        e = info.get(n, {})
        lines.append(f"spider-hoarder-{hyphen(n)}" + (f" ({e['title']})" if e.get("title") else "")
                     + (f": {e['description']}" if e.get("description") else ""))
    return "\n".join(lines)


def _instructions() -> str:
    others = other_servers()
    return driver.instructions + (f"\n\n{others}" if others else "")


mcp = FastMCP(
    name=f"spider-hoarder-{PREFIX}",
    instructions=_instructions(),
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
driver.register(mcp)


# The job tools every backend has: files stored under a job id, whoever produced them.
JOB_TOOLS = [
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


@dataclass
class Ctx:
    """The caller of a tool: handed to the driver's hooks."""
    mcp_session: Any
    owner: str
    sid: str | None
    base: str = BASE  # this backend's public URL prefix, and this replica's id: for links in tool results
    rid: str = RID


def _caller() -> tuple[Any, str, str | None]:
    ctx = server.request_context
    token: AccessToken = ctx.request.scope["user"].access_token
    # the SDK sees the bare id: ReplicaSessions strips the `<rid>.` prefix from requests
    return ctx.session, token.claims["email"].lower(), ctx.request.headers.get("mcp-session-id")



@server.list_tools()
async def list_tools() -> list[types.Tool]:
    own = {t.name for t in [*driver.extra_tools, *JOB_TOOLS]}  # the gateway's versions replace the upstream ones of the same name
    tools = [t for t in await sessions.upstream_tools() if t.name not in driver.hidden | own]
    tools = [t.model_copy(update={"description": (t.description or "") + FILE_TOOLS_NOTE}) if t.name in driver.file_tools else t
             for t in tools]
    return [*driver.decorate_tools(tools), *driver.extra_tools, *JOB_TOOLS]


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
    mcp_session, owner, sid = _caller()
    ctx = Ctx(mcp_session, owner, sid)
    args = arguments or {}
    if name in driver.hidden:
        return _text(f"{name} is disabled", True)
    try:
        if (res := await driver.pre_session(name, args, ctx)) is not None:
            return res
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

        if not driver.known(name):  # before get_session: an unknown tool must not book a session
            return _text(f"unknown tool {name}", True)
        bs = await sessions.get_session(mcp_session, owner, sid)
        bs.last_used = time.time()
        bs.waiting_human = None  # any tool call ends a pending human-help wait
        if (res := await driver.with_session(name, args, ctx, bs)) is not None:
            return res
        if name in driver.file_tools:
            return await driver.file_tool(bs, name, args)
        return await bs.client.call_tool(name, arguments)
    except Exception as e:  # noqa: BLE001
        log.exception("tool %s failed", name)  # the MCP client only sees the one-line summary
        return _text(f"{type(e).__name__}: {e}", True)


# ------------------------------------------------------ live view / web ----
VIEW_RE = re.compile(rf"^/{PREFIX}/view/{RID}/([A-Za-z0-9_-]+)(/|$)")  # a foreign rid never matches


def _owned_session(request: Request, view_id: str) -> sessions.Session | None:
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
    return Response(status_code=200, headers={"X-Upstream": driver.view_upstream(bs.uid, RID)})


def _own(request: Request) -> None:
    if request.path_params["rid"] != RID:
        raise HTTPException(404, "not found")  # nginx routes by rid, so this is only defence in depth


@mcp.custom_route(f"/{PREFIX}/view/{{rid}}/{{view_id}}/start", methods=["GET"])
async def view_start(request: Request) -> Response:
    """Landing link handed to the user; reveals the VNC password only to the owner."""
    _own(request)
    view_id = request.path_params["view_id"]
    bs = _owned_session(request, view_id)
    if not bs:
        return PlainTextResponse(f"This {driver.noun} session does not exist or is not yours.", status_code=403)
    base = f"/{PREFIX}/view/{RID}/{view_id}"
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



@mcp.custom_route(f"/{PREFIX}/upload/{{rid}}/{{token}}", methods=["GET"])
async def upload_page(request: Request) -> Response:
    _own(request)
    _upload_key(request.path_params["token"])
    return HTMLResponse(
        '<!doctype html><html><body><h1>Upload files</h1>'
        '<form method="post" action="" enctype="multipart/form-data">'
        '<input type="file" name="files" multiple><button type="submit">Upload</button></form></body></html>')


@mcp.custom_route(f"/{PREFIX}/upload/{{rid}}/{{token}}", methods=["POST"])
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
        previews.append((filename, storage.read_text(key, f"parsed/{filename}.md") if storage.exists(key, f"parsed/{filename}.md") else "(stored)" if filename.lower().endswith(".apk") else "(archive unpacked)"))
    body = "".join(f"<h2>{html.escape(n)}</h2><pre>{html.escape(t[:5000])}</pre>" for n, t in previews)
    return HTMLResponse(f"<!doctype html><html><body><h1>Uploaded</h1>{body}</body></html>")


@mcp.custom_route(f"/{PREFIX}/blob/{{rid}}/{{token}}", methods=["GET"])
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
