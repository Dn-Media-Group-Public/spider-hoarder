# Plan: Tool file outputs become jobs

## Problem / Goal

Proxied Playwright tools (`browser_take_screenshot`, `browser_pdf_save`, and the
`filename=` option on `browser_snapshot`, `browser_evaluate`,
`browser_console_messages`, `browser_network_request(s)`, `browser_run_code_unsafe`)
write files into the session's homedir on the upstream side. The agent cannot
reach them, and the homedir is wiped on `stop()`. Only `ingest_page` and
`browser_save_page` store anything under a `job_id`.

Goal: **whenever a tool would return a file, it instead returns a `job_id`**
(`{job_id, files, text?}`), fetchable with `list_files`, `get_file_text` and
`get_file_url`. Tools that return content inline keep doing that.

Also: `browser_save_page` must tell the agent to prefer it over `browser_snapshot`
for content extraction (snapshot is for navigation/interaction only).

## Design

### Which calls become jobs

A proxied call becomes a job iff the upstream would write a file:

- `browser_take_screenshot`, `browser_pdf_save`: always (they always write a file).
- Any other tool that accepts `filename` (`browser_snapshot`, `browser_evaluate`,
  `browser_console_messages`, `browser_network_request`, `browser_network_requests`,
  `browser_run_code_unsafe`): only when the agent passes `filename`. Without it they
  return inline as today.

The set is a constant in `app.py` (`FILE_TOOLS`: name -> always|if-filename).

### Getting the bytes: gateway copies from a group-readable `out/` dir

No shim and no new endpoint. The gateway reads the file from the session's
output dir and copies it into blob storage; it never moves or deletes (the
`stop()` wipe removes the homedir anyway).

1. **Session layout (`sessiond.py`).** Homedir becomes `0710`, group `gw-collect`
   (traverse-only for the gateway; it cannot list the homedir). `sessiond` creates
   `/home/s<uid>/out`, owner = session uid, group `gw-collect`, mode `2750`
   (setgid, so files created inside inherit `gw-collect`). Session umask changes
   `077` -> `027`; this is safe because each session has a private gid
   (`group=uid`, `extra_groups=[]`), so "group-readable" only ever means the
   same session. Files in `out/` come out `0640`, group `gw-collect`.
2. **Gateway membership.** The gateway process is a member of `gw-collect`
   (container user setup in `Dockerfile` / `entrypoint.sh`). Sessions are not, and
   have no access to the central data store.
3. **Playwright output dir.** `playwright-mcp` is started with its output dir set
   to `$HOME/out` (`session-group.sh` / `mcp.json`).
4. **Proxy rewrite (`call_tool`).** For a file-producing call, the gateway ignores
   any agent-supplied path: it takes `basename(filename)` (or a default such as
   `screenshot.png`, `page.pdf`) as the *job-internal name*, passes upstream a
   private unique filename (`<uuid>-<name>`, relative, so it lands in `out/`),
   calls the upstream, then reads `out/<uuid>-<name>` and stores it under a new
   `job_id`. The agent never controls a host path.
5. **Safe read.** Open with `O_NOFOLLOW`, `fstat` -> regular file only, reject
   over `MAX_FILE_BYTES`, read from the fd. The path is built by the gateway from
   the session uid and its own uuid, never from upstream output or agent input.
   This blocks a session planting a symlink to gateway files.
6. **Store.** Via `_store_and_parse` (so `storage.write_bytes`, manifest and
   content-type are handled). Binary types the extractor supports (PDF) also get
   a `parsed/<name>.md`; screenshots get `original/` only.

### Result shape (all file-returning tools)

`{job_id, files: [...manifest entries...], text?}`, the same as `ingest_page` and
`browser_save_page` today.

**Inline-text rule, one helper for every tool:** the Markdown/text of the stored
file is returned as `text` when it is under `INLINE_LIMIT`; otherwise it is
omitted and the agent reads it with `get_file_text(job_id, path)` (paging via
`offset`/`length`) or `get_file_url`. This logic already exists in
`_store_and_parse`; it is kept and applied identically to `ingest_page`,
`browser_save_page` and the new file-producing tools. Text-like outputs
(snapshot, console, network, evaluate) are stored as `original/<name>` and their
text is the inline candidate; binary files have no inline text.

See also: [Plan: Terse outline view](terse-view.md) — when the full text is over the limit, the `terse/` outline is returned inline instead (with `text_path`).

### Tool descriptions

- `browser_save_page`: add "Prefer this over `browser_snapshot` for extracting
  page content; use `browser_snapshot` only for navigation and interaction
  (finding refs to click or type into)."
- Proxied tools' descriptions are upstream text; the gateway appends one line to
  those in `FILE_TOOLS`: "When this writes a file, the result is
  `{job_id, files, text?}`; fetch with `get_file_text` / `get_file_url`.
  `filename` only names the file inside the job."
- Server `instructions` string: mention that file outputs come back as jobs.

## Implementation order

1. `sessiond.py`: `gw-collect` group, homedir `0710`, `out/` `2750` with group,
   umask `027`. Update `test_sessiond.py` (modes, group, no cross-session read,
   still wiped by `stop`).
2. `Dockerfile` / `entrypoint.sh`: create `gw-collect`, add the gateway user.
3. `session-group.sh` / `mcp.json`: Playwright output dir -> `$HOME/out`.
4. `app.py`: `FILE_TOOLS`, safe-read helper, call rewrite, shared
   job-result helper; route `ingest_page` / `browser_save_page` through it.
5. `app.py`: description changes (`browser_save_page`, `FILE_TOOLS`
   suffix, `instructions`).
6. Tests: symlink in `out/` rejected, oversize rejected, agent `filename` with
   `../` or absolute path reduced to basename, owner scoping of the new jobs,
   screenshot/PDF round trip via `get_file_url`, inline vs over-limit text.
7. Docs.

## Verify during implementation

- That `playwright-mcp` resolves a relative `filename` against its output dir
  for every tool listed (the tool descriptions say so; confirm against 0.0.83+).
- That the default ACL/setgid behaviour holds on the container's filesystem
  (overlayfs on `/home`).

## Files to change

| File | Change |
|---|---|
| `mcp-research/sessiond.py` | `gw-collect` group, homedir `0710`, `out/` `2750`, umask `027` |
| `mcp-research/test_sessiond.py` | modes/group/isolation/wipe checks |
| `mcp-research/Dockerfile`, `entrypoint.sh` | create group, add gateway user |
| `mcp-research/session-group.sh` | Playwright output dir -> `$HOME/out` |
| `mcp-research/app.py` | `FILE_TOOLS`, call rewrite, safe read, shared result helper, tool descriptions, instructions |
| `mcp-research/test_security.py` / `test_gateway.py` | symlink, size cap, filename sanitising, round trips |

## Docs to update

- `docs/implemented-plans/browser-sessions-plan.md`: the "Anything that must
  outlive a session ... copied by the gateway" paragraph and the sessiond
  homedir/umask description (add See also).
- `docs/plans/agentic-research-mcp.md` tool-surface table (when that plan's docs
  are consolidated): `{job_id, files, text?}` now applies to all file-producing tools.
- `docs/implemented-plans/index.md` link after implementation, per `CLAUDE.md`.
