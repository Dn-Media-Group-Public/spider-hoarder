# Browser tool server (multiple browsers per session)

A Python MCP server (`spider_hoarder_browser_sdk/session_server/toolserver/`, in `backends/browser-sdk`) runs inside each session under the session uid, one Chromium with one persistent
context. It replaced `@playwright/mcp`.
See also: [Plan: Python browser tool server](../implemented-plans/python-browser-tool-server.md), [Plan: Pluggable backends](../implemented-plans/backend-plugins.md) (the toolserver is part of `browser-sdk`)

## Why it exists
`@playwright/mcp` has a single "current tab": a main MCP client and its subagents sharing one MCP session clobber each other. Spikes
(several MCP processes on one Chromium over CDP, shared-context mode, two Chromiums on one profile, profile-copy seeding, patching
playwright-mcp) all failed or were unmaintainable; the plan has the table. Playwright itself has no such limit (one context, many
pages, shared cookies/localStorage), so the wrapper was replaced.

## Model
- **A "browser" is an OS window** (CDP `Target.createTarget {newWindow: true}`) with its tabs. Windows are 1440x900 at (0,0), no
  window manager.
- Pages join a browser via `page.opener()`.
- `browser_start_session` returns an unguessable id. Every other tool takes optional `browser`; default `main` (auto-created; shared by
  the main MCP client and the human). An unknown id gives a plain error and **never lists valid ids**.
- **No locking**: two callers on one id clobber each other. Isolation is by convention, carried in tool descriptions ("subagents:
  start your own browser, never use `main`").
- `browser_end_session(browser)` closes one window; without an id it closes everything and ends the session; closing the last browser
  ends the session. `browser_close` on the last tab of the last browser navigates to `about:blank` (headed Chromium exits with its last window).
- Logins are shared because it is one context.

## Tools
Schemas for the 25 kept `@playwright/mcp` 0.0.83 tools come from `tools.json` (our descriptions; `tools.json.upstream` is the captured
original, checked by `backends/browser-sdk/tests/test_toolserver.py`) plus a `browser` property. Added: 15 cookie / localStorage / sessionStorage tools,
`browser_storage_state`, `browser_set_storage_state` (state passed inline), `browser_start_session`, `browser_end_session`,
`browser_network_request_make`; hidden from the gateway's tool list: `browser_bring_to_front`, `browser_set_policy`. Dropped: `browser_run_code_unsafe`
(use `browser_evaluate`). Full list: [Tool reference](../api/tools.md).

- Element refs come from `page.aria_snapshot(mode="ai")` and resolve with `aria-ref=`; a stale ref gives "Ref … not found in the
  current page snapshot". Action tools return only `### Page` (URL, title) and modal state, **no snapshot**: call `browser_snapshot` or
  `browser_find` again after an action. See also: [Plan: Jobs for all tools](../implemented-plans/jobs-for-all-tools.md)
- `text_cap` (default 128, `null` = none) shortens pure text nodes in snapshots/finds; `find` matches the full text and centres the
  window on the match. `browser_find` shows each match with its ancestor chain (refs), the way to locate a content container.
- Network log: `PageState.network` is append-only; `PageState.net_mark` is the position `browser_network_requests(new=true)` reads
  from and then advances (per tab; calls without `new` leave it). `browser_network_request_get` writes a directory (`request/`,
  `response/`, each `headers.json` and `body.<ext>`, extension from the content-type via `mimetypes`) instead of one text file.
  See also: [Plan: network request jobs](../implemented-plans/network-request-jobs.md)
- `browser_network_request_make`: opens a hidden tab (`Registry.hidden_page`: created with `_ignore`, so it belongs to no browser id and is
  nobody's current tab; one fresh tab per call, closed in `finally`), enables CDP `Fetch` (request stage) and navigates. The first
  `Fetch.requestPaused` is continued with `method`, `headers` and `postData` from the arguments; later requests pass untouched. `Cookie`
  is filled from `ctx.cookies(url)` unless given (a POST from `about:blank` is cross-site, so Lax/Strict cookies would be dropped).
  It is a top-level `Document` request, so layers A and D apply unchanged. Playwright's `Request` does not see the override (reports `GET`
  and no post data), so the final request's method and body come from the arguments (adjusted for the redirect rules); its headers come
  from `all_headers()`. A blocked response yields no Playwright `Response`: the job gets the `content-type` from `Registry.blocked`.
  Limits: our headers are not re-applied on redirect hops; the hidden tab has no `PageState`.
  See also: [Plan: network request make](../implemented-plans/network-request-make.md)
  Request types (`Req` validates a call; `hidden_page` also sets `Network.setBypassServiceWorker`, since a service worker of the target
  origin would intercept the generated page's `fetch` and empty a redirected form post):
  - `navigation`: as above, plus fields encoded by us and per-request `cookies` merged over the jar's.
  - `form` / `xhr` run in a generated page placed at `origin`: the tab's first `Document` request is answered with `Fetch.fulfillRequest`, so no
    network request is made for it. `form`: `<form>` with hidden inputs and one file input per file part (`set_input_files` from the staged
    files), submitted; the request-stage pause stays on to rewrite method, headers (merged onto the browser's own, which keeps the multipart
    boundary) and cookies. A `content_type` override swaps the file via `DataTransfer`, which only works without the pause (size-dependent and
    fatal at 500 MiB with it), so it is allowed only when nothing needs rewriting, and then `Fetch` is disabled after the page loads.
  - `xhr`: `Fetch` is disabled after the page loads (a request-stage pause on a `fetch` with a `File` body hangs at 200 MiB and kills the
    browser at 500 MiB), then the page runs `fetch` with the body as a typeless `Blob` (so no `Content-Type` is inferred), `File`, `URLSearchParams`
    or `FormData`. `getResponseBody` cannot read such a response, so the page streams it to Python in ~1 MiB base64 chunks through an
    exposed function, straight into `response/body.part` (renamed by content-type), never whole in memory.
  - Media policy layer D for `xhr`: once `fetch` resolved (headers in, body untouched) the page asks Python (`__blocked`, `Policy.content_type_blocked`);
    if blocked the body stream is cancelled and `Registry.blocked` gets a layer-`D` record. For `navigation`/`form` the existing `Document` hook applies.
  - Layer A (`ctx.route("**/*")`, no-media sessions) pauses every request and kills Chromium on big `fetch` uploads: `Req` refuses uploads over
    `ROUTED_UPLOAD_CAP` (64 MiB) there, as does the gateway before copying.
  Files come from `in/<random>/<filename>` and are deleted when the call ends.
  See also: [Plan: network request make types](../implemented-plans/network-request-make-types.md)
- `browser_pdf_save` uses CDP `Page.printToPDF` (Playwright's `page.pdf` is headless-only).
- Files (screenshots, PDFs, `filename=` outputs) go to `$HOME/out`; the gateway copies them into jobs ([Jobs](jobs-and-storage.md)).

## Media policy
Fail closed: until the internal one-shot `browser_set_policy` (not in the tool list, hidden by the gateway, called by it right after
`initialize`) has run, every tool errors without even launching Chromium. It stores the `Policy` (`spider_hoarder_browser_sdk/mediapolicy.py`) on the
`Registry` and, when it blocks anything, installs **layer A** (`context.route("**/*")` aborting `image`, `media`, `object`, `imageset`,
`font` requests, so they never leave the browser) and **layer D** (CDP `Fetch.enable`, response stage, `Document` resources only, on
every page including later ones: `Content-Type` is checked at the headers and the request is failed before the body is read).
Blocked requests are logged in `Registry.blocked`, marked in `browser_network_requests`, and counted by `browser_end_session`
(`{"remaining", "blocked"}`). Limits: a popup's very first request can precede its hook; cross-origin (out-of-process) iframes are covered
by layer A for subresources but not by layer D; a lying `Content-Type` passes. See [Session isolation](session-isolation.md#no-media-sessions).
See also: [Plan: No-media sessions](../implemented-plans/no-media-sessions.md)

## Gateway interplay
`browser_end_session` upstream returns `{"remaining": n, "blocked": m}`; the gateway ends the session at 0. `browser_request_human_help` raises the
right window through a hidden `browser_bring_to_front`. Cookies for the gateway's own downloads come from `browser_cookie_list`;
`fetch_html` uses a temporary browser so it cannot clobber anyone's page.
