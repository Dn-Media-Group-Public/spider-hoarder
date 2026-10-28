# Plan: Restructure `mcp-research/` into packages, move tests to `tests/`

## Problem / Goal

`mcp-research/` is one flat directory mixing four things that run in different places and at different privilege levels
(gateway, sessiond, per-session processes, toolserver), plus shared helpers and 14 test files. Imports are flat (`import ports`),
`toolserver/mediapolicy.py` is imported by the gateway (a dependency on in-session code), and tests live next to the code
they test instead of in `tests/`.

Goal: directories that say what runs where, explicit shared code, one installable Python distribution, tests in `tests/`.
No behaviour change.

## Design

### Vocabulary (use in code, docs and comments)

| Name | Meaning |
|---|---|
| **Gateway** | The shared MCP server in each `research-<egress>` container (uid 1000). |
| **Content pipeline** | extract / store / fetch of bytes -> Markdown; part of the gateway. |
| **Sessiond** | The only root process; owns the uid pool; starts/stops sessions. |
| **Session** | One uid + process group (Xvfb, x11vnc, websockify, toolserver, Chromium, egress filter). Not a Docker container. |
| **Toolserver** | The Python MCP browser server inside a session. |
| **Session server** | Umbrella for everything that makes sessions happen on a container: sessiond, session, toolserver. |
| **Common** | Code imported by both the gateway and session server. |

Tor and I2P have no Python of their own: they are the `tor/`, `i2pd/`, `i2pd-seed/` containers. Per-egress behaviour in the
research container lives in `common/ports.py` (SOCKS host/port layout), `gateway/content/netguard.py` (SSRF guard, clear only) and
`session_server/session/filter_proxy.py` + `common/cmp_rules.py` (consent-vendor blocklist).

### Layout

One distribution, installed into the image's `/venv` with `pip install .` from a single `mcp-research/setup.py`. Top-level package
`mcp_research` (a bare `common` package would collide in site-packages) with `__init__.py` in every directory:

```
mcp-research/
  setup.py, requirements.txt, Dockerfile, entrypoint.sh, .dockerignore
  mcp_research/
    common/            ports.py  cmp_rules.py  cmp_blocklist.txt(build artefact)  mediapolicy.py  textcap.py
    gateway/           app.py  auth.py  sessions.py  pool.py  blobtoken.py
      content/         extract.py  storage.py  fetch.py  netguard.py
    session_server/
      sessiond/        sessiond.py (+ __main__)
      session/         session-group.sh  filter_proxy.py
      toolserver/      server.py tools.py browsers.py __main__.py  consent.js  tools.json  tools.json.upstream
tests/
  common/              test_mediapolicy.py  (+ cmp_rules / textcap checks split out of test_gateway.py if cheap)
  gateway/             test_gateway.py  test_replicas.py  test_pool.py  test_security.py
  session_server/      test_toolserver.py
  egress/              test_i2p.py
  live/                smoke_e2e.py  e2e_auth.py  test_sessiond.py   (need the container / root; not run by plain `pytest tests`)
```

### Decisions

- Imports become absolute: `from mcp_research.common import ports`. The in-package relative imports in `toolserver/` stay relative.
- `setup.py` uses `find_packages()` and `package_data` for `*.sh`, `*.js`, `*.json`, `*.json.upstream`, `cmp_blocklist.txt`.
  `requirements.txt` stays as the pinned list (Docker layer caching: deps install before the source is copied); `setup.py`
  declares no `install_requires`.
- Non-Python files are located relative to their module (`__file__`), not `/app`: `SESSION_GROUP` default, `consent.js`
  `initScript`, `tools.json` paths. The `..` in `toolserver/server.py` goes away (files sit beside it).
- Process entry points: `python -m mcp_research.session_server.sessiond`, `python -m mcp_research.session_server.session.filter_proxy`,
  `python -m mcp_research.session_server.toolserver`, `uvicorn mcp_research.gateway.app:app`.
- Dockerfile fetches the CMP blocklist to a fixed path and sets `CMP_BLOCKLIST_PATH` to it (a non-editable install puts the
  package in site-packages, so the build-time file must not depend on the package dir). `cmp_rules.py` default stays the package dir for dev.
- `.dockerignore` no longer needs `test_*.py` (tests are outside the build context).
- `pytest` runs from the repo root against `pip install -e mcp-research`; no `sys.path` tricks.

## Implementation order

1. Create the directory tree with `__init__.py` files; `git mv` every file per the table below (no content edits).
2. Add `setup.py`.
3. Rewrite imports (gateway, sessiond, toolserver, tests); fix `__file__`-relative resource paths and the `cwd=`/`-c "import ports"`
   subprocess snippets in `test_replicas.py` and `test_i2p.py`.
4. Update `Dockerfile` (install via `pip install .`, blocklist path, chmod of `session-group.sh` at its new path),
   `entrypoint.sh` (sessiond and uvicorn commands) and `session-group.sh` (filter_proxy, toolserver commands, `initScript` path).
5. Update `docker-compose*.yml`/`deploy.sh` only if they reference moved paths (expected: no, build context is unchanged).
6. Run `pytest tests` (host-runnable ones), then build the image and run `tests/live/` per the README.
7. Update docs (organization, naming, no "the agent"); move this plan to `docs/implemented-plans/`.

## Files to change

| File (current) | New location / change |
|---|---|
| `app.py`, `auth.py`, `sessions.py`, `pool.py`, `blobtoken.py` | `mcp_research/gateway/` |
| `extract.py`, `storage.py`, `fetch.py`, `netguard.py` | `mcp_research/gateway/content/` |
| `ports.py`, `cmp_rules.py`, `cmp_blocklist.txt` | `mcp_research/common/` |
| `toolserver/mediapolicy.py`, `toolserver/textcap.py` | `mcp_research/common/` (toolserver then imports them from common) |
| `sessiond.py` | `mcp_research/session_server/sessiond/sessiond.py` (+ `__main__.py`) |
| `session-group.sh`, `filter_proxy.py` | `mcp_research/session_server/session/` |
| `toolserver/*`, `consent.js`, `tools.json`, `tools.json.upstream` | `mcp_research/session_server/toolserver/` |
| `test_gateway.py`, `test_replicas.py`, `test_pool.py`, `test_security.py` | `tests/gateway/` |
| `test_toolserver.py` | `tests/session_server/` |
| `test_mediapolicy.py` | `tests/common/` |
| `test_i2p.py` | `tests/egress/` |
| `smoke_e2e.py`, `e2e_auth.py`, `test_sessiond.py` | `tests/live/` |
| `Dockerfile`, `entrypoint.sh`, `.dockerignore` | stay; edited as in step 4 |
| (new) `setup.py`, `__init__.py` files, `tests/conftest.py` only if fixtures need sharing | new |

## Docs to update

Every doc touched must be brought in line with **both** the new directory organization **and** the new naming (vocabulary table
above), not only have its paths patched: reread each affected section and rewrite old terms ("the server", "the container" for a
session, "tool server" vs "toolserver", etc.) to the agreed names.

**Never write "the agent"** in docs, code comments, docstrings or error text: it is unclear which agent is meant. Name the actor:
**the MCP client** (Claude or whatever drives the tools over MCP), **the user** (the human using the live view / answering
`open_session`), or a component from the vocabulary table (gateway, sessiond, toolserver). Occurrences to fix (grep `\bagent\b`
across `README.md`, `docs/architecture/`, `docs/operations/`, `docs/api/` and all moved Python files): `README.md`,
`architecture/overview.md`, `jobs-and-storage.md`, `session-isolation.md`, `browser-toolserver.md`, `gateway/app.py`,
`session_server/toolserver/server.py`, `session_server/toolserver/tools.py`, `tests/gateway/test_gateway.py`. Historical
`docs/implemented-plans/` stay as written.

- `docs/architecture/overview.md`: component table `Source` column, the "Code map" section, vocabulary table above (new "Naming" section).
- `docs/architecture/browser-toolserver.md`: toolserver path.
- `docs/architecture/session-isolation.md`, `jobs-and-storage.md`, `auth.md`: file path references to moved modules.
- `docs/operations/deploy-and-configure.md`: "Tests (`mcp-research/`)" section -> `tests/`, new commands.
- `README.md`: test/run commands (`cd mcp-research`, `test_sessiond.py` bind-mount).
- `docs/implemented-plans/index.md` and each updated section get a "See also" link to this plan.
- Older implemented plans keep their original paths (historical); no edits.

## Risks

- Large mechanical diff; do it as `git mv` first so history follows, then edit imports.
- `sessiond` has strict path/permission assumptions (root-owned launcher, `session-group.sh` executable by session uids):
  the installed package dir must stay world-readable/executable (`chmod -R a+rX`) and the live tests in `tests/live/` are the gate.
- `test_toolserver.py` needs Playwright Chromium; unchanged requirement.
