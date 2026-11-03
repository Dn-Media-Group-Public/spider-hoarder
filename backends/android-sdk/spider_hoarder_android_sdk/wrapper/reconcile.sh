#!/bin/sh
# android-wrapper: creates and supervises the Android slot containers <backend>-<k> (android-clear-1, android-tor-2, ...) of ONE
# backend from its env. Each android_* backend runs its own wrapper: it only ever touches slots carrying its own label value.
#
# THREAT MODEL: this container holds docker.sock, which is root on the host. The only thing that keeps that safe
# is that the wrapper takes NO input except its own env and Docker's answers about its own labelled objects:
# no ports, no API, no network (its service sits on an internal network with nobody else on it). The slot spec
# below is hardcoded, and the one command it execs inside a slot (/app/tor-route.sh) is fixed; the env only sets counts, sizes, the image and the secret. Never add a verb to it.
#
# One wrapper per KVM node (swarm replicas, one per node): wrapper WRAPPER_SLOT (the task slot, 1..) owns slots
# (w-1)*P+1 .. min(ANDROID_SLOTS, w*P), P = ANDROID_SLOTS_PER_WRAPPER (the same slicing the gateways use for REPLICA_SLOT).
# It also makes this node's golden snapshot (quickboot snapshots depend on the host CPU) when the volume's stamp is not the slot image.
#
# Env: STACK_NAME BACKEND (hyphenated backend name: slot names, DNS aliases, label value) ANDROID_EGRESS (clear|tor)
#      ANDROID_SLOTS (total count) ANDROID_SLOTS_PER_WRAPPER WRAPPER_SLOT ANDROID_API ANDROID_RAM_MB ANDROID_SLOT_IMAGE ANDROID_SLOT_CPUS ANDROID_SLOT_MEMORY SLOT_SECRET
#      ANDROID_SLOT_NETWORK (default <stack>_<backend>-slots) ANDROID_SNAPSHOT_VOLUME (default <stack>_<backend>-snapshot)
#      ANDROID_TOR_HOST (tor only: the backend's own Tor daemon service) ANDROID_BOOT_TIMEOUT
#      ANDROID_KVM_GID (else looked up in /host-etc-group) RECONCILE_INTERVAL
# Testing: RECONCILE_ONCE=1 runs one pass; DOCKER overrides the docker binary.
set -eu
: "${STACK_NAME:?}" "${BACKEND:?}" "${ANDROID_EGRESS:?}" "${ANDROID_SLOT_IMAGE:?}" "${SLOT_SECRET:?}"
DOCKER=${DOCKER:-docker}
EGRESS=$ANDROID_EGRESS
WANT=${ANDROID_SLOTS:-0}
PER=${ANDROID_SLOTS_PER_WRAPPER:-0}; [ "$PER" -gt 0 ] || PER=$WANT   # unset: this wrapper owns them all
LO=$(( (${WRAPPER_SLOT:-1} - 1) * PER + 1 ))
HI=$(( ${WRAPPER_SLOT:-1} * PER )); [ "$HI" -le "$WANT" ] || HI=$WANT
TOR_HOST=${ANDROID_TOR_HOST:-}
NET=${ANDROID_SLOT_NETWORK:-${STACK_NAME}_${BACKEND}-slots}
CPUS=${ANDROID_SLOT_CPUS:-2}
MEMORY=${ANDROID_SLOT_MEMORY:-8g}   # 3 GB guest + ~2 GB emulator heap + 4 GB /tmp tmpfs (charged as written); 6g was OOM-killed in use (bring-up plan, W3)
BOOT_TIMEOUT=${ANDROID_BOOT_TIMEOUT:-240}   # snapshot restore reads ~3 GB: disk speed decides (slow cloud disks: minutes)
SNAP=${ANDROID_SNAPSHOT_VOLUME:-${STACK_NAME}_${BACKEND}-snapshot}
INTERVAL=${RECONCILE_INTERVAL:-3}   # a finished session is replaced within this
LABEL=spider-hoarder.android-slot
STACK_LABEL=spider-hoarder.stack   # a wrapper only touches slots carrying its own STACK_NAME: other stacks and hand-run slots are not "extra"
TMP_SIZE=4g   # the emulator's own temp qcow2 (1.5 GB idle) and everything a session writes land in /tmp (tmpfs, counts against --memory)
EMU_UID=2000
KVM_GID=${ANDROID_KVM_GID:-$(awk -F: '$1=="kvm"{print $3}' /host-etc-group 2>/dev/null || true)}
[ -n "$KVM_GID" ] || { echo "no kvm group: set ANDROID_KVM_GID or mount the host's /etc/group at /host-etc-group" >&2; exit 1; }

log() { echo "android-wrapper: $*" >&2; }

# What the old deploy-host preflight checked, now on this node, through its docker (the wrapper itself has no devices)
check_host() {
  [ "$($DOCKER info --format '{{.Architecture}}')" = x86_64 ] || { log "Android slots are x86_64 only"; exit 1; }
  $DOCKER run --rm --device /dev/kvm --entrypoint true "$ANDROID_SLOT_IMAGE" >/dev/null 2>&1 \
    || { log "no usable /dev/kvm on this node (KVM-capable x86_64, nested virtualisation on a VM): is it labelled for PIN_KVM by mistake?"; exit 1; }
}

# name of slot k, and everything derived from it
cname() { echo "${STACK_NAME}-${BACKEND}-$1"; }

spec_hash() {  # the spec: anything that should recreate a slot when it changes
  img=$($DOCKER image inspect -f '{{.Id}}' "$ANDROID_SLOT_IMAGE" 2>/dev/null || echo "$ANDROID_SLOT_IMAGE")
  echo "$img $CPUS $MEMORY $BOOT_TIMEOUT $KVM_GID $SNAP $EGRESS $TOR_HOST $NET $(printf %s "$SLOT_SECRET" | sha256sum | cut -c1-16) spec-v5" | sha256sum | cut -c1-32
}

wipe_state() {  # fresh overlay upper/work dirs (the overlay must not be mounted: the container is gone)
  k=$1; n=$(cname "$k")
  $DOCKER volume rm "$n-avd" >/dev/null 2>&1 || true
  $DOCKER volume create "$n-state" >/dev/null
  $DOCKER run --rm --network none --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    --user 0 -v "$n-state:/s" --entrypoint sh "$ANDROID_SLOT_IMAGE" \
    -c "rm -rf /s/u /s/w && mkdir /s/u /s/w && chown $EMU_UID:$EMU_UID /s/u /s/w"
  lower=$($DOCKER volume inspect -f '{{.Mountpoint}}' "$SNAP")
  upper=$($DOCKER volume inspect -f '{{.Mountpoint}}' "$n-state")
  # the golden AVD is the read-only lower layer, each slot gets its own writable upper (S2); dockerd mounts it,
  # so the slot needs no SYS_ADMIN. AVD paths are absolute, so it is mounted at /avd like the golden volume.
  $DOCKER volume create --driver local --opt type=overlay --opt device=overlay \
    --opt "o=lowerdir=$lower,upperdir=$upper/u,workdir=$upper/w" "$n-avd" >/dev/null
}

report_exit() {  # $1 = exited slot: its logs go with the container, so an abnormal end is written to the wrapper's log first
  st=$($DOCKER inspect -f '{{.State.ExitCode}} {{.State.OOMKilled}}' "$1" 2>/dev/null) || return 0
  set -- "$1" $st
  tail=$($DOCKER logs --tail 60 "$1" 2>&1 | tr -d '\000-\010\013-\037' || true)   # the slot's own output: no control characters in our log
  if [ "$2" != 0 ] || [ "$3" = true ] || printf %s "$tail" | grep -q "slot broken"; then
    log "$1 ended abnormally (exit=$2 oom_killed=$3), last log lines:"
    printf '%s\n' "$tail" | sed 's/^/android-wrapper:   | /' >&2
  fi
}

remove_slot() {  # $1 = container name
  $DOCKER rm -f "$1" >/dev/null 2>&1 || true
}

create_slot() {
  k=$1; n=$(cname "$k"); hash=$2
  log "creating $n"
  remove_slot "$n"
  wipe_state "$k"
  $DOCKER run -d --name "$n" --label "$LABEL=$BACKEND/$k" --label "spider-hoarder.spec=$hash" --label "$STACK_LABEL=$STACK_NAME" \
    --network "$NET" --network-alias "${BACKEND}-$k" \
    --device /dev/kvm --group-add "$KVM_GID" \
    --cap-drop ALL --cap-add NET_ADMIN --cap-add SETUID --cap-add SETGID --cap-add SETPCAP --cap-add KILL \
    --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    --security-opt no-new-privileges \
    --read-only --ulimit nofile=65536:65536 \
    --tmpfs /tmp:rw,exec,nosuid,size=$TMP_SIZE,mode=1777 --tmpfs /run:rw,nosuid,size=16m \
    --tmpfs "/home/emu:rw,nosuid,size=256m,uid=$EMU_UID,gid=$EMU_UID" --tmpfs /home/svc:rw,nosuid,size=128m,uid=3000,gid=3000 \
    --tmpfs /out:rw,nosuid,noexec,size=512m,uid=3000,gid=3000 --tmpfs /in:rw,nosuid,noexec,size=512m,uid=3000,gid=3000 \
    -v "$n-avd:/avd" \
    --cpus "$CPUS" --memory "$MEMORY" --memory-swap "$MEMORY" --pids-limit 4096 \
    --restart no \
    -e "EGRESS=$EGRESS" ${TOR_HOST:+-e "TOR_HOST=$TOR_HOST"} -e "SLOT_SECRET=$SLOT_SECRET" -e "BOOT_TIMEOUT=$BOOT_TIMEOUT" \
    "$ANDROID_SLOT_IMAGE" >/dev/null
}

fix_tor_route() {  # $1 slot. Fixed command, only the exit code is looked at (10 = route changed): nothing the slot says is used.
  rc=0
  $DOCKER exec -u root "$1" /app/tor-route.sh >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 10 ]; then log "$1: tor task moved, default route re-pointed"; fi
}

reconcile() {
  hash=$(spec_hash)
  k=$LO
  while [ "$k" -le "$HI" ]; do
    n=$(cname "$k")
    cur=$($DOCKER inspect -f '{{index .Config.Labels "spider-hoarder.spec"}} {{.State.Running}}' "$n" 2>/dev/null) || cur=missing
    case "$cur" in
      missing) create_slot "$k" "$hash" ;;
      "$hash true") ;;
      # a stopped slot is a finished session (slotd exits on reset), a crash or a reboot: never restart it, its overlay may hold
      # anything the guest wrote. Recreate it from scratch (empty upper), which is the reset.
      "$hash false") log "$n exited, recreating from scratch"; report_exit "$n"; create_slot "$k" "$hash" ;;
      *) log "$n: spec changed, recreating"; create_slot "$k" "$hash" ;;
    esac
    # the tor task gets a new address whenever it is replaced (every deploy): the slot's route must follow
    if [ "$EGRESS" = tor ]; then fix_tor_route "$n"; fi
    k=$((k + 1))
  done
  # extra slots (count lowered, or outside this wrapper's range): only this node's containers are seen
  for l in $($DOCKER ps -a --filter "label=$LABEL" --filter "label=$STACK_LABEL=$STACK_NAME" --format '{{.Names}} {{.Label "'$LABEL'"}}' | awk -v e="$BACKEND" -v lo="$LO" -v hi="$HI" '{split($2,a,"/"); if (a[1]==e && (a[2]+0<lo || a[2]+0>hi)) print $1}'); do
    log "removing extra $l"
    remove_slot "$l"
    $DOCKER volume rm "$l-avd" "$l-state" >/dev/null 2>&1 || true
  done
}

ensure_snapshot() {  # the golden AVD + snapshot `clean` in $SNAP, remade when the slot image (or the RAM size baked in) changed
  image_id=$($DOCKER image inspect -f '{{.Id}}' "$ANDROID_SLOT_IMAGE")${ANDROID_RAM_MB:+ ram=$ANDROID_RAM_MB}
  stamp=$($DOCKER run --rm -v "$SNAP:/avd" --entrypoint cat "$ANDROID_SLOT_IMAGE" /avd/.stamp 2>/dev/null || true)
  [ "$stamp" != "$image_id" ] || return 0
  log "making the Android snapshot (a few minutes)"
  remove_all   # the slots' overlays sit on top of the old snapshot
  # Built in a scratch volume: the working snapshot stays until the new one has passed init's checks. Volumes cannot be
  # renamed, so a good one is copied over $SNAP afterwards.
  new="${SNAP}-new"
  $DOCKER volume rm -f "$new" >/dev/null
  $DOCKER volume create "$new" >/dev/null
  if ! $DOCKER run --rm --ulimit nofile=65536:65536 --device /dev/kvm --group-add "$KVM_GID" \
    --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add KILL --cap-add DAC_OVERRIDE --cap-add FOWNER \
    -v "$new:/avd" -e "IMAGE_ID=$image_id" -e "ANDROID_API=${ANDROID_API:-34}" ${ANDROID_RAM_MB:+-e "ANDROID_RAM_MB=$ANDROID_RAM_MB"} \
    --entrypoint /app/init-snapshot.sh "$ANDROID_SLOT_IMAGE"; then
    log "snapshot init FAILED; keeping the previous snapshot"
    $DOCKER volume rm -f "$new" >/dev/null
    exit 1   # the task restarts and tries again
  fi
  $DOCKER volume rm -f "$SNAP" >/dev/null
  $DOCKER volume create "$SNAP" >/dev/null
  $DOCKER run --rm -v "$new:/from:ro" -v "$SNAP:/to" --entrypoint sh "$ANDROID_SLOT_IMAGE" -c 'cp -a --sparse=always /from/. /to/'
  $DOCKER volume rm -f "$new" >/dev/null
}

remove_all() {
  for n in $($DOCKER ps -a --filter "label=$LABEL" --filter "label=$STACK_LABEL=$STACK_NAME" --format '{{.Names}} {{.Label "'$LABEL'"}}' | awk -v e="$BACKEND" '{split($2,a,"/"); if (a[1]==e) print $1}'); do
    remove_slot "$n"
    $DOCKER volume rm "$n-avd" "$n-state" >/dev/null 2>&1 || true
  done
}

check_host
if [ -z "${RECONCILE_ONCE:-}" ]; then
  trap 'log "stopping: removing all slots"; remove_all; exit 0' TERM INT
fi
ensure_snapshot
while :; do
  reconcile || log "reconcile failed"
  [ -z "${RECONCILE_ONCE:-}" ] || break
  sleep "$INTERVAL" &
  wait $!
done
