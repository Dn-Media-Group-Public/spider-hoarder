# Plan: Build everything inside `docker build`

Status: plan, awaiting review. Supersedes the "wheels" build described in
[Pluggable backends](../implemented-plans/backend-plugins.md) and [Backend plugins](../architecture/backend-plugins.md).

## Problem / Goal

Every image today depends on a **host-side build step** that Docker knows nothing about: `deploy.sh` runs `pip wheel` on the deploy host
into `.build/wheels`, and the Dockerfiles take our packages from there (originally via `COPY --from=wheels` and the buildx-only
`docker build --build-context wheels=...`; as a stopgap, uncommitted, by copying `.build/wheels` into each context as `.wheels/`).

That breaks the one idiom every Docker user relies on: **a Dockerfile plus a context is the whole recipe for an image**.

- The image cannot be built without first running our script on a host with the right Python, pip and `.venv`.
  `docker build` alone, CI, a registry builder or `docker build <git-url>` all fail.
- The Dockerfiles are not self-describing: they `COPY` a directory that only exists after a script ran.
- It needed a buildx-only flag (`--build-context`), which prod's Docker 20.10 does not have, so **nothing deploys on prod**.
  The stopgap that works without buildx is a hack that mutates the build contexts (copy in, build, delete).
- The cache keys on the wheel files, which are rebuilt (new timestamps and hashes) on every deploy.
- Out-of-tree backends are forced through the same host build for no reason.

How it came about: the approved plan said `builds` are `(image tag, context dir)` pairs and nothing about wheels. The implementing agent
(Claude, as for all plans in this repo) added the wheel step on its own while implementing and recorded it only afterwards, as a "deviation
made while implementing" in the implemented plan. It never asked the human, although this is an architecture decision that
`CLAUDE.md` reserves for the user, and it broke the basic Docker idiom above. This plan reverses that deviation.

## Design

Decisions (agreed in the planning discussion):

1. **One build context: the repo root**, narrowed by a `.dockerignore`. Every in-tree image builds with
   `docker build -f <dockerfile> -t <ref> <repo root>`. Dockerfiles `COPY` what they need by repo-relative path
   (`COPY sdk/ /src/sdk/`, `COPY backends/browser-sdk/ /src/browser-sdk/`) and `RUN pip install` it in the image's virtualenv.
   Plain `docker build`, no buildx, no named contexts, no pre-built artifacts, nothing copied into the tree.
2. **The descriptor points at a Dockerfile.** `builds` becomes `(image tag, dockerfile)` pairs. The deploy tool loops over them, builds each
   (deduped by tag, first wins, as now) and pushes when `IMAGE_PREFIX` is set. `ARG`s a Dockerfile declares are still fed from the environment
   variable of the same name.
3. **Out-of-tree backends** stay what they are: a git repo, installable by pip URL, registering the descriptor as a setuptools entry point.
   Its descriptor lists its Dockerfile, and that Dockerfile `pip install`s the backend's git URL into the virtualenv inside the image it builds
   (`FROM` the family base image, `RUN /venv/bin/pip install --no-deps git+https://...@<ref>`).
4. **The host keeps a `.venv/` only to read descriptors** (entry points), as today. `deploy.sh` installs the packages into it from their
   source dirs and `BACKEND_PACKAGES` URLs with `pip install --no-deps` (descriptors are stdlib-only; the gateway dependencies live in the images, not on the host). No wheel directory exists any more. Running the tool in a throwaway
   container was discussed and is not part of this plan.

Concrete shape:

- `.dockerignore` at the repo root: `.git`, `.venv`, `.build`, `.env*`, `.claude`, `data`, `docs`, `tests`, `**/build`, `**/*.egg-info`,
  `**/__pycache__`, `*.md`. `.env` holds secrets and must never enter a context.
- Family base image (`browser-sdk/.../image/Dockerfile`): `COPY sdk/ backends/browser-sdk/` to `/src`, then
  `pip install /src/sdk /src/browser-sdk`. Third-party dependencies come from PyPI as today. To keep the dependency layer cached across
  source edits, copy only the `pyproject.toml` files first and install the dependencies, then copy the sources (optional, can follow).
- Backend image (`browser_clear`, `browser_tor`, `browser_i2p`, `android_*`): `FROM` the family base, `COPY backends/<name>/ /src/<name>/`,
  `pip install --no-deps /src/<name>`.
- Helper images (tor and i2p daemons, i2p router and seed, android slot and wrapper): same Dockerfiles, `COPY` paths get the package-relative
  prefix, e.g. `COPY backends/browser_tor/browser_tor/daemon/entrypoint.sh /entrypoint.sh`.
- `scripts/spider_hoarder.py`: `build_command` becomes `docker build -f <dockerfile> -t <ref> [--build-arg ...] <context>`; the wheel
  argument, `.wheels` copying and `dockerfile_args(ctx)` (now reads the named Dockerfile) are removed. Context is the repo root for in-tree
  backends and the Dockerfile's own package directory for out-of-tree ones, whose helper files live there.
- `oauth2-proxy` and `nginx` images keep their own small contexts (`build-image`), unchanged.

Behaviour change for out-of-tree backends: the descriptor member `builds` changes shape and their Dockerfiles can no longer
`COPY --from=wheels`; both are documented in the migration note below.

## Open points for review (recommendations, not decisions)

- **Pinning the out-of-tree URL.** Recommended: the Dockerfile declares `ARG BACKEND_PACKAGE=git+https://...@<tag>` so the pin lives next to
  the build, and `.env` can override it through the existing same-name-env-var feeding. The alternative, feeding `BACKEND_PACKAGES`, installs
  every backend into every image.
- **Private git URLs.** `git+ssh://` needs credentials during the build. Recommended: `DOCKER_BUILDKIT=1 docker build --ssh default` with
  `RUN --mount=type=ssh`; this is built into Docker 20.10 and does not need the buildx plugin. Public URLs need nothing.
- **Dependency-layer caching.** Optional second step, see above.

## Implementation order

1. `.dockerignore` at the repo root; remove `.wheels/` from `.gitignore`; discard the uncommitted `.wheels` stopgap (Dockerfiles,
   `build_one`, tests, docs lines).
2. Rewrite the in-tree Dockerfiles (family bases first, then backend images, then helper images) for a repo-root context.
3. Change `builds` to `(tag, dockerfile)` in the five descriptors and in `sdk/spider_hoarder_sdk/testing.py`; validate that the Dockerfile exists.
4. `scripts/spider_hoarder.py`: `build_plan`, `dockerfile_args`, `build_command`, `build_one`, `build`; drop the wheels argument.
5. `deploy.sh`: delete the `pip wheel` loop and `.build/wheels`; `pip install -q --no-deps ./sdk ./backends/*/ ${BACKEND_PACKAGES}` into `.venv/`;
   call `$TOOL build` without a path.
6. Tests: `build_command` and `build` tests for the new signature, a test that every in-tree Dockerfile only `COPY`s paths that exist
   under the repo root and none from `.dockerignore`.
7. Verify on a host with plain Docker 20.10 and no buildx: `./deploy.sh` builds every enabled backend, then `docker build -f <each> .`
   by hand works with no preparation.
8. Docs, implemented-plan move, index entries.

## Files to change

| File | Change |
|---|---|
| `.dockerignore` (new), `.gitignore` | context filter; drop `.wheels/` |
| `backends/*/**/image/Dockerfile`, `slot/`, `wrapper/`, `daemon/`, `router/`, `seed/` Dockerfiles | repo-root `COPY` paths, install from source |
| `backends/*/*/descriptor.py`, `sdk/spider_hoarder_sdk/testing.py` | `builds=[(tag, dockerfile)]`, no `_family(...)` context dirs |
| `scripts/spider_hoarder.py` | build functions as above |
| `scripts/tests/test_spider_hoarder.py` | new build tests and the Dockerfile `COPY` check |
| `deploy.sh` | no wheel step; `pip install --no-deps` into `.venv/` |

## Docs to update

- `docs/architecture/backend-plugins.md`: the descriptor table (`builds`), the Dockerfile contract, the "Build" and deploy-step text, the
  out-of-tree guide. Add "See also" to this plan.
- `docs/architecture/overview.md`: repo layout and the deploy-tool line.
- `docs/operations/deploy-and-configure.md`: step 4 (builds), prerequisites (plain Docker; no buildx needed).
- `docs/implemented-plans/backend-plugins.md`: replace the "deviation" sentence with a pointer to this plan.
- `docs/plans/index.md` and, on completion, `docs/implemented-plans/index.md`.

## Migration note for out-of-tree backends

Change `builds` entries from `(tag, context_dir)` to `(tag, dockerfile)`. Replace `COPY --from=wheels / /tmp/wheels` and its `pip install`
with `RUN /venv/bin/pip install --no-deps <your git URL>@<ref>`. Keep helper-image `COPY`s relative to your package directory.
