# Plan: projects, shared access and per-session scoped storage credentials

See also [Plan: parser wrapper](../implemented-plans/parser-wrapper.md) (consumes the scoped credentials and the key layout defined here),
[Jobs, storage and links](../architecture/jobs-and-storage.md) (today's `<owner>/<uuid>` layout and links), [Auth](../architecture/auth.md),
[Scaling](../architecture/scaling.md) (`rid`), [Session isolation](../architecture/session-isolation.md).

## Problem / Goal

Today a job lives at `<sanitised owner email>/<uuid>` and is reachable only by that email; there are no projects and no sharing. The authorisation
is entirely in the gateway, which holds one credential for the whole store. Weaknesses this plan removes:

- no sharing between users, no way to group work;
- the sanitising `re.sub(r"[^a-z0-9._@-]", "_", owner)` can map two emails to one prefix (cross-user access), and the signed link tokens carry the
  readable owner email to whoever receives the link;
- jobs are tied to one gateway replica (`job_id` = `<rid>.<uuid>`, per-replica disk), so a replica restart or reschedule loses them;
- any component that gets storage access gets all of it. The parser wrapper ([Plan: parser wrapper](../implemented-plans/parser-wrapper.md)) must be able to write only
  what it is parsing.

Goal: named **projects** with per-user roles, jobs keyed by project and session, **short-lived credentials limited to one project's read side and one
job's write side** for the components that need to touch the store themselves, and a shared bucket (GCS, S3, dev SeaweedFS) so any replica serves any job.
Non-goals: migrating existing jobs (none is carried over; old `<owner>/<uuid>` jobs become unreachable), per-token revocation, hardening the gateway's own
broad credential.

## Design

### Layout (one bucket)

```
projects/<proj>/access.json                       {name, created, users: {<email>: admin|write|read}}
projects/<proj>/data/<session>/<job>/…            original/ parsed/ simplified/ terse/ metadata/ index.json status.json
users/<user>/<proj>                               index for "my projects" (content: the level, a hint only)
```

`<proj>`, `<session>`, `<job>` are random UUIDs (validated against the UUID regex wherever one arrives, so no path component can contain `/` or `..`).
`<user>` is the **lowercased email, percent-encoded** to one path component (everything outside `[a-z0-9._@+-]` becomes `%XX`; an email always
contains `@`, so a component is never `.` or `..`). `access.json` sits next to `data/`, not inside it, so no session credential (below) can read or
change it.

### Roles

| Role | May |
|---|---|
| `read` | read every file of the project, list, `get_file_text`, `grep_file`, mint short-lived URLs (`get_file_url`) |
| `write` | `read` + create sessions and jobs in the project (browser/Android sessions, `ingest_page`, uploads, `get_upload_link`) |
| `admin` | `write` + add, remove and change users, delete the project |

Any authenticated user may create a project and becomes its admin. A project always keeps at least one admin (the gateway refuses to remove or
demote the last one). Names are not unique; the uuid is the identity. Sessions and jobs cannot be deleted in this plan, only the whole project.

### Authorisation (gateway, every call)

A job reference is **`<proj>/<session>/<job>`** (what the client sees and passes as `job_id`; the owner is never part of it). For each call the gateway
reads `projects/<proj>/access.json` (cached per replica for 30 s), looks up the caller's lowercased token email, and requires the role the tool needs.
No such project and no role give the same answer, "project not found". The gateway is the only component that reads or writes `access.json` and
`users/`. `rid` leaves jobs, `/blob/` and `/upload/` URLs (any replica serves them); it stays in the MCP session id and `/view/<rid>/…`, which are
tied to one replica's browser processes.

Tools: `create_project(name)`, `list_projects()`, `get_project(project)` (members only for admins), `share_project(project, email, role)`,
`unshare_project(project, email)`, `delete_project(project)`; the job-creating tools take a project (see Open decisions 1 and 2); the reading tools
take the three-part `job_id`.

**"My projects"**: list `users/<user>/`, then GET each project's `access.json` in parallel and keep those that still list the caller (the index is only
a hint; a stale entry grants nothing). Grant: write `access.json`, then the index entry. Revoke: write `access.json`, then delete the index entry.
`access.json` is a read-modify-write of one object by the gateway only; two admins saving at the same instant can lose one change (fsspec does not
expose conditional writes), which is accepted for such a rare, admin-only operation.

### Who holds which credential

| Component | Store access |
|---|---|
| Gateway | **broad** credential (service account / IAM user): everything, all authorisation is in its code. It builds every key through one function. |
| Browser and Android sessions | **none**. The gateway writes a session's `out/` files under `projects/<proj>/data/<session>/…` on its behalf and stages project files into `in/`. |
| Parser container's wrapper | **per job**, minted by the gateway: read `projects/<proj>/data/*`, write only `projects/<proj>/data/<session>/<job>/*`. Nothing else, `access.json` included. |
| Parser process, proxy, users' browsers, link recipients | none (links are served by the gateway, see below) |

The scoped credential is minted per parse job, handed to the wrapper in the POST (and reused by the gateway in the status GETs), lives in memory only
and expires on its own. Lifetime 900 s (the minimum for SeaweedFS and AWS STS; the 10-minute job limit plus the 12-minute poll window fit if the
gateway asks for 900 s and re-mints on a retry).

- **SeaweedFS / S3**: STS `AssumeRole` with an inline session policy, called by a non-admin gateway user through a role (research: tested on
  SeaweedFS 4.53, minting ≈ 0.7 ms each, the session policy intersects with the role's policy and cannot widen it; pin 4.31 or later, older releases
  mis-authorised prefixed lists). Template, `P` = `projects/<proj>/data`:
  `s3:GetObject` on `arn:aws:s3:::bkt/P/*`; `s3:PutObject s3:DeleteObject s3:AbortMultipartUpload s3:ListMultipartUploadParts` on
  `arn:aws:s3:::bkt/P/<session>/<job>/*`; `s3:ListBucket` on `arn:aws:s3:::bkt` with `StringLike s3:prefix` `P/*` (about 450 characters, limit 2048). Use
  `s3:` actions only (not the legacy `Read`/`Write` strings in session policies).
- **GCS**: a Credential Access Boundary (downscoped token), two rules: read on objects starting with `P/`, write on objects starting with `P/<session>/<job>/`,
  with the list prefix attribute condition for listing; at most 10 rules per boundary, Cloud Storage only; `gcsfs` takes the token as a credential
  object. (Documentation only so far: the lifetime and `gcsfs` usage are verified in step 1.)
- No revocation: tokens are stateless. Removing a user stops new credentials once the cached `access.json` expires (≤ 30 s); credentials and links
  already issued live to their expiry (≥ 15 min for credentials, 15 min for `/blob/` links). Rotating the STS signing key kills all credentials at once; with
  replicas they all need the same key.

### Links

`get_file_url(job_id, path)` needs `read`. It still returns `…/blob/<token>` (HMAC-signed, 15 min) streamed by the gateway from the bucket, so a
third party needs no account and no network path to the store. The token now carries `<proj>/<session>/<job>/<path>`: no email. `get_upload_link(project)`
needs `write`; the upload token (1 h) carries the new job's `<proj>/<session>/<job>`, an upload writes only into that job. The docs' statement that the token
is opaque is corrected: it is signed, not encrypted.

### Deleting a project (admin)

1. Read `access.json` for the member list. 2. Delete `access.json` (no further authorisation or minting). 3. Delete each member's `users/<user>/<proj>`.
4. End MCP sessions bound to the project. 5. Delete the prefix `projects/<proj>/` (SeaweedFS: the filer HTTP recursive delete, tested 3000 objects in ≈ 30 ms,
the filer port stays on an internal network and is called only by the gateway; elsewhere list plus batch `DeleteObjects`, where the **per-key `Errors` list
must be checked**, the status is 200 even for failures). Credentials minted before step 2 can still write until they expire, so a late parse can leave
orphan objects under the prefix. Nobody can reach them (no `access.json`); a second delete pass after the credential lifetime is open (Open decision 8).

### Dev: SeaweedFS in the stack

`docker-compose.dev.yml` gets a `seaweedfs` service: official image pinned to a tested tag (≥ 4.31, 4.53 verified), one container
(`server -s3 -s3.config=s3.json -s3.iam.config=iam.json -s3.iam.readOnly=false`), a data volume, S3 on the stack network, the **filer port not
published**. `iam.json`: the STS block (`tokenDuration`, `maxSessionLength`, `issuer`, a `signingKey` from `.env`) and default-deny policy; `s3.json`
only the bootstrap admin identity. A one-time idempotent setup (Open decision 5) creates the bucket, the non-admin gateway user with an access key, its
policy (`s3:*` on the bucket) and the role whose trust policy names that user; the trust principal account is `000000000000`, not the STS default.
`.env`: bucket, gateway key/secret, role ARN, signing key (placeholders refused by `deploy.sh`). The gateway reaches SeaweedFS directly
(`http://seaweedfs:8333`, `RESEARCH_BLOB_URL=s3://<bucket>` with the endpoint through fsspec's `FSSPEC_S3_*` configuration); the wrapper through the proxy, which gets the
dev exception from the parser-wrapper plan. `file:///data` stays for unit tests and single-replica setups without the parser container
(Open decision 4).

## Open decisions (suggested defaults, to confirm)

1. **Picking the project.** `open_session(project)` binds an MCP session to one project (needs `write`); every job-creating tool that can run without a
   bound session (`ingest_page`, `get_upload_link`) takes `project`, optional when the session is bound. Reading tools need no binding.
2. **`<session>` for tools without a browser/device session** (uploads, `ingest_page`): one fresh session uuid per MCP session, created lazily at its first job.
3. **Parser credential scope**: one job (`…/<session>/<job>/*`), narrower than the whole session.
4. **`file://` storage** remains for tests and single-replica deployments; with it there are no scoped credentials and the parser container needs an object store.
5. **Who initialises SeaweedFS** (bucket, IAM user, policy, role): a script `scripts/seaweedfs_init.py` the deploy runs once, or the gateway at start
   (idempotent, dev only).
6. **Gateway writes use its broad credential** through the single key-building function; scoped credentials are only for the wrapper.
7. **No session or job deletion tool** in this plan; only project delete.
8. **Post-delete second pass** after the credential lifetime, or accept unreachable orphans until a lifecycle rule or sweeper exists.
9. **Cache TTL** of `access.json` 30 s.

## Risks

- All authorisation is application code in the gateway holding the broad credential; a bug there crosses projects. The scoped credentials protect only
  against the wrapper.
- SeaweedFS STS is version-dependent (earlier prefixed-list and multipart bugs, one fixed in 4.31; open issues around session duration and principals); the pinned
  version is tested in step 1 and re-tested on upgrades.
- Access by email: a reassigned address inherits access; revocation latency above; single-file `access.json` lost-update race.
- Credentials live ≥ 15 min on SeaweedFS/S3 and cannot be revoked; a leaked wrapper credential is limited to one job's write side and a project's read side.
- The signing key is a single secret shared by all replicas.

## Implementation order

1. Verify on the pinned SeaweedFS, re-running the research matrix against exactly the policy template above (`data/` prefixes, `<session>/<job>` write
   scope): read/list of the project, write only in the job, multipart, denied lists with other prefixes (`projects/<proj>/` without `data/`, the bare uuid, a sibling),
   expiry, presigned URLs unaffected, `s3fs` with session credentials and `FSSPEC_S3_*`; on GCS (if available) the downscoped token with the list condition.
2. `storage.py`: keys built from `(proj, session, job)`; a per-call filesystem from given credentials instead of the module-level `BLOB_URL` only.
3. `gateway/projects.py` (email encoding, `access.json`, `users/` index, roles, cache, last-admin rule) and `gateway/creds.py` (STS / CAB / none); unit tests
   against a temp-dir store: role matrix, encoding edge cases (`+`, `/`, `%`, case), index/authority mismatch, last admin, concurrent grant.
4. `jobs.py`, `app.py` and the backend drivers: three-part `job_id`, `_key()` rewritten, project tools, `project` arguments, `rid` removed from jobs and `/blob/` / `/upload/`;
   nginx routes `/blob/` and `/upload/` without the sticky `rid`.
5. Links: tokens carry the job reference, upload token per new job; docs corrected.
6. Dev SeaweedFS service, init, `.env` variables, `deploy.sh` checks, `scripts/spider_hoarder.py validate`.
7. Project delete, including ending bound sessions and the `Errors` check; tests.
8. Docs; move the plan.

## Files to change

| File | Change |
|---|---|
| `sdk/spider_hoarder_sdk/content/storage.py` | project/session/job keys; per-call filesystem with credentials |
| `sdk/spider_hoarder_sdk/gateway/projects.py` (new), `gateway/creds.py` (new) | roles, `access.json`, `users/` index; STS/CAB minting |
| `sdk/spider_hoarder_sdk/gateway/jobs.py`, `gateway/app.py`, `gateway/blobtoken.py`, `gateway/ident.py` | three-part job id, `_key()`, project tools, tokens, `rid` out of jobs and links |
| `backends/*` drivers (`browser-sdk/.../files.py`, `pages.py`, android equivalents) | job creation with project/session |
| `nginx.conf.template` | `/blob/` and `/upload/` routed without `rid` |
| `docker-compose.dev.yml`, `.env.example`, `deploy.sh`, `scripts/spider_hoarder.py`, `scripts/seaweedfs_init.py` | SeaweedFS, variables, checks, init |
| `sdk/pyproject.toml`, gateway Dockerfiles | `s3fs` / `gcsfs` extras |
| `sdk/tests/*` | updated for the new keys; new authz, encoding and minting tests |

## Docs to update

- `docs/architecture/jobs-and-storage.md`: layout, job reference, access, links, storage backends and dev SeaweedFS (the "opaque token" wording corrected).
- `docs/architecture/auth.md`: roles and project authorisation.
- `docs/architecture/scaling.md`: `rid` no longer in jobs and links; shared store; `file://` single replica.
- `docs/architecture/session-isolation.md`: sessions hold no store credentials; the wrapper's scoped credential.
- `docs/architecture/overview.md`, `docs/operations/deploy-and-configure.md`: services, variables (`RESEARCH_BLOB_URL`, gateway key, role ARN, signing key), SeaweedFS.
- `docs/implemented-plans/index.md` and `docs/plans/index.md`; each updated section gets `See also: [Plan: project access model](../implemented-plans/project-access-model.md)`.

## Implementation notes (this supersedes the text above where they differ)

- All nine open decisions: the suggested defaults, except as noted. Android got its own `open_session(project)` (binding only), as it had no such tool.
- **No local or in-memory store, ever** (decision 4 reversed): `RESEARCH_BLOB_URL` is `s3://` or `gs://`, `deploy.sh` refuses anything else, the `/data` volumes are gone. The
  tests run against a real SeaweedFS that `scripts/test.sh` starts (`testing.fresh_store()` gives each test a fresh prefix).
- **Store settings are one JSON object**, `RESEARCH_BLOB_OPTIONS`, handed to fsspec as is (credentials, endpoint, ...). No `AWS_*`, `FSSPEC_S3_*` or `STS_*` settings, no role ARN;
  all backend overlays pass `RESEARCH_BLOB_URL` and `RESEARCH_BLOB_OPTIONS` through, whatever the store. SeaweedFS is an opt-in service of the stack
  (`SEAWEEDFS=1`, default with `--dev`; `docker-compose.seaweedfs.yml`), not a dev-only one: GCS is just as possible in dev and SeaweedFS in prod.
- **Credentials: a new role per job** instead of one role plus an inline session policy. With a broad role (`s3:*` on the bucket) the whole security boundary was the session
  policy; if it were missing or ignored the consumer would get the whole bucket, `access.json` included. Now the role's own policy is exactly the job's scope, the gateway assumes it, and
  the credential is plain fsspec options (`key`, `secret`, `token`; GCS: `token`). Measured on SeaweedFS 4.53: deleting the role kills the credential at once, so the plan's
  "no revocation" no longer holds on S3: `Scoped.release` ends it with the job, `revoke_project` with the project (this replica's), `sweep` (every 5 min) deletes roles a crashed gateway
  left behind. A SeaweedFS restart also ends credentials in flight, since roles live in memory (storeType `filer` did not change that); the consumer re-mints. `mint` raises on any failure and
  deletes a half-created role; it never falls back to the gateway's own credential. GCS stays a downscoped token (not revocable).
- **The gateway needs IAM rights** to create roles: on SeaweedFS its identity has the `Admin` action (one static identity in the generated `s3.json`, keys from `.env`, since IAM
  `CreateAccessKey` cannot produce chosen keys). On real AWS that is a wide privilege (a compromised gateway can create any role); an IAM permission boundary on what it may create is advisable.
  The bucket is created by `deploy.sh` with `weed shell` inside the container, so no port is published and nothing runs on the host.
- **Project delete** deletes the prefix with `fs.rm(recursive=True)` and then lists what is left (raising if any file remains), instead of the SeaweedFS filer HTTP delete or reading
  `DeleteObjects` `Errors` (s3fs drops them). No second pass (decision 8). SeaweedFS keeps an empty directory entry no S3 client sees as an object, so only files count.
- Insufficient role (a member with too low a role) answers "this needs the `<role>` role"; only non-members and missing projects get "project not found" (identical text).
- s3fs on a shared bucket: directory listings are never cached (`use_listings_cache=False`; checked: a second writer's entry stayed invisible otherwise), a credentialed filesystem skips fsspec's
  instance cache, and rewritten objects (`access.json`, `index.json`) are read with one plain GET (`open()` pins the ETag and raised `FileExpired` under a concurrent write; found by the
  concurrent-grants test). `delete_project` ends the bound MCP sessions only on the replica that handled it; other replicas' bindings stop within the 30 s cache window.
- Verified on SeaweedFS 4.53 (`sdk/tests/test_creds.py`, every `scripts/test.sh` run): read/list of the project, write/delete only in the job, multipart, denied lists and writes elsewhere
  (`access.json`, `users/`, siblings, the bucket root), presigned URLs, release / revoke / sweep, failed mint. **Verified since**: credential expiry (works until 890 s, refused at 910 s); GCS on a real bucket with a Credential Access Boundary token (`sdk/tests/test_creds_gcs.py`, run with `TEST_GCS_URL=gs://scratch-bucket`: reads only the project's data side, writes and multipart-uploads only inside its own job, denied everywhere else; not revocable, so the token simply expires); a full `./deploy.sh --dev` (SeaweedFS in the stack, bucket created, gateways on `s3://` + `RESEARCH_BLOB_OPTIONS`) and a dn.no scrape over MCP through nginx with real Keycloak tokens (`tests/live/e2e_dn.py`: `create_project`, `open_session(project)`, `browser_save_page`, `get_file_text` on the job). That run found two deploy-only bugs: a bare `boto3` in the `s3` extra made pip backtrack to `s3fs 0.4.2` (now `s3fs[boto3]>=2024.1`), and `weed shell` exits non-zero when the bucket already exists (a redeploy failed). **Not verified**: real AWS.
- `creds.mint` is not called by anything yet: the parser wrapper that consumes it is a separate plan.
- The two accepted `access.json` races are exercised in `sdk/tests/test_projects.py::test_concurrent_grants_keep_a_valid_access_json`.
