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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi_mcp import FastApiMCP

import blobtoken
import extract
import fetch
import storage

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

app = FastAPI()


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
def ingest_page(url: str) -> dict:
    """Fetch a URL (HTML via headless browser, anything else direct) and parse it
    to Markdown. Returns {job_id, files: [...], text?: str}."""
    job_id = str(uuid.uuid4())
    raw, content_type = fetch.fetch(url)
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
    ],
)
mcp.mount_http()
