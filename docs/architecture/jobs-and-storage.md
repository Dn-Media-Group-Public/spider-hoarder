# Jobs, storage and links

Every tool that produces bulk content returns a **job**: `{job_id, files, text?, text_path?}`.
See also: [Plan: Tool file outputs become jobs](../implemented-plans/file-outputs-as-jobs.md),
[Plan: Jobs for all tools](../implemented-plans/jobs-for-all-tools.md)

## Which tools produce jobs
`FILE_TOOLS` in `app.py`: `browser_take_screenshot`, `browser_pdf_save`, `browser_snapshot`, `browser_find`, `browser_evaluate`,
`browser_console_messages`, `browser_network_request_get`, `browser_network_request_make` (not a reader, but its output is a job), `browser_network_requests`, `browser_storage_state`, plus `browser_save_page`, `ingest_page` and uploads.
A client-supplied `filename` only renames the file inside the job; the agent never controls a host path. A file the session wrote is read
by the gateway from `out/` (opened through an `O_PATH` home fd, then `O_NOFOLLOW`, regular files only) and streamed into storage in 1 MiB chunks, **before** the session's homedir is wiped. Stored files have no size
cap; parsing does (`MAX_PARSE_BYTES`, default 50 MB: a bigger file is kept under `original/` with a `notes` entry, not parsed).
`browser_network_request_get`/`_make` output a directory: only `request|response/headers.json` and `body.<ext>` are read, anything else is ignored.
See also: [Plan: network request jobs](../implemented-plans/network-request-jobs.md)

**`in/`**: for `browser_network_request_make` with `body_job_id`+`body_path` the gateway resolves the file (owner-scoped, `..`/absolute
paths and missing keys rejected, `MAX_REQUEST_BODY`, env, default 64 MiB, also for an inline `body`: Chromium refuses a CDP message over 100 MiB, i.e. ~75 MiB of body, by killing the whole browser context; measured 74 MiB ok, 76 not), writes it to `<home>/in/<random>.body` (same `O_PATH` home fd, `O_NOFOLLOW`,
file `O_EXCL`) and passes that name to the toolserver as a hidden `body_file` argument (any agent-supplied `body_file` is discarded). The
toolserver reads and deletes it; the gateway deletes it again afterwards as a backstop, and the session-end wipe covers the rest.
See also: [Plan: network request make](../implemented-plans/network-request-make.md)

## Layout
fsspec storage (`RESEARCH_BLOB_URL`: `file:///data` or `gs://…`), keyed `<owner>/<uuid>`:

```
original/<path>      the bytes as received (for HTML: the converted HTML, i.e. the target element's if given)
parsed/<path>.md     markitdown output (<path> may be nested, e.g. response/body.html)
simplified/<name>.md trafilatura main-content guess (HTML only, may silently omit text; never returned inline)
terse/<name>.md      outline: headings, links, images of parsed/ (or of a Markdown original)
index.json           {files: [...], url?, target?, text_cap?, depth?, query?, method?, status?}
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
- Every archive (upload, `ingest_page` download, network response body) is unpacked to `original/<archive path>.d/<relpath>`, members
  parsed as any file (`parsed/<archive path>.d/<relpath>.md`); archives inside archives are only stored. Detected by extension or
  content type only (`extract.archive_kind`): zip, tar (`.tar .tgz .tar.gz .tbz .tbz2 .tar.bz2 .txz .tar.xz`, streamed), 7z (`py7zr`),
  rar (`rarfile`, needs `unar` in the image) and single-file `.gz/.bz2/.xz` (one member named without the suffix). Only regular files
  are extracted: names with an absolute path or `..` are dropped, symlinks/hardlinks/devices/FIFOs are skipped and listed in `notes`
  (`"<name> skipped (symlink)"`), never followed (`test_security.py`). Limits, enforced while extracting: `MAX_ARCHIVE_MEMBERS` (50,000)
  and `MAX_ARCHIVE_BYTES` (4 GiB uncompressed, counted from bytes actually read; `MAX_ZIP_*` still read as fallbacks); an aborted unpack
  is reported in `notes`. 7z members are extracted into memory in 64 MiB batches (by declared size). One parse rule serves every
  caller (`_store_one` / `_store_tree` in `app.py`).
  See also: [Plan: network request jobs](../implemented-plans/network-request-jobs.md), [Plan: more file formats](../implemented-plans/more-file-formats.md)
- Binary structured data is decoded to pretty-printed JSON at `parsed/<path>.json` (`extract.binary_to_json`; the original is untouched,
  `.json` originals are not parsed): msgpack (`.msgpack .mp`), CBOR (`.cbor`), BSON (`.bson`, the PyPI `bson` package, concatenated
  documents give a list), plist (`.plist` or `bplist00` magic) and protobuf (`.pb`, `application/(x-)protobuf`, `application/grpc-web*`;
  gRPC-web frames de-framed, `-text` base64-decoded, trailers as text). Detection is by extension / content type, no content sniffing
  except the plist magic. Non-JSON values: bytes → `{"$base64": …}`, datetimes → ISO 8601, NaN/Inf and unknown types → string, sets →
  lists, non-string keys → `str()`. Protobuf is schema-less (`blackboxprotobuf`): `{"message": {field number: value}, "typedef": {guessed
  types}}`. A decode failure is noted in `notes`, the job does not fail.
  See also: [Plan: more file formats](../implemented-plans/more-file-formats.md)
- No retention/cleanup exists yet (out of scope of the plans).
