# Jobs, storage and links

Every tool that produces bulk content returns a **job**: `{job_id, files, text?, text_path?}`.
See also: [Plan: Tool file outputs become jobs](../implemented-plans/file-outputs-as-jobs.md),
[Plan: Jobs for all tools](../implemented-plans/jobs-for-all-tools.md)

## Which tools produce jobs
`FILE_TOOLS` in `app.py`: `browser_take_screenshot`, `browser_pdf_save`, `browser_snapshot`, `browser_find`, `browser_evaluate`,
`browser_console_messages`, `browser_network_request(s)`, `browser_storage_state`, plus `browser_save_page`, `ingest_page` and uploads.
A client-supplied `filename` only renames the file inside the job; the agent never controls a host path. A file the session wrote is read
by the gateway from `out/` (opened through an `O_PATH` home fd, then `O_NOFOLLOW`, regular files only, capped by `MAX_FILE_BYTES`,
default 50 MB) and stored **before** the session's homedir is wiped.

## Layout
fsspec storage (`RESEARCH_BLOB_URL`: `file:///data` or `gs://…`), keyed `<owner>/<uuid>`:

```
original/<name>      the bytes as received (for HTML: the converted HTML, i.e. the target element's if given)
parsed/<name>.md     markitdown output
simplified/<name>.md trafilatura main-content guess (HTML only, may silently omit text; never returned inline)
terse/<name>.md      outline: headings, links, images of parsed/ (or of a Markdown original)
index.json           {files: [...], url?, target?, text_cap?, depth?, query?}
```
`text` is inline if the parsed/original text is under `INLINE_LIMIT` (50,000 chars); else the terse outline if that fits (with
`text_path` naming it); else omitted, and the agent pages with `get_file_text` or searches with `grep_file`.
See also: [Plan: Terse outline view](../implemented-plans/terse-view.md)

## HTML conversion
markitdown converts the whole page or the `target` element losslessly; trafilatura runs on the same HTML into `simplified/`.
Rationale: trafilatura silently drops content (measured), and the client is better placed to choose the container, via
`browser_find`. Consequence: default output is larger and noisier.
See also: [Plan: save-page target](../implemented-plans/save-page-target.md)

## Access and links
- Jobs are owner-scoped (the caller's email); another owner's job is "file not found". With replicas, `job_id` is `<rid>.<uuid>` and
  jobs live on the replica that created them ([Scaling](scaling.md)).
- `get_file_url` returns an HMAC-signed, short-lived (15 min) download link `…/blob/<rid>/<token>`; `get_upload_link` an upload page
  link (1 h) that carries the owner-scoped job key, so uploads need no SSO and cannot cross owners. Secret: `BLOB_TOKEN_SECRET`.
- Uploaded zips are unpacked preserving the tree, with zip-slip rejection (`test_security.py`).
- No retention/cleanup exists yet (out of scope of the plans).
