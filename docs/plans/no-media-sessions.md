# Plan: No-media sessions

## Problem / Goal

Browsing the dark web (Tor, I2P) can expose the user, or their employer, to legal risk if images, video, audio or other binary
objects are even *loaded* by the browser, let alone written to disk. Give the user a session-level option so the browser never
requests media, and `ingest_page` / `fetch.py` never download it.

Decision owner: the user (trained journalists) and their editors. The system provides the mechanism, a visible record of the policy,
and honest documentation of its limits. It does not second-guess the choice.

## Design

### Policy

Chosen once per gateway session (own uid, one Chromium, one Playwright context), **immutable** for its lifetime. Applies to every
browser window in the session (`browser_start_session` windows inherit it; routes and CDP hooks are per context) and to `ingest_page`.

| Field | Meaning | Default |
|---|---|---|
| `media` | `true` = media allowed, `false` = block media | `false` on `tor` / `i2p`, `true` on `clear` |
| `allow` | Extra content types allowed at the response stage (layer D) | `[]` |
| `deny` | Content types blocked at the response stage (layer D) | `[]` |

`allow` / `deny` entries are exact MIME types (`image/svg+xml`) or `type/*` wildcards, case-insensitive, parameters
(`; charset=`) stripped. They apply **only to layer D** (navigations, iframes, `ingest_page`). They never re-enable subresource
types at layer A (`<img>` tags still do not load after `allow=["image/svg+xml"]`).

Precedence: `deny` → `allow` → default text allowlist → block. `deny` applies even when `media=true` (so a clear session can block
only e.g. `text/geojson`). With `media=true` and no `deny`, nothing is blocked.

### How the user sets it: `open_session`

New gateway tool `open_session(media?, allow?, deny?)`.

- Must be the **first** tool call, and callable **once**. A second call fails with an error; the policy cannot change.
- Every other tool (gateway and toolserver, including `ingest_page`) returns an error until it has been called. Fail-closed: the
  agent cannot skip the question. Defaults are chosen by calling `open_session()` with no arguments.
- The tool description instructs the agent: the user must be explicitly asked about the security settings *before* the call; call it
  before any browser calls; pass `allow` / `deny` only if the user requested a specific security setup.
- The effective policy is returned by `open_session` and written to the session log.
- `browser_start_session` takes no policy parameters (windows inherit the session policy).

### Reporting it: `get_session`

New read-only gateway tool `get_session()` so the agent (including subagents, which did not make the decision) can see
what was already decided. It takes no parameters and cannot change anything. `egress` is the session type (`clear`, `tor` or `i2p`). It returns the effective policy:
`{egress, opened, media, blocked_resource_types, allowed_content_types, denied_content_types}`, where the content-type lists are the
*effective* lists for direct navigation and downloads (default text allowlist plus user `allow`, and user `deny`). Before
`open_session` it returns `{opened: false, ...}` with the remaining fields set to the defaults that `open_session()` with no arguments
would apply on this egress; it is the one tool exempt from the fail-closed gate. The `open_session` result has the
same shape.

Known limit, accepted: the agent calls the tool, so correctness of what is passed depends on the agent relaying the user's answer.
The record of the effective policy (tool result and log) lets editors audit it.

### Enforcement: layer A (resource type, before the request)

In the toolserver, on the persistent context: `context.route("**/*", handler)`; the handler aborts requests whose `resource_type`
is in `{image, media, object, imageset, font}`. Covers `<img>`, `<picture>`, `srcset`, favicons, CSS backgrounds, `<video>`,
`<audio>`, `<object>`, `<embed>`, web fonts. Aborted requests never reach the network, so they never reach the cache.
Blocked subresources are **silent** to the page (it just does not render them) but logged (below).

### Enforcement: layer D (Content-Type, at response headers)

CDP `Fetch.enable` with a response-stage pattern on each page's CDP session (`context.new_cdp_session(page)`, already used for
`browser_pdf_save`); on `Fetch.requestPaused` at the response stage, check the `Content-Type` header; if blocked, `Fetch.failRequest`
before the body is read, otherwise `Fetch.continueRequest`. Catches direct navigation to `foo.jpg` and iframes serving media
(Playwright reports these as `document`).

Content-Type rule (when `media=false`): **allowlist**. Allowed: `text/*`, `application/json`, `application/xml`, `application/*+json`,
`application/*+xml`, `application/xhtml+xml`, plus user `allow`. Everything else is blocked, explicitly including `image/*` (SVG too),
`video/*`, `audio/*`, `application/pdf`, `application/octet-stream`, `application/x-shockwave-flash`, and a missing header. User
`deny` is applied first. A failed top-level navigation surfaces as an error: `blocked: media disabled for this session
(<content-type>)`.

### `fetch.py` / `ingest_page`

Same rules, one shared implementation (`toolserver/mediapolicy.py`, pure stdlib: `Policy`, `resource_blocked(type)`,
`content_type_blocked(header)`). `fetch.py` streams the response (`requests` `stream=True`), checks headers, and closes the
connection before reading the body when blocked. The ephemeral browser used by `ingest_page` is a toolserver browser in a session
and so already has layers A + D. `ingest_page` uses the session's policy; no per-call parameters.

### Audit log

Each blocked request is recorded (URL, resource type or content type, layer) and appears in `browser_network_requests` marked
`blocked: media policy`. A short count is included in the `browser_end_session` result.

### Not done (decided)

- No disk-cache flags. The disk is scratched at session end and there is no legal distinction between RAM and disk for temporary
  data. A body that slips past A + D (spoofed `Content-Type`) lives in browser memory / the disposable homedir until session end.
- No placeholder text for blocked media, no URL-extension denylist, no relay-level filtering (the SOCKS relay cannot see content).

### Documented limits

- A server that lies about `Content-Type` is not caught at layer D (headers are the only signal before the body).
- Headers of a blocked response reach the browser.
- The policy is only as good as the question the agent asked the user.

## Implementation order

1. `toolserver/mediapolicy.py`: `Policy` (media, allow, deny, defaults by egress), `resource_blocked`, `content_type_blocked`; unit tests.
2. Toolserver: internal one-shot tool `browser_set_policy` (not listed by the gateway, not in `tools.json` exposed to clients) that
   installs layer A route and layer D CDP `Fetch` hook (also for pages opened later, via `context.on("page")`) and locks. Until it
   runs, every other toolserver tool returns an error.
3. Toolserver: log blocked requests; mark them in `browser_network_requests`.
4. Gateway: `open_session` tool (once, first) and read-only `get_session`; `BrowserSession.policy`; gate all other tools
   (except `get_session`) on it; call `browser_set_policy`; log the effective policy.
5. `fetch.py`: stream + header check via `mediapolicy`; pass the session policy from `app._ingest_page` / `sessions.fetch_html`.
6. Tests (see below), docs, move this plan.

## Files to change

| File | Change |
|---|---|
| `mcp-research/toolserver/mediapolicy.py` | New: policy model, the two block predicates, and the effective-settings summary (shared by toolserver and gateway). |
| `mcp-research/toolserver/browsers.py` | Install route (A) and CDP `Fetch` hook (D) on the context / pages; blocked-request log. |
| `mcp-research/toolserver/tools.py` | `browser_set_policy` (internal, one-shot, locks); error on other tools until set; blocked marker in network requests. |
| `mcp-research/toolserver/server.py` | Gate tool dispatch on the policy being set. |
| `mcp-research/tools.json` | Not changed for clients; `browser_set_policy` is internal only (check `test_toolserver.py` upstream-diff test). |
| `mcp-research/app.py` | `open_session` and `get_session` tools and descriptions; gate other tools; `ingest_page` uses session policy. |
| `mcp-research/sessions.py` | `BrowserSession.policy`; default by `EGRESS`; call `browser_set_policy`; pass policy to `fetch_html`. |
| `mcp-research/fetch.py` | Streamed header check using `mediapolicy`. |
| `mcp-research/test_toolserver.py`, `test_gateway.py`, `test_security.py` | Tests below. |

## Tests

- `mediapolicy`: defaults per egress; precedence deny → allow → default; wildcard and parameter stripping; missing header blocked;
  `deny` effective with `media=true`.
- Toolserver (local test page): `<img>`, `<video>`, CSS background, favicon, font never produce a network request; direct navigation
  to an image URL fails with the blocked error; `text/html` and JSON load; `allow=["image/svg+xml"]` permits navigating to an SVG but
  not `<img src=svg>`; a second window inherits the policy.
- Gateway: any tool before `open_session` errors; second `open_session` errors; a changed MCP reconnect cannot change the policy;
  `browser_set_policy` is not exposed to clients; `ingest_page` of an image URL on a no-media session errors.
- `get_session`: works before `open_session` (`opened: false`, fields equal the no-argument defaults for the egress); after it, lists match the effective policy (defaults per
  egress, `allow` added, `deny` listed); has no parameters that could alter it.
- `fetch.py`: blocked content type closes the connection without reading the body.

## Docs to update

- `docs/architecture/session-isolation.md`: no-media policy, layers A + D, residual risks (new section after Tor/I2P).
- `docs/architecture/browser-toolserver.md`: policy hook, `browser_set_policy`, fail-closed gate, blocked marker.
- `docs/api/tools.md`: `open_session`, `get_session`; updated `ingest_page` and `browser_network_requests` rows.
- Each with a "See also" link to `../implemented-plans/no-media-sessions.md`, and `docs/implemented-plans/index.md` updated.
- On completion move this file to `docs/implemented-plans/no-media-sessions.md` and update `docs/plans/index.md` if it lists it.
