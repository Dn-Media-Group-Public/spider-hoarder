# Plan: Pluggable backends (`browser_*`, `android_*`)

Status: plan, not implemented. **Runs after the [Android hoarder](android-hoarder.md) plan is implemented and merged to master.** It refactors that
code too, so it refers to code by role and class name, never by file or line; the implementation session reads the tree to find them.
Builds on [Browser sessions](../implemented-plans/browser-sessions-plan.md), [I2P egress](../implemented-plans/i2p-egress.md) and
[Scale research containers](../implemented-plans/research-replicas.md).

## Problem / Goal

The set of backends (tor, i2p, clear, android-*) is hardcoded across the compose file, the nginx config, `deploy.sh`, `entrypoint.sh`, the
`EGRESS`/`MODE` branches in the gateway, the landing page and `.env`. Goals:

- A backend is a self-contained, pip-installable package (Python, compose overlay, Dockerfiles, helper services) registered through a
  setuptools entry point. Backends may live **out of tree**.
- A sysadmin chooses what runs with one `BACKENDS` list; disabled backends cost nothing (no images, services, networks, routes, hints).
- Shared code lives in `spider-hoarder-sdk`; code shared by only some backends lives in family packages.

Out of scope: changing tool surfaces, session isolation, auth, the jobs/blobs model, or the Swarm deployment model.

## Preconditions (assumed true when this plan starts)

The Android port is on master: an android gateway mode behind the existing `Pool` backend seam (a slot backend next to the session-daemon
backend), a wrapper service that creates slot containers, the slot image and its toolserver, a static android tool list, per-egress Tor
routing for android (Tor with transparent proxying, shared with browser Tor), a golden-snapshot bake and `/dev/kvm` check in `deploy.sh`,
and `ANDROID_*` env vars. If it differs, adapt the steps below to the actual roles; the design does not change.

## Design

### Decisions (already made)

1. **Enable list.** `BACKENDS` in `.env`: space-separated canonical names (`browser_clear browser_tor android_tor`). An unknown or
   not-installed name is a hard error. `BACKEND_PACKAGES` (also in `.env`) lists extra pip packages to install first (decision 6). `deploy.sh` runs `docker stack deploy --prune` every time, so removing a backend stops its
   services. Networks and volumes are not deleted automatically (documented).
2. **Naming.** Canonical id `browser_tor` (entry-point name, `BACKENDS`, package name). Hyphenated form (`_` to `-`) for the URL
   prefix, Swarm services and DNS. Matrix: `browser_{clear,tor,i2p}`, `android_{clear,tor}`. **Hard rename** of URLs:
   `/browser-tor/mcp` etc. (old `/tor/mcp` clients re-add the server).
3. **Registry.** One entry-point group (`spider_hoarder.backends`); each entry resolves to a **duck-typed, dependency-free descriptor**
   (stdlib only, no SDK base class, no heavy imports at module level). Members; all file paths are strings relative to the package:

   | Member | Meaning |
   |---|---|
   | `name` | canonical id |
   | `title`, `description` | LLM-readable text; feeds the "other servers" list in gateway instructions and the generated landing page |
   | `compose` | compose overlay (merged with `-c`) |
   | `builds` | ordered `(image tag, context dir)` pairs; deduped by tag, first wins (so a family base image builds once) |
   | `services` | services force-restarted after deploy |
   | `preflight` | optional shell script; host checks (e.g. `/dev/kvm`, x86_64); non-zero exit aborts with its stderr |
   | `post_build` | optional shell script; after images, before stack deploy (e.g. golden-snapshot bake) |
   | `env(environ)` | optional pure function returning derived variables (e.g. slot counts from replicas) |
   | `runtime` | `module:Object` import string for the driver, imported lazily inside the container only |

4. **Overlay contract.** The base `docker-compose.yml` keeps only nginx, oauth2-proxy and shared auth network/volumes. An overlay:
   - prefixes **every** service, network and volume with the hyphenated backend name; the gateway service is `<name>-gateway`;
   - adds its own session network(s) to the base `nginx` service (compose list-merge appends);
   - sets `BACKEND=<canonical name>` and `BACKENDS: ${BACKENDS}` in its gateway's environment;
   - uses per-backend env names (`<NAME>_REPLICAS`, e.g. `BROWSER_TOR_REPLICAS`, `ANDROID_TOR_SLOTS`).
   The deploy script validates the prefix rule and refuses a bad overlay.
5. **No helper sharing.** Each backend has its own helpers (Tor daemon, i2pd and its seed, android wrapper). Two Tor daemons if
   `browser_tor` and `android_tor` are both on; browser Tor no longer needs the network-admin privileges android's transparent proxying
   needs. Sharing is code (packages) and base images only.
6. **Deploy tooling.** `deploy.sh` owns the virtualenv at the well-known location `.venv/` in the repo root: it creates it with `virtualenv` (the tool, not the stdlib `venv` module) if
   missing and activates it. It then runs `pip install --no-deps` for the in-tree packages (`sdk/`, `backends/*`; installing all of them is harmless,
   `BACKENDS` decides what is enabled) and for every entry in `BACKEND_PACKAGES` from `.env` (space-separated pip requirement specs,
   including `git+ssh://...` URLs, for out-of-tree backends), each also `--no-deps`. This happens before `BACKENDS` is resolved, so a
   listed out-of-tree backend is registered by the time it is validated. A standalone program under `scripts/` (not part of any
   package) then uses stdlib `importlib.metadata` in that virtualenv. Order: load `.env`; validate `BACKENDS`;
   run each `preflight`; build images (deduped); run each `post_build`; merge `env()` results; generate the landing page; build the `-c`
   list; `docker stack deploy --prune`; force-restart `services`.
7. **nginx.** Location regexes capture `[a-z0-9-]+` and proxy to `tasks.<egress>-gateway`; no backend list in the config. Unknown or
   disabled prefixes get a 502, not a 404 (accepted). Generic android-driven settings (large upload bodies, redirect behaviour) stay in
   the base config. The landing page `/` is a **static file generated at deploy time** (`claude mcp add` lines plus each backend's title
   and description for enabled backends, from `PUBLIC_SCHEME`/`PUBLIC_HOST`), bind-mounted like the nginx template; the gateway's own
   landing route and hardcoded server tuple are deleted.
8. **Other-servers list.** Backend notes contain no sibling names. The gateway appends, with no lead-in label, one entry per other
   enabled backend (MCP server name `spider-hoarder-<hyphenated name>`, `title`, `description`), taken from `BACKENDS` and the
   descriptors' text.
9. **Gateway framework and driver.** `spider-hoarder-sdk` holds the framework: auth, jobs/blobs/uploads, file tools, `/authz` and `/start`
   live-view routes, the session registry and reaper with limits, and MCP plumbing with a dynamic tool list. It loads the driver named by
   `BACKEND`. The driver is a `Pool` backend (start/stop/reset) plus: `upstream_mcp(uid)` (URL, headers, client factory),
   `view_upstream(uid)`, bulk-output fetch and input push, tool tables (file tools, extra tools, static or proxied tool list),
   handling of backend-owned gateway tools, the instructions text, and limits/timeouts (per-user cap, idle, help, close). Optional extra
   routes. All `EGRESS`/`MODE` branches disappear in favour of this.
10. **Packages and layout:**
    ```
    docker-compose.yml, nginx.conf.template, deploy.sh   base: nginx, oauth2-proxy, shared auth network
    scripts/                      deploy tooling
    sdk/                          spider-hoarder-sdk: gateway framework, Pool, auth, jobs/blobs, content pipeline, common, shared test fixtures
    backends/
      browser-sdk/                spider-hoarder-browser-sdk: browser driver, session daemon, toolserver, session scripts, base image
      browser_clear/ browser_tor/ browser_i2p/
                                  each: pyproject + entry point, descriptor, overlay, thin Dockerfile, egress policy, own helpers
      android-sdk/                spider-hoarder-android-sdk: android driver, slot backend, wrapper, slot image, toolserver, tool list
      android_clear/ android_tor/ each: pyproject + entry point, descriptor, overlay, thin gateway Dockerfile, own Tor daemon (tor only)
    ```
    Out-of-tree backends depend on the SDK (and a family package if they want it) and register the same entry point. Descriptors find
    family base-image contexts via `importlib.resources`, so family package `__init__` files stay empty and light.
11. **Images.** Per backend a thin image on a **family base** (`FROM` the family base tag; installs only that backend's package). Browser
    base: Chromium, Xvfb, session daemon, SDK, browser-sdk. Android gateway image is light (SDK + backend, no Chromium); the slot image is
    its own base. Browser gateway and session server stay in one container (existing decision); android keeps gateway and slots separate.
12. **Browser egress hooks** (in `browser-sdk`, supplied by each `browser_*` backend as an egress-policy object) replace the old
    branches: SOCKS host/port layout, the session-uid firewall rules (run from Python instead of the shell `case`), extra session env and
    readiness probes, routing of the gateway's own downloads (proxy, host restriction, timeout), the media default, the egress note.
13. **Android specifics under this design.** One wrapper per backend (so two holders of the container socket if both are on), fully
    parametrized by env set in the backend's overlay: slot container names and DNS aliases, the label value (includes the backend name, so a
    wrapper never touches another's slots), network and snapshot volume names, the Tor daemon hostname. Snapshot bake becomes
    `post_build`; host checks become `preflight`; slot totals come from `env()`. The slot image's context is package-local (no temp-dir
    copy). `android_tor`'s own Tor daemon carries the transparent-proxy configuration and a per-backend subnet variable.

### Discarded options

| Option | Why discarded |
|---|---|
| One compose file with `replicas: 0` for disabled backends | Still builds images and creates networks/volumes; helpers have no off switch; dead routes. |
| Compose profiles | `docker stack deploy` ignores them. |
| Manifest + generator emitting compose and nginx | Extra layer between sysadmin and what deploys; overlays stay plain and greppable. |
| Backend list in nginx regexes | Not needed: URL part is used as a DNS name. |
| Shared helpers (one Tor daemon for several backends) with `requires` edges | Out-of-tree backends would need the helper's package; coupling and shared privileges. Each backend owns its helpers. |
| Deploy-side Python run inside the built image, or host glob without entry points | Overlays/Dockerfiles must be extracted from an image; two registries. |
| One shared gateway image with all backends pip-installed at build | Image content depends on `BACKENDS`; enabling one backend rebuilds every gateway. |
| Backends register into a bare MCP server, or each backend its own gateway app | Registry/reaper behaviour left to each backend; duplicated framework. |
| URL aliases for old `/tor` etc. | Decided: hard rename. |

## Implementation order

Each step leaves a working deploy.

1. Create the package layout (`sdk/`, `backends/*`), move code without behaviour change; all packages installable into one virtualenv;
   fix test paths and shared fixtures.
2. Define the descriptor and entry-point group; write `scripts/` (list, validate, prefix check, builds dedupe, hook runner, landing
   page, `-c` list, env merge); rewrite `deploy.sh` around it with `--prune`: create/activate `.venv/`, `pip install --no-deps` the
   in-tree packages and `BACKEND_PACKAGES`; add `BACKENDS` handling and unknown-name errors.
3. Split `docker-compose.yml` into base + one overlay per backend with the prefix rules, renamed helpers/networks/volumes, per-backend env
   names and `BACKENDS`/`BACKEND` env; update the dev compose overlay and `.env` example.
4. nginx: generic capture and `tasks.<egress>-gateway`; remove the gateway's landing route; static landing file mounted.
5. Extract the driver interface and framework into the SDK: `Pool` stays; replace all `MODE`/`EGRESS` branches by the driver; add the
   other-servers list; the gateway loads the driver from `BACKEND`.
6. `browser-sdk`: browser driver (session-daemon backend, toolserver proxying, save-page, ingest, fetch) and egress-policy hooks; firewall
   rules from Python; the three `browser_*` backends become thin packages with their own helpers and base-image layering.
7. `android-sdk` and `android_*`: android driver and slot backend; per-backend wrapper parametrization; per-backend Tor daemon;
   `preflight`/`post_build`/`env` scripts; package-local slot build context.
8. Remove leftover `EGRESS`/`MODE` code and dead env vars; update tests (descriptor/validation unit tests, deploy-script tests with
   fake entry points, driver-contract tests run against every driver, existing suites per package).
9. Verify: deploy with each single backend and with all; remove one from `BACKENDS` and confirm its services are pruned and its routes and
   hints vanish; confirm an out-of-tree dummy backend package deploys.

## Files to change

| File / area | What changes |
|---|---|
| `docker-compose.yml` | Reduced to base services; backend services move to overlays |
| `docker-compose.dev.yml`, `.env` example | Rebased on the new split; `BACKENDS`, per-backend variables |
| `deploy.sh`, `scripts/` (new) | `.venv/` creation and `--no-deps` installs (in-tree + `BACKEND_PACKAGES`); entry-point driven deploy; hooks; landing page; `--prune` |
| `nginx.conf.template` | Generic capture, `tasks.<egress>-gateway`, static landing, no backend list or network list |
| Gateway app and session registry | Split: framework to `sdk/`, driver pieces to family packages; `EGRESS`/`MODE` removed |
| Container entrypoint and firewall | Browser firewall moves into the browser egress hooks; android gateway needs no firewall step |
| `common` helpers (ports, media policy, caps) | Split between SDK and family packages |
| Top-level `tor/`, `i2pd/`, `i2pd-seed/`, android wrapper/slot/toolserver directories | Move into their backend or family package; helpers renamed with the backend prefix |
| Per-backend packages (new) | pyproject, descriptor, overlay, Dockerfile, hooks scripts |
| `tests/` | Redistributed per package; shared fixtures in `sdk/`; new descriptor, deploy-script and driver-contract tests |
| `README.md` | New install/enable instructions |

## Docs to update

- `docs/architecture/overview.md`: components, naming, code map (packages, layout); add See also link.
- `docs/architecture/scaling.md`: routing by generic prefix, per-backend env names.
- `docs/architecture/session-isolation.md`, `jobs-and-storage.md`, `auth.md`, `browser-toolserver.md`: package locations and the driver seam where they describe the gateway/session server.
- `docs/operations/deploy-and-configure.md`: `BACKENDS`, `BACKEND_PACKAGES`, the `.venv/` that `deploy.sh` manages, per-backend variables, `--prune`, preflight/post-build, installing out-of-tree backends, networks/volumes not auto-removed.
- New `docs/architecture/backend-plugins.md`: descriptor reference, overlay contract, driver interface, writing an out-of-tree backend.
- `docs/api/tools.md`: URL rename and the other-servers instructions text.
- `docs/implemented-plans/index.md` and `docs/plans/index.md`: move this plan on completion; update the android plan's links and status.
