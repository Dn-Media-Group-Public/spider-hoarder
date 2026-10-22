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
| `get_upload_link()` | Page where the *user* uploads files into a new job (1 h). |
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
- Windows: `browser_start_session` (returns a new id; subagents must use their own, never `main`).
- Storage: `browser_cookie_{get,list,set,delete,clear}`, `browser_localstorage_*`, `browser_sessionstorage_*`,
  `browser_set_storage_state` (state inline).
- Refs from snapshot/find stay valid until the page re-renders or navigates; actions return no snapshot.
- Not implemented: `browser_run_code_unsafe`, network mocking, tracing, video, raw mouse.

## Egress
`/tor/mcp` browses only via Tor (anonymised, but some sites block Tor); `/clear/mcp` browses directly
(public internet only, not `.onion`). Use Tor for sensitive research, clear when a site blocks Tor.

## Android tools
Served at `/android-clear/mcp` and `/android-tor/mcp` ([Android emulators](../architecture/android.md)); one device per session, no
`browser` parameter. Bulk results are jobs, as above. See also: [Plan: Android hoarder](../implemented-plans/android-hoarder.md)

| Tool | Behaviour |
|---|---|
| `device_snapshot(filename?)` | Outline of the accessibility tree (visible/interactive nodes with `[ref=eN]`, flags, tap coordinates); webview content included. Job. |
| `device_find(text?, regex?, resource_id?, description?, class_name?)` | Matching nodes with refs. Job. |
| `device_click(ref)`, `device_long_click(ref, seconds?)` | Act on a ref. A stale ref answers "ref not found, take a new snapshot". |
| `device_tap(x, y)`, `device_swipe(x1, y1, x2, y2, seconds?)` | Coordinates, for canvas/game UIs. |
| `device_type(ref, text, clear?, submit?)` | Taps the field, types (Unicode ok), optionally presses enter. |
| `device_press_key(key)` | `back`, `home`, `enter`, `recent`, … |
| `device_wait_for(text?, text_gone?, time?, timeout?)` | Wait for text to appear/disappear, or a number of seconds (max 120). |
| `device_take_screenshot(filename?)` | PNG, always a job file. |
| `device_open_url(url)` | `am start -a VIEW`. |
| `device_install_apk(job_id, path)` | Installs an APK stored in a job (gateway pushes it into the slot). |
| `device_app_start(package, activity?)`, `device_app_stop(package)`, `device_app_list(all?)`, `device_current_app()` | App control. |
| `device_logcat(lines?, regex?, filename?)`, `device_shell(command)` | Logcat / adb shell (jobs). With `regex`, the whole buffer is filtered first, then the last `lines` matches are returned. |
| `device_request_human_help(reason)` | Live-view link, e.g. for a Google/Play sign-in (there is no pre-logged-in account). Suspends the idle timer. |
| `device_end_session()` | End the session; the emulator is wiped. |
| `get_upload_link`, `list_files`, `get_file_text`, `grep_file`, `get_file_url` | As above. |

Not carried over: cookie/storage tools, `ingest_page`, `browser_save_page`, `browser_pdf_save`, network tools, multi-browser.
All slots busy: `initialize` answers 503 immediately (no queue).
