# Plan: `target` for `browser_save_page` / `ingest_page`, markitdown as the main HTML conversion

## Problem / Goal

HTML pages are converted with trafilatura (`extract.html_to_markdown`). Trafilatura
guesses which parts of a page are content, and drops the rest silently. Measured on
real pages:

- On a DN article, one body paragraph was dropped with no error. Cause (trafilatura
  2.2.0, `main_extractor.py`): `div` is only a candidate tag when the page's `<p>` text
  is below `min_extracted_size * 3` characters (750). This page had 1907, so a prose
  paragraph that the site had put in a `<div>` was discarded at
  `handle_other_elements`. `favor_recall` and `favor_precision` do not change this;
  only rewriting that one `<div>` to `<p>` did.
- On a 22-page news corpus, trafilatura missed reference body blocks on 20 pages
  (75 of 523 blocks; headlines and in-article factboxes are a large part of that,
  and the reference is biased towards container-style extraction, so treat the
  numbers as an order of magnitude).

We do not want site-specific rules, a fork of trafilatura, or a home-made extractor.
The principle instead: **once the client has chosen a container, everything in it is
content.** The client is better placed to choose than a heuristic: it can see the
page's accessibility tree and search it with `browser_find`.

Goal: `browser_save_page` and `ingest_page` take an optional `target` (the element to
convert). Conversion is done by markitdown, losslessly. Trafilatura still runs on the
same input and its output is kept as a secondary "simplified" file.

## Design

### Decisions

1. **`target` parameter** on `browser_save_page` and `ingest_page`, a string with the
   same semantics as on `browser_snapshot` / `browser_evaluate`: an exact element ref
   from a page snapshot, or a unique element selector. Resolution happens in the
   browser (upstream `browser_evaluate` already accepts `target`); nothing is
   resolved server-side.
   - `browser_save_page`: refs and selectors both work.
   - `ingest_page`: only selectors are meaningful, because the throwaway browser has
     taken no snapshot, so no refs exist (a ref gives the upstream error). The tool
     description says so.
   - No `target` = the whole page (`document.documentElement.outerHTML`, as today).
   - A `target` that matches nothing or more than one element: the upstream error is
     returned unchanged as a tool error; no job is created.
2. **markitdown converts the HTML** (`parsed/<name>.md`), whole page or the target's
   `outerHTML`. No filtering by us. The input is always UTF-8 (it comes from the
   browser as a string), so the converter is told so explicitly
   (`StreamInfo(extension=".html", charset="utf-8")`) instead of relying on charset
   detection.
3. **Trafilatura runs on the same HTML** with the arguments used today
   (`output_format="markdown"`, no other options) and its output is stored as
   `simplified/<name>.md`. Nothing is written if it returns empty. It is never returned
   inline (see "Assumptions").
4. **Terse is computed from the markitdown output** (`parsed/<name>.md`), not from
   `simplified/`. Unchanged for every other file type.
5. **Inline limits are unchanged.** `_job_result` keeps its rule: `parsed` text inline if
   under `INLINE_LIMIT` (50,000 chars), else the terse outline if that is under the
   limit, else nothing inline; the client pages with `get_file_text`.
6. **`index.json` gains two optional top-level keys**, written next to `files`:
   - `url`: for `browser_save_page` the page's `location.href` at save time; for
     `ingest_page` the `url` argument as given.
   - `target`: the string the client passed; absent when none was given.
   The manifest returned by `list_files` and `_job_result` carries them. Existing jobs
   have neither; readers treat them as absent.
7. **Guidance to the client** (tool descriptions and server instructions): call
   `browser_find` first to locate the main content (search for `article`, `main`, the
   headline, or a distinctive sentence; the result shows each ancestor's ref), then
   pass that ref as `target`. Proposed wording is under "Guidance text".

### Data flow

`browser_save_page(browser?, target?)`:

1. `sessions.page_html(bs, browser, target)` runs one `browser_evaluate` with the
   settle logic (scroll, wait for a quiet DOM) and `target`. The function takes the
   element as its argument and returns `{html, url}`: the element's `outerHTML` if a
   target was given, else the document's, plus `location.href`.
2. `_store_and_parse(key, "page.html", html, "text/html")` stores `original/page.html`
   (the HTML that was converted, i.e. the target's, not the whole page), converts
   it, writes `parsed/`, `simplified/` and `terse/`.
3. `storage.set_meta(job_id, url=…, target=…)`.

`ingest_page(url, target?)`: same, through `fetch_html` → `_navigate_read` →
`page_html(..., target)`; `url` = the argument. Non-HTML content is unchanged
(`fetch.download`, markitdown/other converters); `target` is ignored for it.

### Job layout for an HTML job

```
original/page.html      the converted HTML (whole page, or the target element)
parsed/page.html.md     markitdown output (inline text when small enough)
simplified/page.html.md trafilatura output, if non-empty
terse/page.html.md      outline of parsed/
index.json              {"files": [...], "url": "...", "target": "..."}
```

### Consequences accepted

- The default (no `target`) output is larger and noisier than before: on the DN test
  page the whole page through markitdown was about 18,000 characters against about
  2,100 from trafilatura, and the terse outline lists every teaser and footer heading.
  The inline limits still bound what is returned inline.
- `simplified/` has trafilatura's known behavior of dropping text silently. It is a
  convenience, not a faithful copy; the descriptions say so.
- The quality of the result now depends on the client choosing a good `target`. The
  guidance text is how we steer that.

### Assumptions (not explicitly decided; change before implementing if wrong)

- `simplified/` is never returned inline; it is reachable through `get_file_text`
  like any other job file.
- Plan B ("jobs and metadata for all tools") will generalise the metadata mechanism.
  This plan adds only the minimal `storage.set_meta` and the two keys it needs.
- The guidance ("`browser_find` the content element, pass its ref as `target`") relies on `browser_find` showing a
  match's ancestor chain with refs. It does now (bugfix, see "`browser_find` path and context" in
  [jobs-for-all-tools.md](jobs-for-all-tools.md)); the guidance can say so.

### Guidance text (proposed)

`browser_save_page` description: "Convert the page currently open in the browser to
Markdown and store it. Returns {job_id, files, text?}. With `target` (an element ref
from browser_snapshot or browser_find, or a unique selector) only that element is
converted; without it, the whole page, including navigation, teasers and footers.
To extract an article, first call browser_find (e.g. for `article`, `main`, or the
headline) to get the ref of the element that contains the content, then pass it as
`target`. `simplified/` holds an automatic main-content guess that may omit text."

`ingest_page`: same, adding that `target` must be a selector (no snapshot exists
here, so refs do not work).

Server instructions: replace the sentence about `terse/` with: "terse/<name>.md is an
outline (headings, links, images) of parsed/<name>.md; simplified/<name>.md is an
automatic main-content extraction that may omit text. Before browser_save_page, use
browser_find to locate the main content element and pass its ref as `target`."

## Implementation order

1. `storage.py`: `set_meta(job_id, **kw)` (merge into `index.json`, keep `files`).
2. `extract.py`: `html_to_markdown` → markitdown (utf-8); new `html_to_simplified`
   (the current trafilatura call).
3. `app.py` `_store_and_parse`: HTML branch writes `parsed/` (markitdown), `terse/`
   (from it), `simplified/` (if non-empty).
4. `sessions.py`: `SETTLE_JS` takes the element, returns `{html, url}`;
   `page_html(bs, browser, target)` returns `(html, url)`; `parse_eval_result`
   adapted; `fetch_html` / `_navigate_read` pass `target` through.
5. `app.py`: `_save_page` / `_ingest_page` take `target`, call `storage.set_meta`;
   tool schemas get `target`; descriptions and server instructions updated.
6. Tests (below). Run `test_gateway.py`; run the e2e with a live browser.
7. Docs and the plan move (below).

## Files to change

| File | Change |
|---|---|
| `mcp-research/storage.py` | `set_meta` |
| `mcp-research/extract.py` | `html_to_markdown` via markitdown; `html_to_simplified` |
| `mcp-research/sessions.py` | `SETTLE_JS` with element + `{html, url}`; `page_html`, `fetch_html`, `_navigate_read`, `parse_eval_result` |
| `mcp-research/app.py` | `_store_and_parse` HTML branch; `_save_page`, `_ingest_page`; `browser_save_page` / `ingest_page` schemas and descriptions; server `instructions` |
| `mcp-research/test_gateway.py` | `test_store_terse` and a new test (below) |
| `mcp-research/e2e_auth.py` | one `browser_save_page` call with a `target` |

`requirements.txt` needs no change (markitdown and trafilatura are already there).

### Tests

- `test_store_terse`: the HTML case now also expects `simplified/p.html.md`, and
  terse derived from `parsed/`.
- New: an HTML document with a prose paragraph in a `<div>` (own text plus a link)
  next to enough `<p>` text: the paragraph is in `parsed/`. (Not asserted for
  `simplified/`; that would pin a trafilatura quirk.)
- New: `set_meta` keeps `files` and survives a later `_update_manifest`; a job
  without the keys reads as before.
- e2e (needs a browser): `browser_save_page` with a `target` selector stores only that
  element's text and records `target` and `url` in `index.json`.

## Docs to update

There is no `docs/architecture/` or `docs/api/`; the relevant text is in plans.

- `docs/implemented-plans/terse-view.md`: terse source for HTML is now the markitdown
  output (add See also).
- `docs/implemented-plans/browser-sessions-plan.md`: the `browser_save_page` row
  ("runs trafilatura") (add See also).
- `docs/implemented-plans/file-outputs-as-jobs.md`: the inline-text rule and the
  "prefer `browser_save_page`" wording (add See also).
- `docs/plans/agentic-research-mcp.md`: blob layout (`simplified/**`, `index.json`
  `url`/`target`) when that plan's docs are consolidated.
- `docs/implemented-plans/index.md`: link this plan after implementation, and move this
  file to `docs/implemented-plans/save-page-target.md`.

## Out of scope (Plan B and later)

- Making every output-producing tool return a `job_id` (`browser_snapshot`,
  `browser_find`, evaluate, …) and the general metadata mechanism (`depth`, text cap).
- The text-node length cap on `browser_snapshot`.
- Searching inside stored job files.
- The deployed `filename` failure (`Permission denied` reading the session's `out/`).
- Any fix to trafilatura, and automatic container selection (the client chooses).
