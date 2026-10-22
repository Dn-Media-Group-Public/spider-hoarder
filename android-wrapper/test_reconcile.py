"""Wrapper reconcile pass against a fake `docker` (state in a directory): creates missing slots with the
hardcoded spec, leaves matching ones alone, starts stopped ones, recreates on a spec change, removes extras,
removes everything on stop."""

import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

FAKE = r'''#!/bin/sh
# state: $S/c/<name> = "<spec> <running>" and $S/l/<name> = label value; $S/log = every call
echo "$*" >> "$S/log"
cmd=$1; shift
case "$cmd" in
  image) echo "sha256:img-$IMG" ;;
  volume)
    sub=$1; shift
    case "$sub" in
      inspect) for last; do :; done; echo "/var/lib/docker/volumes/$last/_data" ;;
      create) echo created ;;
      rm) ;;
    esac ;;
  run)
    name=""; spec=""; lab=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --name) name=$2; shift ;;
        --label) case "$2" in spider-hoarder.spec=*) spec=${2#*=} ;; spider-hoarder.android-slot=*) lab=${2#*=} ;; esac; shift ;;
      esac
      shift
    done
    if [ -n "$name" ]; then echo "$spec true" > "$S/c/$name"; echo "$lab" > "$S/l/$name"; fi ;;
  inspect)
    for last; do :; done
    if [ -f "$S/c/$last" ]; then cat "$S/c/$last"; else exit 1; fi ;;
  start) for last; do :; done; read spec _ < "$S/c/$last"; echo "$spec true" > "$S/c/$last" ;;
  exec) echo "$*" >> "$S/exec"; [ -f "$S/route-moved" ] && exit 10; exit 0 ;;
  rm) for last; do :; done; rm -f "$S/c/$last" "$S/l/$last" ;;
  ps)
    for n in $(ls "$S/l"); do
      case "$*" in *"{{.Label"*) echo "$n $(cat "$S/l/$n")" ;; *) echo "$n" ;; esac
    done ;;
esac
'''


class Env:
    def __init__(self, **kw):
        self.d = tempfile.mkdtemp()
        os.makedirs(f"{self.d}/c")
        os.makedirs(f"{self.d}/l")
        p = f"{self.d}/docker"
        open(p, "w").write(FAKE)
        os.chmod(p, 0o755)
        open(f"{self.d}/log", "w").close()
        self.env = {**os.environ, "S": self.d, "DOCKER": p, "STACK_NAME": "st", "ANDROID_SLOT_IMAGE": "android-slot:local",
                    "SLOT_SECRET": "x", "ANDROID_KVM_GID": "993", "RECONCILE_ONCE": "1", "IMG": "1", **kw}

    def run(self, **kw):
        open(f"{self.d}/log", "w").close()
        subprocess.run(["sh", f"{HERE}/reconcile.sh"], env={**self.env, **kw}, check=True, capture_output=True)
        return open(f"{self.d}/log").read()

    def containers(self):
        return sorted(os.listdir(f"{self.d}/c"))


def runs(log):
    return [l for l in log.splitlines() if l.startswith("run -d")]


def test_reconcile():
    e = Env(ANDROID_CLEAR_SLOTS="2", ANDROID_TOR_SLOTS="1")
    log = e.run()
    assert e.containers() == ["st-android-clear-1", "st-android-clear-2", "st-android-tor-1"]
    spec = runs(log)[0]
    for must in ("--device /dev/kvm", "--group-add 993", "--cap-drop ALL", "--security-opt no-new-privileges", "--read-only", "--ulimit nofile=65536:65536",
                 "--network st_android_clear", "--network-alias android-clear-1", "-v st-android-clear-1-avd:/avd",
                 "--memory 6g", "--cpus 2", "--restart unless-stopped"):
        assert must in spec, must
    assert "--privileged" not in spec and "docker.sock" not in spec and "SYS_ADMIN" not in spec
    assert "--network st_android_tor" in runs(log)[2] and "EGRESS=tor" in runs(log)[2]
    # a volume overlay per slot, lower = golden snapshot mountpoint
    assert "o=lowerdir=/var/lib/docker/volumes/st_android_snapshot/_data,upperdir=/var/lib/docker/volumes/st-android-clear-1-state/_data/u" in log

    assert runs(e.run()) == []  # nothing to do the second time
    p = f"{e.d}/c/st-android-clear-2"
    spec_hash = open(p).read().split()[0]
    open(p, "w").write(spec_hash + " false\n")
    log = e.run()
    assert "start st-android-clear-2" in log and runs(log) == []
    log = e.run(IMG="2")  # new slot image: every slot is recreated
    assert len(runs(log)) == 3
    log = e.run(IMG="2", ANDROID_CLEAR_SLOTS="1")  # fewer slots: the extra one goes
    assert e.containers() == ["st-android-clear-1", "st-android-tor-1"] and runs(log) == []
    log = e.run(IMG="2", ANDROID_CLEAR_SLOTS="0", ANDROID_TOR_SLOTS="0")
    assert e.containers() == []


def test_tor_route_is_fixed_by_exec_into_tor_slots_only():
    e = Env(ANDROID_CLEAR_SLOTS="1", ANDROID_TOR_SLOTS="2")
    log = e.run()
    execs = [l for l in log.splitlines() if l.startswith("exec")]
    assert execs == ["exec -u root st-android-tor-1 /app/tor-route.sh", "exec -u root st-android-tor-2 /app/tor-route.sh"], execs
    open(f"{e.d}/route-moved", "w").close()  # exit 10: logged, not an error
    r = subprocess.run(["sh", f"{HERE}/reconcile.sh"], env=e.env, capture_output=True, text=True, check=True)
    assert "st-android-tor-1: tor task moved" in r.stderr and "clear" not in r.stderr


def test_needs_kvm_group_and_secret():
    e = Env(ANDROID_CLEAR_SLOTS="1", ANDROID_KVM_GID="")
    r = subprocess.run(["sh", f"{HERE}/reconcile.sh"], env=e.env, capture_output=True, text=True)
    assert r.returncode != 0 and "kvm" in r.stderr
    e = Env(SLOT_SECRET="")
    assert subprocess.run(["sh", f"{HERE}/reconcile.sh"], env=e.env, capture_output=True).returncode != 0


if __name__ == "__main__":
    test_reconcile()
    test_tor_route_is_fixed_by_exec_into_tor_slots_only()
    test_needs_kvm_group_and_secret()
    print("ok")
