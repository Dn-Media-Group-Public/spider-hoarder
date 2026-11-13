# Plan: OCR in the parse step (Tesseract first, remote multimodal model as fallback)

> **Amended by [Plan: parser wrapper](../implemented-plans/parser-wrapper.md) (implemented):** parsing runs in the parser service, so `ocr.py` lives in `parser/spider_hoarder_parser/` (the sidecar client is there already: `ocr.remote_ocr`), the hook is `parse.py` instead of `gateway/jobs.py::_parse_one`, and the remote step is the `ocr-sidecar` with named profiles selected per backend (`<BACKEND>_PARSE_OCR`, `OCR_PROFILES`) instead of `OCR_BASE_URL_<BACKEND>` variables. Tesseract and rasterising are still to do.

The code is written against any OpenAI-compatible `/v1/chat/completions` endpoint and can ship first with Tesseract only; no particular remote service is assumed.

## Problem / Goal

Images and scanned PDFs parse to nothing useful: markitdown gives EXIF/metadata for images and an empty (or near-empty) text layer for scanned PDFs, so the
agent sees a blank `parsed/<name>.md`. Goal: make `parsed/<path>.md` contain the text of those files, with a cheap local first pass and an escalation to a
multimodal model only when the local result is poor.

## Design

### Scope (to confirm)

Inputs: standalone image files (`image/png|jpeg|webp|gif|tiff|bmp`) and PDF pages with no usable text layer. Archive members get it for free (they flow through
`_store_one` → `_parse_one`). **Out of scope:** images embedded in saved HTML pages (they are not stored as files today), video, and any new tool: OCR is a
property of parsing, not a tool.

### Where it hooks in

`sdk/spider_hoarder_sdk/gateway/jobs.py::_parse_one`, in the non-HTML branch right after `extract.file_to_markdown`. A new module
`sdk/spider_hoarder_sdk/content/ocr.py` holds everything OCR; `_parse_one` only calls it:

```python
text = extract.file_to_markdown(raw, path)          # unchanged
text, how = ocr.maybe_ocr(raw, path, ctype, text)   # no-op unless image, or a PDF page lacking text
if how: notes.append(f"{path}: ocr {how}")          # "tesseract (conf 91)" | "<OCR_MODEL>" | "tesseract (conf 38), remote failed: Timeout"
```

Output is the same artefact as any parse (`parsed/<path>.md`, then `terse/`), so no storage/API change. The provenance goes into the job's `notes`
(never silently: OCR text can be wrong, and the agent should be able to tell Tesseract from a VLM).

### The hybrid

1. **Tesseract** (`tesseract` binary via `pytesseract`, `-l nor+eng`, configurable `OCR_LANGS`). Run `image_to_data` to get per-word confidence;
   accept when mean word confidence ≥ `OCR_MIN_CONF` (default 70) **and** at least `OCR_MIN_CHARS` (default 20) characters came out.
2. Otherwise **remote**, if `OCR_BASE_URL` is set: one chat completion with the image as a `data:` URL content part
   (`{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`), prompt `OCR_PROMPT` (default: "Transcribe all text in this image exactly,
   as Markdown. Do not add commentary."), `temperature=0`, model `OCR_MODEL`. Works with any OpenAI-compatible server; no
   provider-specific API used. Plain `httpx` (already a dependency), no `openai` package.
3. **Remote failure never fails the job:** on error/timeout the Tesseract text (if any) is kept and the note says the remote step failed. Timeout is
   `OCR_TIMEOUT` (default 120 s); the self-hosted model on Cloud Run may be cold (minutes), so the first call after idle can time out and fall back to
   Tesseract. A cold-start retry/async job is **not** designed here (open question below).
4. **The remote step is optional and off by default.** `OCR_BASE_URL` unset (or empty) = the Tesseract result is used as is, whatever its confidence; no
   remote code path runs, no token is minted, `google-auth` is not even imported. The note then reads `tesseract (conf 38)` so a weak result is visible.

A remote result replaces the Tesseract result outright (no merging).

### PDFs

markitdown's text layer first (unchanged). A page counts as scanned when its text layer has < `OCR_MIN_CHARS` characters; the PDF is then rasterised with
`pypdfium2` at `OCR_DPI` (default 200) for those pages only, OCR'd as above, and spliced into the Markdown in page order as `\n\n<!-- page N (ocr) -->\n`
+ text. Limit: `OCR_MAX_PAGES` (default 50 per file, rest noted: `"<path>: ocr stopped at page 50"`). Needs per-page text from the PDF:
use `pypdfium2`'s text extraction for the "is there a text layer" test so markitdown's whole-document output is not re-parsed (to verify at
implementation which of the two is simpler; the contract above is what matters).

### Safety and limits

- `PIL.Image.MAX_IMAGE_PIXELS` kept at Pillow's default (decompression-bomb guard); an oversize image is noted, not OCR'd. Remote images are downscaled so the
  long side is ≤ `OCR_MAX_SIDE` (default 2000 px) and sent as PNG/JPEG.
- OCR only runs on files already under `MAX_PARSE_BYTES`. Tesseract runs with a timeout (`OCR_TESSERACT_TIMEOUT`, default 60 s per page).
- **Privacy:** with a backend's `OCR_BASE_URL` set, that backend's image bytes leave the swarm for the endpoint. So the remote step is configured **per
  backend** (next section): e.g. clear-web backends may use a remote endpoint while `browser_tor`/`browser_i2p`/`android_tor`
  stay Tesseract-only, or point at a truly local OCR server on the LAN.

### Auth to the endpoint (three modes, chosen per backend)

The remote endpoint may need Google service-account auth rather than a static key, so that is a first-class mode, not an afterthought. `OCR_AUTH` selects (default when empty: `google` if `OCR_GOOGLE_CREDENTIALS_FILE` is set, else
`bearer` if `OCR_API_KEY` is set, else `none`; the VM-service-account case needs `OCR_AUTH=google` set explicitly):

| `OCR_AUTH` | Sends | Use for |
|---|---|---|
| `google` | `Authorization: Bearer <Google ID token>`, audience `OCR_TOKEN_AUDIENCE` (required in this mode) | an endpoint behind Google service-account auth |
| `bearer` | `Authorization: Bearer $OCR_API_KEY` | a local OCR server (vLLM/llama.cpp) or a pre-minted token |
| `none` | no header | a local server on a trusted LAN |

`google` credentials, in this order (`ocr._bearer()`, `google-auth` imported lazily only in this mode):
1. `OCR_GOOGLE_CREDENTIALS_FILE`: path to a service-account JSON key → `service_account.IDTokenCredentials.from_service_account_file(path, target_audience=aud)`.
   For hosts that are not on GCP. The key is mounted as a **swarm secret/config per backend** (file path in the env var, never the key itself in an env var or `.env`).
2. **Otherwise the service account of the environment, via `google.oauth2.id_token.fetch_id_token(Request(), aud)`**: on a **Google VM (GCE) this is the VM's
   attached service account, fetched from the metadata server, no key file and no secret at all** (same on Cloud Run/GKE). Also honours
   `GOOGLE_APPLICATION_CREDENTIALS` pointing at a service-account key. (Plain *user* credentials cannot mint ID tokens for a custom audience; a developer
   impersonates a service account instead.)

So on a GCE VM, `OCR_AUTH_<BACKEND>=google` alone is enough (it must be set explicitly: with no key file the mode is not inferred). Notes: the container must be able to reach
the metadata server (`metadata.google.internal` / 169.254.169.254; normally true from a swarm container on a GCE VM, to verify on the first deploy, in particular for
gateways on `internal` overlay networks, though the Tor gateway is also on `public`); and every container on the VM shares the **VM's one** service account, so
per-backend identities (e.g. to revoke one backend) are only possible with per-backend key files (mode 1).

The token is refreshed shortly before `exp` (about an hour) and once on a 401; a failure to mint a token counts as a remote failure (Tesseract text kept, note says
why), never a failed job. **With key files, one service account per backend** is possible, so each backend has its own identity at the endpoint.

### Per-backend remote configuration

Every backend already runs its own gateway service(s) with their own `environment:` in its overlay (`backends/<name>/<name>/docker-compose.yml`,
`BACKEND: <name>` is set there). The gateway code reads only plain `OCR_*` variables and knows nothing about backend names; the **overlay** decides what each
backend gets, from a per-backend deploy variable that defaults to empty:

```yaml
# backends/browser_tor/browser_tor/docker-compose.yml (gateway service environment)
OCR_BASE_URL: ${OCR_BASE_URL_BROWSER_TOR:-}
OCR_MODEL: ${OCR_MODEL_BROWSER_TOR:-}
OCR_AUTH: ${OCR_AUTH_BROWSER_TOR:-}                       # google | bearer | none (empty = inferred)
OCR_API_KEY: ${OCR_API_KEY_BROWSER_TOR:-}
OCR_TOKEN_AUDIENCE: ${OCR_TOKEN_AUDIENCE_BROWSER_TOR:-}
OCR_GOOGLE_CREDENTIALS_FILE: ${OCR_GOOGLE_CREDENTIALS_FILE_BROWSER_TOR:-}   # path of a mounted swarm secret
OCR_TIMEOUT: ${OCR_TIMEOUT_BROWSER_TOR:-120}
```

Same lines in each of the five overlays with their own suffix: `_BROWSER_CLEAR`, `_BROWSER_TOR`, `_BROWSER_I2P`, `_ANDROID_CLEAR`, `_ANDROID_TOR`. There is
**no global fallback** (a shared `OCR_BASE_URL` would defeat the per-backend opt-in); a new backend gets the lines in its overlay like any other setting, and
`docs/architecture/backend-plugins.md`'s "adding a backend" checklist gains one item for it. The `OCR_BASE_URL_*` values are set in `.env`/the deploy environment; none set
= Tesseract-only everywhere, the default. Auth per backend is described above; the secret holding a backend's service-account key is declared in that backend's overlay (`secrets:` on its gateway
service) only when it is used, so an unconfigured backend carries nothing.

Tesseract settings (`OCR_LANGS`, thresholds, DPI, page limits, prompt) are not privacy-relevant and stay global, with the same defaults for all backends.

### Configuration

| Env | Default | Meaning |
|---|---|---|
| `OCR_LANGS` | `nor+eng` | Tesseract languages (packs installed in the image) |
| `OCR_MIN_CONF` / `OCR_MIN_CHARS` | 70 / 20 | accept Tesseract below which remote is tried / page counts as unscanned |
| `OCR_BASE_URL` (per backend, see above) | unset | OpenAI-compatible base URL (ending in `/v1`); unset = Tesseract only |
| `OCR_MODEL` (per backend) | unset (required with URL; startup logs a warning if the URL is set without it, and remote stays off) | the model name the endpoint expects |
| `OCR_AUTH` (per backend) | inferred | `google` (service-account ID token), `bearer`, or `none` |
| `OCR_GOOGLE_CREDENTIALS_FILE` / `OCR_TOKEN_AUDIENCE` (per backend) | unset / unset (required in `google` mode) | service-account key path (unset = the VM's/environment's service account) / token audience |
| `OCR_API_KEY` (per backend) | unset | static bearer for `bearer` mode |
| `OCR_TIMEOUT` (per backend) | 120 | remote call timeout, seconds |
| `OCR_PROMPT`, `OCR_DPI`, `OCR_MAX_PAGES`, `OCR_MAX_SIDE` | see above | tuning (global) |

## Implementation order

1. Images: add `tesseract-ocr`, `tesseract-ocr-nor`, `tesseract-ocr-eng` (apt) to the images that run the gateway parse (the browser-sdk base image and the
   android-sdk image, check which images `import spider_hoarder_sdk.gateway`); `pytesseract`, `pypdfium2`, `Pillow`, `google-auth` to `sdk/pyproject.toml`.
2. `content/ocr.py`: `maybe_ocr`, Tesseract pass with confidence, remote chat call, `_bearer` (three auth modes), PDF per-page path; unit tests.
3. `gateway/jobs.py::_parse_one`: call `maybe_ocr`, write the note.
4. Each backend overlay's gateway environment: the `OCR_*` lines mapped from `OCR_*_<BACKEND>` (default empty); `.env.example`/deploy docs list the
   per-backend variables.
5. Verify against the real endpoint once an endpoint is available (Norwegian sample set: clean scan, phone photo, screenshot, handwriting); tune
   `OCR_MIN_CONF`.
6. Docs; move plan.

## Files to change

| File | Change |
|---|---|
| `sdk/spider_hoarder_sdk/content/ocr.py` | new: hybrid OCR, PDF page path, bearer |
| `sdk/spider_hoarder_sdk/gateway/jobs.py` | `_parse_one` calls `ocr.maybe_ocr`, adds the provenance note |
| `sdk/pyproject.toml` | `pytesseract`, `pypdfium2`, `Pillow`, `google-auth` |
| `backends/browser-sdk/.../image/Dockerfile`, `backends/android-sdk/.../image/Dockerfile` | `tesseract-ocr` + `nor`, `eng` language packs |
| `backends/{browser_clear,browser_tor,browser_i2p,android_clear,android_tor}/*/docker-compose.yml` | gateway env: `OCR_*` mapped from per-backend `OCR_*_<BACKEND>` (default empty) |
| `.env.example` (if present) | document the per-backend variables |
| `sdk/tests/test_ocr.py` | new: see below |

Tests (`sdk/tests/test_ocr.py`, no network): a generated PNG of text → Tesseract path (skipped if `tesseract` is absent); a noise image → low confidence →
remote called via `httpx.MockTransport` and its text wins; remote error → Tesseract text kept and note says so; `OCR_BASE_URL` unset or empty → never calls remote, never imports `google-auth`; `OCR_AUTH=google` with a fake credentials object (both the key-file path and the no-file/environment path, `fetch_id_token` patched) sends `Bearer <id token>` with the right audience and refreshes it after expiry/401, token-mint failure → Tesseract text kept and noted; `bearer`/`none` send the expected header (or none); low-confidence Tesseract text is kept with its confidence in the note; URL set without `OCR_MODEL` → remote off; PDF with one
text page and one image-only page → only the second is OCR'd, order preserved; `OCR_MAX_PAGES` stop is noted; oversize pixel image noted, job not failed.

## Docs to update

- `docs/architecture/jobs-and-storage.md`: parse rules gain "images and scanned PDF pages are OCR'd" with the hybrid, provenance notes, env vars, the privacy
  consequence of a backend's `OCR_BASE_URL`. `See also` this plan.
- `docs/architecture/backend-plugins.md`: per-backend `OCR_*_<BACKEND>` variables in the overlay and in the "adding a backend" checklist.
- `docs/api/tools.md`: wherever `parsed/` and `notes` are described, mention OCR notes.
- `docs/implemented-plans/index.md`: link the moved plan.

## Open questions (decide before implementing)

1. ~~Per-backend switch~~ decided: remote OCR is configured per backend, off unless set (see above).
2. Cold start of the self-hosted model (minutes) vs. a synchronous parse: accept "first call after idle falls back to Tesseract", or add a warm-up/background
   re-OCR job (a larger design)?
3. Embedded images in saved HTML: wanted later? (Would need the page's image resources stored as files first.)
