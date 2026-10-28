# Tool reference

What Claude sees at `/<egress>/mcp`. The server also sends its own `instructions`. Bulk results are jobs
(`{job_id, files, text?, text_path?}`; [Jobs and storage](../architecture/jobs-and-storage.md)).
See also: [Plan: Browser sessions](../implemented-plans/browser-sessions-plan.md),
[Plan: Python browser tool server](../implemented-plans/python-browser-tool-server.md),
[Plan: Jobs for all tools](../implemented-plans/jobs-for-all-tools.md)

## Gateway tools
| Tool | Behaviour |
|---|---|
| `open_session(media?, allow?, deny?)` | **Must be the first call, once.** Sets the session's immutable no-media policy; every other tool fails until then. Ask the user about security settings first; pass `allow` / `deny` (MIME types or `type/*`) only on request. `media` defaults to false on Tor / I2P, true on clear. Returns the effective policy. See also: [Plan: No-media sessions](../implemented-plans/no-media-sessions.md) |
| `get_session()` | Read-only: `{egress, opened, media, blocked_resource_types, allowed_content_types, denied_content_types}`. Works before `open_session` (`opened: false`, the defaults). For subagents. |
| `browser_request_human_help(reason, browser?)` | Link for the user to solve a CAPTCHA/login in the live view (owner-only, SSO). Wait for the user. |
| `browser_save_page(target?, browser?)` | Convert the open page (or `target`: an element ref/selector) to Markdown and store it as a job. Prefer over `browser_snapshot` for content. |
| `ingest_page(url, target?)` | One shot: ephemeral browser, navigate, save, destroy. `target` must be a selector. Non-HTML goes through `requests`. Uses the session's media policy: blocked content types error with `blocked: media disabled for this session (<type>)`. |
| `browser_end_session(browser?)` | Close one browser, or all and the session; the result counts requests the media policy blocked. |
| `get_upload_link()` | Page where the *user* uploads files into a new job (1 h). Archives are unpacked; binary formats (msgpack, CBOR, BSON, plist, protobuf) also get `parsed/*.json`. See also: [Plan: more file formats](../implemented-plans/more-file-formats.md) |
| `list_files(job_id)` | Manifest of a job. |
| `get_file_text(job_id, path, offset?, length?)` | Page through a stored file (20,000-char slices by default). |
| `grep_file(job_id, path, pattern, context=2, max_matches=50)` | Regex search in a stored text file (`/pat/i` accepted). |
| `get_file_url(job_id, path)` | Short-lived (15 min) signed download link. |

## Browser tools (tool server)
Every one takes optional `browser` (default `main`).
- Navigation/interaction: `browser_navigate`, `_navigate_back`, `_click`, `_type`, `_fill_form`, `_press_key`, `_select_option`,
  `_hover`, `_drag`, `_drop`, `_file_upload`, `_handle_dialog`, `_wait_for`, `_resize`, `_emulate_media`, `_tabs`, `_close`.
- Reading (always jobs): `browser_snapshot` and `browser_find` (`text_cap`, default 128), `browser_evaluate`,
  `browser_console_messages`, `browser_network_requests`, `browser_network_request_get`, `browser_take_screenshot`, `browser_pdf_save`,
  `browser_storage_state`.
- `browser_network_requests` takes `new` (only requests since the last `new: true` call on the tab; "No new requests." as plain text
  if none). `browser_network_request_get(index)` (renamed from `browser_network_request`, no alias) returns a job: `original/request|response/headers.json` and `body.<ext>` (raw bytes;
  absent parts have no file), parsed like any file; no `part`/`filename`. `index.json` has `url`, `method`, `status`.
  See also: [Plan: network request jobs](../implemented-plans/network-request-jobs.md)
- `browser_network_request_make(url, type?, method?, headers?, cookies?, body?, origin?, credentials?, timeout_ms?)` is not a reader but
  returns a job in the same layout as `_get` (`index.json`: `url`, `method`, `status` of the final request, `type`, `origin` for form/xhr,
  `body` = the dict body as given with its job references, `redirects: [{url, status}]` if any). It sends the request through the
  session's real browser (cookies, TLS fingerprint, proxy, media policy; on Tor/I2P through them). `method` defaults to `GET`.
  - `type`: `navigation` (default; a rewritten browser navigation: any method/header, `cookies`, body at most `MAX_REQUEST_BODY`, env,
    default 64 MiB, in one CDP message; Accept and `Sec-Fetch-*` are navigation values unless overridden), `form` (a form submit from
    a page at `origin`: fields only; non-GET/POST methods, headers and `cookies` are rewritten onto it; `content_type` of a file part
    only for a plain GET/POST without headers/cookies), `xhr` (a `fetch` from a page at `origin`: CORS, `Origin`/`Referer`, cookies are a
    page script's; `credentials` `include` (default) or `omit`; `Cookie`, `User-Agent`, `Origin`, `Referer`, `Sec-*`, `Proxy-*` and the other
    forbidden headers, `cookies`, and a body with GET/HEAD are errors). `Host`, `Content-Length`, `Transfer-Encoding`, `Connection` are errors everywhere.
  - `body`, by shape: a string is raw text; `{job_id, path}` is the raw bytes of a file of one of the caller's own jobs; any other object is
    a **fields** object `{name: value}`, a value being text, a number or boolean (as JS `String()`), a file part
    `{job_id, path, filename?, content_type?}`, or a list of those (null and nested lists are errors; a field cannot be named `job_id`/`path`).
    `form` takes fields only. Fields are urlencoded (no file part) or multipart (file part) unless a `Content-Type` header says
    `application/x-www-form-urlencoded`, `multipart/form-data` or `text/plain`; with GET/HEAD they go into the query string (for `form`
    replacing the URL's own). Raw bodies are sent as given: `Content-Type` is never inferred. `body_job_id`/`body_path` no longer exist.
  - `cookies` `{name: value}`: extra cookies for this request only, never stored; with `headers.Cookie` or under `xhr` an error. For `form`
    they bypass `SameSite` filtering of the jar cookies (the job's `notes` say so).
  - `origin` (form, xhr): the URL the request appears to come from; default the caller's current tab URL (the request URL if that is not
    http(s)). The page there is generated and its network request never made, so the URL need not exist. A target in a private address
    space is reachable only same-origin (Local Network Access).
  - Limits: file parts of one `form`/`xhr` call at most `MAX_UPLOAD_BYTES` (env, default 500 MiB), checked before anything is copied; in a
    session whose media policy blocks media (every request is routed through layer A) uploads are limited to 64 MiB, because the browser
    dies on larger ones. `timeout_ms` defaults to 30000, at most `MAX_REQUEST_TIMEOUT_MS` (env, default 300000).
  - Redirects are followed (a 301/302 on POST becomes GET); our `headers` apply to the first request only. A response blocked by the media
    policy (every type) gives a job with `blocked: "media policy"`, only the request headers and a `content-type`-only `response/headers.json`, no body, and a
    `notes` entry; `Content-Disposition: attachment` is stored as `response/body.<ext>`; DNS/connection/timeout failures are tool errors, and
    so is an `xhr` response the page may not read (CORS, cross-origin redirect), with a hint (`origin` = target, `credentials: "omit"`, or another type).
  See also: [Plan: network request make](../implemented-plans/network-request-make.md)
  See also: [Plan: network request make types](../implemented-plans/network-request-make-types.md)
  Requests stopped by the media policy show as `[BLOCKED] blocked: media policy`. See also: [Plan: No-media sessions](../implemented-plans/no-media-sessions.md)
- Windows: `browser_start_session` (returns a new id; subagents must use their own, never `main`).
- Storage: `browser_cookie_{get,list,set,delete,clear}`, `browser_localstorage_*`, `browser_sessionstorage_*`,
  `browser_set_storage_state` (state inline).
- Refs from snapshot/find stay valid until the page re-renders or navigates; actions return no snapshot.
- Not implemented: `browser_run_code_unsafe`, network mocking, tracing, video, raw mouse.

## Egress
`/tor/mcp` browses only via Tor (anonymised, but some sites block Tor); `/clear/mcp` browses directly
(public internet only, not `.onion`). Use Tor for sensitive research, clear when a site blocks Tor.
`/i2p/mcp` reaches only `*.i2p` (eepsites, `.b32.i2p`), nothing else. The first request on a fresh session can take a minute (tunnel build);
until then tools fail with "I2P router/tunnels not ready, retry". See also: [Plan: I2P egress](../implemented-plans/i2p-egress.md)
