# Tool reference

What Claude sees at `/<egress>/mcp`. The server also sends its own `instructions`. Bulk results are jobs
(`{job_id, files, text?, text_path?}`; [Jobs and storage](../architecture/jobs-and-storage.md)).
See also: [Plan: Browser sessions](../implemented-plans/browser-sessions-plan.md),
[Plan: Python browser tool server](../implemented-plans/python-browser-tool-server.md),
[Plan: Jobs for all tools](../implemented-plans/jobs-for-all-tools.md)

## Gateway tools
| Tool | Behaviour |
|---|---|
| `browser_request_human_help(reason, browser?)` | Link for the user to solve a CAPTCHA/login in the live view (owner-only, SSO). Wait for the user. |
| `browser_save_page(target?, browser?)` | Convert the open page (or `target`: an element ref/selector) to Markdown and store it as a job. Prefer over `browser_snapshot` for content. |
| `ingest_page(url, target?)` | One shot: ephemeral browser, navigate, save, destroy. `target` must be a selector. Non-HTML goes through `requests`. |
| `browser_end_session(browser?)` | Close one browser, or all and the session. |
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
  `browser_console_messages`, `browser_network_requests`, `browser_network_request`, `browser_take_screenshot`, `browser_pdf_save`,
  `browser_storage_state`.
- `browser_network_requests` takes `new` (only requests since the last `new: true` call on the tab; "No new requests." as plain text
  if none). `browser_network_request(index)` returns a job: `original/request|response/headers.json` and `body.<ext>` (raw bytes;
  absent parts have no file), parsed like any file; no `part`/`filename`. `index.json` has `url`, `method`, `status`.
  See also: [Plan: network request jobs](../implemented-plans/network-request-jobs.md)
- Windows: `browser_start_session` (returns a new id; subagents must use their own, never `main`).
- Storage: `browser_cookie_{get,list,set,delete,clear}`, `browser_localstorage_*`, `browser_sessionstorage_*`,
  `browser_set_storage_state` (state inline).
- Refs from snapshot/find stay valid until the page re-renders or navigates; actions return no snapshot.
- Not implemented: `browser_run_code_unsafe`, network mocking, tracing, video, raw mouse.

## Egress
`/tor/mcp` browses only via Tor (anonymised, but some sites block Tor); `/clear/mcp` browses directly
(public internet only, not `.onion`). Use Tor for sensitive research, clear when a site blocks Tor.
