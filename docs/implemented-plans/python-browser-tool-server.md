# Plan: Python browser tool server with multiple browsers per session

Status: implemented 2026-10-08 (see "Implementation notes" at the end). Builds on
[Browser sessions](browser-sessions-plan.md) and
[Tool file outputs become jobs](file-outputs-as-jobs.md).

## Problem / Goal

A browser session is one `@playwright/mcp` process and one Chromium. Every tool acts on a single
"current tab", so a main agent and its subagents (which share the MCP session) clobber each other:
a navigation by one overwrites the page of the other. We want subagents to browse **in parallel
without clobbering, while sharing the logins** the human made in the main browser.

Measured, not assumed (spikes, 2026-10-08):

| Attempt | Result |
|---|---|
| N `playwright-mcp` processes attached to one Chromium over CDP | All adopt the same tab; they clobber; popups show up in every process |
| `playwright-mcp --shared-browser-context` with several clients | Same: shared tab list |
| Separate Chromiums sharing a profile dir | Impossible: Chromium's `SingletonLock` hands the second launch to the first process |
| Seeding a second Chromium from a profile copy | Cookies and localStorage lag by about 30s (lazy flush, no way to trigger one); needs a live overlay |
| Patching `playwright-mcp` | The tab model is in `playwright-core` (`tools/backend/context.ts` etc.), not in the `@playwright/mcp` shim; the patch means maintaining a `playwright` fork across rolls |

Playwright itself has no such limit: one `BrowserContext` holds any number of pages, and a
persistent context shares cookies, localStorage and IndexedDB across all of them. The single-tab
assumption is only in the MCP wrapper. So: **replace the wrapper**, in Python, in this repo.

The hard part of such a wrapper (the accessibility snapshot with element refs) is public API:
`page.aria_snapshot(mode="ai")` returns `[ref=e3]` annotated snapshots and
`page.locator("aria-ref=e3")` resolves a ref, including iframe refs (`f1e2`). Verified on
`playwright-python` 1.63.0. A stale ref is a plain `TimeoutError`; the tool layer turns it into
"ref not found, take a new snapshot".

## Design

### Decisions (already made)

- **Language/placement:** a new Python MCP server (`mcp==1.29.1`, already pinned, plus
  `playwright-python`, pinned to the version tested, 1.63.0) replaces `@playwright/mcp` in the same
  slot: per-session process under the session uid, MCP over HTTP on `:9000+n`, behind the same
  `Host: mcp-upstream` check. Not in the gateway process (that would break uid isolation).
- **One Chromium, one persistent context per session**, headed, same launch config as today
  (`$HOME/profile`, same args, `consent.js` init script, proxy and host-rules handling).
  Logins are shared by construction: it is one context.
- **A "browser" is an OS window.** Created with CDP `Target.createTarget {newWindow: true}`.
  Verified in the session image (headed, Xvfb, no window manager):
  `context.new_page()` gives a tab in the *same* window (so it is not used for new browsers);
  `window.open()` from a page opens a tab in the *opener's* window; `window.open(.., "popup,...")`
  opens its own OS window; `page.opener()` identifies the opener in all cases;
  `page.bring_to_front()` raises a window with no window manager.
- **Pages belong to browsers via `page.opener()`:** a page created by a page of browser X joins X,
  including feature-popups that get their own OS window.
- **API:**
  - `browser_start_session()` returns a new unguessable id (the model never invents ids).
  - Every other tool takes an optional `browser` id; default `"main"`, auto-created (the first
    window). An unknown id returns a plain "unknown browser id" error and **does not list the
    valid ids** (a subagent must never learn another agent's id). `browser_tabs` lists only that
    browser's pages.
  - `browser_end_session(browser)` closes one browser (window and its tabs).
    `browser_end_session()` with no id closes all browsers and ends the session.
    When the last browser is closed the whole session is closed.
  - `browser_close` keeps playwright-mcp's meaning: closes the *current tab of the given
    browser*; it does not end the session.
- **No concurrency handling.** No per-browser lock, no busy error. Two callers on one id clobber
  each other. Isolation is by convention, carried by tool descriptions: a subagent is told to
  call `browser_start_session` first and use only the id it returns, and to never use `main`
  (shared; the main agent and the human use it).
- **Tool set:**
  - Kept (live today): `browser_navigate`, `_navigate_back`, `_snapshot`, `_find`, `_click`,
    `_type`, `_fill_form`, `_press_key`, `_select_option`, `_hover`, `_drag`, `_drop`,
    `_evaluate`, `_wait_for`, `_tabs`, `_handle_dialog`, `_file_upload`, `_resize`,
    `_emulate_media`, `_take_screenshot`, `_pdf_save`, `_console_messages`,
    `_network_requests`, `_network_request`, `_close`; plus the gateway's own `_save_page`,
    `_request_human_help`, `_end_session`.
  - Dropped: `browser_run_code_unsafe` (JS snippets cannot run in a Python server;
    `browser_evaluate` stays and covers page-level JS).
  - **Added and enabled: all 17 storage tools:** `browser_cookie_{get,list,set,delete,clear}`,
    `browser_localstorage_{get,list,set,delete,clear}`,
    `browser_sessionstorage_{get,list,set,delete,clear}`, `browser_storage_state`,
    `browser_set_storage_state`.
  - Not implemented (disabled today; add later if missed): network mocking
    (`route*`, `network_state_set`), tracing, video, codegen/recording, verify tools,
    dashboard/debug tools (`annotate`, `highlight`, `resume`, `get_config`), raw mouse (`mouse_*`).
- **Response format** (markdown sections, as today, minus the code blocks): `### Result`,
  `### Page` (URL, title), `### Snapshot`, `### Error`. **No `### Ran Playwright code`.**
  `browser_evaluate` must return exactly `### Result\n<json>`: the gateway unwraps it in
  `sessions.py:168` (`parse_eval_result`), which only reads `### Result` up to the next `\n### `.
- **File outputs keep their contract:** output dir `$HOME/out`; tools that take `filename`
  (`snapshot`, `evaluate`, `console_messages`, `network_request(s)`) write there when given;
  `take_screenshot`, `pdf_save` (and `storage_state`) always do. The gateway's `FILE_TOOLS`
  rewrite and copy into jobs is unchanged.
- **Cutover is hard.** `session-group.sh` starts the new server instead of `playwright-mcp`; no
  feature flag; `@playwright/mcp` is removed from the image.
- **Tests:** per-tool pytest against a real headless Chromium, plus a container smoke test through
  the gateway.

### Component structure (new, `mcp-research/toolserver/`)

| Module | Responsibility |
|---|---|
| `server.py` | MCP server (streamable HTTP), `Host` allow-list `mcp-upstream`, tool registry |
| `browsers.py` | `BrowserRegistry`: id -> window (`Target.createTarget`), `page -> browser` attribution via `page.opener()`, `main` auto-create, remaining count |
| `snapshot.py` | `aria_snapshot(mode="ai")`, ref resolution via `aria-ref=`, stale-ref error, snapshot-to-file |
| `tools_*.py` | Tool groups (navigation, interaction, tabs, output, storage), schemas copied from playwright-mcp 0.0.83 so existing prompts keep working, plus the `browser` parameter |
| `format.py` | The response sections above |

### Gateway changes (`mcp-research/app.py`, `sessions.py`)

The gateway is mostly a proxy, but these places depend on today's server:

1. **`cookies_for()` (`sessions.py:~205`) uses `browser_run_code_unsafe`** to read the context's
   cookies for the gateway's own `requests` downloads. Replace with `browser_cookie_list`
   (now implemented), returning `### Result\n<json>` so `parse_eval_result` still applies.
2. **`fetch_html(live=...)` (`sessions.py:~213`)** opens a temporary tab with `browser_tabs new`
   and closes "the current tab". With several browsers, use `browser_start_session()` for a
   temporary browser, read, then `browser_end_session(id)`: it cannot clobber anyone's page.
3. **`browser_end_session` (`app.py:~99`, handler `~288`)** stays a gateway tool, gains optional
   `browser`: with an id, forward to the upstream tool of the same name; with no id, tear down
   the session as today. If the upstream reports no browsers left, the gateway ends the session.
4. **`browser_request_human_help` (`app.py:~90`)** gains optional `browser`; the gateway raises
   that window via a new upstream tool `browser_bring_to_front(browser)`, hidden from the model
   (`HIDDEN`). The live-view link stays per session.
5. **`browser_save_page`** gains optional `browser` (which window to save).
6. **`FILE_TOOLS` (`app.py:~49`)** adds `browser_storage_state`; drops `browser_run_code_unsafe`.
7. `upstream_tools()` (tool list from a throwaway session) and the `Host: mcp-upstream` header
   work unchanged.
8. `test_gateway.py:58` fixture uses the old `### Ran Playwright code` response; update.

### Packaging

- `requirements.txt`: add `playwright==1.63.0` (it needs the compiled `greenlet`; wheels exist).
- `Dockerfile`: drop `npm install -g @playwright/mcp`; install Chromium with
  `playwright install --with-deps chromium` from the venv (`PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`
  kept). Node is then only needed if something else uses it (check; if not, move the base image
  from `node:22-bookworm-slim` to a Python base). Xvfb, x11vnc, noVNC unchanged.
- `session-group.sh`: replace the generated `mcp.json` + `exec playwright-mcp` with
  `exec /venv/bin/python -m toolserver` and pass the same launch args through a small config file.

## Implementation order

1. Before removing anything, capture the golden tool schemas from the running old server
   (`list_tools` from a probe session) into a test fixture; the new server's schemas are checked
   against it, minus `run_code_unsafe`, plus `browser` and the new tools.
2. `toolserver/browsers.py` and the launch: headed persistent context, `main`, new windows,
   opener attribution, remaining count; unit tests with a headless Chromium.
3. `snapshot.py` and the core interaction tools (navigate, snapshot, click, type, ...), `format.py`.
4. Remaining kept tools (tabs, dialogs, upload, resize, emulate_media, screenshot, pdf, console,
   network, find, drag, drop, evaluate, wait_for).
5. The 17 storage tools.
6. `server.py`: MCP over HTTP, `Host` allow-list, `browser_start_session`, `browser_end_session`,
   `browser_bring_to_front` (hidden), tool descriptions carrying the "subagents: start your own
   browser, never use main" instructions.
7. Gateway changes (list above) and their tests.
8. Packaging (`requirements.txt`, `Dockerfile`, `session-group.sh`); remove `@playwright/mcp`.
9. Container smoke test through the gateway: two browsers in parallel, shared login, file jobs,
   `### Result` unwrap, human-help raising the right window, last browser closed ends the session.
10. Docs (below), move this plan to `docs/implemented-plans/`.

## Files to change

| File | Change |
|---|---|
| `mcp-research/toolserver/` (new) | The server, as above |
| `mcp-research/test_toolserver.py` (new) | Per-tool tests, response format, schema golden check |
| `mcp-research/session-group.sh` | Start the Python server instead of `playwright-mcp` |
| `mcp-research/Dockerfile` | Drop npm `@playwright/mcp`; install Chromium via `playwright`; maybe a Python base |
| `mcp-research/requirements.txt` | Add `playwright==1.63.0` |
| `mcp-research/app.py` | `browser` on `browser_end_session`, `browser_request_human_help`, `browser_save_page`; `FILE_TOOLS`; hide `browser_bring_to_front` |
| `mcp-research/sessions.py` | `cookies_for` via `browser_cookie_list`; `fetch_html(live)` via a temporary browser; end session when none left |
| `mcp-research/test_gateway.py` | Update the old-format fixture; cover the new routing |
| `mcp-research/smoke_e2e.py` | Multi-browser scenario |

## Docs to update

`docs/architecture/` and `docs/api/` do not exist in this repo (CLAUDE.md names them); the
affected docs are:

- `docs/implemented-plans/browser-sessions-plan.md`: it states `@playwright/mcp` with "all 27+
  tools, not reimplemented" and shows it in the architecture diagram; correct both and add a
  "See also: Plan: Python browser tool server".
- `docs/implemented-plans/file-outputs-as-jobs.md`: its `FILE_TOOLS` list includes
  `browser_run_code_unsafe`; add the note and the "See also".
- `docs/implemented-plans/index.md`: link this plan after the move.
- `README.md`: tool list and the multi-browser usage, if it describes the browser tools.

## Derived details to confirm in review

These follow from the agreed decisions but were not discussed in detail:

1. **Snapshot delivery.** In the current deployment a post-action response links the snapshot as a
   file (`- [Snapshot](out/page-<timestamp>.yml)`) instead of inlining it. The new server should
   replicate whatever `playwright-mcp` does today here; check the golden outputs in step 1.
2. **`browser_set_storage_state` input.** It reads a file, but the agent cannot place files in the
   session's homedir. Recommendation: accept the state JSON inline (a `state` parameter) instead
   of a `filename`, and keep `browser_storage_state` as an output file/job.
3. **Closing `main`.** Closing `main` with no other browsers ends the session (the last-browser
   rule). Confirm that is wanted, or that `main` is exempt.
4. **Window sizing.** All windows are created at the session's 1440x900 and overlap at (0,0)
   (there is no window manager); `browser_resize` resizes the target browser's window through
   `Browser.setWindowBounds`.
5. **Feature-popups** (own OS window) appear as extra windows to the human, though attributed to
   the opener's browser.

## Not in scope

Per-browser locking or ownership enforcement; seeding separate Chromium processes; any fork of
`playwright` or `playwright-mcp`; the tools listed under "Not implemented".

## Implementation notes

Deviations and details settled while implementing:

- **Modules:** `toolserver/` has `browsers.py`, `tools.py` (all tool groups and `format` helpers in one file), `server.py`
  (schemas and transport) and `__main__.py`. `snapshot.py`/`format.py`/`tools_*.py` were folded in: they were each a few lines.
- **Schemas:** the 25 kept tools are served from `mcp-research/golden_tools.json` (captured from `@playwright/mcp` 0.0.83
  with `--caps pdf`, 26 tools incl. `browser_run_code_unsafe`) plus a `browser` property; `test_toolserver.py` checks them against it.
- **Snapshot delivery** (derived detail 1): as before, post-action responses link `- [Snapshot](out/page-<ts>.yml)`; an explicit
  `browser_snapshot` inlines a yaml block unless given `filename`. **Superseded:** post-action responses no longer carry a
  snapshot (only `### Page` and modal state); `browser_snapshot` / `browser_find` gain `text_cap` (default 128, null = none; cuts
  pure text nodes, `find` centers the window on the match) and `browser_find` also returns `### Page`. The gateway always
  stores their output as a job. See also: [Plan: jobs for all tools](../implemented-plans/jobs-for-all-tools.md).
- **`browser_set_storage_state`** (2): takes the state inline as `state` (object or JSON string).
- **Closing `main`** (3): `browser_end_session(browser="main")` follows the last-browser rule. `browser_close` on the last tab of the
  last browser navigates it to `about:blank` instead of closing it (headed Chromium exits with its last window and would kill the
  server); end the session with `browser_end_session`.
- **Windows** (4): new windows are set to 1440x900 at (0,0) via `Browser.setWindowBounds`; `browser_resize` resizes the target
  browser's window the same way. A new tab (`browser_tabs new`) is made after `bring_to_front` on the browser's current tab, so
  Chromium places it in that window; popups are attributed through `page.opener()`.
- **`browser_pdf_save`** uses CDP `Page.printToPDF` (Playwright's `page.pdf` is headless-only).
- **Gateway:** `browser_end_session` upstream returns `{"remaining": n}`; the gateway ends the session when it reaches 0.
  `parse_result` (any JSON) was split out of `parse_eval_result` (string only).
