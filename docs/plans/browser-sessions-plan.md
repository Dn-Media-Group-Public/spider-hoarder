# Browser sessions: merging newsroom-browsers into mcp-research

Status: proposal, 2026-10-07. Supersedes the container-per-session design in
[newsroom-browsers-plan.md](newsroom-browsers-plan.md) (reuse its Keycloak,
Caddy/oauth2-proxy and Tor sections; its Docker-launching parts are dropped).
Builds on the existing `mcp-research` service.

## Goal

One service that gives Claude per-user, isolated, real (headed) browser
sessions, with a human handoff for CAPTCHAs/logins, optional Tor egress, and
the existing fetch/parse/blob features. **One browser-orchestration system**:
no second Playwright integration.

## Decisions (already made)

- One container per egress (`research-tor`, `research-clear`), shared by users.
  **Not** one container per session. No Docker socket, no container creation.
- Browser sessions are separate processes inside that container, each with
  its own Xvfb display, x11vnc, noVNC and `@playwright/mcp` (all 27+ tools,
  not reimplemented).
- **Each session runs under its own Unix uid, with at most one live session
  per uid, ever.** A uid's homedir and everything else it owns is deleted
  before the uid can be handed out again.
- Fresh throwaway profile directory per session (inside that uid's homedir).
- Parsing (trafilatura/markitdown), preflight/downloads (`requests`), uploads
  and blob storage stay **in the same service**: they are stateless library
  calls, and keeping them here means downloads go through the same egress
  (`socks5h://tor` in the tor instance). A separate service would only add a
  hop and a second egress policy.

## Architecture

```
Claude ─Bearer JWT─▶ Caddy ─▶ research-tor ──▶ tor ──▶ internet
Journalist ─SSO──▶  Caddy ─▶ research-clear ─────────▶ internet
  (live view)       (oauth2-proxy)

research-<egress> container:
  gw (uid 1000, unprivileged)      sessiond (root, tiny)
   ├ MCP server, JWT auth            ├ owns the uid pool and its invariants
   ├ session registry, reaper        ├ start(uid): create home, spawn group as uid
   ├ proxies tool calls ───────────▶ │ stop(uid):  kill, wipe, verify
   ├ extract/storage/blobs/uploads   └ listens on /run/sessiond.sock (gw only)
   └ live-view authz (/authz,/start)
  per session (uid 20000+n, gid same, no supplementary groups):
   Xvfb :n ─ x11vnc (pw) ─ websockify :6080+n ─ playwright-mcp :9000+n ─ Chromium
```

The gateway process parses untrusted documents and fetches arbitrary URLs, so
it must **not** be root. Only `sessiond` (about 100 lines, fixed verbs, no
parsing, no network) is. If you would rather have one process, run the gateway
as root; that is the simplification, at the cost of any parser bug being root.

## The uid lifecycle

Pool: `UID_BASE .. UID_BASE+N-1` (default 20000, N=`MAX_SESSIONS_TOTAL`+2
spare for the one-shot path). Display number, VNC/websockify port and MCP
port are all derived from `uid - UID_BASE`, so there is nothing else to
allocate or leak.

**Invariant: a uid is in exactly one state: `free`, `busy(session)` or
`quarantined`.** `sessiond` is the authority and enforces it independently of
the gateway:

1. `start(uid)` is refused unless the uid is `free` **and** a scan of `/proc`
   finds no process with that real/effective/saved uid **and** its homedir does
   not exist.
2. It creates `/home/s<uid>` (`0700`, owned by uid), then forks: child sets
   `umask 077`, `setgroups([])`, `setgid`, `setuid` (in that order, then checks
   `setuid(0)` fails), applies `RLIMIT_NPROC` (per-uid, so this is a real
   per-session process cap), `RLIMIT_NOFILE`, a clean environment
   (`HOME=/home/s<uid>`, `DISPLAY`, no gateway secrets), `setsid`, `exec`s the
   session group. The container runs with `no-new-privileges`.
3. `stop(uid)`, in order, all of which must succeed or the uid is
   **quarantined** (never reused, logged loudly, capacity shrinks):
   1. `SIGKILL` the process group;
   2. sweep: fork a helper that `setuid`s to the uid and calls `kill(-1, SIGKILL)`
      (kills anything that escaped the group);
   3. wait until `/proc` shows no process owned by the uid (bounded retries);
   4. delete everything the uid owns: `rm -rf /home/s<uid>`, then walk `/tmp`,
      `/var/tmp`, `/dev/shm` and `/run` (not crossing mounts) removing every
      entry with `st_uid == uid`, including `/tmp/.X11-unix/X<n>` and
      `/tmp/.X<n>-lock`;
   5. verify: homedir gone, no owned processes, and a final owned-file walk
      comes back empty;
   6. only then mark `free`.
4. On gateway/sessiond start, run `stop` on **every** pool uid (crash and
   restart recovery). There is no state to trust across restarts.

The gateway side is simple: a `free` set and a `uid -> session` dict under one
lock; a uid is taken on session create and returned only when `stop` reports
success. The reaper (idle timeout, `browser_end_session`, MCP session close,
crashed supervisor) all go through the same `stop`.

Anything that must outlive a session (extracted pages, screenshots, downloads
the model asked to keep) is copied by the gateway into blob storage **before**
`stop`, owned by the gateway, never by the session uid. The homedir (profile,
Playwright output dir, downloads) is disposable by design. An evidence archive
is a non-goal for now; it would be "copy the output dir into storage before
wipe".

## Isolation, honestly

What per-session uids buy: sessions cannot read each other's profile, X socket,
`/proc/<pid>/environ`, `/dev/shm` files, or kill each other; they cannot read
gateway or `sessiond` files. What they do not buy: a shared kernel and a shared
network namespace.

- **Loopback is shared.** Every session's MCP, websockify and x11vnc ports are
  reachable from every other session's pages. Mitigations: playwright-mcp's
  Host check allows only `mcp-upstream` (a page cannot set `Host`); x11vnc and
  websockify need a per-session random password; the gateway's HTTP port needs
  a JWT; the `sessiond` socket is `0600` gw-owned.
- **Egress firewall by uid** (needs `NET_ADMIN` only in the entrypoint, which
  then drops it): `iptables -m owner --uid-owner 20000-20999`:
  - tor instance: ACCEPT only to `tor:9050-9069` and loopback; REJECT the rest;
  - clear instance: REJECT RFC1918, link-local, metadata ranges and the other
    compose services (this is the SSRF fix for browsers), ACCEPT the rest.
  The gateway's own `requests` fetches (non-HTML downloads) need an in-code SSRF
  check: resolve, reject private ranges, pin the IP, re-check every redirect.
- **Chromium sandbox:** start with `chromiumSandbox: false` (container is the
  boundary, as before). Try enabling it once the seccomp/userns story is
  known; with per-session uids the sandbox becomes defence in depth, not the
  only layer.
- **Resource limits:** `RLIMIT_NPROC`/`NOFILE` per uid; memory only via the
  container limit plus `MAX_SESSIONS_PER_USER`/`TOTAL`. No per-session cgroups.
  A runaway session can OOM the container (all sessions on that egress).
- Run on a dedicated VM; gVisor (`runsc`) for the container is still the
  recommended upgrade for hostile targets.

## Tor

- Container `research-tor` is on an `internal` network with `tor` only; no
  other route (belt and braces with the uid firewall).
- `torrc` exposes a pool of SocksPorts (`9050..9069`, one per uid index). Tor
  isolates streams from different SocksPorts by default, so sessions of
  different users do not share circuits. (Chromium cannot do SOCKS auth, so
  per-session credentials are not an option.)
- Chromium: `--proxy-server=socks5://tor:<port>`,
  `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`, and the
  gateway's `requests` use `socks5h://tor:<port>` for the same session.
- Chromium over Tor is not Tor Browser; fingerprint caveats from the old plan
  still apply.

## Tools and flows

Tools the model sees: everything `@playwright/mcp` exposes (tool list cached
from the first session, `browser_install` hidden) plus:

| Tool | Behaviour |
|---|---|
| `browser_request_human_help(reason)` | link `https://HOST/<egress>/view/<id>/start`, owner-only via SSO |
| `browser_save_page()` | calls upstream `browser_evaluate("document.documentElement.outerHTML")`, runs trafilatura, stores under a job, returns `{job_id, files, text?}`; replaces `finish_interactive_session` |
| `browser_end_session()` | `stop(uid)` |
| `ingest_page(url)` | one-shot: create an ephemeral session (same machinery, own uid), navigate, save, destroy. Non-HTML goes through `requests` as today |
| `get_upload_link`, `list_files`, `get_file_text`, `get_file_url` | unchanged, but jobs are now owner-scoped (see Auth) |

Deleted: `start_interactive_session`, `click_element`, `finish_interactive_session`,
`browser_session.py`, the in-process Python Playwright dependency and the
shared Chromium. The one-shot path uses the same supervisor, so there is
exactly one way a browser is started.

Live view: `/start` checks ownership (SSO email == session owner) and redirects
to noVNC with the session's VNC password; `/authz` returns
`X-Upstream: <service>:<6080+n>` for Caddy `forward_auth`, as in the old plan.
websockify binds `0.0.0.0` (Caddy is on another container) and is protected by
the VNC password and the internal network.

## Auth

- Keycloak JWT on `/<egress>/mcp` (aud, email, optional group), RFC 9728
  metadata, as in the old plan. We already have this pattern working on other
  MCP servers.
- **MCP layer moves from `fastapi-mcp` to the low-level `mcp` SDK.** The tool
  list is dynamic (proxied from `playwright-mcp`), which `fastapi-mcp`'s
  generate-from-FastAPI-routes model cannot do. Upload page and `/blob/*` stay
  as plain Starlette/FastAPI routes; `extract.py`, `storage.py`, `blobtoken.py`
  and `test_security.py` carry over.
- Jobs are stored under `<owner>/<job_id>`; `list_files`/`get_file_*` check the
  caller's email. Blob URLs stay short-lived signed capability URLs.

## Consent-banner handling

`consent_click.py` and `cmp_block.py` currently work on a Python Playwright
page/route, which goes away.
- Auto-click: port the keyword list to a small `--init-script` JS (runs in every
  page, MutationObserver, never throws). Cheap.
- Blocklist: needs a network-level block. Options in order of preference:
  Chromium `--host-rules="MAP <host> ~NOTFOUND"` generated at start from the
  Cookiemonster list (**check argv size and Chromium's behaviour with
  thousands of rules**), a few-line local filtering proxy in front of the egress,
  or drop the blocklist and rely on the click script. Decide after measuring.

## Deployment

One image (`Dockerfile`): Debian bookworm + Python + Node, `@playwright/mcp`
(pinned) with its Chromium, `xvfb x11vnc novnc websockify`, fonts, `iptables`,
the gateway and `sessiond`. Compose: `caddy`, `oauth2-proxy`, `research-tor`,
`research-clear`, `tor`; networks as in the old plan, minus `control` and
docker-proxy. Container needs caps `SETUID SETGID CHOWN KILL DAC_OVERRIDE`
(for sweeping files) and `NET_ADMIN` (entrypoint only); everything else
dropped; `no-new-privileges`.

**Open: the current `docker-compose.yml` is a Swarm stack** (`deploy:`, overlay
network). Swarm's support for `cap_add` and `internal` overlay networks needs to
be verified; if it is a problem, this service moves to plain compose.

## Phases

1. **Supervisor first (riskiest).** `sessiond` + gateway registry, session
   group launch (Xvfb/x11vnc/websockify/playwright-mcp), `stop` with the full
   wipe/verify. No auth yet. Tests below.
2. **MCP layer.** Port to the `mcp` SDK, proxy tools, `browser_save_page`,
   `ingest_page` via ephemeral session, delete the old browser code, owner-scoped
   jobs.
3. **Auth + live view.** JWT, `/authz`, `/start`, Caddy, oauth2-proxy.
4. **Tor + firewall.** `tor` service, SocksPort pool, uid firewall rules, SSRF
   check for `requests`.
5. **Consent handling port.** Init-script, then blocklist decision.
6. **Hardening.** Chromium sandbox attempt, gVisor, metrics/log lines for
   start/stop/quarantine/handoff.

## Tests (must exist before relying on it)

- **Pool invariant:** property-style test with a fake `sessiond`: random
  create/stop/crash sequences never yield two live sessions on one uid, and a
  failed `stop` quarantines instead of freeing.
- **Reuse is clean:** session A writes files in home, `/tmp`, `/dev/shm`, leaves
  a background process; after `stop`, the next session on the same uid sees
  none of it and `/proc` has no process owned by the uid.
- **Cross-session isolation:** from inside session A's browser user, cannot read
  B's home/profile/`environ`, cannot signal B, cannot read gateway files or
  connect to `/run/sessiond.sock`.
- **Crash recovery:** `kill -9` the gateway; on restart every pool uid is wiped.
- **Egress:** tor instance: `check.torproject.org` says Tor, WebRTC leak page
  shows no IP, direct connect from a session uid fails. Clear instance: browser
  cannot reach other compose services or `169.254.169.254`.
- **Handoff:** non-owner gets 403 on `/start` and `/authz`; spoofed
  `X-Auth-Request-Email`/`X-Upstream` ignored.
- **CAPTCHA smoke test** through the live view in headed Chromium.
- Existing `test_security.py` still passes.

## Risks and open questions

- `browser_evaluate` must be enabled by default in the pinned
  `@playwright/mcp` (0.0.83 in the old plan); otherwise `browser_save_page`
  needs another route to the page HTML (not a CDP port, which a page could try
  to reach on loopback).
- Shared kernel and shared netns: a kernel or Chromium+kernel escape reaches
  all sessions on that egress. Accepted trade-off.
- Memory exhaustion in one session affects all sessions on that egress.
- A quarantined uid is lost until restart; watch for it in logs.
- Swarm vs plain compose (see Deployment).
- Consent blocklist mechanism (see above).
