# Pluggable backends

A **backend** is a self-contained, pip-installable Python package that brings everything one MCP server needs: its gateway
driver, its compose overlay, its Dockerfiles and its helper services. It registers itself through a setuptools entry point,
so backends can live **out of tree**. A sysadmin chooses what runs with one list, `BACKENDS` in `.env`; a disabled backend
costs nothing (no images, services, networks, routes or hints).

See also: [Plan: Pluggable backends](../implemented-plans/backend-plugins.md)

In tree: `browser_clear`, `browser_tor`, `browser_i2p` (family `browser`), `android_clear`, `android_tor` (family `android`).

## Naming

The canonical id is `browser_tor`. It is the entry-point name, the entry in `BACKENDS`, the package name and the value of
`BACKEND` in the container. The hyphenated form (`_` to `-`: `browser-tor`) is used wherever DNS or a URL is involved:

| Thing | Name |
|---|---|
| URL prefix | `/browser-tor/mcp`, `/browser-tor/view/...`, `/browser-tor/blob/...`, `/browser-tor/upload/...` |
| MCP server name | `spider-hoarder-browser-tor` |
| gateway service | `browser-tor-gateway` (nginx proxies to `tasks.browser-tor-gateway`) |
| every other service, network, volume | prefixed `browser-tor-` |
| replica variable | `BROWSER_TOR_REPLICAS` (canonical name in capitals) |

## Layout

```
docker-compose.yml, nginx.conf.template, deploy.sh   base: nginx, oauth2-proxy, shared auth network
scripts/spider_hoarder.py     deploy tooling (standalone, run by deploy.sh in .venv/)
sdk/                          spider-hoarder-sdk: gateway framework, Pool, auth, jobs/blobs, content pipeline, driver seam
backends/
  browser-sdk/                spider-hoarder-browser-sdk: BrowserDriver, egress hooks, session daemon, toolserver, base image
  browser_clear/ browser_tor/ browser_i2p/
                              each: pyproject + entry point, descriptor.py, runtime.py, docker-compose.yml, image/Dockerfile, egress.py, own helpers
  android-sdk/                spider-hoarder-android-sdk: AndroidDriver, slot backend, wrapper, slot image, tool list, hooks
  android_clear/ android_tor/ each: pyproject + entry point, descriptor.py, runtime.py, docker-compose.yml, image/Dockerfile, own Tor daemon (tor only)
```

Code shared by every backend lives in the SDK; code shared by one family lives in the family package. Descriptors find family
base-image contexts and hook scripts with `importlib.resources`, so family `__init__` files stay empty and light.

## The descriptor

The entry point group is `spider_hoarder.backends`; each entry resolves to a **duck-typed, dependency-free descriptor** (stdlib
only, no SDK base class, no heavy imports at module level: `deploy.sh` loads it in a virtualenv that has no gateway
dependencies). All file paths are strings relative to the package that holds the descriptor; an absolute path (a family package's,
from `importlib.resources`) is used as is.

```toml
[project.entry-points."spider_hoarder.backends"]
browser_tor = "browser_tor.descriptor:descriptor"
```

| Member | Meaning |
|---|---|
| `name` | canonical id; must equal the entry-point name |
| `title`, `description` | LLM-readable text; the landing page and the "other servers" entries in every other gateway's instructions |
| `compose` | compose overlay, merged with `-c` |
| `builds` | ordered `(image tag, context dir)` pairs; deduped by tag, first wins, so a family base image builds once. The context needs a `Dockerfile` |
| `services` | services force-restarted after deploy (prefixed names, they must exist in the overlay) |
| `preflight` | optional shell script; host checks (`/dev/kvm`, x86_64, required variables); non-zero exit aborts the deploy with its stderr |
| `post_build` | optional shell script; after the images are built, before the stack is deployed (the Android golden snapshot) |
| `env(environ)` | optional pure function returning derived variables as `str -> str` (Android slot totals from replicas) |
| `runtime` | `module:Object` import string of the driver; imported lazily, inside the container only |

Hooks run with `sh` and get the deploy environment plus `BACKEND` (canonical), `BACKEND_HYPHEN` and `STACK_NAME`.

### Images

Every backend has a thin image on a **family base**: `FROM` the family base tag, install only the backend's own package. The
browser base holds Chromium, Xvfb/VNC, the session daemon, the SDK and the browser-sdk. The Android gateway image is light (SDK and
android-sdk, no Chromium); the slot image is its own base and its context is package-local.

`deploy.sh` builds a wheel of every in-tree package and of every `BACKEND_PACKAGES` entry into `.build/wheels`, installs them
into `.venv/` and hands the directory to every `docker build` as the named context `wheels`. Dockerfiles take our packages
from there (`COPY --from=wheels / /tmp/wheels`, then `pip install`), third-party dependencies come from PyPI. Every
`ARG NAME` a Dockerfile declares is fed from the environment variable `NAME` when it is set (that is how `ANDROID_FDROID_URL`
reaches the slot image).

## The overlay contract

The base `docker-compose.yml` keeps only nginx, oauth2-proxy and the shared `public` / `auth` networks. A backend's overlay:

- prefixes **every** service, network and volume with the hyphenated backend name; the gateway service is `<name>-gateway`;
- adds its own session network(s) to the base `nginx` service (the compose list merge appends); that is the only thing an overlay
  may set on `nginx`;
- sets `BACKEND=<canonical name>`, `BACKENDS: ${BACKENDS}` and `BACKEND_SERVERS: ${BACKEND_SERVERS}` in its gateway's environment;
- uses per-backend variable names (`<NAME>_REPLICAS`, `ANDROID_TOR_SLOTS`, ...).

`scripts/spider_hoarder.py validate` checks the prefix rule and the gateway's environment and refuses a bad overlay.

Each backend owns its helpers: two Tor daemons run if `browser_tor` and `android_tor` are both enabled, and the browser daemon needs no
network-admin privileges. Sharing is code (packages) and base images only.

`BACKEND_SERVERS` is a JSON list `[{name, title, description}]` that the deploy tool builds from the enabled descriptors. A container
only has its own backend installed, so this is how a gateway learns the text of the others. The gateway appends, with no lead-in label,
one entry per other enabled backend to its instructions: `spider-hoarder-<hyphenated name> (<title>): <description>`. Backend notes
themselves never name a sibling.

## Deploy flow

`./deploy.sh` (see [deploy and configure](../operations/deploy-and-configure.md)):

1. load `.env`; create `.venv/` with `virtualenv` if missing; build wheels (`sdk/`, `backends/*`, `BACKEND_PACKAGES`) and `pip install --no-deps` them
2. `validate`: every name in `BACKENDS` is installed (unknown is a hard error), descriptors and overlays are well formed
3. `preflight` of each backend, then the `docker build` of every image (deduped)
4. `post_build` of each backend
5. `env`: merge every `env()` result, `BACKENDS` and `BACKEND_SERVERS` into the deploy environment
6. generate the landing page (`.build/landing.html`, a static file bind-mounted into nginx: the `claude mcp add` lines and each title and description) and the `-c` list
7. `docker stack deploy --prune`, then force-restart `nginx` and every descriptor's `services`

`--prune` removes the services of a backend that left `BACKENDS`. Its networks and volumes are not deleted (remove them by hand when
you are sure). nginx has no backend list: the location regexes capture `[a-z0-9-]+` and proxy to `tasks.<prefix>-gateway`, so an unknown
or disabled prefix answers 502.

## The driver

`spider-hoarder-sdk` holds the gateway framework: auth, jobs/blobs/uploads, the file tools, `/authz` and the live-view start route, the
session registry and reaper with its limits, and the MCP plumbing with a dynamic tool list. The driver named by `$BACKEND` (the
descriptor's `runtime`) supplies everything backend-specific. A driver subclasses `spider_hoarder_sdk.gateway.driver.Driver`, overrides what
differs, and must not import `sessions` or `app` at module level (they import the driver; use `driver.sessions`, a lazy proxy).

| Member | Meaning |
|---|---|
| `noun`, `idle_timeout`, `help_timeout`, `max_per_user`, `max_total`, `close_timeout` | wording and limits: per-user cap, idle and human-help timers, per-replica cap, how long closing may take |
| `instructions` | the backend's own note for the model |
| `make_pool()` | the `Pool` (backend start/stop/reset plus the uids it owns) |
| `prepare(bs)` | `(env, files)` for `pool.start`; may raise to refuse |
| `upstream_mcp(uid)` | URL, headers and client kwargs of the session's tool server |
| `after_initialize(cs, bs)` | after the upstream MCP handshake (browser: the media policy) |
| `view_upstream(uid, rid)` | `host:port` nginx proxies the live view to |
| `upstream_tools()`, `decorate_tools(tools)`, `hidden`, `extra_tools` | the dynamic tool list: static or probed, notes added, tools never exposed, the gateway's own tools |
| `file_tools`, `capped_tools`, `file_tool(bs, name, args)` | proxied tools whose output becomes a job; how that file is fetched |
| `known(name)` | reject an unknown tool before it books a session |
| `pre_session(name, args, ctx)`, `with_session(name, args, ctx, bs)` | backend-owned gateway tools: before any session exists / with one; return a result or `None` |
| `register(mcp)` | optional extra routes |

`ctx` carries `mcp_session`, `owner`, `sid`, `base` (the public URL prefix) and `rid` (this replica's id) for links in results.

The browser family builds on this with an **egress policy** (`backends/browser-sdk/.../egress.py`), one object per `browser_*` backend: the
SOCKS router host and port layout, the session-uid firewall rules (applied from Python at container start, `firewall.py`, `start.py`), extra
session environment and readiness probes, how the gateway's own downloads are routed (proxy, host restriction, timeout), the media default
and the egress note. The Android family is parametrized by environment set in each overlay: slot names and DNS aliases, the label
value, network and snapshot volume names, the Tor daemon's hostname.

## Writing an out-of-tree backend

1. Make a package (`backend_gopher`) that depends on `spider-hoarder-sdk` (and a family package if you build on one).
2. `descriptor.py`: a `SimpleNamespace` with the members above, stdlib only. Register it in `pyproject.toml` under the entry-point group.
3. `docker-compose.yml`: the overlay, obeying the contract (every service, network and volume `backend-gopher-*`, a `backend-gopher-gateway`
   with `BACKEND: backend_gopher`, `BACKENDS` and `BACKEND_SERVERS`; add its network to `nginx`).
4. `image/Dockerfile`: a thin image (`FROM` a family base, or your own) that installs the package from `COPY --from=wheels`.
5. `runtime.py`: `driver = YourDriver(...)`.
6. In `.env`: `BACKEND_PACKAGES="git+ssh://.../backend_gopher.git"` (and, if it is not already installed, its dependencies) and add
   `backend_gopher` to `BACKENDS`. `./deploy.sh` builds and installs it, validates the overlay and deploys it; its server answers on
   `/backend-gopher/mcp`.

`scripts/tests/` and `sdk/tests/test_driver_contract.py` show how to test a descriptor and a driver against the contract.
