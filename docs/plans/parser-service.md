# Plan: parsing and metadata extraction in a separate, egress-less parser container

See also [Jobs, storage and links](../architecture/jobs-and-storage.md), [Backend plugins](../architecture/backend-plugins.md) (overlay contract),
[Plan: file metadata](../implemented-plans/file-metadata.md) (its child-process isolation is superseded by this), [OCR hybrid](ocr-hybrid.md)
(needs one amendment, see "Open decisions").

## Problem / Goal

Everything that interprets an untrusted file runs inside the gateway container as the gateway's own uid (1000): markitdown, trafilatura, archive
unpacking (`py7zr`, `rarfile`/`unar`), the binary decoders, and now ExifTool, `ffprobe`, GDAL, `lxml` and `pypdf`. A parser bug (memory corruption in a
C library, ExifTool-style eval bugs) or a feature (an `.m3u8` that makes `ffprobe` fetch URLs, a `.vrt` pointing at `/vsicurl/`) then runs next to
`BLOB_TOKEN_SECRET`, `/data` (every user's files), the storage credentials, `sessiond`'s socket and the internal networks. The file-metadata plan
contained this with `prlimit` + `unshare -rn`, but `unshare` is blocked by Docker's default seccomp profile, so in the shipped images there is no network
isolation, and the child still shares the uid and filesystem view of the gateway.

Goal: **all parsing, including metadata extraction, runs in a separate container that has no egress, no secrets, no storage access, and exactly one HTTP
endpoint** that takes a file and streams back the artefacts. The gateway keeps storage, auth, jobs and tools; it stops importing any parser.

## Design

### Topology

- New service `<backend>-parser` per backend, from one shared image `spider-hoarder-parser` (the overlay contract: every service is prefixed with the
  backend name, helpers are per backend, only code and images are shared; keeps Tor and clear traffic on separate networks).
- New network `<backend>-parse`: **`internal: true`** (no route out, no DNS beyond service names), attached **only** to `<backend>-gateway` and
  `<backend>-parser`. The parser is on no other network.
- Parser container: no environment secrets, no volumes except a scratch dir, read-only root filesystem, non-root uid, `no-new-privileges` (via the entrypoint's
  `setpriv`, since Swarm cannot set `security_opt`), `deploy.resources.limits` (memory, cpus), `replicas: ${<NAME>_PARSER_REPLICAS:-1}`, reached through
  the swarm VIP `http://<backend>-parser:8000`. Stateless, so scaling is just replicas.
- The gateway's own session uids (browser Chromium in the gateway container) must not be able to reach the parser; their per-uid egress firewall
  should already drop private ranges (verify in step 5). Even if reachable, the endpoint holds no secret and can only burn CPU.

### The endpoint

One route, `POST /parse` (plus `GET /health` for the swarm healthcheck).

Request: the original file as the raw body (chunked or with `Content-Length`; never buffered in memory). Parameters as headers:
`X-Name` (the job path, percent-encoded; decides detection by extension), `Content-Type` (the original's), `X-Base-Url` (optional, for absolutizing HTML links),
`X-Unzip: 0|1` (members of an archive are not unpacked again).

Response `200 application/x-tar`, **streamed while the worker produces it**. One tar member per artefact, path relative to the job, content type in a PAX
header `SH.content_type`:

```
parsed/<path>.md | parsed/<path>.json   simplified/<path>.md   terse/<path>.md   metadata/<path>.yaml
original/<path>.d/<member>  + parsed/... terse/... metadata/... for each member   (archives only; the top-level original is not echoed)
.result.json                            LAST member: {done: true, notes: [...], main: {text_path, terse_path}}
```

The gateway reads it with `tarfile.open(mode="r|")` and pipes each member into `storage.write_stream` (constant memory, one member at a time, no temp
file). It keeps in memory only what `_job_result` needs for inline `text` (members up to `INLINE_LIMIT`). A stream that ends without `.result.json`
(worker killed by timeout/memory limit, parser restarted) keeps the complete members already stored and adds the note
`"<path>: parse incomplete (…)"`. A non-200, a refused connection or a timeout stores nothing but the original and adds `"<path> not parsed (parser
unavailable: …)"`: **parsing never fails a job and never loses the original**, as today.

Errors, size and time limits stay where they are today, now enforced in the parser: `MAX_ARCHIVE_BYTES`, `MAX_ARCHIVE_MEMBERS`, `MAX_PARSE_BYTES` (a bigger
file gets metadata only), `METADATA_*`, plus new `PARSE_TIMEOUT` (default 300 s per request, then the worker is killed), `PARSE_MAX_OUTPUT` (cap on
total response bytes, default `MAX_ARCHIVE_BYTES`), `PARSE_CONCURRENCY` (default = CPUs; further requests wait in a bounded queue, 503 when it is full).

### Inside the parser

1. The server (Starlette/uvicorn, already SDK dependencies) streams the request body to a per-request directory on the **scratch volume**
   (parsers need a seekable file: markitdown, GDAL, exiftool, `zipfile`), counting bytes against a cap.
2. It starts **one worker subprocess per request** (`python -I -m spider_hoarder_parser.worker <dir>`) under `prlimit` (`--as`, `--cpu`) with a scrubbed
   environment, and forwards the worker's stdout (a tar stream) to the response. The worker runs the whole of today's per-file logic as a pure function
   `parse_file(path, ctype, base_url, unzip) -> iterator of (relpath, bytes|fileobj, ctype)` plus notes: detection, archive unpack (members are parsed
   in the same worker), markitdown/trafilatura, binary decoders, `terse`, and the metadata extractors from the file-metadata plan, which still run as their
   own children (so one bad extractor does not lose the parse) with their `prlimit`s as a second layer. The `unshare -rn` step is dropped: the container's
   network is the isolation.
3. Worker crash/kill → the stream ends without `.result.json` (above). The request directory is deleted in a `finally`.

A warm worker pool (avoiding the per-request interpreter start and imports) is **not** in this plan: measured cold imports are small (see Latency); add
it only if the benchmark in step 6 says so.

### What moves

`content/extract.py` and `content/metadata.py` and the parse half of `gateway/jobs.py` (`_parse_one`, the archive branch of `_store_one`, `_write_metadata`,
`_write_terse`, `_describe_file`'s conversion) move into a new package `parser/` (`spider_hoarder_parser`: `server`, `worker`, `parse`, `extract`,
`metadata`). The gateway keeps `grep_text`, `textcap`, storage, `_job_result`, and gains `gateway/parse_client.py` (`parse_into_job(key, path, ctype,
base_url, unzip, notes) -> (text, text_path, terse, terse_path) | None`), which `_store_one` calls where it now calls `_parse_one`. The
`sdk/pyproject.toml` dependencies of the parsers (`markitdown[all]`, `trafilatura`, `py7zr`, `rarfile`, `msgpack`, `cbor2`, `bson`, `blackboxprotobuf`,
`lxml_html_clean`, `rasterio`, `pyogrio`, `pypdf`, `pyyaml`, ...) move to `parser/pyproject.toml`; the gateway images drop them and
`unar`, `libimage-exiftool-perl`, `ffmpeg` (smaller, faster to build). `get_file_text` / `grep_file` and the tool signatures do not change.

### Latency analysis (whole `job_id` creation)

Per file, the new cost is: upload to the parser + one extra HTTP request + download of the artefacts + worker start; the old gateway-side costs that
disappear are the in-process call and, for large files, `storage.read_bytes` into memory. Parse time itself is unchanged.

Measured here (27-core arm64 host, venv with the SDK, no Docker network, so these are the CPU-side numbers):

| Item | Measured |
|---|---|
| Cold import in a fresh process: markitdown 0.32 s, pyogrio 0.17 s, rasterio 0.09 s, trafilatura 0.05 s, pypdf 0.03 s, lxml 0.01 s | ≈ 0.4–0.6 s for a worker that needs the lot, ≈ 0.1–0.2 s for typical files |
| First `markitdown` call (csv, docx) after import | 0.01–0.02 s |
| 117 KB HTML page: `absolutize` 7 ms, `html_to_markdown` 83 ms, `html_to_simplified` 54 ms, `terse` 66 ms | ≈ 0.21 s of parsing |
| A python metadata extractor as a child process (geojson) | 0.17 s |
| HTTP streaming on loopback (uvicorn + httpx), 1 GB up + 1 GB back | 0.4 s (not a bottleneck: ~5 GB/s) ; small request round trip 0.2 ms |

Not measured, assumed: swarm overlay throughput **~300 MB/s on the same host** and **~110 MB/s between nodes on 1 GbE** (VXLAN overhead), VIP/IPVS
latency ≈ 1 ms. These two numbers are the only ones the table below is sensitive to; step 6 measures them for real.

Expected added latency per job (parse time not included), same host / cross-node:

| Scenario | Today (in-process) | Added by the parser service | Notes |
|---|---|---|---|
| Saved page, 120 KB HTML (`browser_save_page`, the common case) | ≈ 0.2 s parse | **+10–20 ms** warm; **+0.3–0.5 s** if the worker's imports are cold | request ≈ 1–3 ms, artefacts ≈ 150 KB ≈ 1 ms, worker start dominates; ≈ +5–10 % warm. Per-request workers pay ≈ 0.3 s of imports for markitdown/trafilatura: this is the number that decides whether a warm pool is needed |
| Tree of N small files (network request dump) | N × small | N × the row above, **but** the gateway may submit up to K=4 concurrently | the gateway parses serially today; the parser parallelises across replicas/cores, so the wall time can go down |
| 50 MB PDF / docx | seconds to tens of seconds | **+0.2 s / +0.5 s** upload, artefacts small | markitdown dominates |
| Archive, 1 GB unpacked (compressed upload small) | unpack + parse + write | download of the 1 GB of unpacked members: **+3 s / +9 s**, but **pipelined**: members stream to storage while later ones are still being unpacked, so the real cost is `max(unpack, transfer, storage write)` rather than the sum | the largest regression; bounded by `MAX_ARCHIVE_BYTES` (4 GiB → worst case ≈ +13 s / +36 s if transfer-bound) |
| Oversize file (> `MAX_PARSE_BYTES`), metadata only | local temp copy | upload of the whole file: 5 GB ≈ **+17 s / +45 s** | see Open decision 3 |
| Remote blob storage (`gs://`), any file | read back from GCS only for big files | v1 reads `original/<path>` back from storage to feed the parser: an extra download of the file | removed by teeing the input (below) |

Also changes, not counted above: the gateway no longer spends CPU on parsing (MCP latency of *other* sessions improves, and the event loop stops
being blocked by `markitdown`), the parser scales with replicas, and a crashed parser costs one request instead of a gateway worker. Net: **the
common job gets ≈ 10–20 ms slower with a warm worker (≈ 0.3–0.5 s with a cold one); large archives and oversize files pay a transfer proportional to
their size.** Acceptance criterion for step 6: median job latency for the 120 KB page ≤ today + 100 ms on one host.

Streaming: request body → scratch file, scratch file → worker, worker stdout → response, response → storage, all in chunks; memory is bounded by
the chunk size plus whatever the individual parsers hold (markitdown loads the file). The one place streaming cannot overlap is *upload vs parse*:
the parsers need the whole file on disk first.

Optimisation, not in v1: **tee** the input. When `_store_one` is given a stream (uploads, session `out/` files), write it to storage and to the request
body in the same pass instead of reading `original/<path>` back; this removes the read-back (and the GCS download) at the cost of coupling the storage
write to the parser connection. Do it if the benchmark shows the read-back matters.

## Open decisions (to confirm before implementing)

1. **One parser service per backend** (`<backend>-parser`) rather than one shared service. Follows the overlay contract ("sharing is code and base images
   only") and keeps Tor/clear/I2P/Android traffic apart. Cost: N parser services, each with at least one replica of a fairly large image.
2. **Remote OCR needs egress.** The OCR plan's remote-model fallback calls an HTTP endpoint, which this container must not be able to do. Proposal: the
   parser runs Tesseract only; when the confidence is below the threshold it returns the image (or page PNG) as an artefact flagged in `.result.json`,
   and the *gateway* does the remote call and rewrites `parsed/<path>.md`. `ocr-hybrid.md` must be amended accordingly (not done here). Alternative:
   allow the parser egress to the OCR endpoint only, which reintroduces a network path out of the hostile-file process; not recommended.
3. **Oversize files.** The metadata plan gives files over `MAX_PARSE_BYTES` metadata too. Shipping a 5 GB file to the parser for that costs 17–45 s.
   Proposal: a cap `PARSE_MAX_UPLOAD` (default 1 GiB) above which the file is stored with a note and no metadata. Alternatives: always ship (slow), or
   ship only the head and tail (works for most formats' headers, wrong for others).
4. **Wire format: tar stream** (stdlib on both sides, binary-safe, streams, per-member content types via PAX headers) over multipart. Proposal: tar.
5. **No authentication on the endpoint**, only the internal network membership. The parser holds no secret, so a bearer token adds little.
6. **Per-request worker, no warm pool in v1** (see Latency); revisit with the benchmark.
7. **Scratch space on disk, not tmpfs.** tmpfs counts against container memory and archives can be 4 GiB. Proposal: an anonymous volume at `/work` cleaned per
   request, with the byte caps enforced by the server (Docker does not quota volumes).
8. **Parser unavailable** stores the original and notes it (never fails the job), after one retry. No in-process fallback, since that would keep
   the parsers (and their dependencies) in the gateway.
9. **`_describe_file`** (device/session files returned directly) goes through the same endpoint; its "convert but do not store parsed/" behaviour becomes a
   flag (`X-Store: 0`), or it is dropped if it turns out to be the same as storing.

## Implementation order

1. Independent bugfixes that can land first on the current code: `ffprobe -protocol_whitelist file`, drop `.vrt` from the raster extensions.
2. Create the `parser/` package: move `extract.py` and `metadata.py`; write `parse.py` (`parse_file` as a pure function returning artefacts, no
   storage) and port the existing tests (`test_jobs`, `test_metadata`, `test_security`) to call it directly.
3. `worker.py` (tar on stdout, limits, `.result.json`) and `server.py` (`/parse`, `/health`, concurrency, caps, scratch dir cleanup), Dockerfile and image.
4. Gateway: `parse_client.py`, `_store_one` / `_describe_file` switched over, parser deps removed from `sdk/pyproject.toml` and the gateway images.
   Tests run the parser app in-process through Starlette's `TestClient` injected as the HTTP client; plus unavailable, truncated stream, timeout, killed worker.
5. Deployment: `<backend>-parser` service and `<backend>-parse` internal network in every backend's overlay; `scripts/spider_hoarder.py validate` checks
   them; `docker-compose.dev.yml`. Verify with a real swarm: the parser cannot resolve or reach anything outside (`getent hosts example.com`, a
   `connect()` to the gateway's other networks), a hostile `.m3u8` and a `.vrt` with `/vsicurl/` produce no connection, session uids cannot reach the parser.
6. `scripts/bench_parse.py`: job latency for the scenarios in the table, in-process (git baseline) vs service, same host and cross-node; fill the real
   overlay throughput numbers into this doc; decide on warm pool and tee.
7. Docs; move the plan.

## Files to change

| File | Change |
|---|---|
| `parser/` (new package: `server.py`, `worker.py`, `parse.py`, `extract.py`, `metadata.py`, `pyproject.toml`, `Dockerfile`) | the parsers and the endpoint |
| `sdk/spider_hoarder_sdk/content/extract.py`, `content/metadata.py` | moved to `parser/` (`grep_text` stays in the SDK) |
| `sdk/spider_hoarder_sdk/gateway/jobs.py` | parse half removed; `_store_one` calls `parse_client`; `_write_metadata` removed |
| `sdk/spider_hoarder_sdk/gateway/parse_client.py` | new: streaming POST, tar consumer, notes, retry |
| `sdk/pyproject.toml`, gateway Dockerfiles (browser-sdk, android-sdk) | parser dependencies and `unar`/`exiftool`/`ffmpeg` removed |
| each backend's `docker-compose.yml` overlay, `docker-compose.dev.yml`, `deploy.sh`, `scripts/spider_hoarder.py` | `<backend>-parser`, `<backend>-parse` (internal), `PARSER_URL`, `<NAME>_PARSER_REPLICAS`, validate rules |
| `sdk/tests/*`, `parser/tests/*` | tests moved/added, `conftest.py` metadata stub removed |
| `docs/plans/ocr-hybrid.md` | amended per Open decision 2 |

## Docs to update

- `docs/architecture/overview.md`: the service table and "Why one container per backend" (parsing is no longer in-gateway; this is the one exception to it).
- `docs/architecture/jobs-and-storage.md`: where parsing runs, the endpoint, the artefact stream, failure semantics, the metadata isolation paragraph (now: the container).
- `docs/architecture/backend-plugins.md`: overlay contract gains `<name>-parser` and the internal `<name>-parse` network.
- `docs/architecture/session-isolation.md`: the parser's isolation (no egress, no secrets, no storage) next to the session uid model.
- `docs/architecture/scaling.md`: parser replicas and concurrency.
- `docs/implemented-plans/file-metadata.md`: note that its isolation section is superseded by this plan.
- `docs/implemented-plans/index.md` and `docs/plans/index.md`; each updated section gets `See also: [Plan: parser service](../implemented-plans/parser-service.md)`.
