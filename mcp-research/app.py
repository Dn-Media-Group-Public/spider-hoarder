"""mcp-research: generic, Ariadne-agnostic fetch-and-parse MCP server.

No auth, no layers, no knowledge that Ariadne exists. Given a URL or an
uploaded file, returns Markdown text (small files) and/or short-lived opaque
blob URLs (large files). See docs/plans/agentic-research-mcp.md.
"""

import html
import mimetypes
import os
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi_mcp import FastApiMCP

import blobtoken
import browser_session
import extract
import fetch
import storage
from fetch import CHROME_UA

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await browser_session.startup()
    yield
    await browser_session.shutdown()


app = FastAPI(lifespan=_lifespan)


def _check_job_id(job_id: str) -> str:
    if not _UUID_RE.match(job_id):
        raise HTTPException(400, "invalid job_id")
    return job_id


def _check_rel_path(path: str) -> str:
    norm = os.path.normpath(path)
    if norm.startswith("..") or norm.startswith("/") or os.path.isabs(norm):
        raise HTTPException(400, "invalid path")
    return path


@app.get("/api/ingest_page", operation_id="ingest_page")
async def ingest_page(url: str) -> dict:
    """Fetch a URL (HTML via headless browser, anything else direct) and parse it
    to Markdown. Returns {job_id, files: [...], text?: str}.

    HTML fetches make a best-effort attempt to dismiss cookie-consent banners
    first. If the returned Markdown still looks like a consent wall, fall back
    to start_interactive_session/click_element/finish_interactive_session."""
    job_id = str(uuid.uuid4())
    raw, content_type = await fetch.fetch(url)
    filename = os.path.basename(url.split("?")[0]) or "page"
    storage.write_bytes(job_id, f"original/{filename}", raw, content_type)

    if extract.is_zip(content_type, filename):
        for rel_path, data in extract.unpack_zip(raw).items():
            storage.write_bytes(job_id, f"original/{rel_path}", data, "application/octet-stream")
            md = extract.file_to_markdown(data, rel_path)
            storage.write_bytes(job_id, f"parsed/{rel_path}.md", md.encode(), "text/markdown")
        text = None
    elif content_type == "text/html":
        text = extract.html_to_markdown(raw)
        storage.write_bytes(job_id, f"parsed/{filename}.md", text.encode(), "text/markdown")
    else:
        text = extract.file_to_markdown(raw, filename)
        storage.write_bytes(job_id, f"parsed/{filename}.md", text.encode(), "text/markdown")

    manifest = storage.get_manifest(job_id)
    result = {"job_id": job_id, "files": manifest["files"]}
    if text is not None and len(text) < 50_000:
        result["text"] = text
    return result


def _screenshot_result(session_id: str, page_screenshot: bytes, clickable: list[dict]) -> dict:
    storage.write_bytes(session_id, "screenshot.png", page_screenshot, "image/png")
    token = blobtoken.mint(session_id, "screenshot.png")
    return {
        "session_id": session_id,
        "screenshot_url": f"{PUBLIC_BASE_URL}/blob/{token}",
        "clickable_elements": clickable,
    }


@app.get("/api/start_interactive_session", operation_id="start_interactive_session")
async def start_interactive_session(url: str) -> dict:
    """Open a live browser session for a page where automatic cookie-consent
    handling wasn't enough. Returns {session_id, screenshot_url, clickable_elements}
    so the caller can pick an element to click via click_element, then call
    finish_interactive_session once the real content is visible."""
    session_id, session = await browser_session.create_session(url, CHROME_UA)
    page = session["page"]
    clickable = await browser_session.tag_clickable(page)
    shot = await browser_session.screenshot(page)
    return _screenshot_result(session_id, shot, clickable)


@app.get("/api/click_element", operation_id="click_element")
async def click_element(session_id: str, element_id: str) -> dict:
    """Click one of the elements returned by start_interactive_session/click_element
    (by its "id"), then return a fresh screenshot_url + clickable_elements —
    banners can take more than one click."""
    session = browser_session.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found or expired")
    page = session["page"]
    try:
        await page.click(f'[data-mcp-id="{element_id}"]', timeout=5000)
        await page.wait_for_timeout(500)
    except Exception as e:
        raise HTTPException(400, f"click failed: {e}")
    clickable = await browser_session.tag_clickable(page)
    shot = await browser_session.screenshot(page)
    return _screenshot_result(session_id, shot, clickable)


@app.get("/api/finish_interactive_session", operation_id="finish_interactive_session")
async def finish_interactive_session(session_id: str) -> dict:
    """Extract and store the current page of an interactive session, then close
    it. Returns the same shape as ingest_page: {job_id, files, text?}."""
    session = browser_session.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found or expired")
    page = session["page"]
    html_bytes = (await page.content()).encode("utf-8")
    await browser_session.close_session(session_id)

    job_id = str(uuid.uuid4())
    storage.write_bytes(job_id, "original/page.html", html_bytes, "text/html")
    text = extract.html_to_markdown(html_bytes)
    storage.write_bytes(job_id, "parsed/page.html.md", text.encode(), "text/markdown")

    manifest = storage.get_manifest(job_id)
    result = {"job_id": job_id, "files": manifest["files"]}
    if len(text) < 50_000:
        result["text"] = text
    return result


@app.get("/api/get_upload_link", operation_id="get_upload_link")
def get_upload_link() -> dict:
    """Mint a job id and return a link for a human to upload files through a browser."""
    job_id = str(uuid.uuid4())
    return {"url": f"{PUBLIC_BASE_URL}/upload/{job_id}", "job_id": job_id}


@app.get("/api/list_files", operation_id="list_files")
def list_files(job_id: str) -> dict:
    """Return the manifest (index.json) of files stored under a job id."""
    _check_job_id(job_id)
    return storage.get_manifest(job_id)


@app.get("/api/get_file_text", operation_id="get_file_text")
def get_file_text(job_id: str, path: str) -> dict:
    """Return one parsed file's Markdown text inline."""
    _check_job_id(job_id)
    _check_rel_path(path)
    if not storage.exists(job_id, path):
        raise HTTPException(404, "file not found")
    return {"text": storage.read_text(job_id, path)}


@app.get("/api/get_file_url", operation_id="get_file_url")
def get_file_url(job_id: str, path: str) -> dict:
    """Mint a short-lived opaque signed URL for one blob (original or parsed)."""
    _check_job_id(job_id)
    _check_rel_path(path)
    if not storage.exists(job_id, path):
        raise HTTPException(404, "file not found")
    token = blobtoken.mint(job_id, path)
    return {"url": f"{PUBLIC_BASE_URL}/blob/{token}"}


_UPLOAD_PAGE = """<!doctype html>
<html><body>
<h1>Upload files</h1>
<form method="post" action="" enctype="multipart/form-data">
  <input type="file" name="files" multiple>
  <button type="submit">Upload</button>
</form>
</body></html>"""


@app.get("/upload/{job_id}", response_class=HTMLResponse, include_in_schema=False)
def upload_page(job_id: str):
    _check_job_id(job_id)
    return _UPLOAD_PAGE


@app.post("/upload/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def upload_submit(job_id: str, request: Request):
    _check_job_id(job_id)
    form = await request.form()
    files = form.getlist("files")
    parsed_previews = []
    for uf in files:
        if not hasattr(uf, "filename"):
            continue
        data = await uf.read()
        filename = uf.filename or "upload"
        content_type = uf.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        storage.write_bytes(job_id, f"original/{filename}", data, content_type)

        if extract.is_zip(content_type, filename):
            for rel_path, member_data in extract.unpack_zip(data).items():
                storage.write_bytes(job_id, f"original/{rel_path}", member_data, "application/octet-stream")
                md = extract.file_to_markdown(member_data, rel_path)
                storage.write_bytes(job_id, f"parsed/{rel_path}.md", md.encode(), "text/markdown")
                parsed_previews.append((rel_path, md))
        else:
            md = extract.file_to_markdown(data, filename)
            storage.write_bytes(job_id, f"parsed/{filename}.md", md.encode(), "text/markdown")
            parsed_previews.append((filename, md))

    body = "".join(
        f"<h2>{html.escape(name)}</h2><pre>{html.escape(text[:5000])}</pre>"
        for name, text in parsed_previews
    )
    return f"<!doctype html><html><body><h1>Uploaded</h1>{body}</body></html>"


@app.get("/blob/{token}", include_in_schema=False)
def get_blob(token: str):
    try:
        data = blobtoken.verify(token)
    except ValueError as e:
        raise HTTPException(403, str(e))
    if not storage.exists(data["job_id"], data["path"]):
        raise HTTPException(404, "file not found")
    content = storage.read_bytes(data["job_id"], data["path"])
    ctype = mimetypes.guess_type(data["path"])[0] or "application/octet-stream"
    return Response(content=content, media_type=ctype)


mcp = FastApiMCP(
    app,
    name="mcp-research",
    description=(
        "Generic fetch-and-parse service: given a URL or an uploaded file, "
        "returns Markdown text. No knowledge of any downstream graph or auth."
    ),
    include_operations=[
        "ingest_page",
        "get_upload_link",
        "list_files",
        "get_file_text",
        "get_file_url",
        "start_interactive_session",
        "click_element",
        "finish_interactive_session",
    ],
)
mcp.mount_http()
