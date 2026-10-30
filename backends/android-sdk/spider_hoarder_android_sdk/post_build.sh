#!/bin/sh
# post_build of the Android backends (run by scripts/ after the images are built, before the stack is deployed):
# the golden AVD + snapshot `clean` of THIS backend, made here because quickboot snapshots depend on the host CPU and
# docker build has no /dev/kvm. In: STACK_NAME, BACKEND_HYPHEN (e.g. android-tor), ANDROID_API, ANDROID_RAM_MB.
# Skipped when the volume's stamp is this image. A new one must start from an empty volume, and the slots (overlay mounts
# on top of it) must be gone first: the wrapper removes them when it stops.
set -e
IMG=spider-hoarder-android-slot:local
SNAP="${STACK_NAME}_${BACKEND_HYPHEN}-snapshot"
WRAPPER="${STACK_NAME}_${BACKEND_HYPHEN}-wrapper"
IMAGE_ID=$(docker image inspect -f '{{.Id}}' $IMG)${ANDROID_RAM_MB:+ ram=$ANDROID_RAM_MB}   # the stamp: image + what is baked into the snapshot
STAMP=$(docker run --rm -v "$SNAP:/avd" --entrypoint cat $IMG /avd/.stamp 2>/dev/null || true)
[ "$STAMP" != "$IMAGE_ID" ] || exit 0

echo "Making the Android snapshot for $BACKEND_HYPHEN (a few minutes)..."
if docker service inspect "$WRAPPER" >/dev/null 2>&1; then
  docker service scale "$WRAPPER=0" >/dev/null
fi
while docker ps --filter "label=spider-hoarder.stack=$STACK_NAME" --filter label=spider-hoarder.android-slot \
        --format '{{.Label "spider-hoarder.android-slot"}}' | grep -q "^$BACKEND_HYPHEN/"; do sleep 2; done
# Built in a scratch volume: the working snapshot stays until the new one has passed init's checks. Volumes cannot be
# renamed, so a good one is copied over $SNAP afterwards (the only window without a snapshot, and no init is running).
NEWSNAP="${SNAP}-new"
docker volume rm -f "$NEWSNAP" >/dev/null
docker volume create "$NEWSNAP" >/dev/null
if ! docker run --rm --ulimit nofile=65536:65536 --device /dev/kvm --group-add "$(stat -c %g /dev/kvm)" \
  --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add KILL --cap-add DAC_OVERRIDE --cap-add FOWNER \
  -v "$NEWSNAP:/avd" -e "IMAGE_ID=$IMAGE_ID" -e "ANDROID_API=${ANDROID_API:-34}" ${ANDROID_RAM_MB:+-e "ANDROID_RAM_MB=$ANDROID_RAM_MB"} \
  --entrypoint /app/init-snapshot.sh $IMG; then
  echo "Android snapshot init FAILED; keeping the previous snapshot ($SNAP) and restarting the wrapper." >&2
  docker volume rm -f "$NEWSNAP" >/dev/null
  if docker service inspect "$WRAPPER" >/dev/null 2>&1; then
    docker service scale "$WRAPPER=1" >/dev/null
  fi
  exit 1
fi
docker volume rm -f "$SNAP" >/dev/null
docker volume create "$SNAP" >/dev/null
docker run --rm -v "$NEWSNAP:/from:ro" -v "$SNAP:/to" --entrypoint sh $IMG -c 'cp -a --sparse=always /from/. /to/'
docker volume rm -f "$NEWSNAP" >/dev/null
