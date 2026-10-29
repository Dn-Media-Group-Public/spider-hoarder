#!/bin/sh
# Make the golden AVD and its quickboot snapshot `clean` (run by deploy.sh in a one-off container with
# --device /dev/kvm and the empty android_snapshot volume at /avd: a re-run on a used AVD directory fails with an
# `unauthorized` device, S2). Boots the guest once, installs F-Droid and uiautomator2's on-device APKs, makes the UI
# quick and static, records what "clean" looks like (baseline-*.txt: the gateway's reset check compares to it),
# saves the snapshot and stamps the volume with $IMAGE_ID.
set -eu
ulimit -n 65536 || true  # see entrypoint.sh: the emulator stalls on Docker's ~1e9 default
: "${IMAGE_ID:?}" "${ANDROID_API:?}"
export ANDROID_SDK_ROOT=/opt/android-sdk ANDROID_AVD_HOME=/avd PATH=/venv/bin:/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:/opt/android-sdk/cmdline-tools/latest/bin:$PATH
# guest RAM is part of the snapshot: restoring reads this much from disk, and the slot needs about 2 GB more than it
EMU_UID=${EMU_UID:-2000}; SVC_UID=${SVC_UID:-3000}
KVM_GID=$(stat -c %g /dev/kvm)

[ -z "$(ls -A /avd)" ] || { echo "/avd is not empty: start from an empty volume" >&2; exit 1; }
chown "$EMU_UID:$EMU_UID" /avd
/app/prep-homes.sh

as() { u=$1; shift; setpriv --reuid="$u" --regid="$u" --groups="$KVM_GID" --inh-caps=-all -- "$@"; }
adb_() { as "$SVC_UID" env HOME=/home/svc adb "$@"; }

echo "no" | as "$EMU_UID" env HOME=/home/emu avdmanager create avd -n slot \
  -k "system-images;android-$ANDROID_API;google_apis_playstore;x86_64" -d pixel_6 --force
cat >> /avd/slot.avd/config.ini <<CFG
hw.ramSize=${ANDROID_RAM_MB:-3072}
hw.keyboard=yes
hw.gpu.enabled=yes
hw.gpu.mode=swiftshader_indirect
showDeviceFrame=no
CFG

mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
as "$SVC_UID" Xvfb :99 -screen 0 800x1000x24 -nolisten tcp -ac &
sleep 2
as "$EMU_UID" env HOME=/home/emu DISPLAY=:99 ANDROID_SDK_ROOT="$ANDROID_SDK_ROOT" ANDROID_AVD_HOME=/avd \
  emulator -avd slot -port 5554 -no-snapshot-load -no-snapshot-save -gpu swiftshader_indirect \
  -no-audio -no-boot-anim -no-metrics &
EMU=$!

for i in $(seq 1 120); do nc -z 127.0.0.1 5554 2>/dev/null && break; sleep 1; done
adb_ kill-server || true
adb_ start-server
adb_ wait-for-device
for i in $(seq 1 180); do [ "$(adb_ shell getprop sys.boot_completed </dev/null | tr -d '\r')" = 1 ] && break; sleep 2; done
[ "$(adb_ shell getprop sys.boot_completed </dev/null | tr -d '\r')" = 1 ] || { echo "guest did not boot" >&2; exit 1; }

# No Bluetooth in a slot. The emulator's virtual controller (netsimd) talks over loopback, which the slot firewall rejects for the
# emulator uid (netsimd panics "failed to create signal pipe: Connection refused"; the emulator logs "no bluetooth emulation
# available"). Here, without the firewall, it works, so the snapshot holds a Bluetooth stack that is up; restored in a slot it has no
# controller: it aborts ("Can't start stack ... starting Controller"), restarts, and now and then a "Bluetooth keeps stopping" dialog
# covers the screen and hides the app from uiautomator. Off, and stays off in the snapshot.
adb_ shell cmd bluetooth_manager disable </dev/null | grep -q "disable: Success" || { echo "could not disable Bluetooth" >&2; exit 1; }
adb_ shell cmd bluetooth_manager wait-for-state:STATE_OFF </dev/null | grep -q "Success" || { echo "Bluetooth did not turn off" >&2; exit 1; }
[ "$(adb_ shell settings get global bluetooth_on </dev/null | tr -d '\r')" = 0 ] || { echo "bluetooth_on is not 0" >&2; exit 1; }

adb_ install /opt/fdroid.apk </dev/null
# uiautomator2 3.x pushes its jar and starts the on-device agent on demand; start it now so it is running in the snapshot
as "$SVC_UID" env HOME=/home/svc python -c "
import time, uiautomator2 as u2
d = u2.connect('emulator-5554')
for i in range(8):  # a slow host (disk, RAM) can miss the library's 30 s launch window
    try:
        d.start_uiautomator(); break
    except Exception as e:
        print('u2 agent not ready, retrying:', e); time.sleep(5)
else:
    raise SystemExit('u2 agent never started')
print('u2 agent ok', d.window_size())"
for s in window_animation_scale transition_animation_scale animator_duration_scale; do
  adb_ shell settings put global $s 0 </dev/null
done
# Chrome's first run ("Welcome to Chrome", sign-in, notifications) would otherwise block the first URL of every session:
# go through it once, here, so the snapshot has a Chrome that opens pages straight away
adb_ shell am start -a android.intent.action.VIEW -d about:blank -n com.android.chrome/com.google.android.apps.chrome.Main </dev/null
as "$SVC_UID" env HOME=/home/svc python -c "
import time, uiautomator2 as u2
d = u2.connect('emulator-5554')
BUTTONS = ['signin_fre_dismiss_button', 'button_secondary', 'negative_button', 'positive_button', 'more_button', 'ack_button']
quiet = 0
time.sleep(10)  # let the first-run activity come up before judging
for i in range(30):  # done when Chrome shows its tab view and three passes in a row found nothing to dismiss
    clicked = False
    for rid in BUTTONS[:3]:  # the dismissing buttons only: never 'Continue'/'Accept'
        b = d(resourceId='com.android.chrome:id/' + rid)
        if b.exists(timeout=1):
            print('first run:', rid, b.info.get('text')); b.click(); clicked = True; break
    cur = d.app_current()
    quiet = 0 if clicked or 'FirstRunActivity' in cur.get('activity', '') or cur.get('package') != 'com.android.chrome' else quiet + 1
    if quiet >= 3:
        break
    time.sleep(3)
else:
    raise SystemExit('Chrome first run not completed: ' + str(d.app_current()))
print('chrome first run done', d.app_current())"
adb_ shell am force-stop com.android.chrome </dev/null
adb_ shell settings put global stay_on_while_plugged_in 7 </dev/null
adb_ shell settings put system screen_off_timeout 2147483647 </dev/null
adb_ shell input keyevent KEYCODE_HOME </dev/null
sleep 5

# Cap the guest's free storage at ~1.2 GB with a ballast file. The emulator keeps guest writes in a temp qcow2 on the slot's /tmp tmpfs,
# which counts against the slot's memory limit: with the 6 GiB data partition ~3 GB of guest writes (a download, an install) OOM-killed the
# emulator, from inside the guest (bring-up plan, W7). With the cap the guest answers "No space left on device" first. (Neither
# `disk.dataPartition.size` in config.ini nor `-partition-size` shrinks the partition, both tried.) fallocate reserves blocks without writing
# them, so the image does not grow; the file is owned by the shell uid, so apps cannot touch it. It is part of the snapshot.
GUEST_FREE_MB=1200
free_mb=$(( $(adb_ shell df -k /data </dev/null | tr -d '\r' | awk 'END {print $4}') / 1024 ))
[ "$free_mb" -gt "$GUEST_FREE_MB" ] || { echo "guest /data has only ${free_mb} MB free before the ballast" >&2; exit 1; }
adb_ shell fallocate -l "$((free_mb - GUEST_FREE_MB))M" /data/local/tmp/ballast </dev/null
free_mb=$(( $(adb_ shell df -k /data </dev/null | tr -d '\r' | awk 'END {print $4}') / 1024 ))
echo "guest /data free after the ballast: ${free_mb} MB"
[ "$free_mb" -ge $((GUEST_FREE_MB - 200)) ] && [ "$free_mb" -le $((GUEST_FREE_MB + 200)) ] || { echo "ballast did not leave ~${GUEST_FREE_MB} MB free (${free_mb})" >&2; exit 1; }

adb_ shell pm list packages -3 </dev/null | tr -d '\r' | sort > /avd/baseline-packages.txt
grep -q org.fdroid.fdroid /avd/baseline-packages.txt || { echo "F-Droid missing from the baseline" >&2; exit 1; }

# the console wants the auth token from the emulator's home, which the svc uid (so `adb emu`) cannot read, and
# `adb emu` is also limited to a trimmed command set: console.py talks to it as root.
/venv/bin/python /app/console.py avd snapshot save clean
/venv/bin/python /app/console.py kill || true
wait "$EMU" || true
[ -e /avd/slot.avd/snapshots/clean/snapshot.pb ] && [ -e /avd/slot.avd/snapshots/clean/ram.bin ] || { echo "snapshot clean was not saved" >&2; ls -R /avd/slot.avd/snapshots >&2 || true; exit 1; }
adb_ kill-server || true
chown -R "$EMU_UID:$EMU_UID" /avd
chmod a+r /avd/baseline-*.txt
echo "$IMAGE_ID" > /avd/.stamp
echo "snapshot clean ready"
