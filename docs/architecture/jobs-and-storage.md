# Jobs, storage and links

Every tool that produces bulk content returns a **job**: `{job_id, files, text?, text_path?}`.
See also: [Plan: Tool file outputs become jobs](../implemented-plans/file-outputs-as-jobs.md),
[Plan: Jobs for all tools](../implemented-plans/jobs-for-all-tools.md), [Plan: Pluggable backends](../implemented-plans/backend-plugins.md) (job helpers live in the SDK, file-producing tools in the drivers)

## Which tools produce jobs
`FILE_TOOLS` in the browser driver (`spider_hoarder_browser_sdk/driver.py`; the Android driver has its own, `device_*`): `browser_take_screenshot`, `browser_pdf_save`, `browser_snapshot`, `browser_find`, `browser_evaluate`,
`browser_console_messages`, `browser_network_request_get`, `browser_network_request_make` (not a reader, but its output is a job), `browser_network_requests`, `browser_storage_state`, plus `browser_save_page`, `ingest_page` and uploads.
A client-supplied `filename` only renames the file inside the job; the MCP client never controls a host path. A file the session wrote is read
by the gateway from `out/` (opened through an `O_PATH` home fd, then `O_NOFOLLOW`, regular files only) and streamed into storage in 1 MiB chunks, **before** the session's homedir is wiped. Stored files have no size
cap; parsing does (`MAX_PARSE_BYTES`, enforced in the parser, default 50 MB: a bigger file is kept under `original/` with a `notes` entry, not parsed).
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
fails midway), and the session-end wipe covers the rest. `index.json` keeps the `body` argument as given, so a big upload is not stored twice.
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
parsed/<path>.md     markitdown output (<path> may be nested, e.g. response/body.html)
simplified/<name>.md trafilatura main-content guess (HTML only, may silently omit text; never returned inline)
terse/<name>.md      outline: headings, links, images of parsed/ (or of a Markdown original)
metadata/<path>.yaml file metadata (EXIF/GPS, Office revisions, CRS, media streams, PDF history); absent if there is none
index.json           {files: [...], url?, target?, text_cap?, depth?, query?, method?, status?, and for
                     browser_network_request_make: type, origin?, body?, redirects?, blocked?}
```
`text` is inline if the parsed/original text is under `INLINE_LIMIT` (50,000 chars); else the terse outline if that fits (with
`text_path` naming it); else omitted, and the MCP client pages with `get_file_text` or searches with `grep_file`.
See also: [Plan: Terse outline view](../implemented-plans/terse-view.md)

## The parser service
Nothing that interprets an untrusted file runs in a gateway. Every stored original is parsed by the shared **parser service**
(`parser/`, `docker-compose.parse.yml`), one parse per **job**: the gateway stores the job's files under `original/` (`jobs._store_original` /
`_store_tree`, which keep the manifest), then `jobs._parse_job` (`gateway/parse_client.py`) mints the job's scoped credential, `POST`s
`<PARSE_URL>/jobs/<proj>/<session>/<job>` (`Authorization: Bearer $PARSE_TOKEN`, body `{credentials, base_url?, features?}`) and polls the same URL
(50 ms growing to 1 s, 12 min overall; the MCP tool call stays synchronous), then reads `index.json` and the artefacts from the store itself.
No file passes through the gateway. `features` is `{"ocr": "<profile>"}` or absent, set from the backend's `<NAME>_PARSE_OCR`; an unknown feature
or profile is a 400. A job may be parsed again (an upload link adds files over time): the same artefacts are written again. Answers: **202**,
**409** (running with a fresh heartbeat), **503** (no free parser uid: the gateway retries once; so on a refused connection).

Inside the parser container two trust levels share one kernel:
- the **wrapper** (own uid, keeps `CAP_SETUID/SETGID/CHOWN/KILL`, no_new_privs) holds the wrapper token, the proxy token, the OCR profile table and, per job
  and only in memory, the scoped store credential. It pulls `original/` (through the egress proxy) into a scratch directory, starts the parser as a
  **pool uid** (`PARSE_POOL_SIZE` = 4 per container; uids `PARSE_UID_BASE` = 20000 and up; `free | busy | quarantined`), reads its records, validates them and uploads;
- the **parser** (`python -I -u -m spider_hoarder_parser.worker`, one per job, `setsid`, `umask 007` (group = the wrapper, via a setgid `out/`), `RLIMIT_AS` `PARSE_RLIMIT_AS` = 6 GiB (2 GiB was too tight for a real page) / `NPROC` / `NOFILE` / `FSIZE` 4 GiB / no core,
  no_new_privs, wall clock `PARSE_JOB_TIMEOUT` = 10 min then the group is killed) has **no blob credential and no token**; its environment is `PYTHONUNBUFFERED`,
  `PATH`, `HOME`/`TMPDIR` (its work dir) and, only if the job asked for remote OCR, that one profile's `OCR_URL` + `OCR_TOKEN`. It has no route out: the
  container's networks are internal and hold only the gateways, the proxy and the OCR sidecar (it can connect to those; each refuses it without its token).

The parser walks `original/`, runs the one parse rule on every file (metadata first, then archive unpack, markitdown / trafilatura / binary decoders,
`terse/`; members are parsed once and never unpacked again, `<x>.d/` members already next to their archive are not unpacked either) and writes artefacts into its
`out/` directory, announcing each on a private dup of its stdout pipe (stdout itself is dup'ed onto stderr before anything is imported): one JSON line per
record, `{"path", "content_type"}` (an artefact), `{"path", "text": true}` (the manifest entry of that original is plain text the gateway may return
inline) or `{"note"}`. The wrapper validates each record (path under `original|parsed|simplified|terse|metadata/`, no `..`/absolute/`index.json`;
the file opened component by component with `O_NOFOLLOW`, regular files only, size from its own `fstat`; line ≤ 4096 bytes, record count, total bytes and
notes capped), uploads it **while the parser still runs** and deletes it. Any violation kills the parser and fails the job. Afterwards the wrapper kills every
process of the uid, `lchown`s and deletes its directories and anything it left in `/tmp`, `/var/tmp`, `/dev/shm`, and frees the uid only if it owns nothing
(else it is quarantined). The scratch area is ephemeral (an anonymous volume of the task).

The parser process's environment is built by hand; of the wrapper's own, only the non-secret limits `MAX_PARSE_BYTES`, `MAX_ARCHIVE_BYTES`, `MAX_ARCHIVE_MEMBERS`, `METADATA_*` and `OCR_TIMEOUT` are forwarded (`wrapper.PARSER_LIMIT_ENV`; set in `.env`, see [deploy and configure](../operations/deploy-and-configure.md#configuration-env-see-envexample)).

`index.json` is written once at the end (the existing manifest, which the gateway wrote, plus the new `{path, size, content_type}` entries, `text` marks and `notes`),
**before** `status.json` = `{status, owner_replica, updated}` (`running` rewritten every 10 s; older than 30 s = "wrapper lost" and the job may be restarted). Any replica
answers `GET` from memory or from `status.json` with the credential the gateway sends. A failed job keeps its uploaded artefacts, an `index.json` noting
`not parsed (<why>)` and the original; **parsing never fails a gateway job and never loses the original** (service down, 503 after the retry, `failed`, poll timeout:
a note). Plain text gets no `parsed/` copy: the manifest marks the original as text and `text` comes from `original/<path>`.

The gateway's inline text (`jobs._job_result(key, main, notes)`): for the original `main`, `parsed/<main>.md|.json`, else a text-marked `original/<main>`, if its
manifest size is under `INLINE_LIMIT`; else `terse/<main>.md` (or `terse/<main>`) if that fits. `get_file_text`, `grep_file` (`content/grep.py`), `get_file_url`
and the tool signatures are unchanged.

Egress and the store: the wrapper reaches the store only through the **blob-sidecar** (squid 6, HTTP Basic with the proxy token on every request, 407 without;
`http` and `https` CONNECT; no destination allowlist) whose own egress rules, a copy of the browser-clear ones (public internet only; `blob-sidecar/egress_rules.py`),
reject private ranges, link-local/metadata, CGNAT and loopback; `PARSE_PROXY_ALLOW` (dev: `seaweedfs:8333`) is the one exception, for exactly that address and port.
Remote OCR is the **ocr-sidecar** (stateless forwarder, own pinned egress, optional): see [Plan: parser wrapper](../implemented-plans/parser-wrapper.md).
See also: [Plan: parser wrapper](../implemented-plans/parser-wrapper.md)

### File metadata
Every stored file (archive members too, and files over `MAX_PARSE_BYTES`: the extractors read headers) gets `metadata/<path>.yaml`
(`application/yaml`, listed in `files` like `terse/`; `text` and the tool signatures are unchanged). No metadata → no file; a file
whose only finding is its type (ExifTool's `File` group) counts as none.

Top-level keys are groups. ExifTool's own (`exiftool -json -g1 -struct`: `EXIF`, `XMP-dc`, `QuickTime`, `File`, ...) pass through as
produced, minus the filesystem facts of the staged copy (`System`, `ExifTool`, file name/size/times); binary tags are not embedded.
Our extensions are further groups named `SpiderHoarder-<Format>`, values arbitrarily nested:

| Group | Files | Content | Tool |
|---|---|---|---|
| `SpiderHoarder-Raster` | GeoTIFF/GDAL rasters | `CRS` (`EPSG`, `WKT`), `BBox`, `BBoxWGS84` (only with a CRS), `Resolution`, `Size`, `Bands`, `NoData`, `Tags` | `rasterio` |
| `SpiderHoarder-Vector` | GPX, KML, GeoJSON, GeoPackage, shapefile zip | `CRS`, `BBox`, `BBoxWGS84`, `Layers` (name, geometry type, features) | `pyogrio` |
| `SpiderHoarder-Office` | docx/xlsx/pptx | `Revisions` (docx tracked changes: author, date, insert/delete, text), `Comments`, `LastModifiedBy`, `RevisionCount` | `zipfile` + `lxml` (no entity expansion, size/ratio limits) |
| `SpiderHoarder-PDF` | pdf | `IncrementalUpdates` (`%%EOF` count − 1; a linearized PDF reads one too high), XMP `History`, `Producer`, `Creator`, `CreatorTool` | `pypdf` + raw scan |
| `SpiderHoarder-Media` | audio/video | `Duration`, `Streams`, `Chapters`, `Tags` | `ffprobe` |

Code: `parser/spider_hoarder_parser/metadata.py`, run by the parser on the pulled `original/<path>` (so big files work). Each extractor runs in its own child
process inside the parser (`prlimit` address space `METADATA_MAX_MEM` = 1 GiB, CPU and output-size limits; wall clock `METADATA_TIMEOUT` = 30 s, killed on
overrun; scrubbed environment). Isolation is the parser container (no egress, a pool uid that holds nothing), not a per-extractor network namespace
(`unshare` is blocked by Docker's default seccomp profile, so the old `unshare -rn` never applied in the shipped images). `ffprobe` runs with
`-protocol_whitelist file`; `.vrt` rasters are not opened. A failing extractor is a note (`"<path>: metadata <extractor> failed (<error>)"`),
never a failed job; the other extractors' output is still written. The YAML is capped at `METADATA_MAX_BYTES` (1 MiB): the longest
list of an extension group is halved until it fits, with `Truncated: true` in the group and a note.
See also: [Plan: file metadata](../implemented-plans/file-metadata.md) (its isolation section is superseded), [Plan: parser wrapper](../implemented-plans/parser-wrapper.md)

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
- **Scoped store credentials** (`gateway/creds.py`): for components that touch the store themselves (the parser wrapper, [Plan: parser
  wrapper](../implemented-plans/parser-wrapper.md)) the gateway mints a short-lived credential (900 s) limited to read `projects/<proj>/data/*` and
  write `projects/<proj>/data/<session>/<job>/*`, and hands it over as plain fsspec options (`key`, `secret`, `token`; GCS: `token`). S3 /
  SeaweedFS: a **new role per job** whose own policy is exactly that (no broad role behind it, no session policy to forget), assumed
  by the gateway; deleting the role revokes the credential at once, so it ends with the job (`Scoped.release`) and with the project
  (`revoke_project`), and a sweeper (every 5 min) deletes roles a crashed gateway left behind. GCS: a Credential Access Boundary
  (downscoped token; narrow by construction, not revocable). Every failure raises: no fallback to the gateway's own credential. The gateway
  needs the right to create roles (SeaweedFS: the `Admin` action). Sessions hold no store credentials (the gateway writes their `out/` files for
  them). Verified on SeaweedFS 4.53 (`sdk/tests/test_creds.py`); `gateway/parse_client.py` mints one per parse and releases it after the poll.
- **The stack's SeaweedFS** (`SEAWEEDFS=1`, default with `--dev`; `docker-compose.seaweedfs.yml`): pinned image, S3 on its own overlay network that
  the gateways join, no published port; `deploy.sh` generates its config from `SEAWEEDFS_*`, creates the bucket with `weed shell` and gives the gateways
  `RESEARCH_BLOB_URL` / `RESEARCH_BLOB_OPTIONS`. Its roles live in memory: a restart ends the credentials in flight (the consumer re-mints).
  See also: [Plan: project access model](../implemented-plans/project-access-model.md)
- Every archive (upload, `ingest_page` download, network response body) is unpacked to `original/<archive path>.d/<relpath>`, members
  parsed as any file (`parsed/<archive path>.d/<relpath>.md`); archives inside archives are only stored. Detected by extension or
  content type only (`archive_kind`): zip, tar (`.tar .tgz .tar.gz .tbz .tbz2 .tar.bz2 .txz .tar.xz`, streamed), 7z (`py7zr`),
  rar (`rarfile`, needs `unar` in the parser image) and single-file `.gz/.bz2/.xz` (one member named without the suffix). Only regular files
  are extracted: names with an absolute path or `..` are dropped, symlinks/hardlinks/devices/FIFOs are skipped and listed in `notes`
  (`"<name> skipped (symlink)"`), never followed (`parser/tests/test_archive_security.py`). Limits, enforced while extracting: `MAX_ARCHIVE_MEMBERS` (50,000)
  and `MAX_ARCHIVE_BYTES` (4 GiB uncompressed, counted from bytes actually read; `MAX_ZIP_*` still read as fallbacks); an aborted unpack
  is reported in `notes`. 7z members are extracted into memory in 64 MiB batches (by declared size). One parse rule serves every
  caller (`parser/spider_hoarder_parser/parse.py`, run in the parser service). The archive is unpacked in the parser, never in a gateway.
  See also: [Plan: network request jobs](../implemented-plans/network-request-jobs.md), [Plan: more file formats](../implemented-plans/more-file-formats.md)
- Binary structured data is decoded to pretty-printed JSON at `parsed/<path>.json` (`parser/spider_hoarder_parser/extract.py` `binary_to_json`; the original is untouched,
  `.json` originals are not parsed): msgpack (`.msgpack .mp`), CBOR (`.cbor`), BSON (`.bson`, the PyPI `bson` package, concatenated
  documents give a list), plist (`.plist` or `bplist00` magic) and protobuf (`.pb`, `application/(x-)protobuf`, `application/grpc-web*`;
  gRPC-web frames de-framed, `-text` base64-decoded, trailers as text). Detection is by extension / content type, no content sniffing
  except the plist magic. Non-JSON values: bytes → `{"$base64": …}`, datetimes → ISO 8601, NaN/Inf and unknown types → string, sets →
  lists, non-string keys → `str()`. Protobuf is schema-less (`blackboxprotobuf`): `{"message": {field number: value}, "typedef": {guessed
  types}}`. A decode failure is noted in `notes`, the job does not fail.
  See also: [Plan: more file formats](../implemented-plans/more-file-formats.md)
- No retention/cleanup exists yet (out of scope of the plans).
