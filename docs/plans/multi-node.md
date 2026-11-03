# Plan: Multi-node swarm support

Mirrors the `ariadne` change (`/home/redhog/article-graph`, commit "Multi node setup", `docs/implemented-plans/multi-node.md`).
Platform-side setup (node join, NAT, LB instance group, registry): `/home/redhog/eai-docs/adding-a-second-node.md`.

## Problem / Goal

The stack assumes one node: images are built as `:local` and never pushed, and nothing keeps the services with local state on the node
that holds their volume or bind mounts. Make it deployable on a multi-node swarm **without changing dev or single-node behaviour**:
no new `.env` settings means identical output to today (no registry, no push, no pinning, `--dev` unchanged).

## Design

All settings come from `.env`; unset = current behaviour. Same variable names as ariadne.

| Variable | Default | Meaning |
|---|---|---|
| `IMAGE_PREFIX` | empty | Registry prefix with trailing slash, e.g. `localhost:5000/`. Non-empty: images are pushed, deploy runs without `--resolve-image never`, no forced updates. |
| `IMAGE_TAG` | `local` | Image tag. With `IMAGE_PREFIX` set and `IMAGE_TAG` unset, `deploy.sh` uses `git rev-parse --short HEAD`. |
| `PIN_DATA` | empty | Placement constraint for services with local state, e.g. `node.labels.storage == primary`. Empty = no constraint. |
| `PIN_KVM` | `node.role == manager` | Same format as `PIN_DATA`: placement constraint selecting the Android (KVM) nodes, e.g. `node.labels.kvm == true`. Replaces `ANDROID_PLACEMENT` (removed, no alias). May match **several** nodes. |
| `ANDROID_KVM_NODES` | `1` | How many nodes match `PIN_KVM`: the number of wrapper replicas, one per node. |

Registry and pinning are independent; either works alone. `--dev` ignores neither: it simply leaves them unset.

### Images

- Compose files: every locally built `image:` becomes `${IMAGE_PREFIX:-}<name>:${IMAGE_TAG:-local}`. Files: base `docker-compose.yml`
  (`oauth2-proxy-dcr`) and every `backends/*/*/docker-compose.yml` (gateway, daemon/router, wrapper, slot images).
  Third-party images (`nginx:1.27`, Keycloak dev) are untouched.
- Descriptors keep their `builds` tags as `<name>:local` (the contract stays readable). The deploy tool derives the real tag:
  strip the `:local`, build and push `${IMAGE_PREFIX}<name>:${IMAGE_TAG}`. Compose and tool then agree by construction.
  `build_plan` still dedupes on the descriptor tag (a family base image builds once).
- `build_command`/`build` in `scripts/spider_hoarder.py` take prefix and tag from `environ`; a new `push` step runs after each build
  when `IMAGE_PREFIX` is set. The `oauth2-proxy-dcr` build in `deploy.sh` goes through the same helper.
- Android slot image: `ANDROID_SLOT_IMAGE` is created by the wrapper with the node's own docker (`docker.sock`), not by Swarm. Using the
  prefixed form makes the KVM node pull it from the registry on first use; `reconcile.sh`'s `docker image inspect … || echo "$IMAGE"`
  already falls back to the name, so it needs no change. In local mode the image must have been built on the KVM node (it is: the manager).

### Pinning

- Which services have local state: the base `nginx` (bind-mounts `nginx.conf.template` and `.build/landing.html`) and `oauth2-proxy`
  (`o2p_state`), plus per backend its daemon/router (Tor/I2P state volumes) and Android wrapper. Gateway replicas keep files and jobs
  per replica by design (`docs/architecture/scaling.md`) and stay schedulable anywhere.
- New optional descriptor member `pinned` (list of service names, like `services`, validated to be a subset of `services` plus the
  wrapper). `scripts/spider_hoarder.py pin <out>` writes `.build/docker-compose.pin.yml` with `deploy.placement.constraints: [<PIN_DATA>]`
  for the base services and every enabled backend's `pinned` list. `deploy.sh` adds it to `FILES` only when `PIN_DATA` is non-empty
  (same conditional-overlay pattern as ariadne; no empty constraint ever reaches the base files). Generated rather than static
  because the services depend on `BACKENDS`.
- The Android wrappers are placed by `PIN_KVM`, not `PIN_DATA` (the KVM nodes are not necessarily the data node). Unlike `PIN_DATA`
  it is applied in the backend compose files with the default `node.role == manager`, so unset behaves as `ANDROID_PLACEMENT` did.
- Gateway replicas on node 2: routing by task IP over the overlay should work, but is verified by hand (see Verification); blobs live on the
  replica's own volume and are lost on reschedule, as the replica's IP (and with it every link) already changes then. `RESEARCH_BLOB_URL=gs://…` remains the upgrade path.

### Several Android (KVM) nodes

Today one wrapper per backend makes all slots on one node (`docker.sock`, local volumes). Slots are already addressed by
overlay DNS alias `<backend>-<k>` on the attachable `android-*-slots` network, so a slot on any node is reachable by the gateways and
nginx. What is node-local, and must be split per node:

- **Wrapper:** `replicas: ${ANDROID_KVM_NODES:-1}`, `max_replicas_per_node: 1`, constraint `${PIN_KVM:-node.role == manager}`, env
  `WRAPPER_SLOT: "{{.Task.Slot}}"` and `ANDROID_SLOTS_PER_WRAPPER` (= ceil(total / `ANDROID_KVM_NODES`), derived by the descriptor's
  `env()` next to `ANDROID_*_SLOTS`). Wrapper `w` owns slots `(w-1)*P+1 .. min(total, w*P)`: the same slicing the gateways use for
  `REPLICA_SLOT`, so no allocator. `reconcile.sh` loops over its range instead of `1..ANDROID_SLOTS`; "extra slot" cleanup is by that range
  (it only sees its own node's containers). Slot names stay `<stack>-<backend>-<k>`, globally unique.
- **Golden snapshot:** a quickboot snapshot depends on the host CPU and lives in a node-local volume (`<stack>_<backend>-snapshot`).
  `post_build.sh` makes it on the deploy host only. It moves into the wrapper: on start, if the volume's stamp is not the slot image id
  (+ RAM), the wrapper removes its slots, runs `init-snapshot.sh` into a scratch volume and swaps it in, exactly the current steps minus
  the `docker service scale` calls, then reconciles. This is the one path for single and multi node (a second, host-side path would
  rot); `post_build` is dropped from the android descriptors. Cost: first start of a wrapper takes the few minutes snapshot time.
- **Preflight:** the `/dev/kvm` and `x86_64` checks no longer apply to the deploy host (it may be a plain manager). They move to the
  wrapper's start (clear error in its log, task fails). Secret/pin checks (`SLOT_SECRET`, F-Droid) stay on the deploy host.
- **Images:** the slot and wrapper images must come from the registry on every KVM node (the Registry section above); the slot image is
  pulled by each node's own docker on first `docker run`.
- **`android-tor`:** slots default-route to `android-tor-daemon` over the overlay, so slots on other nodes than the daemon work if
  VXLAN carries the fixed subnet `10.99.0.0/24`; `fix_tor_route` is a local `docker exec` and needs no change. To be verified.
- Everything else (idle timeout, resets, `REPLICA_SLOT` slicing of the gateways, noVNC through nginx) is unaffected.

Each KVM node needs: label (`docker node update --label-add kvm=true <node>`), `/dev/kvm`, x86_64, the `kvm` group (or `ANDROID_KVM_GID`).

### `deploy.sh`

- `.env` is already sourced before builds; add the `IMAGE_PREFIX`/`IMAGE_TAG` defaulting block right after it and export both.
- Registry mode: `docker stack deploy --prune "${FILES[@]}"` (no `--resolve-image never`), skip the forced-update loop.
  Local mode: unchanged.
- `docker swarm init` stays; `swarm init` is skipped on workers because the check is on local state `active`.

## Implementation order

0. Android multi-node (below) is independent of the registry/pin work and can be a second step in the same session.
1. `scripts/spider_hoarder.py`: tag derivation, push, `pinned` member (optional), `pin` subcommand; unit tests in `scripts/tests/test_spider_hoarder.py`
   (tag derivation with and without prefix, pin overlay content, empty `PIN_DATA` produces no overlay).
2. Compose files: parametrise `image:` lines; add `pinned` to each in-tree descriptor.
2b. Android: `PIN_KVM` + wrapper replicas/slicing in both android compose files and descriptors' `env()` (add `ANDROID_*_SLOTS_PER_WRAPPER`);
   `reconcile.sh` range loop and snapshot-on-start (tests in `wrapper/test_reconcile.py`: slot ranges for 1 and 2 wrappers, extra-slot cleanup by range);
   drop `post_build` from android descriptors; split `preflight.sh`.
3. `deploy.sh`: env defaults, helper for `oauth2-proxy-dcr`, registry-mode deploy, pin overlay.
4. Verify single node: `docker stack config` before/after with no new vars must be identical. Then `PIN_DATA="node.role == manager"`
   and `IMAGE_PREFIX=localhost:5000/`; then `./deploy.sh --dev` untouched.
5. Docs.

## Verification on a real second node (manual, per the platform doc)

`docker pull localhost:5000/<image>` on node 2; a second KVM node: its wrapper makes its own snapshot and its half of the slots, a session on a
slot there works from a gateway on the other node, `android-tor` slot traffic still exits through Tor; a `browser-*-gateway` replica on node 2 reached through nginx (MCP `initialize`, a
`/blob/<rid>/…`, `/view/<rid>/…`); the per-uid firewall does not block the overlay addresses of the daemon on node 1.

## Files to change

| File | Change |
|---|---|
| `scripts/spider_hoarder.py` | prefixed tags, push, `pinned` member, `pin` subcommand |
| `scripts/tests/test_spider_hoarder.py` | tests for the above |
| `deploy.sh` | env defaults, registry-mode deploy, pin overlay, `oauth2-proxy-dcr` via helper |
| `docker-compose.yml` | `oauth2-proxy` image line |
| `backends/*/*/docker-compose.yml` | all local `image:` lines and `ANDROID_SLOT_IMAGE`; android wrappers: `PIN_KVM`, replicas, `max_replicas_per_node`, `WRAPPER_SLOT` |
| `backends/*/*/descriptor.py` | `pinned=[…]`; android: `ANDROID_*_SLOTS_PER_WRAPPER` in `env()`, no `post_build` |
| `backends/android-sdk/.../wrapper/reconcile.sh`, `test_reconcile.py` | slot range per wrapper, snapshot made on start, kvm/arch checks |
| `backends/android-sdk/.../post_build.sh`, `preflight.sh` | post_build removed (logic moves to wrapper); preflight keeps secret checks only |
| `.env.example` | commented-out `IMAGE_PREFIX`, `IMAGE_TAG`, `PIN_DATA`, `PIN_KVM`, `ANDROID_KVM_NODES`; `ANDROID_PLACEMENT` removed |

## Docs to update

- `docs/architecture/android.md`: several KVM nodes, wrapper slicing, per-node snapshot made by the wrapper, `PIN_KVM`/`ANDROID_KVM_NODES`.
- `docs/architecture/scaling.md`: replace the single-node bind-mount note with the pin behaviour; multi-node section.
- `docs/architecture/backend-plugins.md`: `pinned` member; image tag contract (`<name>:local` in descriptors, prefixed form in compose).
- `docs/operations/deploy-and-configure.md`: the three variables, `PIN_KVM`/`ANDROID_KVM_NODES` (replacing `ANDROID_PLACEMENT`), node labelling, registry and node setup pointers.
- `docs/implemented-plans/index.md` and each section above: "See also" link, then move this plan from `docs/plans/`.
