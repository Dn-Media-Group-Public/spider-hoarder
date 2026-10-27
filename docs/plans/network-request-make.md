# Plan: `browser_network_request_make`, and `browser_network_request` renamed to `_get`

## Problem / Goal
The agent can only cause a request by driving a page (navigate, click, `browser_evaluate` + `fetch`), which is limited by CORS,
by the page's origin, and by what a page can express. There is no way to send "this exact GET/POST with these headers and this
body" and read the response. Separately, `browser_network_request` reads as if it makes a request, but it only reads an entry from
the tab's network log.

Goal: a tool that sends one specific HTTP request through the session's real browser (same cookies, TLS fingerprint, proxy/egress,
media policy) and returns the result as an ordinary job; and a clearer name for the existing reader.

## Design

### Tools
- `browser_network_request` is renamed `browser_network_request_get(index)`. Behaviour, args and job layout unchanged
  ([Plan: network request jobs](../implemented-plans/network-request-jobs.md)). No alias for the old name.
- New `browser_network_request_make`:

  | arg | |
  |---|---|
  | `url` | required |
  | `method` | default `GET` |
  | `headers` | optional object; the agent supplies `Content-Type` itself, it is never inferred |
  | `body` | optional text |
  | `body_job_id` + `body_path` | optional pair, mutually exclusive with `body`; any other combination is an error |
  | `timeout_ms` | default 30000, clamped to a maximum |

  Result: a job in the same layout as `browser_network_request_get` (`original/request|response/headers.json`, `body.<ext>`,
  `index.json` with `url`, `method`, `status`). No request id: there is nothing to exchange later.

### How the request is sent: rewritten navigation in a hidden tab
1. Open an internal tab in the session's context. It is created with the existing "internal page" path (`Registry._ignore`) so it
   belongs to no browser id and never becomes anyone's current tab.
2. `Fetch.enable` on that page (request stage, all URLs) and `Page.navigate(url)`. On the first `Fetch.requestPaused`, call
   `Fetch.continueRequest` with `method`, `headers` and `postData` (base64) from the arguments. Later requests (redirect hops) pass
   through untouched.
3. `Cookie`: unless `headers` already has one, fill it from `ctx.cookies(url)`. A POST navigation started from `about:blank` is
   cross-site, so Chromium would otherwise drop `SameSite=Lax/Strict` cookies.
4. It is a top-level `Document` request, so the media policy applies unchanged: layer D checks `Content-Type` at the headers and
   fails the request before the body is read, and the block is recorded in `Registry.blocked`.
5. The tab's `PageState.network` holds the real Playwright `Request`/`Response` objects. The tool picks the final entry and builds the
   job with the same code as `browser_network_request_get`.
6. The tab is closed in `finally`, one fresh tab per call.

### Response variants
- **Redirects**: followed by the browser. The job holds the final request and response; `index.json` has the final `url`, `method`,
  `status`, and `redirects: [{url, status}]`. A 301/302 on a POST turns into a GET by browser rules and the job reflects that.
- **Blocked by the media policy**: a job with the request, the response headers and no body file; `index.json` has
  `blocked: "media policy"` and `notes` says so. Not a tool error.
- **`Content-Disposition: attachment`** (navigation becomes a download): the file from Playwright's download event is stored as
  `response/body.<ext>`, headers from the navigation response. Same precedent as `ingest_page` downloads. Confirmed in the spike.
- **Failure** (DNS, connection, timeout): a tool error with the message; no job.

### Body from a job file
Jobs live in the gateway's storage; the toolserver sandbox cannot see them. The gateway therefore:
1. Resolves `body_job_id` (owner-scoped; another owner's job is "file not found") and `body_path`. The path is any storage-relative
   path inside that job (`original/...`, `parsed/...`, `terse/...`, `simplified/...`, `index.json`); the bytes at exactly that key are used.
   Rejects `..`, absolute paths and missing keys.
2. Writes them to `in/<random name>` under the session home, opened through the `O_PATH` home fd with `O_EXCL | O_NOFOLLOW`, the
   mirror image of how `out/` is read.
3. Calls the toolserver with a hidden internal argument naming that file; the schema the agent sees has only `body_job_id` and
   `body_path`, hidden the same way `filename` is hidden today.
4. The toolserver reads the file, deletes it after sending, and never exposes `in/` to the agent. The session-end homedir wipe is the
   backstop. A size cap on `in/` files applies (see "Open for review").

### Not changing
`browser_network_requests`, storage layout, parsing, the media policy itself, egress handling.

## Implementation order
1. **Spike (throwaway script, not committed)**: confirm in this setup that `Fetch.continueRequest` can override method, headers and
   body of a navigation; that layer D still fires on it; how a redirect and an attachment behave; that the `Cookie` override is sent.
   If the spike fails, stop and revisit the plan with the user.
2. Toolserver: rename `network_request` to `network_request_get`; extract its job-building into a helper. Add
   `network_request_make` (hidden tab, interception, cookie fill, read from `in/`, helper for the job). `tools.json` entries.
3. Gateway: rename in `FILE_TOOLS`, `_file_tool`'s tree branch and `_without_filename`; add `_make` handling (job_id/path resolution,
   `in/` write, hidden argument, schema hiding, argument validation); add the new tool to the tree-job branch and to the allow-list.
4. Tests: rename in existing tests; new toolserver tests (GET, POST with body, header override, cookie fill, redirect chain, blocked
   media type, attachment, timeout); gateway tests (argument validation, owner scoping, path traversal, `in/` file removed).
5. Docs, move the plan.

## Files to change

| file | what |
|---|---|
| `mcp-research/toolserver/tools.py` | rename; `network_request_make`; shared job-building helper |
| `mcp-research/toolserver/browsers.py` | internal-tab creation helper if the existing `_ignore` path needs one |
| `mcp-research/tools.json` | rename entry; new entry and descriptions |
| `mcp-research/app.py` | `FILE_TOOLS`, `_file_tool` tree branch, `_without_filename`, `in/` writer, `body_job_id`/`body_path` handling |
| `mcp-research/test_toolserver.py`, `test_gateway.py`, `test_security.py` | renames and new tests |

## Docs to update
- `docs/api/tools.md`: the rename, the new tool's signature and results, the "Reading (always jobs)" line (`make` is not a reader but
  returns a job), egress and media-policy notes for `make`.
- `docs/architecture/browser-toolserver.md`: hidden tab and `Fetch` rewrite, cookie fill, internal tab not attributed to a browser.
- `docs/architecture/jobs-and-storage.md`: `FILE_TOOLS` list, the `in/` directory and its handling.
- `docs/architecture/session-isolation.md`: `in/` as a gateway-to-sandbox channel.
- Each updated section gets `See also: [Plan: network request make](../implemented-plans/network-request-make.md)`; add the plan to
  `docs/implemented-plans/index.md` when it moves.

## Open for review
- Cap on `in/` body files: proposed constant of 25 MiB in the toolserver, as the body is held in memory as base64 for CDP anyway.
- Maximum for `timeout_ms`: proposed 120000.
- One fresh hidden tab per call, closed afterwards, proposed for isolation and no leaked state; a reused tab is the alternative.
- Sec-Fetch-* and `Accept` are navigation values unless the agent overrides them; sites that treat navigation and XHR differently
  may behave differently than for a `fetch()`.
