# Plan: the parser service as a generic library (git submodule)

> **Status: implemented.** All twelve open questions and the follow-up details were decided (see "Open questions", which records the
> decisions). It built on [Plan: parser wrapper](parser-wrapper.md) and moved that code; the security model changed only as described under
> "Security changes". [Plan: OCR hybrid](ocr-hybrid.md) is affected (see "OCR").
>
> **Deviations while implementing** (decisions of the implementation session, flagged for review):
> - `index.json` is the gateway's only manifest (the user chose "gateway adopts the new shape"): this project's own keys (`url`, `target`, `text_cap`, the content types of the originals) moved to `meta.json`, and the MCP `files` result is the library's per-input recursive shape (`{<input>: {size, outputs, files?}}`), not a flat list. A job from before the library reads as no index (rebuilt from the stored originals) with its meta keys.
> - The wrapper learns the allowed top-level output directories from a separate `python -I -m cadmus_parser_server.discover` process at its start (it imports no processor code), because a parser-supplied list would not confine a hostile parser.
> - A processor whose import fails (the `files` extra is not installed) is skipped with a line on stderr, so the base image has no processor.
> - `renew_loop` revokes the credential *before the previous one* after each renewal (a transfer begun before the renewal may still use the previous one); `Client.renew` / `renew_loop` take `rekey` for grants wider than a posted URL (this project's read grant is project-wide, its input is one job); `Client.wait` accepts a callable returning the current credential.
> - Files an earlier run unpacked (`<x>.d/…` next to `<x>`) are inputs of their own on a second parse (flagged `derived`, never unpacked), and their archive is not unpacked again.
> - A consumer image does not derive from a library base image (the user asked for it): it `ADD`s `lib/cadmus-parser` and runs `server/install.sh [extras]` (tini, util-linux, the `wrapper` uid, `/scratch`, a venv, `pip install`) on any Debian base with python3; `server/Dockerfile` is only a ready-made processor-less image.
> - The default processors are split per kind of file / library, not one `files` processor (the user asked for it during implementation): `metadata`, `sizecap`, `archives`, `markdown`, `binary`, `html`, `apk`, `documents` (markitdown and OCR), `members`; they share the options entry `files` (`ctx.options_of`).
> - `MAX_PARSE_BYTES` stays a setting of the `files` processor (stored files over it get metadata only), beside the wrapper's `input_mib` (this project sets `default.input_mib` and `output_mib` to 20480).
> - The sidecars' one `egress_rules.py` is `common/egress_rules.py` (`public_rules`, `pinned_rules`); the sidecar images are built with the library root as context. The overlay generator is `cadmus_parser_server.overlay` (JSON output, which is YAML); the minting module is `cadmus_parser.minting` (the function `mint` would shadow a module of that name).

## Problem / Goal

Everything that interprets an untrusted file (documents, images, archives, audio, video) must run as a process that holds no credential and has no
route out. The parser service solves that for this project: a small trusted wrapper moves bytes between the blob store and a sandboxed parser on a
pool uid, behind an authenticated egress proxy, with an optional sidecar for remote OCR.

Nothing in that mechanism is specific to this project's file types, but three things are hard-coded to it:

- the job address: `Job.prefix` is `projects/<proj>/data/<session>/<job>` and `UUID_RE` checks three UUIDs (`wrapper.py`). The library must know
  nothing about how a consumer lays out its store: it is given one input base URL and one output base URL, and that is all;
- the feature set: `FEATURES = {"ocr"}`, and the per-file parse rule (`parse.py`, `extract.py`, `metadata.py`) is the only thing the parser can run;
- the limits: one global `PARSE_RLIMIT_AS`, `PARSE_JOB_TIMEOUT`, `MAX_PARSE_BYTES` for every job, which suits a web page and not a 2-hour video
  (10 min wall clock; `RLIMIT_FSIZE` 4 GiB is smaller than some single outputs of a video decode).

Goal: a **generic library in its own repository, included here as a git submodule**, that any system deployed as a Docker Swarm stack can use to
run untrusted-file processing safely: the wrapper, the sandbox and uid pool, the worker protocol, both sidecars, the client, and a stack file to
deploy them. This project becomes its first consumer, with unchanged behaviour. Each consuming stack runs **its own instance** of the services
(own store, own tokens); what is shared is code and images, not a running service.

Non-goals: new processing features (video, face detection and so on belong to a consumer's own processor), a different isolation model (a
`sessiond`-style capability-free helper, gVisor, one container per job), merging the two sidecars, replacing the consumer's own storage code.

## Design

### What the library is

A repository `cadmus-parser` (`git@github.com:Dn-Media-Group-Public/cadmus-parser.git`, MIT), submodule path `lib/cadmus-parser/`, versioned with tags. It contains:

| Part | From | Notes |
|---|---|---|
| `wrapper` (`wrapper.py`, uid `Pool`, status/heartbeat, upload) | `parser/spider_hoarder_parser/wrapper.py` | generalised as below |
| worker protocol (`worker.py`: protocol fd dup, `PipeSink`, record format) | `worker.py`, the `Sink` class of `parse.py` | protocol extended: every record names its input as a path list (see "Contract" item 1, `index.json`); the worker loads **all installed processors**, sorts them and dispatches each file (see "Processors") |
| `blob-sidecar` (squid, token auth, egress rules) | `blob-sidecar/` | unchanged |
| `ocr-sidecar` (profile table, token per profile, forwarder) | `ocr-sidecar/` | stays OCR-specific (it still builds the prompt and talks to the OCR endpoint) but speaks the common sidecar protocol (open question 9) |
| `egress_rules.py` | the two copies in the sidecars | one copy for both. The browser's own rules in this project's `browser-sdk` stay a separate copy (they may drift on purpose, per the parser-wrapper plan) |
| `client` (POST, read `status.json`/`index.json` from the output base with backoff and the heartbeat rule, one retry on 503/refused connection; `renew_loop`, see "Security changes" item 6) | `sdk/.../gateway/parse_client.py` minus the inline-text logic | the inline-text and terse logic is the consumer's |
| base image | `parser/Dockerfile` | wrapper, Python, tini, util-linux, fsspec; **no** processor and no format tools |
| stack file and overlay generator | `docker-compose.parse.yml`, `scripts/spider_hoarder.py parse-overlay` | see "Deployment" |
| default processor `files` (optional extra; registers **one** processor first, splitting it later is optional) | `parse.py`, `extract.py`, `metadata.py`, `ocr.py` | decided (open question 2): moves, `terse/`/`simplified/` included, groups renamed `Cadmus-*`; adapted to the per-file interface (open question 12) |

**Packages (decided).** One repo, two installable Python packages, each with its own `pyproject.toml`:
- **`cadmus-parser`** (import `cadmus_parser`): the **client** that consumers use, with the minting helper (see "Security changes" item 4) and
  its `[mint-s3]` / `[mint-gcs]` extras. It does not depend on the service.
- **`cadmus-parser-server`** (import `cadmus_parser_server`): the **service side**: the wrapper, the worker, and the `files` processor as an
  optional extra, plus the S3/GCS storage extras the wrapper needs.
The two are versioned together by the repo's tags. The packages live in `client/` and `server/` at the repo root (decided), next to `blob-sidecar/`, `ocr-sidecar/` and `docker-compose.parse.yml`. The Docker images keep the
`cadmus-parser*` prefix.

It does **not** contain: the decision of when and for what to mint a scoped credential (the consumer decides; the library only ships a small minting helper, see "Security changes" item 4; the wrapper only receives one per request), authorisation of end users
(the consumer authorises; the wrapper authorises only the consumer by its token, as today), or anything that reads the result and turns it into
tool output.

### Contract between a consumer and the library

Four surfaces between a consumer and the library (the sidecar protocol, a fifth, is documented for sidecar authors, see open question 9), all documented in the library and versioned together (`GET /version` on the wrapper returns the API version; breaking changes bump
the major version and the consumer pins a tag):

1. **Wrapper HTTP API.** `POST /jobs` with the wrapper token and a body
   `{input: {url, options?}, output: {url, options?}, options?, features?}`:
   - `input.url` / `output.url` are the two base URLs (fsspec URLs, e.g. `s3://bucket/some/prefix`). The wrapper pulls everything under the input
     base (a prefix, or a single object, which is a one-file tree) into the parser's input directory, preserving relative paths, and uploads what
     the parser writes under the output base. It never interprets the URLs beyond that: no segment checks, no templates, no layout.
   - `input.options` / `output.options` are the credential for that side (the consumer's scoped credential, as an fsspec `storage_options`
     fragment), merged over `PARSE_BLOB_OPTIONS`, in memory only. They are passed to fsspec as given, with no
     filtering (see "Security changes"). The two sides may be different stores.
   - `options` is **keyed by processor name** (`{"files": {…}}`); a processor sees only its own entry. There is **no `processor` field**: the
     consumer does not select processors, the worker runs every installed one that accepts a file (see "Processors"). `features` stays
     `{name: profile}`. There is no class or limit field: the wrapper decides the media class itself (see "Limits").
   - Answers 202 / 409 / 503 / 400 as today. The job's identity (for the 409 check and the in-memory state) is its output base URL.
   - **The 202 body carries `id` (an opaque, unguessable parse job id the wrapper generates) and `owner`**: `owner` is the address (`ip:port`) of the replica that took the job, taken from the **local address of the connection
     the POST arrived on**, so it is the address as seen on the caller's own network (the wrapper is attached to several per-consumer networks).
     The stack stays in VIP endpoint mode; the consumer caches `(id, owner)` per job for renewals. `owner_replica` in `status.json` is unchanged.
   - **`POST /jobs/renew`** (same wrapper token), body `{id: <parse job id>, options: {<url>: {…storage_options…}, …}}`, sent by the consumer to the
     cached `owner` address. `options` is keyed by URL, the same `{url: options}` mapping `mint` returns, so the consumer passes it through as is.
     The wrapper applies each entry to the job's `input.url` or `output.url` it equals (exact string match with what was posted) and replaces that
     side's credentials in memory; a key matching neither URL answers 400. It answers 200; an unknown `id` (or a replica that does not own it)
     answers 404, and a job that has already finished (`done`/`failed`; its id is remembered in memory for a while) answers 410. The consumer's
     renewal loop stops on any non-200 answer; the two codes only tell it "lost" from "finished". The job's identity for the 409 check remains its output base URL; the `id` is only the handle for renewal. The wrapper opens its filesystems **per file transfer**, not once per job, so each pull and
     each upload part uses the newest credentials. A job that stops receiving renewals fails when its current credential expires.
   - **Errors have the sidecar shape.** Every non-2xx answer of the wrapper has a JSON body `{"error": "<string>", "traceback"?: "<string>"}`, as
     in the sidecar protocol (the wrapper decides whether a `traceback` is included). The same members are stored in `status.json` when a job
     is `failed` (`{"status": "failed", "error": "…", "traceback"?: "…", "owner_replica", "updated"}`), so a failure after the 202, such as the
     input-size check, a credential error or a limit kill, has the same shape as one returned over HTTP.
   - **There is no GET.** The wrapper writes `status.json` (`running`, then `done`/`failed`, with `owner_replica` and `updated`) and `index.json`
     under the output base, `index.json` before `status.json`, and writes `running` before it answers 202. The consumer reads them from the store
     with the credential it already has; the client library implements the heartbeat rule (10 s rewrite, `running` older than 30 s is `failed`,
     "wrapper lost"). This removes the one place the wrapper needed to know a job's address on a read.
   - **`index.json` is per input file and recursive.** The wrapper builds it from the records, one fresh file per job (it does not merge with an
     existing one):
     ```
     {"files": {"<input path>": {"size", "content_type"?, "meta"?, "notes"?,
                                 "outputs": {"<output path>": {"size", "content_type"?, "meta"?}},
                                 "files": {"<member path>": { …same shape, for archive members… }}}},
      "notes"?: "<wrapper-level remarks>"}
     ```
     Input keys are relative to `input.url`, output keys relative to `output.url`; there are **no outputs without an input**. The wrapper creates
     an entry for every pulled input (also when nothing came out of it) and takes `size` from its own `fstat` for pulled inputs and for every
     output. `content_type`, `meta` (an object, passed through unchanged, e.g. a text mark) and `notes` (one string per entry, several joined
     with newlines) are declared by the processors, are **size-capped and untrusted** (the parser may be hostile), and may be absent. The `size`
     of an archive **member** is processor-claimed (the member exists only in the parser's scratch space). `index.json` and `status.json` are
     never listed. The failure reason of a job is in `status.json` (`error`), not in `index.json`.
   - **Sink records carry the input address as a list**, `["a.zip", "docs/m.pdf"]` (one element per archive level; `["a.pdf"]` for a plain input).
     The wrapper checks the first element against the files it pulled (an unknown input is a protocol violation, as for any bad record); deeper
     elements are the worker's claim and the wrapper creates the nesting on demand. A depth cap (8) and an entry cap (the existing
     `MAX_ARCHIVE_MEMBERS`) bound the tree; exceeding one adds a note and does not fail the job.
2. **Processor entry points.** A Python package registers one or more `cadmus_parser.processors` entry points (the group name stays
   `cadmus_parser.processors`, decided, although the service that loads them is `cadmus-parser-server`). Each resolves to a processor with a
   `name`, optional `before` and `after` lists of other processor names, the `top_dirs` it may write, `accepts(file) -> bool` and
   `process(sink, file, ctx)`, with no storage access and no network. The consumer does not select processors: the base image has none, and a
   consumer's image gets whichever packages it installs (the `files` extra, its own). Nothing the caller sends is imported. See "Processors".
3. **Stack file.** `docker-compose.parse.yml` (a swarm stack file, deployed with `docker stack deploy -c … -c …`) defines `parser`, `blob-sidecar`
   and, when profiles are configured, `ocr-sidecar`, with the networks and capabilities of today. The consumer includes it with `-c`.
4. **Environment variables**, listed in one table in the library docs (limits, tokens, `PARSE_BLOB_OPTIONS`, profiles, pool size).

### Generalisations (the only behaviour changes)

- **Input and output base URLs instead of a job address.** The `UUID_RE` check, `Job.prefix` and the `projects/…/data/…` layout leave the
  library. This project sends `input.url` = `…/<job>/original` and `output.url` = `…/<job>` (so its `original/`, `parsed/`, `metadata/` layout is
  just what its processor writes); a consumer that stores by content hash sends whatever prefixes it wants.
  What the wrapper still enforces on the output side, independent of layout: every path a parser announces is relative, has no `..`, is a regular
  file opened with `O_NOFOLLOW`, starts with a top-level name some installed processor declared, and so stays under the output base; size from its own
  `fstat`; record, line and total-byte caps; `index.json` and `status.json` reserved.
  This loosens the wrapper's confinement; see "Security changes" below for exactly what, and what replaces it.
- **Processors: registered, ordered, dispatched per file (open question 12).**
  - *Ordering.* No numeric priority. A processor may declare `before` (processors that must run after it) and `after` (processors that must run
    before it); names of processors that are not installed are ignored, and either side may declare an edge. The **worker sorts topologically at
    start of every job, at import time**; free choices are broken alphabetically by name, so the order is the same on every run. A **cycle is an
    immediate error** naming the processors involved: the job fails and the error is in `status.json`. The wrapper imports no processor code.
  - *Dispatch.* The worker walks the input tree. For each file it runs **every processor whose `accepts` is true, in order**; each adds its own
    outputs. A processor may claim a file exclusively (`ctx.claim()`), which stops the later ones for that file. Two processors writing the same
    output path for the same input is a violation: the later write is rejected and noted on that entry.
  - *Archives.* An archive processor unpacks and calls `ctx.recurse(path)` for each member; the worker runs the same chain on it (depth and entry
    caps as in "Contract") and builds the nested `files` entries and record addresses itself. No processor needs archive logic of its own.
  - *Unhandled and failing files.* A file no processor accepts gets the note "not parsed (no processor)" and is not a failure. A processor that
    raises on a file leaves a note on that entry and the rest continue. The parser is still one process per job, so a crash or a limit kill ends
    the whole job, as today.
  - *Options and features.* `ctx.options` is the processor's own entry of the POST `options`; `ctx.features` as today.
  - *Classes.* The wrapper's media classes (for limits) and the types a processor accepts are independent.
- **Features registry.** `FEATURES` becomes a table in wrapper config: a feature name maps to a profile table (`<FEATURE>_PROFILES`:
  name → `{url, token}`); the wrapper still puts **only the requested profile's** `<FEATURE>_URL`/`<FEATURE>_TOKEN` in the parser's environment.
  Only `ocr` ships; a new kind of sidecar is added by adding an entry, not code.
- **Limits, per media class.** The operator configures limits in one JSON env var, `PARSE_LIMITS`, nested by class and then limit name; in dotted
  notation, as the docs and `.env.example` write it:

  ```
  default.deadline_s = 600    default.ram_mib = 6144    default.fsize_mib = 4096    default.input_mib = 50      default.output_mib = 4096
  video.deadline_s   = 3600   video.ram_mib   = 16384   video.fsize_mib   = 16384   video.input_mib   = 20480   video.output_mib   = 20480
  image.ram_mib      = 44
  ```

  - Names (the unit is part of the name): `deadline_s` (wall clock, seconds, then the process group is killed), `ram_mib` (address space per parser, MiB; 0 = no `RLIMIT_AS`, rely on
    the container memory limit), `fsize_mib` (largest single output file, MiB, `RLIMIT_FSIZE`), `input_mib` (largest input tree, MiB; 0 = unlimited;
    replaces `MAX_PARSE_BYTES`), `output_mib` (total uploaded for a job, MiB; 0 = unlimited), and `nproc` / `nofile` (plain counts, no unit) as today.
  - **Every built-in default is finite, for every class, including `video`** (open question 4): large where a class needs it, but never
    unlimited, so a job cannot exhaust the wrapper's scratch disk or memory. `0` (unlimited) is accepted only when the operator sets it
    explicitly in `PARSE_LIMITS`; the library never ships it. The disk bound is `input_mib` + `output_mib` + `fsize_mib`, since `RLIMIT_AS` does not limit disk.
  - A class has the built-in default of each limit (today's values), overridden by `default.*` if set, overridden by `<class>.*` if set. A limit
    not mentioned for a class falls through.
  - **The wrapper picks the class; neither the consumer nor the parser does.** The classes are a small fixed set defined by the library
    (`video`, `audio`, `image`, `document`, `archive`, `other`), so the operator's `PARSE_LIMITS` keys are known. After the wrapper has pulled the
    input tree, it classifies **each file** by extension and, failing that, by comparing its first 16 bytes with a built-in table of magic numbers
    (stdlib byte comparison; the wrapper decodes nothing). The job's limit set is, **per limit, the maximum over the classes present** (a tree
    with a photo and a video gets the video's deadline and the larger of the two `ram_mib` values; a limit of 0 = unlimited counts as the largest).
    An archive gets the `archive` class for itself and everything in it: its members are only known after unpacking, which happens inside the
    parser under the limits already chosen, so the operator should set `archive.*` generously and the existing archive caps
    (`MAX_ARCHIVE_BYTES`, `MAX_ARCHIVE_MEMBERS`) remain the bound on what an archive can expand to.
  - Why the wrapper and not the parser: `RLIMIT_AS`, `RLIMIT_FSIZE` and the deadline are fixed when the parser process is started and cannot be raised
    by it afterwards, so the class has to be known before the start; a parser-side classification would need a separate sniffing process first
    (a second start per job) and would let a hostile file name the class it wants. The wrapper reading 16 bytes per file as a byte comparison is
    the smallest thing that works. Ordering: the pull itself is capped at the largest `input_mib` of any class (the operator's ceiling), then the
    class's own `input_mib` is checked before the parser starts and the job fails with a clear status if it is exceeded.
  - **Only the operator sets numbers.** A caller cannot pass or raise a limit.
  - Hours-long jobs: the wrapper's heartbeat is unchanged; the client's poll window is a parameter (default 12 min), so a consumer with long
    classes polls longer or reads `status.json` asynchronously. Credentials are kept alive by the consumer's `renew_loop` (see "Security changes"
    item 6), independent of the class's `deadline_s`.
- **Output directories.** `TOP_DIRS` (the top-level names a parser may write) is declared by the installed processors (the union of their `top_dirs`), not hard-coded; `index.json` and
  `status.json` stay reserved.

### Security changes

**What the old design guaranteed.** Three layers kept a job inside its own data:

1. *Who may call:* the wrapper token. Only the gateways, on their per-backend parse networks, can reach the wrapper at all.
2. *Where it may touch:* the wrapper built every key itself. The store (`RESEARCH_BLOB_URL`: scheme and bucket) came from the operator's
   configuration; the only caller-supplied parts were three UUIDs, which the wrapper checked against a regex. So even a fully compromised caller
   could only make the wrapper read and write under `<root>/projects/<uuid>/data/<uuid>/<uuid>/`, on the operator's store.
3. *What it may touch:* the scoped credential the gateway mints (read the project's `data/`, write only `<session>/<job>/`, 900 s), enforced by
   the store.

The hostile process is the parser, and it is out of all three: it holds no credential, token or URL.

**What changes.** Layer 2 is gone. The caller now supplies two complete URLs (scheme, bucket, prefix) and the credential options. The wrapper
no longer knows what a "job location" looks like, so it cannot tell a job's prefix from any other prefix. Layers 1 and 3 and the parser's
confinement are unchanged.

**What this does and does not make possible**, by who the attacker is:

- *The hostile file or the parser (the main adversary).* Unchanged. URLs and credentials never reach the parser; the paths it announces are still
  validated (relative, no `..`, regular file via `O_NOFOLLOW`, declared top-level directory), so it can write only under the output base the wrapper
  chose to upload to, never elsewhere. File contents cannot influence the URLs, which the consumer takes from its own state.
- *A bug in the consumer that builds the wrong URL* (for example a path assembled from user text, with `..` or a different project's id).
  Before: layer 2 confined the wrapper to a UUID-shaped job path, layer 3 confined it to the credential's scope. Now only layer 3 confines it, and
  the wrapper will faithfully read or overwrite anything the credential permits. For this project the read credential is project-wide
  (`projects/<proj>/data/*`), so such a bug could now make the wrapper copy another session's files into a job's input or overwrite another job's
  `original/`, `parsed/`, `index.json` or `status.json` within that project (previously: only the one job path). The consumer already has direct
  access to the same data, so this is lost defence in depth, not lost secrecy from the consumer; the fix is a credential scoped to the job
  (which this plan documents as the consumer's responsibility) and the consumer building URLs from validated ids.
  **This project's decision:** the read credential stays **project-wide** (`projects/<proj>/data`), because a parser may legitimately read other,
  earlier files of the same project for correlation. The library's read confinement is therefore the input base the wrapper pulls
  (`<job>/original`); the credential does not narrow it further for this project. This is a change in how this project uses the unchanged API,
  not a library change, and it is the same read scope as today. The write side stays job-scoped (`<session>/<job>/`).
- *Someone who steals the wrapper token* (it is one shared secret, in the gateways' environment). Before: they also needed a credential the store
  accepts, and could only name job-shaped paths on the operator's store. Now, **without further limits**, they could name any bucket on any host
  reachable through the blob-sidecar, which has no destination allowlist and allows the whole public internet (it blocks private ranges, the
  metadata server and other stack services; the dev store is an explicit exception). The wrapper would become an authenticated fetcher and
  uploader: pull files from an attacker's bucket, run them through the parsers (free compute, and a way to attack the parsers from outside the
  intended path) and upload the results to another attacker-chosen bucket. It would not reach the real store without a credential for it.
- *fsspec options and URL chaining.* This one already exists today: the wrapper does `fsspec.core.url_to_fs(blob_url, **{**blob_options,
  **creds})`, so the gateway-supplied credential dict goes into fsspec as keyword arguments, unfiltered. For `s3fs` that includes `endpoint_url`
  and `client_kwargs`; for `gcsfs` `token` can be a path to a local credentials file. With a caller-chosen URL it gets worse: fsspec URLs can be
  chained (`simplecache::s3://…`), can name other filesystems (`file://`, `http://`, `sftp://`, `zip://`), and the filesystem is a plug-in
  chosen by the scheme. Unfiltered, a caller could make the wrapper read local files of its container (its own 0600 secret files are in reach of the
  wrapper uid) or make HTTP requests. This is the most dangerous part of the loosening and also a hole to close now, independent of this plan.
- *Overlapping jobs.* The job's identity is its output URL. Two jobs whose output bases are equal or nested (`x/` and `x/y/`) share or contain each
  other's `status.json`, `index.json` and artefacts: one can report the other as `running`/`done`, or be restarted by it as "stale". The old
  design had the same collision only for an identical job triple; nesting could not occur.
- *Resource abuse.* A caller can name an arbitrarily large input tree. The `input_mib` limit is checked against the object listing before anything is
  pulled (it needs a listing right in the credential, which any working job has).

**What replaces layer 2.** None of these is as strong as "the wrapper builds the key", but together they restore a confinement the operator
controls instead of one fixed in the code:

1. **No base-URL allow-list (decided, open question 10).** The wrapper accepts any `input.url` and `output.url`. The consumer is trusted: it
   decides the read base and the write base, and confines the credential(s) it gives the wrapper to exactly those two. `PARSE_ALLOWED_BASES` is
   not part of the library.
2. **No scheme or option allow-list (decided, open question 10).** The URL and the options go to fsspec as given: any scheme, chaining
   (`::`), any option. The parser never sees or influences them, and the consumer is trusted. The library docs state that the consumer must
   never build URLs or options from untrusted input, because they can name `file://`, `http://` or chained filesystems, or (for `gcsfs`) a local
   credentials file.
3. **Identical jobs only (decided).** The job's identity is its output base URL, and the wrapper refuses (409) a job whose output base is
   *identical* to that of a job running on that replica, and, across replicas, to one whose `status.json` shows it running (the stale-heartbeat rule
   already reads it first); this is today's behaviour with the URL in place of the job triple. Nested or overlapping bases are **not** detected:
   keeping output bases disjoint is the consumer's responsibility, and the library docs say that nested bases share and can corrupt each other's
   `status.json`, `index.json` and artefacts.
4. **Job-scoped credentials (decided: the library ships a small minting helper).** The requirement is unchanged: the credential allows reading
   the input base and writing the output base and nothing else, and the wrapper cannot verify it. The library contains a **small Python helper
   for minting such credentials** (for S3 and GCS), generalised from this project's `gateway/creds.py`, which today builds a per-job IAM role,
   policy and STS session for S3 and a Credential Access Boundary (downscoped token) for GCS, with a 900 s lifetime and a sweep of leftovers.
   The consumer still decides when to mint and for which bases; the helper only turns "read base, write base, lifetime" into the `storage_options`
   fragment the request needs. No test of the over-broad-credential failure mode.
   **Details:** (a) **decided:** the S3 mechanism is a role and policy per job plus STS, as today (works on AWS and SeaweedFS; needs IAM permissions and a
   sweep of leftover roles);
   (b) **decided:** a generic `mint(grants, lifetime=900)` where `grants` is a list of `(url, "read" | "write")` entries. **`read` means read and
   list; `write` means read, list and write** (the wrapper must read `status.json` under the output base and list the input base). It returns a mapping
   `{url: options}`, one entry per grant, where `options` is the `storage_options` fragment for that URL, so the consumer looks up each side by its
   own URL (`input.options`, `output.options`). The helper groups grants by store and mints **one credential (one role, policy and session) per
   store**; grants in the same store share it, and their entries in the mapping hold the same credential, so nothing is duplicated;
   (c) **decided:** the dependencies (`boto3`, `google-auth`) are optional extras of the consumer-side (client) package (for example
   `[mint-s3]` and `[mint-gcs]`), imported only when the helper uses that store; (d) **decided:** cleanup of leftovers. What `mint` returns is a **handle**: it behaves as the `{url: options}` mapping of (b) and has a
   `revoke()` that removes the S3 role and policy it created straight away (GCS needs nothing, the token expires). The helper also provides
   `sweep(max_age)`, which removes any helper-created role older than `max_age`; the consumer schedules it from its own periodic task, and the
   helper names its roles with its own prefix so that `sweep` touches only those. Two additions: the handle can be **persisted** (serialised
   to a plain structure, for example into a database, and restored later in another process to `revoke()` it), and it works as a **context
   manager** (`with mint(...) as creds:` revokes at the exit of the block). The serialised form holds **only what is needed to
   revoke** (role and policy identifiers, the store, the creation time) and **no secrets**, so it is safe in a database or a log; a restored handle
   can `revoke()` but its credentials exist only in the process that minted them;
   (e) **decided:** the helper lives in **the same package as the client** that consumers use, and **not** in the wrapper/parser package.
   The library therefore builds **at least two installable packages**: a consumer-side one (client and minting helper, with the `boto3` and
   `google-auth` extras) and a service-side one (wrapper, worker and the `files` processor), as `cadmus-parser` and `cadmus-parser-server`.
   (f) **This project's grants:** it calls `mint` with a **project-wide read grant** (`…/projects/<proj>/data`, read) and a **job write grant**
   (`…/<session>/<job>`, write). The library's "job-scoped" recommendation is not followed on the read side here; see "Security changes".
5. **Input size checked in two steps (decided).** Before the pull, the wrapper lists the objects under the input base and sums their sizes
   against the largest `input_mib` of any class (the operator's ceiling); this needs list permission on the input base, and a credential without
   it gives a clear error. After the pull and the classification, the tree is checked against the job's class `input_mib` (see Limits) and the job
   fails with a clear status before the parser starts if it is exceeded.

6. **Credential renewal (decided: push).** A job, a long video or a large archive, can run orders of magnitude longer than a credential's TTL, so
   renewal is required. The **consumer pushes**: a background thread per running job mints fresh credentials every N minutes (N well under half
   the TTL) and sends them with `POST /jobs/renew` (`{id, options}`) to the `owner` address returned in the 202. The library ships this as
   `renew_loop(grants, id, owner, ...)` in the client package, tying `mint` to the wrapper API; the consumer starts it after the 202 and stops it when `status.json` shows
   `done`/`failed` (then `revoke()`). Why push: the wrapper never initiates a connection to the consumer, holds no extra secret and needs no
   consumer-side service; revoking is stopping the pushes. A retry on 503 or a transient connection error is part of the loop; a connection error
   or 404 that persists means the replica (and the job) is gone, so the loop stops and the consumer marks the job failed ("wrapper lost").
   A consumer restart kills its loops and the job fails when its credential expires; resuming renewal across a consumer restart is out of scope.

**Residual risk (accepted, open question 10):** with no scheme or option filtering either, a stolen wrapper token can also name `file://` or
`http://` URLs and make the wrapper read its own files or make requests. confinement of the *consumer* is no longer the wrapper's job. A consumer bug, or a stolen
wrapper token together with a credential, reaches whatever that credential reaches; with no base-URL allow-list a stolen token alone could also
make the wrapper fetch from and upload to any store the blob-sidecar can reach. What the wrapper does guarantee is the confinement of the
**parser** to the one reading prefix and the one writing prefix it was given.

### Images and process layout

The library builds a **base image** (`<IMAGE_PREFIX>cadmus-parser-base:<tag>`). A consumer's own Dockerfile starts `FROM` it, installs its processor
package and its format tools (ExifTool, ffmpeg, Tesseract, `unar`, …), and the entry script of the base image starts the wrapper. One consumer image
per stack; the sidecars are used as built by the library. Build context concerns (this project builds with the repo root as context) are handled by
`deploy.sh` building the base image from the submodule directory first.

### OCR

Tesseract runs on untrusted pixels, so it is part of a processor inside the sandbox; the remote step goes through the `ocr-sidecar` because the
sandboxed process cannot hold endpoint credentials. Both therefore live in the library with the `files` processor (`ocr.py`), and
[Plan: OCR hybrid](ocr-hybrid.md) is implemented **there**: this plan only moves the sidecar client and the wrapper's profile selection; the
Tesseract pass, the confidence gate and the PDF rasterising are that plan's work, done in the library repo. That plan gets an amendment pointing to
this one when this plan is implemented.

### Deployment

- Submodule at `lib/cadmus-parser/`, pinned to a tag. `git clone --recurse-submodules`; `deploy.sh` runs `git submodule update --init` and fails with a
  clear message if the directory is empty. Docs and `README.md` say so.
- `deploy.sh` includes `lib/cadmus-parser/docker-compose.parse.yml` where it includes `docker-compose.parse.yml` today; the project-specific overlay
  (a `<backend>-parse` network per gateway, `PARSE_URL`/`PARSE_TOKEN` in each gateway, the dev store's network, `PARSE_PROXY_ALLOW`) stays here
  but is produced by the library's small overlay generator, which takes a list of consumer service names and an optional dev-store address and prints a
  stack file fragment. The generator is the library's only deploy-time code; this project's `scripts/spider_hoarder.py` calls it.
- Only Docker Swarm stacks are supported and documented (no other orchestrator).

### Migration (this project)

Behaviour must be identical after each step, with the existing parser tests passing against the moved code.

1. Create the library repo by **cloning this repository** (full history), pointing the clone's `origin` at
   `git@github.com:Dn-Media-Group-Public/cadmus-parser.git`, and turning the clone into the library: remove everything except `parser/`,
   `blob-sidecar/`, `ocr-sidecar/`, `docker-compose.parse.yml` and `LICENSE`, split it into the two packages `cadmus-parser` (client, new, in `client/`) and `cadmus-parser-server` (the
   existing `parser/` renamed to `server/`, with `spider_hoarder_parser` renamed to `cadmus_parser_server`), and rename the images to `cadmus-parser*`, and add the library's own README and CI (the existing unit tests). Nothing is pushed until the owner says so.
2. Replace the three directories here with the submodule; fix build contexts in `deploy.sh` and the stack file.
3. Generalise in the library, one commit per item, each with a test: input/output base URLs and removal of the GET status endpoint, per-side credential pass-through, features
   registry, per-class limits (`PARSE_LIMITS`), processor entry points, output directories, `owner` in the 202 and `POST /jobs/renew` with
   per-transfer filesystem opening. This project sends `input` = `<job>/original`,
   `output` = `<job>`; its config reproduces the old limits as `default.*`. Also here: the output-base identity check (409 for an identical base
   only), the two-step input size check, wrapper-side classification into the six classes, and the `ocr-sidecar` moving to the common protocol
   (`POST /process`, standard status codes, JSON error body).
4. Move the `files` processor behind an entry point (one processor, adapted to the per-file `accepts`/`process` interface; it is discovered
   automatically, nothing selects it). Rename the `SpiderHoarder-*` metadata groups to `Cadmus-*`
   in the library and every place here that reads them, in one step.
5. Write the client (`client/`) and the minting helper (`mint`, generalised from `gateway/creds.py`: role and policy per job plus STS, handle with
   `revoke()`, persistence and context manager, `sweep`) and `renew_loop` (the gateway starts one per running job, project-wide read grant and job
   write grant re-minted each round); `gateway/parse_client.py` shrinks to the inline-text and terse logic on top of the client,
   and `gateway/creds.py` is replaced by the helper.
6. Overlay generator; `parse-overlay` here delegates to it.
7. Docs (below) and `docs/implemented-plans/index.md`; move this plan.

### Library documentation (requirement)

The library repo must be usable by a project that has never heard of spider-hoarder, so its docs contain no spider-hoarder concepts or examples:

- `README.md`: the problem, the threat model, what is and is not protected, the parts.
- Quickstart: a minimal consumer swarm stack and a toy processor, end to end.
- Reference for the contract surfaces (wrapper HTTP API, processor entry point, stack file, environment variables, and the **sidecar protocol**
  as a written spec for anyone adding a sidecar), with the environment variable table and the limits (`PARSE_LIMITS`) reference.
- "How to write a processor", "how to build a consumer image" and "how to write a sidecar" (the protocol spec).
- Client and minting helper: how a consumer calls the wrapper, reads `status.json`, and mints, persists, revokes and sweeps scoped credentials
  (S3 and GCS, with the extras), and how `renew_loop` keeps them alive using the `owner` address from the 202.
- Security guide: what the consumer must do (job-scoped credentials, keeping output bases disjoint, never building URLs or options from
  untrusted input, keeping the wrapper token away from untrusted data) and what the wrapper guarantees (the parser is confined to the one read
  prefix and one write prefix).
- Versioning and upgrade notes (API version, tags).

## Open questions (all decided)

1. **Name, repo location and licence.** **Decided:** name `cadmus-parser`, repository
   `git@github.com:Dn-Media-Group-Public/cadmus-parser.git`, licence MIT, the same as spider-hoarder. The library must not carry this project's
   name and must be fully usable, and documented, for a separate project that knows nothing about spider-hoarder (see "Library documentation").
2. **The default `files` processor.** **Decided:** it moves into the library (markitdown, trafilatura, archives, binary decoders,
   ExifTool/`ffprobe`/GDAL metadata, OCR), as an optional extra. The `terse/` and `simplified/` outputs move with it and are always written;
   consumers that do not want them ignore them (no `outputs` option). The ExifTool extension groups are renamed from `SpiderHoarder-<Format>` to
   `Cadmus-<Format>` (`Cadmus-Media`, `Cadmus-Raster`, `Cadmus-Vector`, `Cadmus-Office`, `Cadmus-PDF`); this changes output this project reads
   today, so the rename and the reading code here are changed in the same step (migration step 4).
3. **History.** **Decided:** keep it by cloning this repository in full and reducing the clone to the library (migration step 1), instead of
   `git filter-repo` or a squashed first commit. The new repo therefore carries the whole of this project's history, including paths that are
   not part of the library.
4. **Heavy decoders and `ram`.** **Decided:** large but finite built-in defaults for every limit of every class, so the wrapper cannot run
   out of disk or memory; `0` (unlimited) stays possible only as an explicit operator setting. The numbers for heavy classes are chosen when the
   first such processor is added; the example in "Limits" is illustrative.
5. **Units and names of the limits.** **Decided:** self-describing names with the unit as a suffix: `deadline_s` (seconds), `ram_mib`,
   `fsize_mib`, `input_mib`, `output_mib` (MiB), plus the plain counts `nproc` and `nofile`. All the limit names above, in the examples and in the
   library docs and `.env.example`, use these.
6. **Classification by the wrapper: the details.** **Decided:**
   - The **wrapper** classifies, before the parser starts. Today's code classifies inside the parser (`mimetypes` on the name in `parse.py`,
     `metadata.py`, `extract.py`, plus a magic check for a few binary formats) and applies one global set of limits; per-class limits need the class
     before the process starts, so classification moves to the wrapper. Classification inside the sandbox is rejected.
   - **Extension first, magic number only as a fallback** when the extension says nothing, following the `mimetypes` approach used today. A
     small built-in table of magic numbers covers the fallback.
   - **Class set decided:** six classes, `video`, `audio`, `image`, `document`, `archive`, `other`. `PARSE_LIMITS` accepts exactly these keys
     plus `default`.
   - **Mixed trees decided:** the job's limit set is, per limit, the maximum over the classes present (0 = unlimited counts as the largest), as
     in "Limits". A tiny file next to a huge video therefore runs under the video's limits. The docs mention that a consumer who needs tighter
     limits can split the tree into one job per class itself; the wrapper does not split.
7. **No GET status endpoint.** **Decided:** no GET and no `POST /jobs/status`. The consumer reads `status.json` and `index.json` from the output
   base with the credential it already has; the client library implements the heartbeat rule. The wrapper stays ignorant of layout on reads.
8. **One shared running service across stacks?** **Decided:** no. Each consuming stack runs its own instance (own store and tokens), so a compromise in one stack cannot reach another's data through the wrapper. A shared service, which would need
   per-consumer wrapper tokens, allowed bases and pool quotas, is out of scope and would need its own plan.
9. **Sidecar kinds beyond OCR.** **Decided:** define one common **sidecar protocol** now, so that adding a new sidecar is smooth. Each sidecar stays
   specific to its job and does its own work behind the protocol (the `ocr-sidecar` still builds the prompt and talks to its endpoint; nothing
   about OCR moves into the protocol or into the processor). The protocol is plain HTTP, bytes in and bytes out: the request body is the input
   bytes, with headers such as `Content-Type` (MIME type) and `Content-Disposition` / filename for what they describe; the response body is the
   result bytes with its own `Content-Type` and, where relevant, filename. The sidecar keeps holding its profile table and the per-profile
   credentials, and the wrapper still gives a job only the requested profile's sidecar URL and token, as today. The `ocr-sidecar` is adapted to
   this protocol.
   **Tokens decided:** the per-profile token stays an **opaque secret that indexes the sidecar's own profile table** (the sidecar holds every
   profile's full data, including upstream URL, model, API keys and prompt; the wrapper holds only name -> `{sidecar url, token}`; the sandboxed
   process holds one token). Signed or encrypted tokens carrying profile data (JWT/JWE, public-key crypto) were considered and rejected: they
   would move the API keys into the wrapper's configuration and add key management, for a property (a job can only use the profile it was given)
   that the opaque token already provides.
   **Details:**
   - (a) **decided:** one route for every sidecar, `POST /process`: one file in (the body, with `Content-Type` and filename headers), parsed data
     out. Nothing else names the feature or profile: what the sidecar does and which profile it uses is controlled by the token and the file
     content. The `ocr-sidecar`'s `/ocr` becomes `/process`.
   - (b) **decided:** a sidecar is **not a proxy**. It forwards no request header, ever. It reads the request headers itself and makes its own
     choices from them: `Authorization: Bearer <token>` carries the token (which selects the profile), `Content-Type` describes the posted file
     (`image/png`, a Word document, ...), `Content-Disposition` gives its filename, and any other header is allowed and is used as that sidecar
     sees fit. What it sends upstream is built by the sidecar. The response carries `Content-Type` and, where relevant, `Content-Disposition`, set
     by the sidecar. The only methods are `POST /process` and `GET /health` (as today).
   - (c) **decided:** each sidecar defines its own size caps, concurrency and timeouts (as the `ocr-sidecar` does with `OCR_MAX_BODY`,
     `OCR_MAX_CONCURRENT` and its upstream timeout); there are no protocol-wide numbers. The protocol requires only that a sidecar enforces *some*
     request-size limit and answers `413` when it is exceeded, and answers `503` when it is busy. The library docs state this.
   - (d) **errors decided:** a sidecar uses the standard HTTP status codes with their standard meanings; the protocol defines no codes of its own.
     An error response has a JSON body with an `error` member (a string describing the error) and may have a `traceback` member (a string with a
     formatted traceback). Whether a `traceback` is sent, and what it contains, is **the sidecar's own decision**: it knows its setup and what could
     leak, and may decide differently per call. The protocol sets no rule. **Options** travel as extra request headers that the sidecar interprets,
     as decided in (b); their names use the standard HTTP `X-` prefix only (for example `X-Language`), with no project-specific prefix.
     (d) is fully decided;
   - (e) **decided:** the library gives a **written spec only** for the sidecar protocol (route, headers, status codes, error body, the `413`/`503`/
     `GET /health` requirements). No conformance test suite and no skeleton service; the library's own sidecars have their own unit tests.
     Open question 9 is fully decided.
10. **Confinement in place of key construction.** **Decided (partly):** the consumer is **trusted**; the parser is the one that may be hostile.
    The wrapper's guarantee is therefore to confine the parser to **one reading prefix** (`input.url`: the wrapper pulls only from it) and **one
    writing prefix** (`output.url`: the wrapper uploads only under it, from validated relative paths). No `PARSE_JOB_REF` pattern and no
    mandatory key shape; the library builds no keys. **`PARSE_ALLOWED_BASES` is dropped:** the wrapper accepts any base URLs; it is the consumer's job to choose them and to confine the
    credential(s) to those two prefixes. **The scheme and option allow-lists are dropped too**
    (the parser never sees the URLs or options, so they only matter for a stolen token or a consumer bug, which is accepted as for the bases).
    Open question 10 is fully decided.

11. **Credential renewal.** **Decided:** required (long jobs outlive any credential TTL). The consumer **pushes** renewals with `POST /jobs/renew`
    to the replica address the wrapper returns as `owner` in the 202 (the local address of the POST's connection), identifying the job by the
    opaque `id` the 202 also returns, with a body `{id, options: {<url>: {…}}}`; the stack stays in VIP mode,
    and no broadcast, `dnsrr` or consumer-side renewal service is used. `read` grants mean read+list, `write` grants mean read+list+write. See
    "Security changes" item 6.
12. **Processors and `index.json`.** **Decided:** processors are not consumer-selected; each registered processor declares its own `before`/`after`
    lists (no numeric priority), sorted topologically by the worker at the start of every job, a cycle being an immediate error. Every processor that
    accepts a file runs, in order, with an exclusive-claim option; the worker drives archive recursion; the interface is per file
    (`accepts`, `process(sink, file, ctx)`); `options` is keyed by processor name; unhandled files get a note; `files` registers one processor
    first. `index.json` is per input file and recursive, with the input address a list in the sink records (see "Contract" item 1).

## Files to change

| File | Change |
|---|---|
| `lib/cadmus-parser/` (new submodule), `.gitmodules` | the library (repo `cadmus-parser`: `client/`, `server/`, `blob-sidecar/`, `ocr-sidecar/`, `docker-compose.parse.yml`) |
| `parser/`, `blob-sidecar/`, `ocr-sidecar/` | removed (moved into the library; `parser/` becomes `server/`) |
| `parser/Dockerfile` replacement (here) | `FROM` the library base image; installs the processor, ExifTool, ffmpeg, `unar` |
| `docker-compose.parse.yml` | removed here; the library's file is included |
| `deploy.sh` | submodule init and check, base image build, stack file path |
| `scripts/spider_hoarder.py` | `parse-overlay`, `parse-env`, `build-parse` delegate to the library generator; `PARSE_LIMITS`, no `PARSE_ALLOWED_BASES` |
| `sdk/spider_hoarder_sdk/gateway/parse_client.py` | thin layer over the library client (`cadmus-parser`) |
| `sdk/spider_hoarder_sdk/gateway/creds.py` | replaced by the library's minting helper (`cadmus_parser`, extras `[mint-s3]`/`[mint-gcs]`); this project keeps only its policy for which bases to mint: project-wide read, job-scoped write |
| `tests/live/e2e_parser.py`, `docs/architecture/jobs-and-storage.md`, `docs/implemented-plans/file-metadata.md` | `SpiderHoarder-<Format>` metadata groups renamed to `Cadmus-<Format>` (migration step 4) |
| `.env.example` | `PARSE_LIMITS` (dotted notation, unit-suffixed names), processor settings, with this project's values |
| `README.md`, `CLAUDE.md` | clone with `--recurse-submodules`; where the parser code now lives |

## Docs to update

- `docs/architecture/jobs-and-storage.md` (the parser service section) and `docs/architecture/overview.md` (service table, file table): the code
  now lives in the library; keep the security description and link to the library docs.
- `docs/architecture/backend-plugins.md`: parse wiring through the generator.
- `docs/operations/deploy-and-configure.md`: submodule, the new variables, per-class limits (`PARSE_LIMITS`).
- `docs/architecture/jobs-and-storage.md`: the `Cadmus-*` metadata groups, the sidecar protocol and the removal of the status endpoint.
- `docs/implemented-plans/parser-wrapper.md`: a "See also" to this plan; `docs/plans/ocr-hybrid.md`: amended to say the work is in the library.
- `docs/plans/index.md` (now), `docs/implemented-plans/index.md` (on implementation).
- In the library: see "Library documentation (requirement)" (README, quickstart, contract reference including the sidecar protocol spec, how to
  write a processor and a sidecar, the client and minting helper, the security guide, versioning).
