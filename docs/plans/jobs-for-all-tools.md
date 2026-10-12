# Plan: jobs and metadata for all output tools, text cap, `grep_file`, no post-action snapshots

Depends on [save-page-target.md](save-page-target.md) for `storage.set_meta` and the `url` / `target` keys in
`index.json`. If this plan is implemented first, move step 1 of that plan here.

## Problem / Goal

1. **Large tool output lands in the agent's context.** `browser_snapshot`, `browser_find`,
   `browser_evaluate`, console and network tools return their text inline unless the client passes
   `filename` (`FILE_TOOLS` with a `None` default, `app.py:52`). A real article page gives a snapshot of tens of
   thousands of characters, most of it paragraph text the agent only needs for navigation.
2. **Output has no durable handle.** Without `filename` there is no `job_id`, so output cannot be paged
   (`get_file_text`) or re-read later, and nothing records where it came from.
3. **There is no way to search a stored file.** `browser_find` searches the live page only; a stored job
   file can only be read in 20,000-character slices.
4. **Every action writes a snapshot file nobody can read.** `post()` (`toolserver/tools.py:66`) writes
   `out/page-<timestamp>.yml` after each navigate/click/type and links it in the result. The path is inside the
   container; no code reads it and the client cannot (see upstream-intent notes below). It costs one snapshot per
   action.
5. **The `filename` path does not work in the deployed container.** Both attempts this session returned
   `Permission denied: '/home/s2000N'`. `_read_out` (`app.py:223`) opens the session home with `O_RDONLY`;
   `sessiond.py:154` creates it `0o710` (group `gw-collect` may traverse, not read), and opening a directory
   for reading needs the read bit. Reproduced locally: `O_RDONLY|O_DIRECTORY` on an x-only directory fails with
   `EACCES`; `O_PATH|O_DIRECTORY|O_NOFOLLOW` succeeds, and `openat` through it reads `out/` and its files.

Goal: every tool that returns bulk text returns a job, with inline text only under the existing limit; snapshots
and finds get a text cap so navigation output stays small; stored files are searchable; refs come only from the
tools the client calls.

## Design

### Decisions

1. **Always a job.** These proxied tools return `{job_id, files, text?, text_path?}` always (not only with
   `filename`): `browser_snapshot`, `browser_find`, `browser_evaluate`, `browser_console_messages`,
   `browser_network_requests`, `browser_network_request`. In `FILE_TOOLS` their `None` default becomes a
   default job-internal name: `snapshot.yml`, `matches.txt`, `evaluate.json`, `console.txt`,
   `network-requests.txt`, `network-request.txt`. A client-supplied `filename` still renames the file inside the
   job. Inline text uses the existing rule (`_store_file` → `_job_result`): the file's text inline if under
   `INLINE_LIMIT` (50,000 chars), otherwise no inline text, and the client pages with `get_file_text` or
   searches with `grep_file`. No terse outline is made for these files.
2. **Text cap** on `browser_snapshot` and `browser_find`, implemented in the toolserver:
   - Parameter `text_cap`, schema `{"type": ["integer", "null"], "minimum": 1, "default": 128}`: omitted =
     128; JSON `null` = no cap; an integer = that many characters. Same pattern as `get_file_text.length`
     (`app.py:142`).
   - It applies to **pure text nodes, under any parent**: lines of the form `- text: …` and the inline text of
     an element whose only child is text (`paragraph [ref=e204]: …`). It does not apply to quoted names
     (`link "…"`, `button "…"`, `heading "…"`), attributes, or `/url:` lines.
   - A text longer than the cap is cut to the cap and `...` is appended. The result is for people and LLMs,
     not a YAML parser; validity of the YAML after cutting is not guaranteed.
   - `browser_snapshot`: the stored `snapshot.yml` **is the capped tree**.
   - `browser_find`: matching runs on the **full** (uncapped) text. A shown line whose text exceeds the cap
     is cut to a window of `text_cap` characters **centered on the match** with `...` on each cut side; if
     the match is outside the text part (in a name or a ref), the text part is cut from the start. Lines with
     short text are unchanged.
   - `DEFAULT_TEXT_CAP = 128` is defined once, in a small module `toolserver/textcap.py` (no playwright import)
     together with the cap functions, and imported by the gateway to record the effective cap.
   - Rationale for 128, measured on the DOM text of 22 news pages: 98.5% of h1–h3 headings, 99.6% of link
     texts and 47.6% of paragraphs are at most 128 characters; paragraph text is reduced to 61%.
3. **Metadata in `index.json`** (via `storage.set_meta`, optional keys, absent when not applicable):
   - `url`: the page URL, taken from the `### Page` section of the tool's result (the toolserver's `find` and
     the other file tools return that section too).
   - `target`, `depth`: as given to `browser_snapshot` (none when absent).
   - `text_cap`: the cap actually applied (integer, or `null` for none).
   - `query`: for `browser_find`, `{"text": …}` or `{"regex": …}` as given.
4. **No post-action snapshots.** `post()` stops writing `out/page-*.yml` and drops the `### Snapshot` link;
   it keeps the `### Page` and modal-state sections. Refs are obtained with `browser_snapshot` /
   `browser_find`, which call the same `aria_snapshot(mode="ai")`.
   Verified with Playwright 1.63.0 in headless Chromium (not through our toolserver): with no snapshot taken, an
   `aria-ref` resolves to nothing; after a snapshot, refs resolve; an unchanged page gives identical snapshots;
   after a click adds an element, existing elements keep their refs and the new one gets a new ref (old refs
   keep resolving even before a new snapshot); a subtree snapshot's refs equal the later full snapshot's
   refs for the same elements; iframe refs (`f1e2`) resolve. Refs are **invalidated** when an element's
   role/name changes or its container is re-created (framework re-render); and on navigation old refs resolve
   to nothing, with new refs namespaced per document (`f1e3`, then `f2e3`), so a stale ref fails instead of
   pointing at another element (our `act()` already turns that into "Ref … not found in the current page
   snapshot. Try capturing new snapshot."). Not verified: whether elements cut off by `depth` get refs.
5. **`grep_file(job_id, path, pattern, context=2, max_matches=50)`** — new gateway tool next to
   `get_file_text`:
   - Any text file in the job: no NUL byte in the first 1 KB (the sniff `_store_file` already uses), decoded as
     UTF-8 with replacement. Binary files are refused with a clear message.
   - `pattern` is a Python regular expression; the `/pattern/flags` form (flags `i` only) is accepted, the same
     convention as `browser_find`'s `regex`. An invalid pattern is a tool error.
   - Output per match: line number, character offset of the line in the file (for `get_file_text` `offset`),
     the matching line and `context` lines before and after. Each line is trimmed to about 300 characters
     around the match with `...`. Matches whose context overlaps are merged. At most `max_matches` matches;
     the total output is bounded by `INLINE_LIMIT` and ends with a "showing M of N matches" note when cut.
   - Owner-scoped like `get_file_text` (`_key(owner, job_id)`, `_check_rel_path`, `storage.exists`).
6. **`_read_out` fix.** Open the session home with `os.O_PATH | O_DIRECTORY | O_NOFOLLOW` instead of
   `O_RDONLY`. Everything after it is unchanged: `out/` is opened `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`
   relative to the home fd (`0o2750`, group-readable), then the file `O_NOFOLLOW | O_NONBLOCK` (umask `0o027`,
   group-readable), regular files only, size-capped.
7. **Guidance** (tool descriptions, server instructions): snapshots and finds are jobs; `text_cap`; refs are
   valid until the page re-renders or navigates, so call `browser_find` / `browser_snapshot` again after an action
   that changes the page; `grep_file` for stored files.

### Result shape change

`browser_snapshot` and `browser_find` results become the same JSON as other job results. The previous inline
`### Page` header and YAML code fence are no longer in the text; the page URL is in `index.json`.

### `browser_find` path and context (already fixed, not part of this plan's work)

The description of `browser_find` (inherited from upstream, `golden_tools.json:313`) promised matching nodes
"with a few lines of surrounding context … each shown under its path from the root of the tree", but
`toolserver/tools.py` returned only the matching lines. That was fixed as a bugfix, outside this plan:
`find_snippets()` ports upstream's algorithm (3 lines of context per match, overlapping windows merged, each
window preceded by its ancestor chain, `...` for skipped lines, windows separated by `----`, header
`Found N match(es) for <query>:`). It gives the same output as upstream's JavaScript on 72 comparisons (8 real
pages, 9 queries each). Tests: `test_find_snippets_context_and_ancestors`, `test_find_via_tool`.

For this plan: the cap applies to every line `find` shows (matching, context and ancestor lines), centering only
on matching lines. `find`'s result still has no `### Page` section (step 3 adds it).

### Out of scope

- Job retention and cleanup (every snapshot/find is now a job; no TTL or quota exists; decided not to be a
  concern of this plan).
- A job per action (the post-action snapshot is removed, not converted).
- Plan A's `browser_save_page` / `ingest_page` changes.
- A text cap on tools other than `browser_snapshot` and `browser_find`.

## Implementation order

1. `storage.py`: `set_meta` (from plan A), if not yet present.
2. `toolserver/textcap.py`: `DEFAULT_TEXT_CAP`, `cap_snapshot(text, cap)`, `cap_found_line(line, match_span, cap)`.
3. `toolserver/tools.py`: `snapshot` and `find` accept `text_cap` (distinguish omitted from null); `find` adds the
   `### Page` section; `post()` loses the snapshot file and link.
4. `toolserver/server.py`: `build_tools` adds the `text_cap` property to `browser_snapshot` and `browser_find`.
5. `app.py`: `FILE_TOOLS` defaults; `_file_tool` records metadata (`url` from `### Page`, `target`, `depth`,
   effective `text_cap`, `query`); `_read_out` uses `O_PATH`.
6. `app.py`: `grep_file` tool (schema, description, handler), with the matching function placed in `extract.py`.
7. Descriptions and server instructions.
8. Tests; docs; move the plan.

## Files to change

| File | Change |
|---|---|
| `mcp-research/toolserver/textcap.py` | new: default and cap functions |
| `mcp-research/toolserver/tools.py` | `snapshot`, `find` (`text_cap`, `### Page`), `post()` without snapshot |
| `mcp-research/toolserver/server.py` | `text_cap` in the two tool schemas |
| `mcp-research/app.py` | `FILE_TOOLS` defaults, metadata in `_file_tool`, `_read_out` `O_PATH`, `grep_file`, descriptions, instructions |
| `mcp-research/extract.py` | `grep_text` (line/offset/context matching) |
| `mcp-research/storage.py` | `set_meta` if not already added by plan A |
| `mcp-research/test_toolserver.py` | `test_schemas_match_golden` allows `text_cap` on the two tools; ref tests; cap tests |
| `mcp-research/test_gateway.py` | default job names, metadata, `_read_out`, `grep_file` |

## Tests

- **Cap (unit, `textcap`):** fixtures taken from a real snapshot: a `- text:` node; `paragraph [ref=…]: <long>`;
  a `generic` with `text` / `link` / `text` children (the DN paragraph that trafilatura dropped); a quoted name
  longer than the cap (unchanged); a `/url:` line (unchanged); `null` cap (unchanged); cap smaller than the
  marker. `find` window: match at the start, middle, end, in a name, and in a short line.
- **Toolserver (browser):** an action result contains `### Page` and no `Snapshot` link and writes no
  `page-*.yml`; `browser_find` gives refs that resolve and click; after a click that adds an element the old ref
  still works and a new `browser_find` shows the new element; after navigation the old ref fails with the
  "not found" error.
- **Gateway:** `browser_snapshot` and `browser_find` with no `filename` return a `job_id`; inline `text` only
  under `INLINE_LIMIT`; `index.json` has `url`, `text_cap` (and `query` / `target` / `depth` when given);
  `_read_out` reads `out/` when the home directory has execute but not read permission (temporary directory at
  `0o100`, `ports.home` patched); `grep_file`: context lines, `max_matches` cut and note, `/pat/i`, invalid
  regex error, binary refused, line trimming, a job of another owner is "file not found".
- **e2e** (`smoke_e2e.py` or `e2e_auth.py`): navigate, `browser_find`, click by ref, `browser_find` again,
  `grep_file` on the stored matches.

## Docs to update

There is no `docs/architecture/` or `docs/api/`.

- `docs/implemented-plans/file-outputs-as-jobs.md`: "Tools that return content inline keep doing that" is no longer
  true for the tools above (add See also).
- `docs/implemented-plans/python-browser-tool-server.md`: post-action snapshot removed; `find` / `snapshot`
  parameters and schema addition (add See also).
- `docs/plans/agentic-research-mcp.md`: tool table gains `grep_file`; blob layout / `index.json` keys when that
  plan's docs are consolidated.
- `docs/implemented-plans/index.md`: link this plan after implementation, and move this file to
  `docs/implemented-plans/jobs-for-all-tools.md`.
