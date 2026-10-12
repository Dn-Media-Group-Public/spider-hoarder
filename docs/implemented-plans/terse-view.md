# Plan: Terse outline view of job content

## Problem / Goal

A job stores `original/<name>` and, for most types, `parsed/<name>.md`. Parsed
documents can be large, and an agent often only needs the structure and the
outbound references. Add a derived third tree, `terse/`, holding only the
headings, links and images of a document, so the agent can get a cheap outline
and decide what to page into with `get_file_text`.

## Design

### Storage

- A new tree `terse/<name>.md`, listed in the manifest like any other file.
  `list_files`, `get_file_text` and `get_file_url` work on it unchanged; there is
  no API change.
- **Source:**
  - every `parsed/<x>.md` gets `terse/<x>.md` (zip members included);
  - an original that is already Markdown (`.md`, `.markdown`) has no `parsed/`
    and gets `terse/<name>` computed from the original (no `.md.md`);
  - plain text and other files with no Markdown get no terse view.
- If the terse output is empty (no headings, links or images), nothing is written.
- Existing stored jobs are not backfilled (no prod data yet).

### `terse(markdown: str) -> str` (new, in `extract.py`)

Uses `markdown-it-py` (already installed as a `markitdown` dependency; pinned in
`requirements.txt`), default CommonMark preset with `linkify` and tables enabled,
walking the token stream.

Output is a heading tree with one bullet per link or image:

```
# Title
* [text](url)
* ![alt](src)
## Section
* [![alt](img)](url)
```

Rules:

- **Headings:** all ATX levels, emitted with their original `#` depth. Heading
  text is flattened to plain text (bold/italic/code markers dropped) but links
  inside a heading are kept as links, `[text](url)`, in the heading line and
  are **not** copied into the bullet list. Setext headings are also headings
  (markdown-it normalises them) and are emitted as ATX.
- **Bullets:** every link or image anywhere else, one per line, prefixed `* `.
  - Text before, between and after links is dropped.
  - Links/images in tables, list items and blockquotes count; the surrounding
    table/list/quote is dropped.
  - **Nesting is not undone:** `[![alt](img)](url)` stays one bullet, as written.
  - Link text styling is flattened (`[**bold** text](url)` -> `[bold text](url)`),
    but an image inside link text is kept as `![alt](src)`.
  - Bare autolinks (`<https://x>` and linkified URLs) are kept as `* <https://x>`.
  - Reference-style links are resolved to inline `[text](url)` form.
  - Link titles are dropped.
  - No deduplication.
- **Skipped entirely:** fenced and indented code blocks, inline code, raw HTML
  blocks, and all other prose.
- **Order:** document order; bullets before the first heading appear first.

### Inline text in job results

`_job_result(key, text)` gains the terse text. `text` in the result is:

1. the parsed text if under `INLINE_LIMIT`;
2. otherwise the terse text if that is under `INLINE_LIMIT`, plus
   `text_path` naming the terse file, so the agent knows it is the outline and
   that the full text is available via `get_file_text`;
3. otherwise omitted.

`text_path` is also set in case 1 (the parsed or original-md path), so the field
is always present when `text` is. For zips (several files) `text` is omitted as
today.

### Tool descriptions

- `get_file_text` / `list_files`: one sentence in the descriptions and the server
  `instructions`: "`terse/<name>.md` is an outline (headings, links, images) of
  `parsed/<name>.md`."

## Implementation order

1. `extract.py`: `terse()` with its token-stream walker.
2. `app.py` `_store_and_parse`: write `terse/` for every parsed file and for
   Markdown originals; pass terse text into `_job_result`; implement the inline
   fallback and `text_path`.
3. `requirements.txt`: pin `markdown-it-py`.
4. Descriptions and `instructions` string.
5. Tests.
6. Docs.

## Tests

- `terse()`: headings of all levels and setext; links/images split to bullets;
  nested image-in-link kept as one bullet; link-in-heading kept in heading and
  not duplicated; bare autolinks; reference links; table/list/quote links;
  fenced code with `# comment` and links skipped; prose dropped; no dedup;
  empty result.
- (HTML: terse is now computed from the markitdown `parsed/` output. See also: [Plan: save-page target](../implemented-plans/save-page-target.md))
- `_store_and_parse`: HTML/PDF/zip members get `terse/`; `.md` original gets
  `terse/` and no `parsed/`; `.txt` gets none; empty terse not written.
- Inline fallback: parsed small, parsed large + terse small (`text_path` is the
  terse file), both large (no `text`).

## Files to change

| File | Change |
|---|---|
| `mcp-research/extract.py` | `terse()` |
| `mcp-research/app.py` | write `terse/`, `_job_result` fallback and `text_path`, descriptions, `instructions` |
| `mcp-research/requirements.txt` | pin `markdown-it-py` |
| `mcp-research/test_gateway.py` | tests above |

## Docs to update

- `docs/plans/agentic-research-mcp.md`: blob layout (`terse/**`) and the
  `get_file_text` row (when that plan's docs are consolidated).
- `docs/implemented-plans/file-outputs-as-jobs.md`: the inline-text rule
  (add See also).
- `docs/implemented-plans/index.md`: link this plan after implementation.
