# spider-hoarder

Ever tried to **scrape a web site** using your LLM, only for it to get stuck on **CAPTCHAs**, **logins** or your model considering
**robots.txt** over your direct request to read a specific page? Or hitting docx or pdf files?

Spider-hoarder is an MCP server that hands every user and conversation a private, isolated,
real (headed) Chromium that Claude drives with a full set of browser tools: navigate, click, type, snapshot, find, cookies and
storage, screenshots, PDFs. It copes with JavaScript-heavy sites, consent walls and logins. When Claude hits a CAPTCHA or login
it cannot pass, it sends you a link to the *same* browser (live view) to solve it, then carries on. Egress is direct, anonymised through Tor, or via i2p.

Pages come back as Markdown and everything bulky is stored as a *job* Claude can page through, search
(`grep_file`) or link to, so large pages never flood its context; Claude can also fetch a URL in one shot (`ingest_page`) or have
you upload documents. Subagents can browse in parallel in their own windows while sharing logins. 

Documentation (architecture, API, design decisions): [docs/index.md](docs/index.md).

## Features

(features marked ^ exist in plain playwright-mcp)

* Uses a real headed chromium browser - javascript rendered pages work fine ^
* Browser-in-browser view (using [noVNC](https://novnc.com/)) whenever the MCP client gets stuck on a captcha
* Content conversion
  * Converts everything to markdown using [markitdown](https://github.com/microsoft/markitdown)
  * Terse outline view of every parsed document (headings, links, images) for cheap skimming
  * Archives (zip, tar, tgz, 7z, rar, gz/bz2/xz) are unpacked
  * Binary structured formats such as msgpack, CBOR, BSON, plist and protobuf are turned into JSON
* Network inspection: list and inspect HTTP requests and responses
* Every session is a separate Unix uid with its own process group, profile and egress firewall rules in a shared container; its files are wiped when it ends
* Network request synthesizing: Make arbitrary HTTP requests in the browser context
* [TOR](https://www.torproject.org/) support for anonymous browsing and accessing .onion sites
* [i2p](https://i2p.net/) support: `*.i2p` eepsites only, no outproxy
* No-media sessions: images, video and audio are never requested or stored (default on Tor and i2p), chosen once via `open_session`
* Browser automation using [playwright](https://playwright.dev/)
* SSO integration (OAUth2)

## Quick start

```bash
cp .env.example .env   # fill in PUBLIC_HOST, OIDC_ISSUER and the secrets
./deploy.sh            # prod: builds the images and deploys the stack (Docker Swarm)
./deploy.sh --dev      # local: same stack + a local Keycloak, http://localhost:7297, no TLS
```

Three MCP endpoints appear:

- `$PUBLIC_SCHEME://$PUBLIC_HOST/tor/mcp` — browsers egress via Tor only
- `$PUBLIC_SCHEME://$PUBLIC_HOST/clear/mcp` — direct egress, public internet only
- `$PUBLIC_SCHEME://$PUBLIC_HOST/i2p/mcp` — i2p only, reaches `*.i2p` sites and nothing else (first request can take a minute while tunnels build)

Optionally (see [Android](#android)) two more, giving Claude a real Android emulator instead of a browser:
`/android-clear/mcp` and `/android-tor/mcp`.

TLS must end in front of nginx (the stack only speaks http); auth is an external OIDC provider (Keycloak). More replicas:
`docker service scale spider-hoarder_research-clear=3`. Details: [deploying and configuring](docs/operations/deploy-and-configure.md),
[scaling](docs/architecture/scaling.md).

### Dev (`./deploy.sh --dev`)

Adds a throwaway Keycloak. Ports `HTTP_PORT` (7297) and 8085 must be free.

| | |
|---|---|
| MCP | `http://localhost:7297/clear/mcp`, `http://localhost:7297/tor/mcp` |
| Keycloak admin console | <http://localhost:8085> — `admin` / `admin` |
| **User that can use spider-hoarder** | **`researcher` / `researcher-pw`** |

## Keycloak setup

`keycloak/setup.sh` initializes a realm (`KC_REALM`, default `newsroom`) idempotently: a default client scope, Dynamic Client
Registration for Claude (redirects on `claude.ai`, `claude.com`, `localhost` only) and one user (`researcher`; its password is reset
to `KC_USER_PASSWORD` on every run, so **change it in `.env` outside dev**). `--dev` runs it for you. For a real Keycloak, run it
once (re-run after changing `.env`) using the script from the Keycloak image:

```bash
set -a; source .env; set +a
docker run --rm -v $PWD/keycloak:/s -e KC_URL=https://keycloak.example.org \
  -e KC_ADMIN=admin -e KC_ADMIN_PASSWORD=... -e KC_REALM -e KC_USER -e KC_USER_PASSWORD \
  -e PUBLIC_HOST -e PUBLIC_SCHEME -e MCP_SCOPE \
  --entrypoint bash quay.io/keycloak/keycloak:26.0 /s/setup.sh
```

## Using it

Claude registers itself via DCR, so there is no client id or secret to configure. Give it the MCP URL
of the egress you want, then sign in as the Keycloak user above (a consent screen appears once per client).

| | URL |
|---|---|
| browsers via Tor | `$PUBLIC_SCHEME://$PUBLIC_HOST/tor/mcp` |
| browsers direct | `$PUBLIC_SCHEME://$PUBLIC_HOST/clear/mcp` |
| browsers via i2p | `$PUBLIC_SCHEME://$PUBLIC_HOST/i2p/mcp` |

**claude.ai / Claude Desktop:** Settings → Connectors → *Add custom connector* → paste the URL →
*Connect* → sign in.

**Claude Code:**

```bash
claude mcp add --transport http spider-hoarder $PUBLIC_SCHEME://$PUBLIC_HOST/clear/mcp   # dev: http://localhost:7297/clear/mcp
# then in Claude Code: /mcp -> spider-hoarder -> Authenticate (opens the Keycloak login)
```

What to tell Claude: nothing special, the server sends its own instructions. In short: it calls `open_session` first
(media policy, asking you about security settings), then gets a private browser per conversation (`browser_*` tools), `browser_request_human_help` gives a link
for the user to solve a CAPTCHA/login (they sign in with the same Keycloak account),
`browser_save_page` parses and stores the current page, `ingest_page` fetches a URL in one shot,
and `browser_end_session` frees the browser. Use `tor` for sensitive research, `clear` when a site
blocks Tor, `i2p` for eepsites.

## Android

Claude can also drive a real Android device (x86_64 emulator, Google Play image) per conversation: the `device_*` tools
(accessibility-tree snapshot with refs, click/type/swipe/tap, screenshots, install an APK from a job, logcat, adb shell) plus the
same jobs, uploads and live view. `device_request_human_help` gives you a link to the *same* device, which is how you sign in to
Google/Play (there is no pre-logged-in account; sessions are wiped when they end, after `ANDROID_IDLE_TIMEOUT`, or on
`device_end_session`). F-Droid is preinstalled. On `android-tor`, Play Store is best effort (Google tends to block Tor exits) and
only TCP and DNS leave the device.

It is opt-in and needs a **KVM-capable x86_64 host** (`/dev/kvm`; on a cloud VM, nested virtualisation): set
`ANDROID_CLEAR_REPLICAS` / `ANDROID_TOR_REPLICAS` (and `SLOT_SECRET`, `ANDROID_FDROID_URL`, `ANDROID_FDROID_SHA256`) in `.env` and run
`./deploy.sh` on that host: it builds the slot image and makes the golden emulator snapshot there (a few minutes the first time).
Each emulator ("slot") serves one session at a time, takes 2 CPUs and 8 GB RAM (guest RAM `ANDROID_RAM_MB`, default 3 GB, plus ~5 GB: the emulator heap is ~2 GB; 6 GB was OOM-killed in use) and ~5 MB of disk (all slots share one ~2.8 GB golden snapshot volume; the slot image is 5.3 GB), and a server with no free slot answers "busy" at once. A slot is recreated from scratch after every session, so the next session waits for that: **65-90 s** on a VM with a fast disk (8 vCPU / 32 GB), 2-3x that when a cloud disk throttles, and minutes on a slow one; plan for 8 vCPU and 12 GB per slot (two slots fit in 24 GB, four need the whole of a 32 GB host and not four heavy sessions at once), SSD-class disk reads, and see the [measured numbers](docs/architecture/android.md#image-and-golden-snapshot). A user gets about 1.2 GB of free storage in the guest (more would let guest writes OOM-kill the emulator). Every `./deploy.sh` restarts the wrapper, which removes all slots: a redeploy ends live sessions. Chrome's first-run dialogs are dismissed in the snapshot.
Add it to Claude like the others: `claude mcp add --transport http spider-hoarder-android-clear $PUBLIC_SCHEME://$PUBLIC_HOST/android-clear/mcp`.
Details: [Android emulators](docs/architecture/android.md), [tool reference](docs/api/tools.md#android-tools),
[configuration](docs/operations/deploy-and-configure.md#android-opt-in).

## Tests

```bash
pip install -e mcp-research
pytest tests                                                   # host (test_toolserver.py needs Playwright + Chromium)
# needs root + the image's tools; run with the deployed capability set, plain root hides permission bugs:
docker run --rm --cap-drop ALL --cap-add NET_ADMIN --cap-add SETUID --cap-add SETGID --cap-add CHOWN --cap-add KILL \
  --cap-add DAC_OVERRIDE --cap-add FOWNER -v $PWD/tests:/tests -w /tests --entrypoint python mcp-research:local live/test_sessiond.py
# end to end (stub Keycloak inside one container; restart it per run, the gateway caches the signing key):
docker run -d --name e2e --add-host nginx:172.17.0.1 <same caps> -e EGRESS=clear -e GATEWAY_PORT=8000 \
  -e PUBLIC_HOST=localhost:8000 -e PUBLIC_SCHEME=https -e OIDC_ISSUER=http://127.0.0.1:9999/realms/r \
  -e BLOB_TOKEN_SECRET=x -v $PWD/tests:/tests mcp-research:local
docker exec -u 1000 -w /tests e2e python live/e2e_auth.py
```

Android (host, no KVM needed; `test_android.py` must run on its own because it sets `MODE=android`):

```bash
(cd mcp-research && python test_android.py)
(cd android-toolserver && python test_android_toolserver.py)   # if you change the tools: python export_tools.py first
(cd android-slot && python test_slot.py) && (cd android-wrapper && python test_reconcile.py)
# on a KVM host, inside a research-android-* container as uid 1000: python smoke_android.py
# on a KVM host, against the deployed --dev stack through nginx (real Keycloak tokens; see the docstrings for the docker run line):
#   tests/live/e2e_android_setup.sh; python e2e_android.py [android-clear|android-tor]; python e2e_android_view.py; python e2e_android_lifecycle.py <phase>
```
