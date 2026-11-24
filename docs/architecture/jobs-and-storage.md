# Jobs, storage and links

Every tool that produces bulk content returns a **job**: `{job_id, files, text?, text_path?}`.
See also: [Plan: Tool file outputs become jobs](../implemented-plans/file-outputs-as-jobs.md),
[Plan: Jobs for all tools](../implemented-plans/jobs-for-all-tools.md), [Plan: Pluggable backends](../implemented-plans/backend-plugins.md) (job helpers live in the SDK, file-producing tools in the drivers)

## Which tools produce jobs
`FILE_TOOLS` in the browser driver (`spider_hoarder_browser_sdk/driver.py`; the Android driver has its own, `device_*`): `browser_take_screenshot`, `browser_pdf_save`, `browser_snapshot`, `browser_find`, `browser_evaluate`,
`browser_console_messages`, `browser_network_request_get`, `browser_network_request_make` (not a reader, but its output is a job), `browser_network_requests`, `browser_storage_state`, plus `browser_save_page`, `ingest_page` and uploads.
A client-supplied `filename` only renames the file inside the job; the MCP client never controls a host path. A file the session wrote is read
by the gateway from `out/` (opened through an `O_PATH` home fd, then `O_NOFOLLOW`, regular files only) and streamed into storage in 1 MiB chunks, **before** the session's homedir is wiped. Stored files have no size
cap; parsing does (`MAX_PARSE_BYTES`, enforced by the default `documents`, `html`, `binary`, `markdown` and `archives` processors, default 50 MB: a bigger file is kept under `original/` with a `notes` entry, not parsed; the parser service's `PARSE_LIMITS` bound the whole job).
`browser_network_request_get`/`_make` output a directory: only `request|response/headers.json` and `body.<ext>` are read, anything else is ignored.
See also: [Plan: network request jobs](../implemented-plans/network-request-jobs.md)

**`in/`**: for `browser_network_request_make` the gateway resolves every `{job_id, path}` in `body` (read role in the job's project, `..`/absolute paths and
missing keys rejected) and checks all sizes before copying anything: `navigation` bodies (files, text and encoded fields together) against
`MAX_REQUEST_BODY` (env, default 64 MiB: Chromium refuses a CDP message over 100 MiB, i.e. ~75 MiB of body, by killing the whole browser
context; measured 74 MiB ok, 76 not), the file parts of `form`/`xhr` against `MAX_UPLOAD_BYTES` (env, default 500 MiB; 64 MiB in a session
whose policy blocks media). Each file is streamed (`storage.open_read`) to `<home>/in/<random>/<filename>` (same `O_PATH` home fd,
`O_NOFOLLOW`, directory `0777` so the session user can read and delete it, file `O_EXCL` `0644`; one directory per file, so equal filenames
never collide and the name is exactly what the browser uploads). The job references in the arguments are replaced by the staged names
(`body_file` for a raw file body, `{file, filename, content_type?}` for a file part); any client-supplied `body_file` is discarded. The
toolserver deletes the directories when the call ends, success or failure; the gateway removes them again as a backstop (also when staging
fails midway), and the session-end wipe covers the rest. `meta.json` keeps the `body` argument as given, so a big upload is not stored twice.
See also: [Plan: network request make types](../implemented-plans/network-request-make-types.md)

## Layout
fsspec storage in **one shared bucket** (`RESEARCH_BLOB_URL`: `gs://…` or `s3://…`, with `RESEARCH_BLOB_OPTIONS`, a JSON object handed to
fsspec as is: credentials, endpoint, ...; the stack can run its own SeaweedFS, `SEAWEEDFS=1`). There is no local-disk or in-memory store: every replica
must see the same files, so any gateway replica serves any job. Keys are built in one place (`storage.job_key`):

```
projects/<proj>/access.json                       {name, created, users: {<email>: admin|write|read}}   (gateway only)
projects/<proj>/data/<session>/<job>/…            the job files below
users/<user>/<proj>                               index for "my projects" (a hint only; <user> = lowercased email, %XX-encoded)
```
`<proj>`, `<session>`, `<job>` are random UUIDs, validated wherever one arrives, so no path component can hold `/` or `..`. A **job id** is
`<proj>/<session>/<job>` (never the owner). `<session>` is one uuid per MCP session, created at its first job. `access.json` sits beside
`data/`, so no per-job credential can read or change it. Under a job:

```
original/<path>      the bytes as received (for HTML: the converted HTML, i.e. the target element's if given)
<dir>/<path...>      whatever the installed processors wrote, `<dir>` being one of their top directories; nothing here lists them, `index.json` is the
                     truth. Today, among others: parsed/<path>.md (markitdown; <path> may be nested, e.g. response/body.html), simplified/<name>.md (trafilatura
                     main-content guess, may silently omit text; never returned inline), terse/<name>.md (outline: headings, links, images of parsed/ or of a
                     Markdown original), metadata/<path>.yaml (EXIF/GPS, Office revisions, CRS, media streams, PDF history), media/<path>/ (thumbnail,
                     keyframes, audio.opus, renditions, frame store), asr/, caption/, frame-ocr/ (see the library's docs/reference.md)
index.json           the parser library's manifest, the job's only one: {files: {<input path under original/>: {size, content_type?, meta?, notes?,
                     outputs: {<path in the job>: {size, content_type, meta?}}, files?: {<archive member>: …}}}, notes?}
status.json          {status, owner_replica, updated, error?, traceback?, usage?}: the parser service's heartbeat and result; `usage` (written once the job ends)
                     is the sidecars' own record of each model call of the job: {calls: [{feature, profile, model, outcome, unit, input, output, t}],
                     truncated, error?}, only when a sidecar with an admin token served the job (see reference.md#statusjson-and-indexjson of the library)
meta.json            the gateway's own keys: {url?, target?, text_cap?, depth?, query?, method?, status?, content_types: {<original>: type}, and for
                     browser_network_request_make: type, origin?, body?, redirects?, blocked?}
```
`list_files` returns the index with the meta keys (minus `content_types`) merged in; every output the parser announced is in it and readable by its path
(`get_file_text`, `grep_file`, `get_file_url`): the gateway filters none. Only the inline `text` picks `parsed/` or `terse/` by name. Before any parse, or when none could be written, the index is built from
the stored originals; a job from before the parser library (a list `files` and the meta keys at the top of `index.json`) reads as no index and its meta keys.

**Usage.** When a sidecar has an admin token (deploy.sh always sets one), the parser writes `usage` (`calls`, `truncated`, `error?`) into the job's `status.json`. The MCP tool `get_usage(project, job_id?)` returns it for a job, or summed over all jobs of the project (one row per feature, profile, model, outcome and unit; `tokens` and `audio_seconds` never mixed; jobs without usage counted in `jobs` only). No prices. It reads `status.json` of each job on request, no state kept.
See also: [Plan: usage reporting](../implemented-plans/usage-reporting.md)
`text` is inline if the parsed/original text is under `INLINE_LIMIT` (50,000 chars); else the terse outline if that fits (with
`text_path` naming it); else omitted, and the MCP client pages with `get_file_text` or searches with `grep_file`.
See also: [Plan: Terse outline view](../implemented-plans/terse-view.md)

## The parser service
Nothing that interprets an untrusted file runs in a gateway. Every stored original is parsed by the shared **parser service**, which is a library in its own
repository, **cadmus-parser**, included as the git submodule `lib/cadmus-parser/` (its documentation, in `lib/cadmus-parser/docs/`, is the reference for the wrapper API,
`index.json`, the worker protocol, the limits, the sidecar protocol and the security model; this section is only what is specific to this project). Each stack runs its own
instance. The library's parts used here: the wrapper and its uid pool, the library's own `cadmus-parser` image (`server/Dockerfile`: ExifTool, ffmpeg,
Tesseract, `unar` and the default processors, the `files` extra: `metadata`, `archives`, `html`, `documents` ...), `blob-sidecar`, `ocr-sidecar`, the stack file `docker-compose.parse.yml`, the overlay generator and the
client (`cadmus_parser`, in the gateway images).

One parse per **job**: the gateway stores the job's files under `original/` (`jobs._store_original` / `_store_tree`, which remember each file's content type in `meta.json`), then
`jobs._parse_job` (`gateway/parse_client.py`, a thin layer over the library client):

1. mints the job's scoped credential (`gateway/creds.py`: one read grant for the project's `data/` (a parser may correlate with earlier files of the same
   project) and one write grant for `<job>/`, 900 s; S3: a role per store, revoked after the job; GCS: a downscoped token),
2. `POST`s `<PARSE_URL>/jobs` with `input.url = <store>/<job>/original`, `output.url = <store>/<job>`, each side's credential, `options = {"files": {base_url,
   ctypes}}` and `features` = `ocr`, `asr`, `asr-fallback`, `caption`, `caption-fallback` -> profile, from the backend's `<NAME>_PARSE_OCR`, `_PARSE_ASR`, `_PARSE_ASR_FALLBACK`, `_PARSE_CAPTION`, `_PARSE_CAPTION_FALLBACK` (only those set), or none; one retry on a 503 or a refused connection,
3. starts `renew_loop`, which pushes fresh credentials to the replica that took the job every 5 minutes (the grant is project-wide, so renewals are re-keyed to the
   posted input URL; every minted handle is tracked in `creds.active` for `revoke_project`),
4. waits for `status.json` under `<job>/` (the library's heartbeat rule; 12 min overall, the MCP tool call stays synchronous) and reads `index.json` and the artefacts from
   the store itself. No file passes through the gateway.

A job may be parsed again (an upload link adds files over time): a fresh `index.json` of everything stored, with stored archive members (`<x>.d/…`) as inputs of their own and
their archive not unpacked again. **Parsing never fails a gateway job and never loses the original**: service down, 503 after the retry, `failed`, poll timeout: a note
(`not parsed (<why>)`, the `error` of `status.json` when there is one) and the stored originals still listed.

The gateway's inline text (`jobs._job_result(key, main, notes)`): for the original `main` (its entry of `index.json`), `parsed/<main>.md|.json`, else the original itself when
a default processor marked it as text (`meta.text`), if its size is under `INLINE_LIMIT`; else `terse/<main>.md` (or `terse/<main>`) if that fits. Notes are the
index's, every entry's and the member entries'. `get_file_text`, `grep_file` (`content/grep.py`), `get_file_url` take paths in the job (`original/<name>`, `parsed/…`).

**Limits** are the library's per-class `PARSE_LIMITS` (see [deploy and configure](../operations/deploy-and-configure.md#configuration-env-see-envexample)), with finite
built-ins; this project sets today's per-job limits as `default.*` in `.env`. **Egress and the store**: the wrapper reaches the store only through the **blob-sidecar** (squid 6, HTTP
Basic with the proxy token on every request; public internet only, `PARSE_PROXY_ALLOW` = `seaweedfs:8333` in dev for exactly that address and port), the parser has no route out.
**Trust change from the first design:** the wrapper no longer builds the job's storage keys; the gateway sends complete base URLs and credentials, so the confinement to one job
rests on the credential (here the read side is project-wide, deliberately) and on the gateway building URLs only from validated ids; see the library's security guide.

**OCR** (`lib/cadmus-parser/server/cadmus_parser_server/ocr.py`, called by the `documents` processor for non-HTML files): standalone images (png, jpeg, webp, gif, tiff, bmp) and
PDFs with pages whose text layer has < `OCR_MIN_CHARS` (20) characters are OCR'd into `parsed/<path>.md`. Tesseract (`OCR_LANGS`, default `nor+eng`) runs
first; its result is accepted at mean word confidence >= `OCR_MIN_CONF` (70) and >= `OCR_MIN_CHARS` characters, otherwise, **only if the job has a remote OCR
profile**, the image (downscaled to `OCR_MAX_SIDE` 2000 px) goes to the **ocr-sidecar** (the common sidecar protocol: `POST /process`, the image as the body, the transcript as the
`text/plain` response; stateless, own pinned egress, optional) and its text replaces Tesseract's. Without a profile Tesseract's text is used
whatever its confidence. A failed remote step keeps Tesseract's text. A scanned PDF is rebuilt per page from pdfium's text, OCR'd pages marked
`<!-- page N (ocr) -->` (rasterised at `OCR_DPI` 200, at most `OCR_MAX_PAGES` 50 per file). Provenance is always in the job's `notes`
(`<path>: ocr tesseract (conf 91)`, `ocr remote`, `ocr tesseract (conf 38), remote failed: TimeoutError`, `page N ocr …`, `ocr stopped at page N`); an image over
Pillow's pixel limit is noted and not OCR'd. Privacy: images leave the swarm only for a backend whose `<NAME>_PARSE_OCR` names a profile. Tuning variables are
global, forwarded by the wrapper to the parser. See also: [Plan: OCR hybrid](../implemented-plans/ocr-hybrid.md)

See also: [Plan: parser wrapper](../implemented-plans/parser-wrapper.md), [Plan: the parser service as a library](../implemented-plans/parse-sandbox-library.md)

### File metadata
Every stored file (archive members too, and files over `MAX_PARSE_BYTES`: the extractors read headers) gets `metadata/<path>.yaml`
(`application/yaml`, listed in `files` like `terse/`; `text` and the tool signatures are unchanged). No metadata → no file; a file
whose only finding is its type (ExifTool's `File` group) counts as none.

Top-level keys are groups. ExifTool's own (`exiftool -json -g1 -struct`: `EXIF`, `XMP-dc`, `QuickTime`, `File`, ...) pass through as
produced, minus the filesystem facts of the staged copy (`System`, `ExifTool`, file name/size/times); binary tags are not embedded.
Our extensions are further groups named `Cadmus-<Format>`, values arbitrarily nested:

| Group | Files | Content | Tool |
|---|---|---|---|
| `Cadmus-Raster` | GeoTIFF/GDAL rasters | `CRS` (`EPSG`, `WKT`), `BBox`, `BBoxWGS84` (only with a CRS), `Resolution`, `Size`, `Bands`, `NoData`, `Tags` | `rasterio` |
| `Cadmus-Vector` | GPX, KML, GeoJSON, GeoPackage, shapefile zip | `CRS`, `BBox`, `BBoxWGS84`, `Layers` (name, geometry type, features) | `pyogrio` |
| `Cadmus-Office` | docx/xlsx/pptx | `Revisions` (docx tracked changes: author, date, insert/delete, text), `Comments`, `LastModifiedBy`, `RevisionCount` | `zipfile` + `lxml` (no entity expansion, size/ratio limits) |
| `Cadmus-PDF` | pdf | `IncrementalUpdates` (`%%EOF` count − 1; a linearized PDF reads one too high), XMP `History`, `Producer`, `Creator`, `CreatorTool` | `pypdf` + raw scan |
| `Cadmus-Media` | audio/video | `Duration`, `Streams`, `Chapters`, `Tags` | `ffprobe` |

Code: `lib/cadmus-parser/server/cadmus_parser_server/metadata.py`, run by the `metadata` processor in the parser on the pulled `original/<path>` (so big files work). Each extractor runs in its own child
process inside the parser (`prlimit` address space `METADATA_MAX_MEM` = 1 GiB, CPU and output-size limits; wall clock `METADATA_TIMEOUT` = 30 s, killed on
overrun; scrubbed environment). Isolation is the parser container (no egress, a pool uid that holds nothing), not a per-extractor network namespace
(`unshare` is blocked by Docker's default seccomp profile, so the old `unshare -rn` never applied in the shipped images). `ffprobe` runs with
`-protocol_whitelist file`; `.vrt` rasters are not opened. A failing extractor is a note (`"<path>: metadata <extractor> failed (<error>)"`),
never a failed job; the other extractors' output is still written. The YAML is capped at `METADATA_MAX_BYTES` (1 MiB): the longest
list of an extension group is halved until it fits, with `Truncated: true` in the group and a note.
See also: [Plan: the parser service as a library](../implemented-plans/parse-sandbox-library.md) (the `Cadmus-*` groups), [Plan: file metadata](../implemented-plans/file-metadata.md) (its isolation section is superseded), [Plan: parser wrapper](../implemented-plans/parser-wrapper.md)

## HTML conversion
markitdown converts the whole page or the `target` element losslessly; trafilatura runs on the same HTML into `simplified/`.
Rationale: trafilatura silently drops content (measured), and the client is better placed to choose the container, via
`browser_find`. Consequence: default output is larger and noisier.
See also: [Plan: save-page target](../implemented-plans/save-page-target.md)

## Access and links
- Jobs belong to **projects** with per-user roles (`read`, `write`, `admin`; [Auth](auth.md#projects-and-roles)). Every call goes through
  `jobs._key(owner, job_id, need)`, which checks the caller's role in `projects/<proj>/access.json` (cached 30 s per replica,
  `ACCESS_CACHE_SECONDS`); no such project and no role both answer "project not found". Job-creating tools need `write` and take a
  `project` argument, optional when `open_session(project)` bound the MCP session; the job's `<session>` directory is that MCP session's.
  Jobs are not tied to a replica ([Scaling](scaling.md)); a project can be deleted (all its sessions and jobs), nothing smaller.
- `get_file_url` returns an HMAC-signed, short-lived (15 min) download link `…/blob/<token>`, streamed by the gateway from the bucket (a
  recipient needs no account and no network path to the store); `get_upload_link(project?)` an upload page link (1 h) for a new job, so
  uploads need no SSO and write only into that job. The token is **signed, not encrypted**: it carries `<proj>/<session>/<job>/<path>`
  (no email) and anyone holding the link can read it. Secret: `BLOB_TOKEN_SECRET`. A deleted project takes its upload links with it.
- **Scoped store credentials** (`gateway/creds.py`, this project's policy on the cadmus-parser minting helper `cadmus_parser.minting`): for components that touch the store themselves (the parser wrapper, [Plan: the parser service as a
  library](../implemented-plans/parse-sandbox-library.md)) the gateway mints a short-lived credential (900 s) limited to read `projects/<proj>/data/*` and
  write `projects/<proj>/data/<session>/<job>/*`, and hands it over as plain fsspec options (`key`, `secret`, `token`; GCS: `token`). S3 /
  SeaweedFS: a **new role per job** whose own policy is exactly that (no broad role behind it, no session policy to forget), assumed
  by the gateway; deleting the role revokes the credential at once, so it ends with the job (`creds.release`) and with the project
  (`revoke_project`), and a sweeper (every 5 min) deletes roles a crashed gateway left behind. GCS: a Credential Access Boundary
  (downscoped token; narrow by construction, not revocable). Every failure raises: no fallback to the gateway's own credential. The gateway
  needs the right to create roles (SeaweedFS: the `Admin` action). Sessions hold no store credentials (the gateway writes their `out/` files for
  them). Verified on SeaweedFS 4.53 (`sdk/tests/test_creds.py`); `gateway/parse_client.py` mints one per parse, renews it while the job runs and revokes it after.
- **The stack's SeaweedFS** (`SEAWEEDFS=1`, default with `--dev`; `docker-compose.seaweedfs.yml`): pinned image, S3 on its own overlay network that
  the gateways join, no published port; `deploy.sh` generates its config from `SEAWEEDFS_*`, creates the bucket with `weed shell` and gives the gateways
  `RESEARCH_BLOB_URL` / `RESEARCH_BLOB_OPTIONS`. Its roles live in memory: a restart ends the credentials in flight (the client re-mints).
  See also: [Plan: project access model](../implemented-plans/project-access-model.md)
- Every archive (upload, `ingest_page` download, network response body) is unpacked to `original/<archive path>.d/<relpath>`, members
  parsed as any file (`parsed/<archive path>.d/<relpath>.md`); archives inside archives are only stored. Detected by extension or
  content type only (`archive_kind`): zip, tar (`.tar .tgz .tar.gz .tbz .tbz2 .tar.bz2 .txz .tar.xz`, streamed), 7z (`py7zr`),
  rar (`rarfile`, needs `unar` in the parser image) and single-file `.gz/.bz2/.xz` (one member named without the suffix). Only regular files
  are extracted: names with an absolute path or `..` are dropped, symlinks/hardlinks/devices/FIFOs are skipped and listed in `notes`
  (`"<name> skipped (symlink)"`), never followed (`lib/cadmus-parser/server/tests/test_archive_security.py`). Limits, enforced while extracting: `MAX_ARCHIVE_MEMBERS` (50,000)
  and `MAX_ARCHIVE_BYTES` (4 GiB uncompressed, counted from bytes actually read; `MAX_ZIP_*` still read as fallbacks); an aborted unpack
  is reported in `notes`. 7z members are extracted into memory in 64 MiB batches (by declared size). One parse rule serves every
  caller (`lib/cadmus-parser/server/cadmus_parser_server/parse.py`, the `files` processor, run in the parser service). The archive is unpacked in the parser, never in a gateway.
  See also: [Plan: network request jobs](../implemented-plans/network-request-jobs.md), [Plan: more file formats](../implemented-plans/more-file-formats.md)
- Binary structured data is decoded to pretty-printed JSON at `parsed/<path>.json` (`lib/cadmus-parser/server/cadmus_parser_server/extract.py` `binary_to_json`; the original is untouched,
  `.json` originals are not parsed): msgpack (`.msgpack .mp`), CBOR (`.cbor`), BSON (`.bson`, the PyPI `bson` package, concatenated
  documents give a list), plist (`.plist` or `bplist00` magic) and protobuf (`.pb`, `application/(x-)protobuf`, `application/grpc-web*`;
  gRPC-web frames de-framed, `-text` base64-decoded, trailers as text). Detection is by extension / content type, no content sniffing
  except the plist magic. Non-JSON values: bytes → `{"$base64": …}`, datetimes → ISO 8601, NaN/Inf and unknown types → string, sets →
  lists, non-string keys → `str()`. Protobuf is schema-less (`blackboxprotobuf`): `{"message": {field number: value}, "typedef": {guessed
  types}}`. A decode failure is noted in `notes`, the job does not fail.
  See also: [Plan: more file formats](../implemented-plans/more-file-formats.md)
- No retention/cleanup exists yet (out of scope of the plans).
