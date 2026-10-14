# Browser tool server (multiple browsers per session)

A Python MCP server (`mcp-research/toolserver/`) runs inside each session under the session uid, one Chromium with one persistent
context. It replaced `@playwright/mcp`.
See also: [Plan: Python browser tool server](../implemented-plans/python-browser-tool-server.md)

## Why it exists
`@playwright/mcp` has a single "current tab": a main agent and its subagents sharing one MCP session clobber each other. Spikes
(several MCP processes on one Chromium over CDP, shared-context mode, two Chromiums on one profile, profile-copy seeding, patching
playwright-mcp) all failed or were unmaintainable; the plan has the table. Playwright itself has no such limit (one context, many
pages, shared cookies/localStorage), so the wrapper was replaced.

## Model
- **A "browser" is an OS window** (CDP `Target.createTarget {newWindow: true}`) with its tabs. Windows are 1440x900 at (0,0), no
  window manager.
- Pages join a browser via `page.opener()`.
- `browser_start_session` returns an unguessable id. Every other tool takes optional `browser`; default `main` (auto-created; shared by
  the main agent and the human). An unknown id gives a plain error and **never lists valid ids**.
- **No locking**: two callers on one id clobber each other. Isolation is by convention, carried in tool descriptions ("subagents:
  start your own browser, never use `main`").
- `browser_end_session(browser)` closes one window; without an id it closes everything and ends the session; closing the last browser
  ends the session. `browser_close` on the last tab navigates to `about:blank` (headed Chromium exits with its last window).
- Logins are shared because it is one context.

## Tools
Schemas for the 25 kept `@playwright/mcp` 0.0.83 tools come from `tools.json` (our descriptions; `tools.json.upstream` is the captured
original, checked by `test_toolserver.py`) plus a `browser` property. Added: 15 cookie / localStorage / sessionStorage tools,
`browser_storage_state`, `browser_set_storage_state` (state passed inline), `browser_start_session`. Dropped: `browser_run_code_unsafe`
(use `browser_evaluate`). Full list: [Tool reference](../api/tools.md).

- Element refs come from `page.aria_snapshot(mode="ai")` and resolve with `aria-ref=`; a stale ref gives "Ref … not found in the
  current page snapshot". Action tools return only `### Page` (URL, title) and modal state, **no snapshot**: call `browser_snapshot` or
  `browser_find` again after an action. See also: [Plan: Jobs for all tools](../implemented-plans/jobs-for-all-tools.md)
- `text_cap` (default 128, `null` = none) shortens pure text nodes in snapshots/finds; `find` matches the full text and centres the
  window on the match. `browser_find` shows each match with its ancestor chain (refs), the way to locate a content container.
- `browser_pdf_save` uses CDP `Page.printToPDF` (Playwright's `page.pdf` is headless-only).
- Files (screenshots, PDFs, `filename=` outputs) go to `$HOME/out`; the gateway copies them into jobs ([Jobs](jobs-and-storage.md)).

## Gateway interplay
`browser_end_session` upstream returns `{"remaining": n}`; the gateway ends the session at 0. `browser_request_human_help` raises the
right window through a hidden `browser_bring_to_front`. Cookies for the gateway's own downloads come from `browser_cookie_list`;
`fetch_html` uses a temporary browser so it cannot clobber anyone's page.
