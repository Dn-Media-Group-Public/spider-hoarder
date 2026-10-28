# Plan: `browser_network_request_make` request types (navigation, form, xhr)

## Problem / Goal
[Plan: network request make](../implemented-plans/network-request-make.md) sends one request as a rewritten browser navigation. That has
limits:
- Its body goes to Chromium as one base64 CDP message. Chromium refuses messages over 100 MiB and then kills the whole browser context,
  so the real ceiling is about 75 MiB of body (measured: 74 MiB works, 76 MiB does not). `MAX_REQUEST_BODY` (64 MiB) exists to stay below it.
- It cannot send a form (multipart or urlencoded) or a request as a page script would (`fetch`), cannot send per-request cookies, and
  the origin the request appears to come from is not under the caller's control.

Goal: one tool, three request types, each with the limits of the browser mechanism behind it, so an agent can post large files, forms
and API calls through the session's real browser (cookies, TLS fingerprint, proxy/egress, media policy).

## Measured facts this plan rests on
All measured in throwaway containers (`mcp-research:local` image, headless Chromium 153, local HTTP server).

| fact | result |
|---|---|
| CDP `postData` body (today's navigation) | 74 MiB ok, 76 MiB kills the browser context |
| `fetch(url, {method: 'PUT', body: File})`, no interception | 500 MiB, byte-exact, 1.2 s |
| form POST, multipart, file input via `set_files` | 500 MiB, 2.0 s; Playwright reads the response body |
| method/header rewrite of the form navigation (`Fetch.continueRequest`) | `PUT`, `PATCH`, `DELETE`, `Cookie`, `User-Agent`, custom headers all work, up to 500 MiB |
| request-stage `Fetch` pause on a `fetch` with a `File` body | 90 MiB ok, 200 MiB hangs, 500 MiB kills the browser |
| response-stage `Fetch` pause on XHR responses | `getResponseBody` works and `failRequest` blocks; but a 100 MiB upload took 30.7 s and 500 MiB killed the browser |
| `Network.getResponseBody` for a `fetch` with a `File` body | always fails ("evicted from inspector cache"), independent of size and buffer settings, same via Playwright or our own CDP session; string bodies, no body and redirected hops are fine |
| `fetch` request headers set from JS | `Cookie`, `User-Agent`, `Host`, `Origin`, `Sec-Fetch-*` silently dropped |
| same-origin `fetch` | HttpOnly `SameSite=Strict` session cookie is sent automatically |
| cross-origin redirect during `fetch` | the request (with its full body) is sent, the response is lost (`Failed to fetch`, `net::ERR_FAILED`) |
| `fetch` response blocked in our JS (`body.cancel()` after the headers) | server sent only 0.1 MiB of 100 before `BrokenPipe`; nothing in the HTTP cache. Layer D on a navigation: 0.2 MiB, not cached |
| media policy on `fetch` today | **not enforced** (layer D only watches `Document` responses, layer A only aborts by resource type) |

## Design

### Arguments
`browser_network_request_make(url, type?, method?, headers?, cookies?, body?, origin?, credentials?, timeout_ms?)`

| arg | |
|---|---|
| `url` | required, `http(s)` |
| `type` | `navigation` (default), `form`, `xhr` |
| `method` | default `GET` |
| `headers` | object of strings; rules per type below |
| `cookies` | object `{name: value}`, extra cookies for this request only |
| `body` | text, or `{job_id, path}`, or a fields object (below); see the matrix |
| `origin` | URL the request appears to come from; `form` and `xhr` only (error under `navigation`) |
| `credentials` | `xhr` only (error otherwise): `include` (default) or `omit`; whether `fetch` sends the session's cookies |
| `timeout_ms` | default 30000, at most `MAX_REQUEST_TIMEOUT_MS` |

`body_job_id` and `body_path` are removed with no alias: they only exist in the uncommitted implementation of the plan above.

### How `body` is read (by shape, independent of `type`)
- A string is raw text.
- A dict with exactly the keys `job_id` and `path` is the raw bytes of that job file (owner-scoped, same path rules as today:
  no `..`, no absolute paths, missing key is "file not found").
- A dict with neither of those keys is a **fields** object. A dict with `job_id` or `path` plus anything else, or with only one of the
  two, is an error. A form field therefore cannot be named `job_id` or `path`.
- A field value is one of:
  - a string; a number or boolean is coerced to text like JS `String()` (integers as digits, integral floats without `.0`,
    `true`/`false`); `null` is an error;
  - `{job_id, path, filename?, content_type?}`: a file part. `filename` defaults to the basename of `path`; `content_type` defaults to
    the type the browser derives from the filename's extension (the same for `form` and `xhr`). For `form`, `content_type` is only
    allowed when no request-stage pause is needed (see "The three types"), else an error;
  - a list of the above: repeated parts under one name.
  Parts keep the dict's insertion order, list items in order.

### Which bodies each type accepts

| body | navigation | form | xhr |
|---|---|---|---|
| none | yes | yes | yes |
| text | yes | error | yes |
| job file (raw) | yes, up to `MAX_REQUEST_BODY` | error | yes, up to `MAX_UPLOAD_BYTES` |
| fields | yes, encoded by us, up to `MAX_REQUEST_BODY` | yes | yes (`URLSearchParams` / `FormData`) |

- `GET`/`HEAD` with fields: the fields go into the query string, as in a real form (a file part is an error, since a real GET form sends
  only the filename). For `form` this replaces the query string of `url`, as a browser does: to keep a query, pass its parameters as fields.
- `GET`/`HEAD` with a text or file body: allowed for `navigation` (the request is rewritten, as today), an error for `xhr` (`fetch` cannot).
- Raw bodies (text, job file) are sent as given; `Content-Type` is never inferred, the agent supplies it.

### Encoding of fields (one rule for all three types)
Chosen by the agent through a `Content-Type` header; only its media type is looked at.
- Default: `application/x-www-form-urlencoded` if there is no file part, else `multipart/form-data`.
- Explicit: any of `application/x-www-form-urlencoded`, `multipart/form-data`, `text/plain`. Urlencoded or `text/plain` together with a
  file part is an error; any other `Content-Type` with fields is an error.
- `navigation`: we build the bytes (boundary included) and set the header. `form`: it is the form's `enctype`. `xhr`: `URLSearchParams`,
  `FormData`, or a `text/plain` string.

### Headers
- `navigation`, `form`: all headers are applied, by rewriting the paused request (`Fetch.continueRequest`). Exception: for `form`, the
  browser owns `Content-Type` (it only selects the encoding, above).
- `xhr`: a page script can only set what `fetch` allows. A header it would silently drop (`Cookie`, `User-Agent`, `Origin`, `Referer`,
  `Sec-*`, `Accept-Encoding`, the other forbidden names of the Fetch spec) is an error naming the header. No size-dependent
  exceptions: header behaviour must not depend on body size. `Referer`/`Origin` come from `origin`.
- All types: `Host`, `Content-Length`, `Transfer-Encoding`, `Connection` are errors (they break the framing; a `Host` rewrite hung a `fetch`).
- The job records the headers actually sent (`all_headers()`), not the arguments.

### Cookies
`cookies` are added to the session's own cookies for this request only and are never stored. Values are coerced like field values.
- `navigation`: the `Cookie` header is the session's cookies for the URL (as today, `SameSite` not applied) with `cookies` added, winning by name.
- `form`: without `cookies` the browser handles cookies (so a real form's `SameSite` rules apply). With `cookies`, the `Cookie` header is
  set as for `navigation`, which bypasses `SameSite` filtering of the jar cookies for that call; `index.json` notes it.
- `xhr`: `cookies` is an error (a page script cannot set `Cookie`; rewriting it only works for small bodies). The error points to
  `browser_cookie_set` beforehand, or `type=form` / `navigation`. `fetch` uses `credentials: 'include'` by default, so the session's cookies
  are sent cross-origin too; readability then needs the target's CORS to answer explicitly (`Access-Control-Allow-Credentials: true`, an
  explicit `Allow-Origin`, and no `*` in the preflight's `Allow-Methods`/`Allow-Headers`). With `credentials: "omit"` no cookies are sent
  and wildcard CORS is readable. If a response is unreadable under `include`, the error suggests `origin` = the target (same-origin) or
  `credentials: "omit"`.
- `headers.Cookie` together with `cookies` is an error for every type.
- `Set-Cookie` in a response is stored in the session's jar, as in any browser (unchanged from today).

### Origin
For `form` and `xhr` the request is made from a page we generate in the hidden tab, placed at `origin` (a full URL) by answering the
tab's first navigation with `Fetch.fulfillRequest`: no network request is made for it, so the URL need not be reachable.
- Default: the URL of the caller's current tab (`browser` id) at call time, fragment stripped. If that is not `http(s)` (`about:blank`,
  `data:`, a browser error page), the request URL is used. `index.json` records the origin used.
- The page is ours: only the URL and origin are reproduced, not the real page's scripts, state or CSP, and the real tab is not touched.
- Real browser rules follow from the origin: `Origin`/`Referer` headers, cross-site `SameSite` cookie dropping for `form`, CORS
  (preflight for `PUT` or non-simple `Content-Type`, readability needs the target's CORS headers) and mixed-content blocking for `xhr`.

### The three types
- **`navigation`** (today's mechanism): hidden tab, request-stage `Fetch`; the first `requestPaused` is continued with `method`,
  `headers` and `postData`. New: fields are encoded by us; `cookies`.
- **`form`**: the generated page at `origin` holds `<form method enctype action=url>` with a hidden input per text field and a file input
  per file part (repeated names: one input per file with the same name). File inputs are filled with `set_files` from the staged files,
  then the form is submitted. Request-stage `Fetch` stays on to rewrite the method (when it is not `GET`/`POST`), headers and cookies.
  Because a file swapped in with `DataTransfer` (needed for `content_type`) is size-dependent and unusable under that pause (50 MiB 9.8 s,
  120 MiB 45 s, 500 MiB kills the browser), `content_type` on a file part is allowed only for a plain `POST`/`GET` with no `headers` and
  no `cookies`; otherwise it is an error. `filename` always works (we name the staged file). The
  response is a `Document`: layer D, redirects, attachments and body reading work as for `navigation` (our headers apply to the first request only).
- **`xhr`**: the generated page holds one file input per file part. Request-stage `Fetch` is disabled once the page has loaded
  (required for large file bodies), then the page runs `fetch(url, {method, headers, body, credentials})` (default `'include'`) with the body as
  string, `File`, `URLSearchParams` or `FormData`. `content_type` and `filename` overrides use `new File([file], filename, {type})`.
  - Response: status and headers come from Playwright's response event; the body is read in the page and streamed to the toolserver in
    chunks (exposed binding), straight into the job's `response/body.<ext>` file, never whole in memory. (`getResponseBody` cannot be
    used, see the table.)
  - Media policy: see below.
  - Failures: a cross-origin redirect or CORS failure is a tool error saying the response is not readable by `xhr` and suggesting
    `origin` = target or `type=navigation`/`form`; network failures report Playwright's `requestfailed` reason (e.g. `ERR_NAME_NOT_RESOLVED`).

### Media policy
- Layer D applies to every type. `navigation` and `form`: the existing `Document` hook.
- `xhr`: in our page, once `fetch` has resolved (headers in, body untouched), the `Content-Type` is sent to Python through a binding that
  calls `Policy.content_type_blocked`; if blocked, the page cancels the body stream and Python records the block in `Registry.blocked`
  with layer `D`. Result: a job with `blocked: "media policy"`, request headers, the response `content-type`, no body, and a `notes` entry,
  same as a blocked navigation. As with D today, the request is made and the headers are seen; measured, only about 0.1 MiB of a
  100 MiB body reaches the network stack, it is dropped and not cached.
- Layer A needs nothing: our requests are `Document` or `fetch`, which it never aborts.
- Out of scope: closing the same gap for any tab's `fetch`/XHR (site scripts, `browser_evaluate`). That needs its own plan: extend the
  registry hook to XHR responses, which must skip `make`'s hidden tabs (our in-page check owns them) and decide what to do about large
  uploads in normal tabs (the response-stage pause is slow at 100 MiB and kills the browser at 500 MiB).

### Staging job files for `form` and `xhr` (gateway to sandbox)
The gateway resolves every `{job_id, path}` (owner-scoped), checks the manifest sizes against `MAX_UPLOAD_BYTES` (total of all file parts of
one call) **before copying**, and streams each file with `storage.open_read` into `<home>/in/<random>/<filename>` (the same `O_PATH` /
`O_NOFOLLOW` / `O_EXCL` discipline as today; `filename` sanitised to a basename). It passes the toolserver a hidden argument that replaces
each job reference by the staged name (any agent-supplied hidden argument is discarded). The toolserver deletes the files when the call
ends, success or failure; the gateway deletes them again as a backstop; the session-end wipe covers the rest. For `navigation` bodies the
bytes are staged as a single file as today (capped by `MAX_REQUEST_BODY`, which also covers text and encoded fields).

### Limits
- `MAX_REQUEST_BODY` (env, default 64 MiB): `navigation` bodies, including text and encoded fields.
- `MAX_UPLOAD_BYTES` (env, default 500 MiB): total file parts of one `form`/`xhr` call.
- `timeout_ms`: default 30 s, at most `MAX_REQUEST_TIMEOUT_MS` (env, default 300 s). The gateway's client to the toolserver uses the MCP
  default `sse_read_timeout` of 5 minutes, so it is raised to the maximum plus a margin. The agent's own MCP client timeout is outside our control.

### Result job
Same layout as before (`request/headers.json`, `response/headers.json`, `response/body.<ext>`, `index.json`). `index.json` adds `type`,
`origin` (when used) and `body` (the argument as given, with job references) in place of a stored copy for job-sourced bodies, so a 500 MiB
upload is not stored twice. Text and encoded-field bodies are stored as today. As before it carries `url`, `method`, `status`,
`redirects`, `blocked`. For a rewritten request Playwright reports the wrong method and no body, so these come from the arguments.

## Spike results (step 1, done)
- `xhr`: `new File([f], name, {type})` honours filename and part type at 500 MiB (1.7 s, ~128 MiB RSS); a raw `PUT` sends exactly the file.
- `form`: the `DataTransfer` swap honours filename/type only without a request-stage pause; with it, size-dependent and crashes at 500 MiB.
  A directly `set_files` input works with the pause at 500 MiB, type from the extension. Hence the `content_type` rule above.
- `xhr` response: 500 MiB through the binding, sha-exact, 13.5 s, Python 123 MiB, Chromium ~263 MiB. `all_headers()` works for a `File` body.
- Fabricated `https` origin: works in default Chromium, no network request. `Origin: null` on an https to http form post is spec behaviour.
- Cross-origin `xhr`: preflight and `credentials` behave per spec (see Cookies); https to http is blocked as mixed content.
- **Service workers**: one registered for the target origin intercepts the generated page's `fetch` and empties the body of a redirected,
  rewritten form post. `Network.setBypassServiceWorker` fixes both, so it is enabled on every hidden tab, all three types.
- Redirected form posts resend method and body in all cases without a service worker; our overridden headers apply to the first hop only.

## Implementation order
1. **Spike: done (throwaway, results in "Spike results"), plan revised accordingly.**
   - `new File([file], filename, {type})` for `xhr`, and `DataTransfer` files for `form`, at 500 MiB: do filename and part `Content-Type` arrive as set?
   - `xhr` response transport at 500 MiB (chunks through an exposed binding: time, memory), and `all_headers()` for a `fetch` with a `File` body.
   - A fulfilled `https` origin without any certificate flags and an unreachable host.
   - Service workers registered for the target origin: do they intercept the generated page's `fetch`? Mitigation if so.
   - Cross-origin redirect of a rewritten form post.
2. Gateway: body shape parsing and validation, caps, streaming copy to `in/<random>/<filename>`, hidden argument, schema, removal of
   `body_job_id`/`body_path`; tests.
3. Toolserver: `Network.setBypassServiceWorker` in `hidden_page`; shared validation (headers, cookies, origin, body/type matrix), `navigation` (field encoding, cookies), `form`, `xhr`
   (page, chunked response, policy D), result jobs; tests per type including D per type and a large-file smoke test.
4. `sessions.py` timeout; docs; move the plan.

## Files to change

| file | what |
|---|---|
| `mcp-research/app.py` | body shape rules, job resolution, `MAX_UPLOAD_BYTES` / `MAX_REQUEST_TIMEOUT_MS`, streaming copy to `in/`, hidden argument, schema, `index.json` fields |
| `mcp-research/sessions.py` | raise the toolserver client's `sse_read_timeout` |
| `mcp-research/toolserver/tools.py` | `make` split by `type`; field encoders; header/cookie/origin handling; form and xhr pages; chunked response; policy D for xhr |
| `mcp-research/toolserver/browsers.py` | helper to answer the hidden tab's first navigation with the generated page |
| `mcp-research/toolserver/server.py` | new schema of `browser_network_request_make` |
| `mcp-research/test_toolserver.py`, `test_gateway.py`, `test_security.py` | new tests; old `body_job_id`/`body_path` tests replaced |

## Docs to update
- `docs/api/tools.md`: the new signature, the body and type matrix, headers, cookies, origin, limits, results.
- `docs/architecture/browser-toolserver.md`: the three types, the generated page and `Fetch.fulfillRequest`, why `xhr` disables the request-stage pause, response transport, media policy D per type.
- `docs/architecture/jobs-and-storage.md`: `in/<random>/<filename>` staging, caps, deletion.
- `docs/architecture/session-isolation.md`: `in/` as a gateway-to-sandbox channel for files (per-call directories).
- Each updated section gets `See also: [Plan: network request make types](../implemented-plans/network-request-make-types.md)`; add the plan to `docs/implemented-plans/index.md` when it moves.

## Open for review
- `MAX_UPLOAD_BYTES` default 500 MiB and `MAX_REQUEST_TIMEOUT_MS` default 300 s are proposals; a slow link needs more time than 5 minutes for 500 MiB.
- `credentials` defaults to `include` (decided); wildcard-CORS targets then need `credentials: "omit"` or `origin` = target.
- Concurrent `make` calls on one session can each stage up to `MAX_UPLOAD_BYTES` in `in/`; there is no per-session total.
- Separate plan needed: media policy for `fetch`/XHR in ordinary tabs (see Media policy).

## Decided during implementation
- **Layer A and big uploads.** In a no-media session layer A is a context-wide `route("**/*")`, which pauses every request at request stage;
  measured: a 250 MiB `fetch` PUT with a `File` body kills the browser with it (1 s without). Decision (user): in such sessions uploads of
  `xhr`, and of `form` with a `content_type` override, are capped at `ROUTED_UPLOAD_CAP` (64 MiB) and refused with an error, by the gateway
  before copying and again by the toolserver; the browser never crashes.
- **Local Network Access.** The generated origin page has no address space, so Chromium blocks its cross-origin requests to loopback/private
  targets; same-origin (the default `origin` for a target on such an address) works.
- Service-worker bypass (`Network.setBypassServiceWorker`) is set on the CDP session `hidden_page` returns; no test registers a service worker.
