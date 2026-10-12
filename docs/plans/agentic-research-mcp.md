# Agentic Research MCP

## Problem / Goal

Enable an agentic workflow: an LLM agent queries the graph/timeline/geography
for gaps or leads, does web research (search + fetch) outside Ariadne, and
ingests what it finds back into the graph — then keeps iterating, using the
growing graph as long-horizon cross-document memory rather than re-deriving
everything from the single page it just read. It also needs to support the
user uploading their own documents (PDF, Word, a zip of spreadsheets, etc.)
from claude.ai/Desktop.

An earlier draft of this plan made `mcp-research` a proxying, ACL-aware
gateway in front of `graph-explorer-api`: it forwarded the caller's JWT,
re-exposed every upstream `/mcp` tool through a catch-all proxy, and
pre-checked layer roles before minting upload links. That version is
dropped. It coupled a fetch/parse tool to Ariadne's auth and layer model for
no real benefit — the agent already holds an MCP connection to
`article-graph` directly and can call both servers itself.

**New shape: two fully independent MCP servers, no proxying between them.**

- `mcp-research` — a generic, Ariadne-agnostic fetch-and-parse service.
  Given a URL or an uploaded file, it returns text. It has no concept of
  layers, auth, or ACLs, and no dependency on `graph-explorer-api` at all.
- `article-graph` (existing `/mcp` on `graph-explorer-api`) — gains one new
  write tool, `ingest_text`, wrapping the existing `POST
  /api/ingest/nodes`, auth'd and ACL-scoped exactly like the REST endpoint
  per [Layer Access Control](layer-access-control.md).

The agent is the orchestrator: it calls `mcp-research` to get text (or a
reference to text), then calls `article-graph`'s `ingest_text` itself with
its own JWT to write it into a layer. Nothing routes through `mcp-research`
on the way into the graph.

**Depends on [Layer Access Control](layer-access-control.md)**: `ingest_text`
reuses that plan's JWT ACL/auto-create branch on `POST /api/ingest/nodes`.

## Design

### Component boundaries

```
LLM agent (holds two independent MCP connections)
  │
  ├─▶ mcp-research (new service — Playwright + trafilatura + markitdown + blob storage)
  │     no auth, no layers, no knowledge that Ariadne exists
  │     returns: text (small files) and/or short-lived opaque URLs (large files)
  │
  └─▶ article-graph /mcp (graph-explorer-api, existing service)
        oauth2 Bearer JWT, ACL per Layer Access Control
        existing read tools (get_nodes, search, ...) + new: ingest_text(layer, title, body_md | source_url)
```

`mcp-research` holds no database connection, no JWT verification, and no
`graph-explorer-api` dependency. It only talks to whatever blob storage
backend it's configured with. `graph-explorer-api`'s ingest endpoint gains
the ability to fetch `source_url` itself with plain `requests` — it never
runs Playwright and never talks to `mcp-research` directly; the *agent*
carries the URL across.

Assumption carried over from the existing `/mcp` deployment: `mcp-research`
sits behind the same network perimeter (not open to the public internet),
since it has no auth of its own and will fetch arbitrary URLs / accept
arbitrary uploads on request.

### mcp-research: fetch flow (`ingest_page(url)`)

1. **Preflight with `requests`** (`HEAD`, falling back to a small ranged
   `GET` if `HEAD` is unsupported) to read `Content-Type`.
2. If it's HTML: launch Playwright, **with a real desktop Chrome User-Agent
   string** (not the default `HeadlessChrome` UA), navigate, **wait for the
   page to settle** (network-idle plus a short quiescence check, not just
   `DOMContentLoaded`) so client-rendered content is present, then
   `page.content()` → **trafilatura** (`output_format="markdown"`) for the
   main article text + title.
3. If it's anything else (zip, PDF, docx, ...): skip Playwright entirely,
   download the bytes directly with `requests`.
4. Save the raw bytes and run the same extraction used for uploads (below)
   to produce parsed Markdown.
5. Persist to blob storage (layout below) under a fresh job UUID.
6. Return to the agent: the job id, the manifest (`index.json` contents),
   and the extracted text inline when it's a single, reasonably-sized
   document (so the agent can act on it immediately without a second call).

### mcp-research: upload flow

1. Agent calls `get_upload_link()` → mcp-research mints a job UUID, returns
   `/upload/<uuid>`. Printed by the agent as a plain link for the user to
   click.
2. `GET /upload/<uuid>` — static drag-and-drop/file-picker page, no auth.
3. `POST /upload/<uuid>` — for each uploaded file: save the original bytes;
   if it's a `.zip`, unpack with stdlib `zipfile`; run **markitdown** on
   every resulting file (pdf/docx/xlsx/pptx/csv/html/etc.) to Markdown.
   Persist everything to blob storage under that job's UUID, and render a
   confirmation page showing the extracted text directly (no separate
   status-check step — nothing else needs to be polled for the human).

### Blob storage layout

One job = one UUID-keyed "directory" (backend-agnostic — same layout on
GCS, S3, or local disk via **fsspec**, selected purely by the configured
storage URL, e.g. `gs://bucket/prefix`):

```
<job_uuid>/
  original/<filename>            # raw bytes exactly as fetched or uploaded
  parsed/**                      # mirrors the original's internal structure
  terse/**                       # outline (headings, links, images) of each parsed/Markdown file
    report.pdf.md                # e.g. for a zip: same relative paths, +.md
    subdir/notes.docx.md
  simplified/**                  # trafilatura guess for HTML jobs (may omit text)
  index.json                     # manifest: files, plus optional url/target for page jobs (See also: [Plan: save-page target](../implemented-plans/save-page-target.md)); snapshot/find jobs also `depth`, `text_cap`, `query` (See also: [Plan: jobs for all tools](../implemented-plans/jobs-for-all-tools.md))
```

Deployment will most likely use GCS with a service-account credential
mounted into the container (standard ADC — no bucket/backend-specific code
in the app). No retention/lifecycle policy is defined here; if cleanup is
needed later, it's a bucket lifecycle rule, not app code.

### Tool surface on mcp-research

| Tool | Behavior |
|---|---|
| `ingest_page(url)` | Fetch flow above. Returns `{job_id, files: [...], text?: str}`. |
| `get_upload_link()` | Mints a job UUID, returns `{url, job_id}`. |
| `list_files(job_id)` | Returns that job's `index.json`. |
| `get_file_text(job_id, path)` | Returns one parsed file's Markdown inline — for small files the agent wants to read directly. Also serves `terse/<name>.md` outlines. See also: [Plan: Terse outline view](../implemented-plans/terse-view.md) |
| `grep_file(job_id, path, pattern, context=2, max_matches=50)` | Regex search of one text file: matching lines with context, line numbers and character offsets (for `get_file_text`). See also: [Plan: jobs for all tools](../implemented-plans/jobs-for-all-tools.md) |
| `get_file_url(job_id, path)` | Mints a short-lived **opaque** signed URL for one blob (original or parsed) — for large files the agent wants to hand to Ariadne by reference instead of pasting inline. |

All file-producing tools (screenshots, PDFs, any `filename=` output) also return `{job_id, files, text?}`. See also: [Plan: Tool file outputs become jobs](../implemented-plans/file-outputs-as-jobs.md)

`get_file_url` does **not** use fsspec's native presigning (`fs.sign()`),
which only some backends support and which would leak the job UUID/path in
the URL. Instead `mcp-research` runs its own tiny proxy route:

```
GET /blob/<token>
```

`<token>` is an HMAC-signed, opaque value encoding `{job_id, path, exp}` —
the URL itself carries no readable job UUID or path. The route verifies the
signature and expiry, then streams the blob from whatever fsspec backend is
configured. This keeps signing backend-agnostic (works identically on local
disk, which has no native presigning, and on GCS/S3, which do).

### Ariadne side: `ingest_text` MCP tool + `source_url` support

New tool on the existing `article-graph` `/mcp` (via `FastApiMCP`, same
mechanism as the current read tools — see `graph-explorer/api/app.py`),
wrapping `POST /api/ingest/nodes`:

```
ingest_text(layer: str, title: str, body_md: str | None = None, source_url: str | None = None)
```

Exactly one of `body_md` / `source_url` is required. When `source_url` is
given (an `mcp-research` `/blob/<token>` link), `graph-explorer-api` fetches
it itself with plain `requests` — no Playwright, no fsspec, no new runtime
dependency in `graph-explorer-api` beyond an HTTP client it already has.
Auth and ACL are unchanged from the REST endpoint: the caller's JWT is
checked against the target layer's role exactly as
[Layer Access Control](layer-access-control.md) defines for
`POST /api/ingest/nodes`.

No other pipeline changes: `stage_ner`/`stage_llm` already pick up any
Article node missing their pipeline-state row regardless of how it arrived.

### Deployment

```yaml
mcp-research:
  build:
    context: .
    dockerfile: mcp-research/Dockerfile   # playwright/python base image + chromium install
  image: mcp-research:local
  environment:
    RESEARCH_BLOB_URL: ${RESEARCH_BLOB_URL:-file:///data}   # fsspec URL; gs://... in prod
    BLOB_TOKEN_SECRET: ${BLOB_TOKEN_SECRET:-}                # signs/verifies /blob/<token>
    GOOGLE_APPLICATION_CREDENTIALS: /var/secrets/gcs-sa.json # when RESEARCH_BLOB_URL is gs://...
  networks:
    - age_net
  deploy:
    replicas: 1
    restart_policy:
      condition: on-failure
```

New nginx locations (mirrors the existing `/mcp` block; no oauth2-JWT
pass-through needed since `mcp-research` has no auth of its own):

```nginx
location /mcp-research {
    proxy_pass http://mcp-research:8080/mcp;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 120s;
}

location /mcp-research/upload {
    proxy_pass http://mcp-research:8080/upload;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    client_max_body_size 100m;
}

location /mcp-research/blob {
    proxy_pass http://mcp-research:8080/blob;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
}
```

## Out of scope for this plan

- Layer ACL/ownership model, scopes, and the write-enforcement endpoint this
  plan depends on — see [Layer Access Control](layer-access-control.md).
- Blob lifecycle/retention policy (cleanup of old jobs).
- `web_search` — left to the calling agent's own tools.

## Implementation order

Assumes [Layer Access Control](layer-access-control.md) is already
implemented (its `POST /api/ingest/nodes` JWT branch).

1. `mcp-research/` — new service skeleton: FastAPI + `fastapi-mcp`,
   Playwright, trafilatura, markitdown, fsspec deps, `Dockerfile`. No DB
   client dependency at all.
2. `storage.py` — fsspec wrapper: write/read blobs under `<job_uuid>/...`,
   build/read `index.json`.
3. `fetch.py` — `requests` preflight by content-type; Playwright fetch with
   custom Chrome UA + network-idle/quiescence wait for HTML; direct
   `requests` download for everything else.
4. `extract.py` — trafilatura (HTML) + markitdown (pdf/docx/xlsx/pptx/csv/
   etc.) + stdlib `zipfile` unpack, preserving the original's directory
   structure under `parsed/`.
5. `blobtoken.py` — HMAC-signed opaque token mint/verify for `/blob/<token>`.
6. `app.py` — MCP tools (`ingest_page`, `get_upload_link`, `list_files`,
   `get_file_text`, `get_file_url`) + `GET/POST /upload/<job_uuid>` +
   `GET /blob/<token>`.
7. `graph-explorer/api` — add `ingest_text` route/tool wrapping `POST
   /api/ingest/nodes`, with `source_url` support (fetch via `requests`
   when given instead of `body_md`).
8. `docker-compose.yml` (+ `docker-compose.dev.yml` if needed) — add
   `mcp-research` service.
9. `graph-explorer/frontend/nginx.conf` / `nginx.dev.conf` — add
   `/mcp-research`, `/mcp-research/upload`, `/mcp-research/blob` locations.
10. Manual smoke test: (a) `ingest_page` on an HTML article returns text and
    the agent can `ingest_text` it into a layer it can write to; (b)
    `ingest_page` on a direct `.pdf`/`.zip` link skips Playwright, produces
    a mirrored `parsed/` tree, and `get_file_url` + `ingest_text(source_url=...)`
    round-trips one file into the graph; (c) upload a PDF and a zip of
    spreadsheets through the browser page, confirm the confirmation page
    shows extracted text and `list_files`/`get_file_text`/`get_file_url`
    all work against that job afterward; (d) an expired or tampered
    `/blob/<token>` is rejected.

## Files to change

| File | Change |
|---|---|
| `mcp-research/Dockerfile` | New — Playwright/Chromium + Python base |
| `mcp-research/app.py` | New — FastAPI app, `fastapi-mcp` mount, MCP tools, upload + blob routes |
| `mcp-research/fetch.py` | New — `requests` preflight, Playwright HTML fetch (custom UA, settle-wait), direct download for non-HTML |
| `mcp-research/extract.py` | New — trafilatura, markitdown, zip unpack preserving tree |
| `mcp-research/storage.py` | New — fsspec-backed blob read/write, `index.json` manifest |
| `mcp-research/blobtoken.py` | New — signed opaque token mint/verify |
| `graph-explorer/api/routes/ingest_nodes.py` | Add `source_url` support (fetch via `requests` when given) |
| `graph-explorer/api/app.py` | Expose new `ingest_text` tool alongside existing MCP tools |
| `docker-compose.yml` | Add `mcp-research` service |
| `docker-compose.dev.yml` | Add dev overrides if any |
| `graph-explorer/frontend/nginx.conf` | Add `/mcp-research`, `/mcp-research/upload`, `/mcp-research/blob` locations |
| `graph-explorer/frontend/nginx.dev.conf` | Same, dev variant |

## Docs to update

- `docs/architecture/index.md` — mention the second, independent MCP
  service and that it has no Ariadne coupling
- `docs/api/rest.md` — document `ingest_text` and `source_url` alongside
  the existing `/api/ingest/nodes` and `/mcp` entries
- `docs/implemented-plans/index.md` — add entry when this plan is moved
