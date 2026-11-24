# Plan: policy levels (admin, project, session) for media and external parsing

## Problem / Goal

Two session-scoped security choices exist or are about to:

- the [no-media policy](../implemented-plans/no-media-sessions.md) (`media`, `allow`, `deny`): chosen by the user in `open_session`, immutable, fail-closed;
- which external models may see files (`ocr`, `asr`, `caption`): today only the operator decides, per backend (`<BACKEND>_PARSE_OCR` ...), and nothing records per job why a feature was or was not used.

Goal: one mechanism for both. Three levels (admin env, project, session), each able to narrow what the level above allows, and a way to say "this is deliberately unset: the level
below must choose" (`ASK`), which is how media behaves today. Parse features default to "inherit, never ask", so nothing changes for a deployment until someone sets a level or `ASK`.

Out of scope (decided): content-type, size or volume filters for the parse features (the policy is a dict and can gain fields); chains longer than two profiles per feature (needs a
`cadmus-parser` change, `features` as ordered lists; here the list shape is kept and `MAX_CHAIN = 2` is the one constant to lift later); re-parsing or purging earlier results when a policy changes.

Prerequisite: [Plan: captioning](caption-feature.md) (the `caption` feature).

## Design

### Settings

| Setting | Value | Narrowing (what "lower" means) |
|---|---|---|
| `media` | bool | `true` -> `false` only |
| `deny` | list of MIME types / `type/*` | may only grow |
| `allow` | list of MIME types / `type/*` | may only shrink |
| `ocr`, `asr`, `caption` | ordered profile list, at most `MAX_CHAIN` (2): primary, then fallback; first success wins; `[]` = off | drop or reorder entries; never add one |

Domains (what any value must be inside): `media` any bool; `allow` / `deny` any MIME list; a feature any profiles of the deployment's table (`OCR_PROFILES`, `ASR_PROFILES`, `CAPTION_PROFILES`), at most `MAX_CHAIN`.

### Levels and `ASK`

| Level | Where | Set by |
|---|---|---|
| **Admin** | env at deploy, per backend, one variable per setting named like the policy: `<BACKEND>_POLICY_MEDIA`, `_POLICY_ALLOW`, `_POLICY_DENY`, `_POLICY_OCR`, `_POLICY_ASR`, `_POLICY_CAPTION` (format below) | the operator |
| **Project** | `projects/<proj>/access.json`, new key `policy: {<backend>: {<setting>: value | "ASK"}}` (gateway only) | anyone with the `admin` role in the project |
| **Session** | `open_session(...)` arguments for the current backend, until the session ends (re-opening resets it) | the user, on request |

At each level a setting holds a value or `ASK`.

- **`ASK` is outside the narrowing order.** It never restricts the level below. The bound for a level is the **nearest non-`ASK` value above it**; if every level above is `ASK`, the bound is the setting's whole domain.
- A level may set `ASK` over a value above it: the level below must then choose, still bounded by that value.
- A value above the bound is rejected (not clipped) at the tool call. A level never adds what the bound lacks.
- **Effective value** = the last value set going down the chain. A setting that is `ASK` after the session level is **unresolved**.
- If the operator later tightens a ceiling, stored lower-level values outside the new bound are dropped at resolution time (stored data is not rewritten); so are profile names that no longer exist.
- Changes apply to later jobs and later sessions only.

### Admin-level env variables

The names mirror the policy (`media`, `allow`, `deny`, `ocr`, `asr`, `caption`), and the formats mirror the project and session values:

- `<BACKEND>_POLICY_MEDIA`: `true`, `false` or `ASK`.
- `<BACKEND>_POLICY_ALLOW`, `_POLICY_DENY`: a comma-separated list of MIME types / `type/*`, or `ASK`.
- `<BACKEND>_POLICY_OCR`, `_POLICY_ASR`, `_POLICY_CAPTION`: a comma-separated ordered profile list, the chain (`lan` or `lan,remote`; at most `MAX_CHAIN`), or `ASK`. This replaces `<BACKEND>_PARSE_<FEATURE>` and `_FALLBACK`: the fallback is the second entry.
- **Unset or empty** is the setting's admin default, which keeps today's behaviour: `media` is `ASK`, `allow` and `deny` are empty, and a feature is **off** (`[]`), as an unset `<BACKEND>_PARSE_OCR` is today. Because stack files pass `${VAR:-}`, unset and empty mean the same thing.
  A feature the sessions may choose freely needs an explicit `ASK`.
- The old `<BACKEND>_PARSE_*` names are not read any more. `validate` fails with a message naming the replacement if one is still set, so a silent switch-off cannot happen.
- Every profile named must exist in the deployment's table, and every list is validated like a project or session value (same code).

### Media migrated onto it (behaviour unchanged at first)

- Out of the box the media setting is `ASK` at admin level and `allow` / `deny` are empty. With no `<BACKEND>_POLICY_*` variable the effective behaviour is today's: `open_session` is forced, `media` is chosen there, and the per-egress default (`false` on Tor and I2P, `true` on clear) is the *suggested* value the call confirms.
  (`open_session()` with no media argument still means "take the suggestion", as today.)
- New: an operator can fix `media` (admin) or a project admin can (project); `ASK` can be put back at any level.
- The enforcement layers A and D, the toolserver hook and `get_session`'s shape for media are unchanged; only where the `Policy` comes from changes.

### The gate: refuse to open while anything is unresolved

- Fail closed as today: every tool except `get_session` errors until `open_session` succeeds.
- `open_session` **refuses** while any setting that applies to the backend is `ASK` after the arguments are applied; the error names the settings (and their bounds). A session is never half-open.
- With no `ASK` on a backend (the default for parse features, not for media), `open_session` is still the first call (as today for media) but needs no arguments.
- Android's `open_session(project)` today only binds the project; it gains the same arguments for the parse features.

### Project tools

- `create_project(name, policy?)`: `policy` is `{backend: {setting: value | "ASK"}}`, validated against the admin level.
- `get_project(project)`: returns `policy` (stored) and, per backend and setting, the effective bound and its source (`admin` | `project`).
- `update_project(project, policy)`: **admin role only**; replaces the stored policy, same validation; can raise (up to the admin bound) as well as lower.

### Session tools

- `open_session(project?, media?, allow?, deny?, ocr?, asr?, caption?)`: each setting a value, validated against the bound from the levels above (the project's policy when `project` is bound, else the admin's). Returns the effective policy.
- `get_session()`: unchanged shape for media, plus the effective chain per feature, with `source` per setting (`admin` | `project` | `session`) and, before `open_session`, which settings are `ASK` and their bounds.

The session part is per MCP session and lives in the gateway next to `projects.bind`, not in a backend driver, so every backend shares one implementation.

### Enforcement of the parse features (one point)

`parse_client.parse()` takes the effective policy and passes only those profiles as `features` (`ocr`, `asr`, `asr-fallback`, `caption`, `caption-fallback`: chain entry 0 and 1). The wrapper hands a parser only the profiles its job names, so a denied
feature is unreachable from the sandbox. Callers (`jobs._parse_job` / `_store_and_parse`) pass the backend and the session's policy. The admin level comes from the `<BACKEND>_POLICY_*` variables.

### Uploads

`get_upload_link` runs in an opened session, so every `ASK` is resolved by then. The link **inherits that session's resolved chain per feature, signed into the token** (so it is fixed for the link's hour). The upload page shows it read-only and lets the uploader
**lower** it (drop entries; never add one), sent with the form. `upload_submit` passes it to `_parse_job`. A token with no policy (an old link) means unresolved: remote steps off, with a note.

### A denied or unresolved feature

The job still runs; local steps (Tesseract, metadata, everything not remote) work as before. A note on the job names what was off and the level that set it, for example `ocr: remote off (project policy)`, `caption: off (session policy)`,
`ocr: off (unresolved)`. A restrictive policy never fails a job and never loses data.

### Audit

- Per job, the effective chain per feature and its source go in the job's notes, also when nothing was denied (`ocr: lan (admin)`).
- What was actually sent is already recorded by the sidecars' trusted usage records (`get_usage`); unchanged.
- `get_session`, `open_session` and `get_project` show the effective policy.

### Known limits (documented, accepted)

- The session level is only as good as what the agent relays from the user (as for media today).
- The upload page's lowering is the uploader's choice; it cannot widen anything.
- The policy does not look at content; no per-file or per-type rule for the parse features.
- A project admin can raise to the admin bound at any time; the admin level is the operator's boundary.

## Implementation order

1. `sdk/.../gateway/policy.py` (new, stdlib): setting types (domain, narrowing rule), `bound(levels, setting)` (nearest non-`ASK`, else domain), `validate`, `resolve(admin, project, session)` -> `{setting: (value, source)}` or unresolved, `MAX_CHAIN`. Unit tests for every rule above (nearest non-`ASK`, all-`ASK` domain, `ASK` over a value, a tightened ceiling, a missing profile, each narrowing rule).
2. Admin level: parse `<BACKEND>_POLICY_*` (comma lists, `ASK`, unset = the setting's admin default), reject the old `<BACKEND>_PARSE_*` names with a message naming the replacement; the stack overlay's per-gateway `PARSE_<FEATURE>` extra env goes away (validated in `scripts/spider_hoarder.py::check_parse_env` / `validate`, using the same validation code as project and session values).
3. `projects.py`: `policy` in `access.json` (create, get, update with the admin check; an update invalidates the local 30 s cache entry); `create_project`, `get_project`, new `update_project` in `app.py` (`PROJECT_TOOLS`).
4. Session state in the gateway; `open_session` refuse-to-open rule; `get_session`.
5. Browser driver: build the media `Policy` from the resolved policy (enforcement unchanged); Android driver: `open_session` takes the parse arguments.
6. `parse_client.parse(key, base_url, backend, policy)`: `features` from the effective policy, policy notes; callers in `jobs.py` pass them.
7. Uploads: sign the chain into the token (`blobtoken`); page form with checkboxes; `upload_submit` passes the lowered chain.
8. `.env.example`: replace every `<BACKEND>_PARSE_*` example with the `<BACKEND>_POLICY_*` variables: one commented example per setting (`MEDIA`, `ALLOW`, `DENY`, `OCR`, `ASR`, `CAPTION`) showing the list format (`lan,remote`), `ASK`, and the unset default, plus a note on migrating old names. Keep the sidecar profile tables (`OCR_PROFILES` ...) as they are.
9. Tests; docs; move the plan.

## Files to change

| File | Change |
|---|---|
| `sdk/spider_hoarder_sdk/gateway/policy.py` | new: settings, bounds, validation, resolution |
| `sdk/spider_hoarder_sdk/gateway/projects.py` | `policy` in `access.json`: create, get, update |
| `sdk/spider_hoarder_sdk/gateway/app.py` | `create_project(policy?)`, `get_project` result, new `update_project`; upload link token and page |
| `sdk/spider_hoarder_sdk/gateway/sessions.py` (next to `projects.bind`) | per-MCP-session policy |
| `sdk/spider_hoarder_sdk/gateway/parse_client.py`, `jobs.py` | `features` from the effective policy; notes; pass backend and policy |
| `sdk/spider_hoarder_sdk/gateway/blobtoken.py` | carry the chain in the upload token |
| `backends/browser-sdk/.../driver.py`, `mediapolicy.py` | `open_session` / `get_session` via the resolver; `Policy` built from it |
| `backends/android-sdk/.../driver.py` | `open_session` takes the parse arguments; `get_session` |
| `.env.example` | the `<BACKEND>_POLICY_*` examples in place of `<BACKEND>_PARSE_*` (list format, `ASK`, defaults, migration note) |
| `scripts/spider_hoarder.py` | `<BACKEND>_POLICY_*` variables, validation, overlay extra env removed |
| `sdk/tests/`, `backends/*/tests`, `scripts/tests/` | tests above |

## Docs to update

- `docs/architecture/auth.md`, `jobs-and-storage.md`: `policy` in `access.json`, the three levels and `ASK`, where `parse()` applies it, the upload token.
- `docs/architecture/session-isolation.md`, `browser-toolserver.md`: the media policy's source is now the resolver (behaviour unchanged).
- `docs/api/tools.md`: `create_project`, `get_project`, `update_project`, `open_session`, `get_session`, `get_upload_link`.
- `docs/operations/deploy-and-configure.md`: the `<BACKEND>_POLICY_*` variables replace `<BACKEND>_PARSE_*` (format, `ASK`, defaults), and the migration of old `.env` files.
- Each with a "See also" link to `../implemented-plans/policy-levels.md`; `docs/implemented-plans/index.md` updated on completion.
