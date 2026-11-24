# Plan: enable captioning (mirrors OCR and ASR)

## Problem / Goal

The `cadmus-parser` library ships a `caption-sidecar` (a vision model describing images and video keyframes) and the `caption` / `caption-fallback`
features, and its overlay generator already takes `caption=True`. Our wiring knows only `ocr` and `asr`, so captioning can never be turned on.
Goal: the same operator experience as ASR: a `CAPTION_PROFILES` table, a per-backend primary and optional fallback profile, nothing sent unless a backend names a profile.

No new decisions: this copies the ASR shape (primary + fallback, `<KIND>-sidecar` on the same internal network pattern, generated admin token for usage reporting).

## Design

- **Config** (`.env`): `CAPTION_PROFILES` (JSON `{name: {token, base_url, model, auth, api_key?, prompt?, max_output_tokens?}}`), per backend
  `<BACKEND>_PARSE_CAPTION` and `<BACKEND>_PARSE_CAPTION_FALLBACK` (empty = never). Also `CAPTION_TIMEOUT`, `CAPTION_MAX_BODY`, `CAPTION_MAX_CONCURRENT`
  (read by the library's overlay, defaults 120 / 32 MiB / 4).
- **Privacy default unchanged:** a backend sends frames and images to a caption profile only when it names one. The sidecar is deployed only when
  `CAPTION_PROFILES` is set.
- **Wiring** in `scripts/spider_hoarder.py`: `SIDECARS = ("ocr", "asr", "caption")`, `FALLBACKS` gains `"caption": "caption-fallback"`;
  `parse_env` adds `PARSE_CAPTION_TABLE` and `PARSE_CAPTION_FALLBACK_TABLE` (same table, as for ASR); `parse_overlay` passes `caption=bool(profiles_of(environ, "caption"))`
  and adds `CAPTION` / `CAPTION_FALLBACK` to the gateway's `PARSE_*` extra env. `check_parse_env`, `parse_services` and `build_parse` already loop over `SIDECARS`
  and `FALLBACKS`, so validation (profile completeness, unique tokens, admin token distinct, backend names an existing profile) and image build come for free.
- **Client** (`sdk/.../gateway/parse_client.py`): add `("caption", "PARSE_CAPTION")` and `("caption-fallback", "PARSE_CAPTION_FALLBACK")` to the features tuple.
- **Usage:** `deploy.sh` generates `CAPTION_ADMIN_TOKEN` alongside the other two, so `get_usage` covers caption calls (unit `tokens`, from the sidecar).
- **Scripts test list:** add `$LIB/caption-sidecar` to `areas` in `scripts/test.sh` (the library's tests, as for ocr and asr).

## Implementation order

1. `scripts/spider_hoarder.py`: `SIDECARS`, `FALLBACKS`, `parse_env`, `parse_overlay` (and the docstrings that list ocr/asr).
2. `sdk/spider_hoarder_sdk/gateway/parse_client.py`: the two feature mappings.
3. `deploy.sh`: add `CAPTION_ADMIN_TOKEN` to the generated-token loop. `scripts/test.sh`: add the caption-sidecar test area.
4. Tests: extend `scripts/tests/test_spider_hoarder.py` (an ASR-style case: tables, overlay contains `caption-sidecar` and `PARSE_CAPTION*` env, `parse_services`, unknown
   profile rejected, admin token in the table) and `sdk/tests/test_parse_service.py` (features contain `caption` only when `PARSE_CAPTION` is set).
5. `.env.example` block and docs.

## Files to change

| File | Change |
|---|---|
| `scripts/spider_hoarder.py` | caption as a third sidecar kind with fallback; tables and overlay |
| `sdk/spider_hoarder_sdk/gateway/parse_client.py` | `caption`, `caption-fallback` features from `PARSE_CAPTION`, `PARSE_CAPTION_FALLBACK` |
| `deploy.sh` | generate `CAPTION_ADMIN_TOKEN` |
| `scripts/test.sh` | run the caption-sidecar tests |
| `.env.example` | commented `CAPTION_PROFILES`, `<BACKEND>_PARSE_CAPTION[_FALLBACK]`, `CAPTION_TIMEOUT` |
| `scripts/tests/test_spider_hoarder.py`, `sdk/tests/test_parse_service.py` | tests as above |

## Docs to update

- `docs/operations/deploy-and-configure.md`: the config table (`CAPTION_PROFILES`, `<BACKEND>_PARSE_CAPTION[_FALLBACK]`, `CAPTION_ADMIN_TOKEN`), with a note that captions send images and
  video keyframes (scaled to 1536 px) to the profile's endpoint.
- `docs/architecture/jobs-and-storage.md`: the `features` list at the parse step (add `caption`, `caption-fallback`).
- `docs/implemented-plans/index.md` (when implemented).
