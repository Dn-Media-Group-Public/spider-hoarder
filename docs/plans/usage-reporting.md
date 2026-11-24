# Plan: usage reporting

## Problem / Goal

The model sidecars (OCR, ASR) spend money on behalf of jobs. The library records every upstream call, per job, in `status.json` (`usage`), but only when the
sidecar has an admin token, and nothing here collects it or shows it. Goal: every sidecar has an admin token, and a user can ask an MCP tool how many tokens
and audio seconds a job, or a whole project, used. We report what cadmus reports: no prices, no estimates.

## Design

### Admin tokens

- One admin token per deployed sidecar (`ocr`, `asr`), **always**, as environment variables like `WRAPPER_TOKEN` and `PROXY_TOKEN`: the library needs no change.
- `deploy.sh` generates `OCR_ADMIN_TOKEN` / `ASR_ADMIN_TOKEN` (`openssl rand -hex 32`) into `.build/admin-tokens.env` when missing (`.build/` holds secrets and is not
  committed, as for the SeaweedFS config) and sources it. They are not in `.env`; an operator may still set them there, and that wins. A restart or redeploy keeps them.
- `parse_env`: `admin_token` in each entry of `PARSE_OCR_TABLE`, `PARSE_ASR_TABLE`, `PARSE_ASR_FALLBACK_TABLE` (the wrapper uses it, the parser never gets it).
  `parse_overlay`: `OCR_ADMIN_TOKEN` in the `ocr-sidecar` (the library's overlay sets only `ASR_ADMIN_TOKEN`, from the environment, already). `check_parse_env`:
  the admin tokens differ from every other token.
- Trade-off against a Docker secret: visible in `docker inspect` of the service, as the other tokens are today. Swarm secrets are the upgrade if that matters, and
  would need `_FILE` variants in the library.

### MCP tool `get_usage`

`get_usage(project, job_id?)`, in the gateway beside `list_files`, so every backend has it. Needs the `read` role on the project (`projects.require(owner, proj, "read")`;
with `job_id`, `jobs._key(owner, job_id, "read")`, which checks the same).

- With `job_id`: reads `<job>/status.json` and returns `{job_id, status, calls: [...as cadmus wrote them], truncated, totals}`.
- Without: reads `status.json` of every job of the project (`projects/<proj>/data/*/*/status.json`) and returns `{project, jobs, jobs_with_usage, truncated_jobs, totals}`.
  No state is kept: it is summed on request.
- `totals` = one row per `(feature, profile, model, outcome, unit)`: `{feature, profile, model, outcome, unit, calls, input, output}`, summed. Units are not mixed
  (`tokens` and `audio_seconds` are separate rows). A job whose `usage.error` is set counts in `jobs_with_usage` as missing and its message is listed in `errors`.
- No prices, no currency, no time range, no per-user split.

Known ceiling: a project sum is one listing and one read per job. Fine for hundreds of jobs; if projects get large, a per-job `usage` summary in `index.json` or a
rolling tally is the upgrade, not needed now.

## Implementation order

1. `spider_hoarder.py`: `parse_env` entries, overlay, check; `deploy.sh`: generate and source the tokens.
2. `gateway/jobs.py`: `usage(owner, project, job_id)` (pure summing function plus the store reads); `gateway/app.py`: register the tool.
3. Tests: summing (mixed units, truncated, errored jobs, empty); role check (read ok, no role refused, another project's job refused); overlay and env; a parser-service
   test with a fake `usage` in `status.json`.
4. Docs.

## Files to change

| File | What changes |
|---|---|
| `scripts/spider_hoarder.py`, `deploy.sh` | admin tokens: generated, table entries, overlay, check |
| `sdk/spider_hoarder_sdk/gateway/jobs.py`, `app.py` | `get_usage` |
| `sdk/tests/`, `scripts/tests/` | tests above |

## Docs to update

`docs/architecture/jobs-and-storage.md` (usage in `status.json`, the tool), `docs/architecture/auth.md` (the tool's role), `docs/operations/deploy-and-configure.md`
(the admin tokens, rotation = delete `.build/admin-tokens.env`), `docs/architecture/overview.md` (tool list).

## Open questions

None open. A project-wide sum reads all jobs, including those from before the sidecars had admin tokens (no `usage`); they count as without usage.
