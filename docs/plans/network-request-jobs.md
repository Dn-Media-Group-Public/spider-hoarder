# Plan: network requests as multi-file jobs, `new` flag, tree parsing, no size cap on stored files

Depends on the current job model ([Jobs, storage and links](../architecture/jobs-and-storage.md)) and the Python tool server
([Browser tool server](../architecture/browser-toolserver.md)).

## Problem / Goal

1. **`browser_network_request` squeezes four artifacts into one text file.** `toolserver/tools.py:398` joins request
   headers, request body, response headers and response body into `network-request.txt` with `-- part --` separators.
   Response bodies are decoded with `decode("utf-8", "replace")`, so binary bodies (images, PDFs) are corrupted. HTML and
   other parseable bodies never reach the parse pipeline; `grep_file` searches headers and bodies together; `part` exists only
   to work around the single file.
2. **Tracing "what did this click cause" is clumsy.** `browser_network_requests` is cumulative for the page's lifetime
   (`PageState.network`, `toolserver/browsers.py:26`, only ever appended to). The agent has to remember the last index.
3. **The gateway cannot take a tree, and caps stored files.** `_read_out` (`app.py:253`) reads exactly one file, whole, into
   memory, and rejects anything over `MAX_FILE_BYTES` (50 MB). A big download seen by the network tool would fail. Parsing has
   no cap at all, and `unpack_zip` (`extract.py:63`) reads the whole zip and every member into memory with no count limit.
4. **Parsing is duplicated.** `_store_and_parse` has a zip branch that parses members one way (generic `file_to_markdown`,
   no `simplified/`, no inline text) and a single-file branch that parses another way (HTML via `html_to_markdown` +
   `absolutize` + `simplified/`).

Goal: one parse rule applied per file to a tree of files; `browser_network_request` produces a job holding the request and
response as separate files; `browser_network_requests` gains `new`; storage has no size cap, parsing has one.

## Design

### Job layout for `browser_network_request`

```
original/request/headers.json       pretty-printed (indent=1), lower-case names, one header per line
original/request/body.<ext>         only if the request had a body; ext from the request content-type
original/response/headers.json      only if there was a response
original/response/body.<ext>        raw bytes; ext from the response content-type; only if the body could be read
parsed/request/body.<ext>.md        and so on: parsed/<same path>.md, appended not replaced
parsed/response/body.html.md
simplified/response/body.html.md    HTML only, as for a saved page
original/response/body.zip.d/…      if the body is a zip: members, each parsed like any file (see "Zips inside a tree")
terse/…
index.json                          {files, url, method, status}
```

- No empty files are created: a missing part simply has no file; the manifest shows which exist.
- `<ext>` comes from stdlib `mimetypes.guess_extension` on the content-type (parameters stripped); fallback `.bin`. A test
  pins the results for json, html, xml, javascript, css, plain, png, jpeg, gif, svg, webp, pdf, zip so Python/OS
  `mime.types` quirks (e.g. `.jpe`) are caught.
- The original MIME type is recoverable from `response/headers.json`; it is not stored separately.
- Naming rule is unchanged from today: `parsed/<path>.md` appends `.md` to the full original path (`page.html` →
  `parsed/page.html.md`). No regression for page downloads.

### Tool signature changes

- `browser_network_request(index)`: `part` and `filename` removed from the schema the agent sees. The toolserver still takes
  a private output name from the gateway (as for every `FILE_TOOLS` tool); here it names a directory instead of a file.
- Result: `{job_id, files, text?, text_path?}`. `text` is the response body if it is text (content-type text/*, JSON, XML,
  JavaScript, or a NUL-free sniff, as `_store_file` does today) and under the inline limit, by exactly the page-dump logic
  (`_job_result`: parsed Markdown for HTML, falling back to the terse outline, else omitted). `index.json` carries
  `url`, `method`, `status`; the toolserver result line (`net_line`) is also returned so the gateway reads them as it
  reads `Page URL` today.
- `browser_network_requests` gains `new` (boolean, optional). Description text:
  > "If true, return only requests completed since the last call to this tool with `new: true` on this tab (everything
  > since page load on the first such call), then remember the position. Call it repeatedly with `new: true` between
  > actions, e.g. after each click, to see exactly which requests each action caused. Indexes stay global: they are the
  > same numbers `browser_network_request` takes. The position moves whether or not `filter`/`static` hide some of the
  > new requests. Calls without `new` neither read nor move the position. Requests still in flight when you call appear in
  > the next call. If there are no new requests the result is a short text, not a file."
- Semantics: `PageState.net_mark` (per page = per tab, init 0). With `new`: show entries with global index ≥ `net_mark + 1`,
  then set `net_mark = len(network)` as it was read before filtering. Empty result → plain text "No new requests.", no job.
  The gateway returns the upstream text unchanged when it contains no `out/` file link (instead of erroring "produced no
  readable output file").

### Gateway: reading a tree, streaming, caps

- Replace `_read_out` with an fd-based reader that keeps today's hardening and works for a file or a directory: session
  home opened `O_PATH|O_NOFOLLOW`, every component `O_NOFOLLOW` relative to the previous fd, `O_NONBLOCK`, regular files
  only. For the network tool the gateway **allow-lists** names: `request/headers.json`, `request/body.*`,
  `response/headers.json`, `response/body.*`; anything else in the directory is ignored. (The session cannot add files that
  reach storage, and there is no count cap to tune.)
- **No size cap on stored files.** New `storage.write_stream(job, rel, fileobj, content_type)` copies in 1 MiB chunks
  and records the size in the manifest; `write_bytes` becomes a thin wrapper. The reader copies fd → storage with it,
  so memory use is constant. Applies to every `FILE_TOOLS` result (one reader for all), including screenshots and PDFs.
- **Parsing cap.** `MAX_FILE_BYTES` is removed; `MAX_PARSE_BYTES` (env, default 50 MB) replaces it. A file over the cap is
  stored under `original/` but not parsed, not inlined, and the result notes `"<path> not parsed (N bytes > MAX_PARSE_BYTES)"`.
  The cap is also applied to uploads, `ingest_page` downloads and zip members, which were unbounded before.

### One parse rule for trees

- Extract the per-file logic of `_store_and_parse` into `_store_one(key, path, raw, ctype, base_url="")`; add
  `_store_tree(key, files, base_url="")` that applies it to each file and returns the manifest. `_store_and_parse` for a single
  file calls `_store_one`; for a zip it calls `_store_tree` with members streamed from the zip.
- `base_url` is the option you asked for: HTML files get `extract.absolutize(raw, base_url)` only when it is non-empty.
  Zip members pass nothing, so no absolutizing (unchanged); network response bodies pass the response URL
  (`resp.url`, i.e. after redirects).
- Which files are parsed: **the same for every caller**, network job files included. Every file is attempted with the
  rule `_store_and_parse` applies today (HTML via `html_to_markdown`; `.md` gets `terse/` only; everything else via
  `file_to_markdown`). So a JSON or plain-text response body gets whatever `file_to_markdown` makes of it, exactly like a
  downloaded page of that type; there is no network-specific skip list. JSON pretty-printing/YAML is out of scope (another
  plan). If `file_to_markdown` raises on a file, `parsed/` is skipped for it and the error is noted; it never fails the job.
- **Zips: one rule everywhere.** Any file that is a zip (a response body, an upload, an `ingest_page` download, root or
  deep in a tree) is unpacked to `<zip path>.d/<relpath>`, whether or not the zip's members contain slashes:
  `original/path.zip` → `original/path.zip.d/<relpath>`, parsed as `parsed/path.zip.d/<relpath>.md`;
  `original/response/body.zip` → `original/response/body.zip.d/a/b.html`. Members go through `_store_one` like any file.
  Zips inside zips are not unpacked again (stored, and parse attempted as today).
- **Zip limits, enforced at extraction.** `MAX_ZIP_BYTES` (env) is a cap on the **total uncompressed bytes** of one zip;
  `MAX_ZIP_MEMBERS` (env, default 50,000, i.e. well over 10,000) caps the member count. Both are enforced while extracting,
  not trusted from the headers: the member count is checked against `namelist()` before the first member is written and
  counted again while writing; the byte total is the running sum of bytes actually written by `write_stream` (declared
  `file_size` values in a zip can lie), and extraction aborts as soon as it is exceeded. An aborted zip is reported in the
  result (`"zip not unpacked: N members > MAX_ZIP_MEMBERS"` or `"... bytes > MAX_ZIP_BYTES"`); members already written stay
  in the job, and the zip itself stays under `original/`. Members are written one at a time with `write_stream`, not
  collected into a dict (`unpack_zip` becomes a generator). Zip-slip rejection is unchanged. There is no cap on the
  compressed size: the zip itself is still held in memory by `fetch.download` / uploads (unchanged, and a separate concern
  from this plan).

### Intended behaviour changes (review these)

0. **Zip layout changes for existing callers.** Members of an `ingest_page` / uploaded zip move from `original/<relpath>` to
   `original/<name>.zip.d/<relpath>` (and `parsed/`, `terse/` likewise). Anything that reads those jobs by path, including any
   client that uploads a zip via `get_upload_link` expecting its tree at the job root, must use the new paths.
1. Zip members: HTML members now get `simplified/` and `html_to_markdown` (no absolutizing); `.md` members no longer get
   `parsed/x.md.md`, only `terse/`; unpacking aborts over `MAX_ZIP_BYTES` (uncompressed) / `MAX_ZIP_MEMBERS`.
2. `MAX_FILE_BYTES` is gone. `MAX_PARSE_BYTES` now also caps uploads / `ingest_page` / zip-member parsing (unbounded before).
3. `browser_network_request` loses `part` and `filename`; its result shape changes from one text file to a multi-file job.
4. Gateway passes through a file tool's text result when no file was produced.

### Open points for review

- Defaults (`MAX_PARSE_BYTES` 50 MB, `MAX_ZIP_BYTES` 4 GiB uncompressed, `MAX_ZIP_MEMBERS` 50,000) are mine.
- Existing tests and docs that assume zip members at the job root (`test_gateway.py`, `test_security.py` zip-slip cases,
  `jobs-and-storage.md` "Uploaded zips are unpacked preserving the tree") must be updated to the `.zip.d/` paths; I have not
  checked whether anything outside this repo reads uploaded-zip jobs by root-relative path.
- `new` is not exposed on `browser_network_request`; the response body of a request still in flight is whatever Playwright
  can read at call time (unchanged).

## Implementation order

1. `storage.py`: `write_stream`; `write_bytes` delegates to it. Unit test with a fake file object larger than memory-chunk size.
2. `extract.py`: `unpack_zip` → generator of `(name, fileobj)` with the member-count and size checks.
3. `app.py`: `_store_one` / `_store_tree` extracted from `_store_and_parse`; `MAX_PARSE_BYTES`, parse-skip note; zip branch
   switched to `_store_tree`.
4. `app.py`: fd-based tree reader replaces `_read_out`; `_file_tool` handles a directory result for
   `browser_network_request` (allow-list, `base_url` = response URL, `index.json` meta) and passes through file-less results.
5. `toolserver/browsers.py`: `PageState.net_mark`. `toolserver/tools.py`: `network_requests` gains `new`;
   `network_request` writes the directory and drops `part`; shared `mimetypes` extension helper; `tools.json`: `new` added,
   `part` removed, descriptions updated; gateway hides `filename` for `browser_network_request`.
6. Tests (below); docs; move this plan to `docs/implemented-plans/`.

## Files to change

| File | Change |
|---|---|
| `mcp-research/storage.py` | `write_stream`; `write_bytes` wraps it |
| `mcp-research/extract.py` | `unpack_zip` generator with count/size limits |
| `mcp-research/app.py` | `_store_one`, `_store_tree`, `MAX_PARSE_BYTES` (replaces `MAX_FILE_BYTES`), `MAX_ZIP_*`, tree reader, `_file_tool` network branch, pass-through, hide `filename`/`part` |
| `mcp-research/toolserver/browsers.py` | `PageState.net_mark` |
| `mcp-research/toolserver/tools.py` | `new` flag; directory output for `browser_network_request`; extension helper |
| `mcp-research/tools.json` | `new` parameter and description; `part` removed |
| `mcp-research/test_toolserver.py` | `new` cursor (first call, second call, filter, no-`new` calls don't move it, empty); directory output, no empty files, binary body byte-exact |
| `mcp-research/test_gateway.py` | replace the `MAX_FILE_BYTES` tests (lines ~98-104) with parse-cap and no-store-cap tests; tree parse; zip unified behaviour (HTML member gets `simplified/`, no absolutizing); zip count limit and running uncompressed-bytes limit (a zip whose headers under-declare sizes still aborts); a zip response body unpacks under `body.zip.d/` |
| `mcp-research/test_security.py` | tree reader: symlink at every level, FIFO, non-regular file, unexpected names ignored, zip-slip unchanged |

## Docs to update

- `docs/api/tools.md`: `browser_network_request` signature/result, `browser_network_requests` `new`, the "Reading (always jobs)" line.
- `docs/architecture/jobs-and-storage.md`: layout (nested `original/` and `parsed/` paths, tree rule), caps (`MAX_PARSE_BYTES`,
  `MAX_ZIP_*`, no stored-size cap), zip behaviour, `index.json` keys. Replace the `MAX_FILE_BYTES` sentence.
- `docs/architecture/browser-toolserver.md`: network log, `net_mark`, directory output.
- `docs/implemented-plans/index.md`: link this plan once moved. Each updated section gets
  `See also: [Plan: network request jobs](../implemented-plans/network-request-jobs.md)`.
