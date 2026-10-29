#!/bin/sh
# android-wrapper: creates and supervises the Android slot containers android-<egress>-<k> from its env.
#
# THREAT MODEL: this container holds docker.sock, which is root on the host. The only thing that keeps that safe
# is that the wrapper takes NO input except its own env and Docker's answers about its own labelled objects:
# no ports, no API, no network (its service sits on an internal network with nobody else on it). The slot spec
# below is hardcoded, and the one command it execs inside a slot (/app/tor-route.sh) is fixed; the env only sets counts, sizes, the image and the secret. Never add a verb to it.
#
# Env: STACK_NAME ANDROID_CLEAR_SLOTS ANDROID_TOR_SLOTS ANDROID_SLOT_IMAGE ANDROID_SLOT_CPUS ANDROID_SLOT_MEMORY
#      SLOT_SECRET ANDROID_SNAPSHOT_VOLUME ANDROID_BOOT_TIMEOUT ANDROID_KVM_GID (else looked up in /host-etc-group) RECONCILE_INTERVAL
# Testing: RECONCILE_ONCE=1 runs one pass; DOCKER overrides the docker binary.
set -eu
: "${STACK_NAME:?}" "${ANDROID_SLOT_IMAGE:?}" "${SLOT_SECRET:?}"
DOCKER=${DOCKER:-docker}
CLEAR=${ANDROID_CLEAR_SLOTS:-0}
TOR=${ANDROID_TOR_SLOTS:-0}
CPUS=${ANDROID_SLOT_CPUS:-2}
MEMORY=${ANDROID_SLOT_MEMORY:-8g}   # 3 GB guest + ~2 GB emulator heap + 4 GB /tmp tmpfs (charged as written); 6g was OOM-killed in use (bring-up plan, W3)
BOOT_TIMEOUT=${ANDROID_BOOT_TIMEOUT:-240}   # snapshot restore reads ~3 GB: disk speed decides (slow cloud disks: minutes)
SNAP=${ANDROID_SNAPSHOT_VOLUME:-${STACK_NAME}_android_snapshot}
INTERVAL=${RECONCILE_INTERVAL:-3}   # a finished session is replaced within this
LABEL=spider-hoarder.android-slot
STACK_LABEL=spider-hoarder.stack   # a wrapper only touches slots carrying its own STACK_NAME: other stacks and hand-run slots are not "extra"
TMP_SIZE=4g   # the emulator's own temp qcow2 (1.5 GB idle) and everything a session writes land in /tmp (tmpfs, counts against --memory)
EMU_UID=2000
KVM_GID=${ANDROID_KVM_GID:-$(awk -F: '$1=="kvm"{print $3}' /host-etc-group 2>/dev/null || true)}
[ -n "$KVM_GID" ] || { echo "no kvm group: set ANDROID_KVM_GID or mount the host's /etc/group at /host-etc-group" >&2; exit 1; }

log() { echo "android-wrapper: $*" >&2; }

# name of slot k of an egress, and everything derived from it
cname() { echo "${STACK_NAME}-android-$1-$2"; }

spec_hash() {  # the spec: anything that should recreate a slot when it changes
  img=$($DOCKER image inspect -f '{{.Id}}' "$ANDROID_SLOT_IMAGE" 2>/dev/null || echo "$ANDROID_SLOT_IMAGE")
  echo "$img $CPUS $MEMORY $BOOT_TIMEOUT $KVM_GID $SNAP $(printf %s "$SLOT_SECRET" | sha256sum | cut -c1-16) spec-v4" | sha256sum | cut -c1-32
}

wipe_state() {  # fresh overlay upper/work dirs (the overlay must not be mounted: the container is gone)
  e=$1; k=$2; n=$(cname "$e" "$k")
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
  e=$1; k=$2; n=$(cname "$e" "$k"); hash=$3
  log "creating $n"
  remove_slot "$n"
  wipe_state "$e" "$k"
  $DOCKER run -d --name "$n" --label "$LABEL=$e/$k" --label "spider-hoarder.spec=$hash" --label "$STACK_LABEL=$STACK_NAME" \
    --network "${STACK_NAME}_android_$e" --network-alias "android-$e-$k" \
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
    -e "EGRESS=$e" -e "SLOT_SECRET=$SLOT_SECRET" -e "BOOT_TIMEOUT=$BOOT_TIMEOUT" \
    "$ANDROID_SLOT_IMAGE" >/dev/null
}

fix_tor_route() {  # $1 slot. Fixed command, only the exit code is looked at (10 = route changed): nothing the slot says is used.
  rc=0
  $DOCKER exec -u root "$1" /app/tor-route.sh >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 10 ]; then log "$1: tor task moved, default route re-pointed"; fi
}

reconcile_egress() {  # $1 egress, $2 wanted count
  e=$1; want=$2; hash=$(spec_hash)
  k=1
  while [ "$k" -le "$want" ]; do
    n=$(cname "$e" "$k")
    cur=$($DOCKER inspect -f '{{index .Config.Labels "spider-hoarder.spec"}} {{.State.Running}}' "$n" 2>/dev/null) || cur=missing
    case "$cur" in
      missing) create_slot "$e" "$k" "$hash" ;;
      "$hash true") ;;
      # a stopped slot is a finished session (slotd exits on reset), a crash or a reboot: never restart it, its overlay may hold
      # anything the guest wrote. Recreate it from scratch (empty upper), which is the reset.
      "$hash false") log "$n exited, recreating from scratch"; report_exit "$n"; create_slot "$e" "$k" "$hash" ;;
      *) log "$n: spec changed, recreating"; create_slot "$e" "$k" "$hash" ;;
    esac
    # the tor task gets a new address whenever it is replaced (every deploy): the slot's route must follow
    if [ "$e" = tor ]; then fix_tor_route "$n"; fi
    k=$((k + 1))
  done
  # extra slots (count lowered)
  for l in $($DOCKER ps -a --filter "label=$LABEL" --filter "label=$STACK_LABEL=$STACK_NAME" --format '{{.Names}} {{.Label "'$LABEL'"}}' | awk -v e="$e" -v w="$want" '{split($2,a,"/"); if (a[1]==e && a[2]+0>w) print $1}'); do
    log "removing extra $l"
    remove_slot "$l"
    $DOCKER volume rm "$l-avd" "$l-state" >/dev/null 2>&1 || true
  done
}

remove_all() {
  for n in $($DOCKER ps -a --filter "label=$LABEL" --filter "label=$STACK_LABEL=$STACK_NAME" --format '{{.Names}}'); do
    remove_slot "$n"
    $DOCKER volume rm "$n-avd" "$n-state" >/dev/null 2>&1 || true
  done
}

if [ -z "${RECONCILE_ONCE:-}" ]; then
  trap 'log "stopping: removing all slots"; remove_all; exit 0' TERM INT
fi
while :; do
  reconcile_egress clear "$CLEAR" || log "reconcile clear failed"
  reconcile_egress tor "$TOR" || log "reconcile tor failed"
  [ -z "${RECONCILE_ONCE:-}" ] || break
  sleep "$INTERVAL" &
  wait $!
done
