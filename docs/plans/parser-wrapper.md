# Plan: parser service with a storage wrapper, a no-secret parser uid, an OCR sidecar and an authenticated egress proxy

See also [Plan: file metadata](../implemented-plans/file-metadata.md) (its child-process isolation is superseded by this plan),
[Jobs, storage and links](../architecture/jobs-and-storage.md), [Session isolation](../architecture/session-isolation.md) (the uid lifecycle and the
`browser_clear` egress rules this plan copies), [Backend plugins](../architecture/backend-plugins.md) (overlay contract),
[Plan: OCR hybrid](ocr-hybrid.md) (its remote step moves to the OCR sidecar below).
**Depends on [Plan: project access model](../implemented-plans/project-access-model.md)**: the key layout (`projects/<proj>/data/<session>/<job>/…`), the three-part job
reference, the shared bucket and the per-job scoped credentials are defined there; this plan only uses them.

## Problem / Goal

Everything that interprets an untrusted file runs inside the gateway container as the gateway's own uid (1000): markitdown, trafilatura, archive
unpacking (`py7zr`, `rarfile`/`unar`), the binary decoders, and now ExifTool, `ffprobe`, GDAL, `lxml` and `pypdf`. A parser bug (memory corruption in a
C library, ExifTool-style eval bugs) or a feature (an `.m3u8` that makes `ffprobe` fetch URLs, a `.vrt` pointing at `/vsicurl/`) then runs next to
`BLOB_TOKEN_SECRET`, `/data`, the storage credentials, `sessiond`'s socket and the internal networks. The file-metadata plan contained this with
`prlimit` + `unshare -rn`, but `unshare` is blocked by Docker's default seccomp profile, so in the shipped images there is no network isolation, and the
child still shares the uid and filesystem view of the gateway.

The obvious fix is a parser container with no egress and no storage access that takes a file over HTTP and streams the artefacts back. That makes the
gateway carry every file and every artefact both ways (gateway → parser → gateway) and keeps it the whole data path, which does not scale with a blob store.

Goal here: with a real blob store (GCS, S3, in dev SeaweedFS) parsing runs in a separate, **shared and horizontally scalable** service whose **parser process
can read nothing, reach nothing and authenticate to nothing (except, when a job asks for remote OCR, the OCR sidecar with that one profile's token)**, while a
small trusted **wrapper** in the same container moves the bytes between the blob store and the parser.
The gateway only says "parse job X" and later reads the result from the blob store, and stops importing any parser. Non-goal: removing the gateway's own direct storage access (uploads,
`get_file_text`, `get_file_url`, `/blob/` all stay as they are).

## Design

### Components

| Part | Runs as | Holds | Network |
|---|---|---|---|
| **wrapper** (in each parser container) | its own uid (not a pool uid), keeps `CAP_SETUID`/`CAP_SETGID` (+ what the uid lifecycle needs, e.g. chown, kill) | the **wrapper API token**, the **proxy token**, the **OCR profile table** (name → sidecar URL + token), and **no standing blob credential**: it gets a per-job scoped credential from the gateway in each POST/GET, in memory only (all other secrets in memory or 0600 files) | the `<backend>-parse` networks (to the gateways) and the egress network of the proxy; the OCR sidecar's network; nothing else |
| **parser** | one uid from a pool, one parse at a time, forked by the wrapper | **no blob credential, no wrapper or proxy token**; minimal environment: `PYTHONUNBUFFERED=1` and, **only if the job asked for remote OCR, that one profile's `OCR_URL` + `OCR_TOKEN`**; its per-job input and output directories | same container network as the wrapper, no firewall rule: it **can connect to the proxy but the proxy rejects it without the token**; it can reach the OCR sidecar, which accepts only a profile token |
| **blob-sidecar** (own container, shared) | proxy uid | the proxy token (to check it) | the egress network, and the public internet through its own firewall (below) |
| **ocr-sidecar** (own container, shared, optional) | its own uid | the profile table (endpoint, model, auth, credentials per profile) and one token per profile | the parser container's network; its own pinned egress (below); **no blob access** |

Decided: **no per-uid firewall** in the parser container. The container's networks contain only the gateways (for the wrapper API), the proxy and the OCR
sidecar; authentication, not packet filtering, is what keeps the parser out of the proxy and the sidecar. This leaves the parser able to open TCP
connections to the gateways' listening ports and to the proxy's and sidecar's ports (see Risks).

Decided: **one shared parser service** (not one per backend), scaled by replicas: each replica has its own uid pool and answers 503 when full, any replica
answers status reads, so the swarm VIP is enough. One shared proxy and one shared sidecar.

Decided: **the blob-sidecar and the ocr-sidecar stay two containers** (merging was considered and rejected). The blob-sidecar is a generic forward proxy that holds
nothing worth stealing, and it is the component the hostile parser can attack before authenticating; the ocr-sidecar holds the real endpoint credentials
and has its own pinned egress (possibly a LAN address). Merging would put those credentials behind the generic proxy's pre-auth surface and force one
egress policy on two different jobs. The ocr-sidecar is also optional: it is only deployed when an OCR profile is configured.

Decided: **the wrapper keeps `CAP_SETUID`/`CAP_SETGID`** itself and owns the uid lifecycle invariants (`free | busy | quarantined`, wipe, verify) from
[Session isolation](../architecture/session-isolation.md). A `sessiond`-style root helper (wrapper without capabilities) is a possible later hardening, not
part of this plan.

### Data flow for one job

1. The gateway (browser `out/` collection, upload, `ingest_page`, ...) streams the original into the blob store as today
   (`projects/<proj>/data/<session>/<job>/original/<path>`), then mints the job's scoped credential
   ([project access model](../implemented-plans/project-access-model.md): read the project's `data/`, write only `<session>/<job>/`, 900 s) and calls the wrapper.
2. `POST <wrapper>/jobs/<proj>/<session>/<job>` with the wrapper token and the scoped credential; body `{path, content_type, base_url?, unzip?, features?}`.
   `features` (see "Remote OCR") is `{"ocr": "<profile>"}` or absent; an unknown feature key or an unknown profile name is a **400**. The wrapper
   builds the storage key itself from the three uuids (validated against the UUID regex), never from a caller-supplied path; the gateway has
   already authorised the user, the wrapper authorises only the gateway (its token) and the store enforces the credential's scope. Answers **202** at once, **409** if
   the job is `done` or `running` with a fresh heartbeat (a `running` job with a stale heartbeat is restarted, safe because outputs are deterministic),
   **503** if no parser uid is free.
3. The wrapper pulls the original from the blob store **through the proxy** into the parser's input directory (disk scratch volume, not tmpfs), writes the
   request (`path`, `content_type`, `base_url`, `unzip`, `features`) as a job file next to it, then
   starts the parser as the reserved uid: `setgid/setuid`, minimal environment (above), `umask 027`, `setsid`, `RLIMIT_AS` 2 GiB, `RLIMIT_NPROC`, `RLIMIT_NOFILE`,
   `RLIMIT_FSIZE` 4 GiB, `no-new-privileges`, all inherited fds closed. Wall-clock limit **10 min** per job, then the whole process group is killed.
4. The parser writes finished artefacts into its output directory and announces each on **stdout, which is a pipe to the wrapper**: one JSON record per
   line, `{"path": ..., "content_type": ...}`, written with one `os.write` on a private fd (each record under `PIPE_BUF`, so atomic).
   Before importing anything the parser does `proto = os.dup(1)` (close-on-exec) and `os.dup2(2, 1)`, so every tool it starts writes to stderr; the
   environment sets `PYTHONUNBUFFERED=1`. stderr goes to the wrapper's log, capped, never to the client.
5. For each record the wrapper validates (regular file via `O_NOFOLLOW`, size from its own `fstat`, never from the record; no absolute or `..` path;
   line, record-count and total-byte caps; the key stays under the job prefix) and uploads the file **concurrently with the parser still running**,
   deleting it from the output directory after its upload. Pipe backpressure bounds memory.
6. When the parser exits with 0 and a clean EOF, the wrapper writes **`index.json`** (the manifest, built in memory and written once, with `notes`),
   then `status.json` = `done`. A non-zero exit, a kill by the limit or a truncated last record gives `index.json` with the failure noted (artefacts
   already uploaded stay) and `status.json` = `failed`. `index.json` is always written **before** `status.json`.
   Afterwards the wrapper wipes the parser's directories and verifies the uid owns nothing before freeing it (decision 5).
7. The gateway polls and, on `done`, reads `index.json` from the blob store and builds `{job_id, files, text?}` itself (see below).

### Status

- `GET <wrapper>/jobs/<proj>/<session>/<job>` returns `{status: not_started|running|done|failed, error?}` and nothing else. The gateway sends the same
  scoped credential it minted for the POST (still valid, 900 s covers the 12-minute poll window together with a re-mint on retry), so a replica that does
  not own the job can read `status.json` without holding any credential of its own.
- **Both** a replica-memory cache and the blob store: the replica that owns a job answers from memory; any other replica reads
  `<job>/status.json` = `{status, owner_replica, updated}` from the blob store with that credential. The owner rewrites it every **10 s** while `running` (heartbeat).
  `running` with `updated` older than **30 s** (and the owner not answering) is reported as `failed` with "wrapper lost". No `status.json` and no memory
  entry is `not_started`. No replica id in the URL: any wrapper replica answers, so the swarm VIP is enough.
- `status.json` is not listed in `index.json`'s `files`. Two POSTs for the same new job at the same instant could both start (no atomic
  create-if-absent through fsspec); only the gateway calls the endpoint and a duplicate parse writes the same bytes, so this is accepted.

### Gateway side

`gateway/parse_client.py`: POST, then poll (50 ms growing to 1 s, overall 12 min), so the MCP tool call stays synchronous for the client. On `done` it
reads `index.json`; the **inline-text logic moves here** (`INLINE_LIMIT`, terse fallback): it reads `parsed/…` (or, for a file the manifest marks as text, `original/…`) or `terse/…` only if the size in the
manifest is under the limit. `get_file_text`, `grep_file`, `get_file_url` and the tool signatures do not change. Parser unavailable, 503 after retries,
`failed`, or a poll timeout stores nothing more, adds `"<path> not parsed (…)"` to the notes and **never fails the job or loses the original**
(parsing never fails a job). A single retry is made on 503 or a refused connection. No in-process fallback: that would keep the parsers and their
dependencies in the gateway. The gateway decides per job whether to send `features.ocr` (a per-backend setting in the overlay,
replacing the `OCR_BASE_URL_<BACKEND>` variables of the OCR plan).

### The proxy

- A forward proxy that requires authentication (the **proxy token**, HTTP Basic or equivalent) on every request and answers 407 without it. Both
  `http` and `https` CONNECT, because dev SeaweedFS is plain http.
- **No destination allowlist** (a bucket host on an external service is hard to pin and fragile).
- Its **own copy** of today's `browser_clear` egress rules, applied to its own egress by its start script (`NET_ADMIN`, dropped after the rules are in,
  the same pattern as `browser-sdk/start.py`): public internet only; REJECT RFC1918, link-local (including the metadata server), CGNAT, loopback and
  other stack services; the configured DNS resolver stays reachable. A copy, **not an import** from `browser-sdk`: the two are allowed to drift.
  The file starts with a comment saying where it was copied from and when.
- **Explicit exception for the dev blob store**: `PARSE_PROXY_ALLOW` (space-separated host:port, empty in production) is resolved at start and gets
  ACCEPT rules **before** the REJECT rules, for exactly those addresses and ports (not for whole private ranges). In dev it is the SeaweedFS S3 endpoint.
  Rules are applied at connect time, so DNS rebinding cannot get around them.
- The proxy token and the wrapper token are different secrets, both in `.env`, both refused as placeholders by `deploy.sh` like `BLOB_TOKEN_SECRET`.
- The parser can send the proxy arbitrary bytes before authenticating, so the proxy's pre-auth request parsing is attack surface from the hostile process
  (relevant to the proxy software choice, decision 3).

### Remote OCR (sidecar)

The remote step of [Plan: OCR hybrid](ocr-hybrid.md) cannot run in the parser (no egress, no credentials), so it is a separate container. Tesseract and
rasterising/downscaling stay in the parser (`ocr.py` moves into `parser/`).

- **`ocr-sidecar`**: a stateless forwarder. `POST` with the image bytes (PNG/JPEG, already downscaled by the parser to `OCR_MAX_SIDE`) and
  `Authorization: Bearer <profile token>`; it sends them to the profile's OpenAI-compatible endpoint and returns the transcript text. It **never takes a URL or
  a path, has no blob access and does not decode images**; body-size and concurrency caps. `google-auth` and the auth modes (`google`, `bearer`, `none`)
  of the OCR plan live here.
- **Profiles**: the sidecar's table maps a profile name to `{base_url, model, auth, audience, credentials}` and to one **token per profile**; it picks the
  profile from the token the caller presents. Named profiles (for example `remote` and `lan`) keep the option of a local model for backends that must not
  use a third party.
- **Selection is wrapper-controlled**: the POST's `features.ocr` names a profile; the wrapper looks it up in its own table (name → sidecar URL + token) and
  puts **only that profile's** `OCR_URL` and `OCR_TOKEN` in the parser's environment. No `features.ocr` = no OCR variables = Tesseract-only, no remote code
  path. This is the only OCR logic in the wrapper. A parser never holds a token for a profile its job did not request, so a hostile archive member cannot
  send the job's other files to a profile the gateway did not allow.
- Remote failure (sidecar down, timeout, token or endpoint error) never fails the job: the Tesseract text is kept and the note says why (OCR plan rule).
- **Egress**: its own firewall, **pinned** to the configured profile endpoints (and the metadata server only if a `google` profile uses it), not the
  proxy's "public internet only" rules, so a LAN-local profile is possible.
- A leaked profile token gives free OCR on that profile only (accepted).

### Dev: SeaweedFS behind the proxy

The `seaweedfs` service, its bucket, users and STS setup are defined in the [project access model](project-access-model.md#dev-seaweedfs-in-the-stack). Here
only the wrapper's side: it reaches the store through the proxy via `HTTP_PROXY`/`HTTPS_PROXY` with the token as proxy credentials (botocore/aiobotocore
honour them, to verify in step 1), using the scoped credential from the POST. `PARSE_PROXY_ALLOW=seaweedfs:8333`. SeaweedFS is on a network the proxy is
on; the parser container is not.

### Storage dependency

`sdk/pyproject.toml` gets optional extras `gcs = ["gcsfs"]` and `s3 = ["s3fs"]`; the images install the one the deployment uses (build `ARG`).

### Inside the parser

The parser process runs today's whole per-file logic as a pure function `parse_file(path, ctype, base_url, unzip, features)`: it yields
`(relpath, file, ctype)` artefacts plus notes, with no storage access. That covers detection by extension, archive unpack (members are parsed in the same
process; `unzip=0` for members so archives are not unpacked again), markitdown/trafilatura, the binary decoders, `terse`, and the metadata extractors of
the file-metadata plan. The extractors still run as their own child processes (one bad extractor does not lose the parse) with their `prlimit`s as a second
layer. `unshare -rn` is dropped: the container's networks and the uid separation are the isolation. A per-job process: **no warm worker pool** in this
plan (cold imports are small, see Latency); add one only if the benchmark in step 8 says so.

### What moves

`content/extract.py` and `content/metadata.py` and the parse half of `gateway/jobs.py` (`_parse_one`, the archive branch of `_store_one`, `_write_metadata`,
`_write_terse`, `_describe_file`'s conversion) move into a new package `parser/` (`spider_hoarder_parser`: `wrapper`, `worker`, `parse`, `extract`,
`metadata`, `ocr`). The gateway keeps `grep_text`, `textcap`, storage and `_job_result`, and gains `gateway/parse_client.py`, which `_store_one` calls where
it now calls `_parse_one`. The `sdk/pyproject.toml` dependencies of the parsers (`markitdown[all]`, `trafilatura`, `py7zr`, `rarfile`, `msgpack`, `cbor2`,
`bson`, `blackboxprotobuf`, `lxml_html_clean`, `rasterio`, `pyogrio`, `pypdf`, `pyyaml`, ...) move to `parser/pyproject.toml`; the gateway images drop them
and `unar`, `libimage-exiftool-perl`, `ffmpeg` (smaller, faster to build). `get_file_text` / `grep_file` and the tool signatures do not change.

Errors and size/time limits stay as they are today, now enforced in the parser: `MAX_ARCHIVE_BYTES`, `MAX_ARCHIVE_MEMBERS`, `MAX_PARSE_BYTES` (a bigger file
gets metadata only), `METADATA_*`. Parsing never fails a job and never loses the original.

### Latency

Per file, the new cost is the wrapper's pull of the original from the store, the upload of the artefacts, the parser start and the gateway's poll
(50 ms growing to 1 s); the old gateway-side costs that disappear are the in-process call and, for large files, `storage.read_bytes` into memory. Parse time
itself is unchanged. Unlike the earlier gateway-streaming design, no file or artefact passes through the gateway, so large archives and oversize files cost the
gateway nothing; their transfer is store ↔ parser.

Measured on a 27-core arm64 host, venv with the SDK, no Docker network (CPU-side numbers only):

| Item | Measured |
|---|---|
| Cold import in a fresh process: markitdown 0.32 s, pyogrio 0.17 s, rasterio 0.09 s, trafilatura 0.05 s, pypdf 0.03 s, lxml 0.01 s | ≈ 0.4–0.6 s for a process that needs the lot, ≈ 0.1–0.2 s for typical files |
| First `markitdown` call (csv, docx) after import | 0.01–0.02 s |
| 117 KB HTML page: `absolutize` 7 ms, `html_to_markdown` 83 ms, `html_to_simplified` 54 ms, `terse` 66 ms | ≈ 0.21 s of parsing |
| A python metadata extractor as a child process (geojson) | 0.17 s |
| HTTP streaming on loopback (uvicorn + httpx), 1 GB up + 1 GB back | 0.4 s (~5 GB/s, not a bottleneck); small request round trip 0.2 ms |

Not measured, assumed: swarm overlay throughput ~300 MB/s on the same host and ~110 MB/s between nodes on 1 GbE (VXLAN overhead), VIP/IPVS latency ≈ 1 ms.
The proxy hop (squid) and the store's own latency are not in any number here. Expectations: the common job (a ~120 KB saved page) pays one POST, one pull, a
cold-import start of ≈ 0.1–0.5 s, a few small uploads and up to one poll interval; a tree of N small files can be parsed in parallel across the uid pool and
replicas, so wall time can go down; large archives are bounded by `max(unpack, transfer, store write)` because artefacts upload while the parser still runs.
Acceptance criterion for step 8: median job latency for the 120 KB page ≤ today + 100 ms on one host.

Side effects, not counted: the gateway spends no CPU on parsing (other sessions' MCP latency improves, and `markitdown` stops blocking the event loop), and a
crashed parser costs one job instead of a gateway worker.

## Risks and residual exposure

- The parser can connect to the gateways' listening ports (same Docker network as the wrapper API) and to the proxy and the sidecar. None accepts it without a
  token, but all are reachable attack surface. Mitigations to consider in the plan review: keep the gateway↔wrapper network separate from the
  egress network, and bind nothing on the gateway side of the parse network except what is needed.
- **Shared service, shared kernel:** files from Tor, I2P and clear-web backends are parsed in the same container kernel. A parser-escape bug crosses
  backends; the per-job scoped credentials still limit what an escape can read in the store. **To review:** the shared service must be attached to every
  backend's `<backend>-parse` network (one shared network would let the gateways reach each other), which also lets the parser reach every backend's gateway
  ports.
- **Wrapper keeps `CAP_SETUID`/`CAP_SETGID`** (decision 1): compromising the HTTP-facing wrapper (the process that reads the parser's records) gives root
  inside the container with its bounded capability set, and any pool uid. It already holds the wrapper token, the proxy token and the OCR tokens, so the added
  exposure is the container-root pivot (other uids' state, a larger kernel surface for an escape).
- Same kernel, same filesystem namespace, same PID view as the wrapper (hidepid=2 mitigates reading `/proc`); a kernel privilege-escalation bug defeats
  the uid separation. Weaker than a second container; the same trust level as the browser sessions.
- One shared proxy token: a leak gives general internet access through the proxy but not the blob credentials (rotate it). A leaked scoped credential is
  limited to one project's read side and one job's write side, for at most its 900 s lifetime (S3: revocable at once by deleting the job's role). A leaked OCR profile token gives free OCR on
  that profile only.
- **OCR privacy** rests on the gateway sending `features.ocr` only where allowed (it already holds the data) and on the wrapper handing the parser only the
  requested profile's token. A compromised parser can send its own job's input, nothing else, to that profile's endpoint.
- `unshare` is blocked by Docker's default seccomp profile in the shipped images, so there is **no** per-parse network namespace; this design does not
  rely on one.
- A compromised parser steering the wrapper's writes is bounded twice: by the wrapper's key computation and path validation (tests of their own) and by the
  store, since the credential can only write under the one job's prefix.

## Decisions and open decisions

Wire format is not a decision here: there is no gateway → parser upload (the wrapper pulls from the store), records go over a pipe, and every endpoint is
authenticated by token. One parser per job, forked by the wrapper, no warm pool (see Inside the parser).

1. ~~**Privilege mechanism.**~~ **Decided:** the wrapper keeps `CAP_SETUID`/`CAP_SETGID` (see Components).
2. ~~**Per backend or shared.**~~ **Decided:** one shared parser service, one proxy, one sidecar, scaled by replicas (see Components, Risks).
3. ~~**Proxy software.**~~ **Decided: squid** for the blob-sidecar (the proxy is reachable pre-auth by the hostile parser, which favours a mature, heavily fuzzed
   implementation over tinyproxy; stock nginx has no `CONNECT`; a stdlib Python proxy was the alternative). Squid configuration: caching off
   (`cache deny all`), `via off`, `forwarded_for delete`; Basic auth through `auth_param basic` with `basic_ncsa_auth` and an htpasswd file the start script
   generates from the proxy token; `http_access deny !authenticated`; `CONNECT` allowed to any port (the default `SSL_ports` restriction is lifted, the
   firewall rules are the destination control). The wrapper supplies the token as Basic
   credentials in `HTTP_PROXY`/`HTTPS_PROXY`; the parser gets no proxy variables. **Verified in throwaway containers (s3fs 2026.9.0, botocore 1.43,
   aiobotocore 3.10, aiohttp 3.14, SeaweedFS 4.53, squid 6.13):** the credentials in the proxy URL are honoured for plain-http endpoints and for
   https `CONNECT`; lowercase `http_proxy` works too; no or wrong credentials give 407 (http) or `ProxyConnectionError` (https), never a hang; the
   session token reaches the store as `X-Amz-Security-Token`; mkdir/put/cat/ls/rm and a 60 MB multipart upload work on both paths.
   **Squid findings that go into the image:** use **squid 6 or newer** (Debian trixie, 6.13). **Squid 5.7 (Debian bookworm) resets the connection on plain-http
   uploads of about 10 MB and up to SeaweedFS** (it logs `ERR_CLIENT_GONE` once the request buffer fills; raising `client_request_buffer_max_size`, dropping
   `Expect: 100-continue` and changing the checksum mode did not help; https `CONNECT` is unaffected); set `max_filedescriptors 4096` (inside a container the
   default fd limit makes squid die with `xcalloc: Unable to allocate`); log to a file such as `/var/log/squid/access.log` (squid runs as `proxy` and cannot
   open `/dev/stdout`); `ignore_expect_100` and `chunked_request_body_max_size` are obsolete and must not be set; the version is pinned and security updates tracked.
4. ~~**Remote OCR.**~~ **Decided:** a separate `ocr-sidecar` (stateless forwarder, no blob access), called by the parser over HTTP with a per-profile
   token; profiles chosen by `features.ocr` in the POST; the wrapper injects only the requested profile's variables (see Remote OCR).
5. ~~**Oversize files**~~ **Decided:** `PARSE_MAX_UPLOAD` is removed (there is no gateway→parser upload any more; the wrapper pulls from the blob store). The
   remaining size limits are `MAX_PARSE_BYTES` and `MAX_ARCHIVE_*` in the parser and the parser's `RLIMIT_FSIZE`. `MAX_PARSE_BYTES` is unchanged: a file over it
   is stored and gets metadata only, no content parse (file-metadata plan). **No cap on the wrapper's pull:** the scratch area's size is the bound. A full
   disk (or any pull error) fails that job only: no parse, the original is kept, the notes say why, the scratch is wiped. **Scratch cleanup:** the wrapper deletes the
   parser's whole home/job directory (input, output, job file) when the parse ends, whatever the outcome (done, failed, timeout, kill), after the process
   group is dead and before the uid goes back to `free`; a failed wipe or leftover files owned by the uid quarantine it (the uid lifecycle of
   [Session isolation](../architecture/session-isolation.md)). The scratch area is **ephemeral**: it lives in the container's own filesystem or an anonymous
   volume of the task, never a named volume, so a container restart deletes everything left over.
6. ~~**Cleanup of partial jobs.**~~ **Decided: nothing in this plan.** A failed job keeps its uploaded artefacts, its `index.json` (with the failure noted) and
   `failed` status; partial results stay readable and a retry overwrites the same keys. Removing old data is a general retention question for the whole
   `data/` tree (for example a bucket lifecycle rule), outside this plan.
7. ~~**OCR cold start**~~ ([OCR hybrid](ocr-hybrid.md), open question 2) **Decided:** the OCR plan's default stands. A self-hosted model may take minutes
   to start; the first call after idle times out (`OCR_TIMEOUT`), the Tesseract text is kept and the note says the remote step failed. No longer
   timeout and no warm-up call; revisit with measured cold-start times.
8. ~~**`_describe_file`**~~ **Decided: one path.** Today the gateway has two ways from an original to the `original/`, `parsed/`, `terse/`, `metadata/` tree:
   `_store_one` (uploads, `ingest_page`, browser trees, archive members) and the lighter `_describe_file` / `_store_file` (single files a tool produced:
   browser screenshots/PDFs, Android file tools), which skips metadata, archives and OCR. Both are removed in favour of **one**: every stored original is
   handed to the wrapper with the same POST and gets the same treatment, whoever produced it. `_describe_file`, `_store_file` and the NUL sniff are deleted; the
   result is always built from `index.json`. Consequence: tool-produced screenshots and PDFs now get metadata and (if the backend allows) OCR, and each such
   tool call waits for one parse. **Plain text** gets no `parsed/` copy: `index.json` marks the file as text and the gateway reads the inline text
   straight from `original/<path>` (under the same `INLINE_LIMIT`; over it, the `terse/` outline if there is one).

## Limits (starting values, not measured)

Parser uid pool 4 per container (= parallel parses; further POSTs get 503); container memory limit 8 GiB; per-parser `RLIMIT_AS` 2 GiB; job wall clock
10 min (metadata extractors keep their 30 s); heartbeat 10 s, stale after 30 s; gateway poll 50 ms to 1 s, timeout 12 min; `MAX_PARSE_BYTES`,
`MAX_ARCHIVE_*` unchanged, now enforced in the parser; scratch on a disk-backed, ephemeral (not named-volume) area sized for about 4 × the largest expected archive, wiped after every job; sidecar body cap and
concurrency cap (values to set when implementing).

## Implementation order

0. The [project access model](../implemented-plans/project-access-model.md) steps 1 to 6 first (layout, credentials, SeaweedFS in dev); this plan assumes them.
   Independent bugfixes that can land first on the current code: `ffprobe -protocol_whitelist file`, drop `.vrt` from the raster extensions.
1. Verify the unknowns. **Done:** `s3fs` through an HTTP proxy with credentials and a session token (see decision 3). **Still to verify:** SeaweedFS
   behind the proxy rules with the exception in place (the test ran squid without the firewall rules), STS-issued scoped credentials from the project access
   model, `gcsfs` through the proxy, and the `os.dup2` and `PYTHONUNBUFFERED` behaviour with markitdown, `ffprobe` and `exiftool`.
2. Create the `parser/` package (see "What moves"; `extract.py`, `metadata.py`, `parse.py` as a pure `parse_file` returning artefacts), plus
   `worker.py` for the stdout record protocol and `ocr.py` (Tesseract, rasterising, sidecar client; from the OCR plan); port the existing tests.
3. The wrapper: API (POST/GET, tokens, `features` validation and OCR profile lookup), key computation, pull, uid launch (with its own capabilities), record
   validation, concurrent upload, `index.json` and `status.json`, heartbeat, limits. Tests: key escape attempts, hostile records (bad JSON, long lines,
   `..`, symlinks, size lies), crash, timeout, stale heartbeat, duplicate POST, unknown profile, only the requested profile's variables in the parser's environment.
4. The proxy image: proxy software, authentication, its own copy of the egress rules, `PARSE_PROXY_ALLOW`, start script. A rules test like the existing
   `firewall.rules` pure-function test.
5. The OCR sidecar: forwarder, profile table and token selection, auth modes, caps, pinned egress rules and their test.
6. Gateway: `parse_client.py`, `_store_one` switched, `_describe_file` / `_store_file` removed (decision 8), inline-text logic moved, `features.ocr` sent per backend setting, parser
   dependencies dropped from the gateway images.
7. Deployment: services, networks, tokens in `.env`/`deploy.sh`/`scripts/spider_hoarder.py validate`, `docker-compose.dev.yml` with SeaweedFS. Verify
   on a real swarm: the parser uid gets 407 from the proxy and 401 from the sidecar without a token, cannot read the wrapper's `/proc/<pid>/environ` or files,
   cannot reach the metadata server or private addresses through the proxy, a hostile `.m3u8` and a `.vrt` with `/vsicurl/` produce no connection.
8. `scripts/bench_parse.py`: job latency in-process (git baseline) vs service, same host and cross-node, with the poll loop and the uploads, against
   SeaweedFS and one real bucket; fill the real overlay throughput numbers into the Latency section; decide on a warm pool.
9. Docs; move the plan.

## Files to change

| File | Change |
|---|---|
| `parser/` (new: `wrapper.py`, `worker.py`, `parse.py`, `extract.py`, `metadata.py`, `ocr.py`, `pyproject.toml`, `Dockerfile`, tests) | wrapper, parser, image |
| `blob-sidecar/` (new: proxy config, `egress_rules.py` (own copy), `start.py`, `Dockerfile`, tests) | authenticated proxy with its own egress rules |
| `ocr-sidecar/` (new: forwarder, profile table, `egress_rules.py`, `start.py`, `Dockerfile`, tests) | OCR forwarder with pinned egress |
| `sdk/spider_hoarder_sdk/content/extract.py`, `content/metadata.py` | moved to `parser/` (`grep_text` stays) |
| `sdk/spider_hoarder_sdk/gateway/jobs.py` | parse half removed; `_store_one` calls `parse_client`; inline-text logic from the manifest |
| `sdk/spider_hoarder_sdk/gateway/parse_client.py` | new: POST (with `features`), poll, notes, result building |
| `sdk/pyproject.toml`, gateway Dockerfiles (browser-sdk, android-sdk) | parser dependencies and `unar`/`exiftool`/`ffmpeg` removed (the `gcs`/`s3` extras and `storage.py` changes are in the project access model plan) |
| each backend's `docker-compose.yml` overlay, `docker-compose.dev.yml`, `deploy.sh`, `scripts/spider_hoarder.py`, `.env.example` | parse/OCR service wiring, per-backend networks, tokens, `PARSE_PROXY_ALLOW`, per-backend OCR feature setting (the SeaweedFS service itself is in the project access model plan) |
| `scripts/bench_parse.py` | new: latency benchmark |
| `docs/plans/ocr-hybrid.md` | reconcile with this plan (the OCR plan's remote step, per-backend `OCR_*` variables and `content/ocr.py` location change) |

## Docs to update

- `docs/architecture/overview.md`: service table (parser, proxy, OCR sidecar), the parsing exception to "one container per backend".
- `docs/architecture/jobs-and-storage.md`: where parsing runs, the wrapper API and `features`, `status.json`, the artefact protocol, failure semantics, the metadata isolation paragraph; the storage section (backends, extras, SeaweedFS in dev).
- `docs/architecture/session-isolation.md`: the parser uid model and the proxy's own copy of the egress rules next to the browser egress table.
- `docs/architecture/backend-plugins.md`: overlay contract additions.
- `docs/architecture/scaling.md`: parser replicas, 503 and retry, any-replica status.
- `docs/operations/deploy-and-configure.md`: new variables (tokens, `PARSE_PROXY_ALLOW`, OCR profile table, limits), `RESEARCH_BLOB_URL` for `s3://` and `gs://`, the extras.
- `docs/implemented-plans/file-metadata.md`: its isolation section is superseded.
- `docs/implemented-plans/index.md` and `docs/plans/index.md`; each updated section gets `See also: [Plan: parser wrapper](../implemented-plans/parser-wrapper.md)`.
