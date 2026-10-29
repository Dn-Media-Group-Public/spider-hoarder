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
    name=""; spec=""; lab=""; stk=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --name) name=$2; shift ;;
        --label) case "$2" in spider-hoarder.spec=*) spec=${2#*=} ;; spider-hoarder.android-slot=*) lab=${2#*=} ;; spider-hoarder.stack=*) stk=${2#*=} ;; esac; shift ;;
      esac
      shift
    done
    if [ -n "$name" ]; then echo "$spec true" > "$S/c/$name"; echo "$lab" > "$S/l/$name"; echo "$stk" > "$S/s/$name"; fi ;;
  logs) for last; do :; done; cat "$S/logs/$last" 2>/dev/null ;;
  inspect)
    for last; do :; done
    case "$*" in *ExitCode*) cat "$S/exit/$last" 2>/dev/null || echo "0 false"; exit 0 ;; esac
    if [ -f "$S/c/$last" ]; then cat "$S/c/$last"; else exit 1; fi ;;
  start) for last; do :; done; read spec _ < "$S/c/$last"; echo "$spec true" > "$S/c/$last" ;;
  exec) echo "$*" >> "$S/exec"; [ -f "$S/route-moved" ] && exit 10; exit 0 ;;
  rm) for last; do :; done; rm -f "$S/c/$last" "$S/l/$last" "$S/s/$last" ;;
  ps)
    want=""; for a; do case "$a" in label=spider-hoarder.stack=*) want=${a#label=spider-hoarder.stack=} ;; esac; done
    for n in $(ls "$S/l"); do
      [ -z "$want" ] || [ "$(cat "$S/s/$n")" = "$want" ] || continue
      case "$*" in *"{{.Label"*) echo "$n $(cat "$S/l/$n")" ;; *) echo "$n" ;; esac
    done ;;
esac
'''


class Env:
    def __init__(self, **kw):
        self.d = tempfile.mkdtemp()
        os.makedirs(f"{self.d}/c")
        os.makedirs(f"{self.d}/l")
        os.makedirs(f"{self.d}/s")
        os.makedirs(f"{self.d}/exit")
        os.makedirs(f"{self.d}/logs")
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
                 "--memory 8g", "--cpus 2", "--tmpfs /tmp:rw,exec,nosuid,size=4g", "--label spider-hoarder.stack=st", "--restart no"):
        assert must in spec, must
    assert "--privileged" not in spec and "docker.sock" not in spec and "SYS_ADMIN" not in spec
    assert "--network st_android_tor" in runs(log)[2] and "EGRESS=tor" in runs(log)[2]
    # a volume overlay per slot, lower = golden snapshot mountpoint
    assert "o=lowerdir=/var/lib/docker/volumes/st_android_snapshot/_data,upperdir=/var/lib/docker/volumes/st-android-clear-1-state/_data/u" in log

    assert runs(e.run()) == []  # nothing to do the second time
    p = f"{e.d}/c/st-android-clear-2"
    spec_hash = open(p).read().split()[0]
    open(p, "w").write(spec_hash + " false\n")
    log = e.run()  # a stopped slot (session over, crash, reboot) is replaced, never restarted: fresh overlay
    assert "start st-android-clear-2" not in log and len(runs(log)) == 1 and "st-android-clear-2-state" in log
    assert open(p).read().split() == [spec_hash, "true"]
    log = e.run(IMG="2")  # new slot image: every slot is recreated
    assert len(runs(log)) == 3
    log = e.run(IMG="2", ANDROID_CLEAR_SLOTS="1")  # fewer slots: the extra one goes
    assert e.containers() == ["st-android-clear-1", "st-android-tor-1"] and runs(log) == []
    log = e.run(IMG="2", ANDROID_CLEAR_SLOTS="0", ANDROID_TOR_SLOTS="0")
    assert e.containers() == []


def test_wrapper_only_touches_its_own_stack():
    a = Env(ANDROID_CLEAR_SLOTS="1", ANDROID_TOR_SLOTS="1")
    a.run()
    # a second wrapper (another stack name) on the same daemon, with other counts: it must not remove the first one's slots
    b = Env(ANDROID_CLEAR_SLOTS="1", ANDROID_TOR_SLOTS="0", STACK_NAME="s2")
    for sub in ("c", "l", "s"):
        os.rmdir(f"{b.d}/{sub}")
        os.symlink(f"{a.d}/{sub}", f"{b.d}/{sub}")
    b.run()
    assert a.containers() == ["s2-android-clear-1", "st-android-clear-1", "st-android-tor-1"], a.containers()
    b.run(ANDROID_CLEAR_SLOTS="0")  # lowering its own count removes only its own
    assert a.containers() == ["st-android-clear-1", "st-android-tor-1"], a.containers()


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


def test_an_abnormal_slot_end_is_logged_before_the_container_goes():
    """Logs are deleted with the container, so a slot that died (non-zero exit, OOM kill, emulator lost) is written to the wrapper's log first."""
    e = Env(ANDROID_CLEAR_SLOTS="1", ANDROID_TOR_SLOTS="0")
    e.run()
    p = f"{e.d}/c/st-android-clear-1"
    h = open(p).read().split()[0]

    def end(code, logs):
        open(p, "w").write(h + " false\n")
        open(f"{e.d}/exit/st-android-clear-1", "w").write(code + "\n")
        open(f"{e.d}/logs/st-android-clear-1", "w").write(logs)
        return subprocess.run(["sh", f"{HERE}/reconcile.sh"], env=e.env, check=True, capture_output=True, text=True).stderr

    err = end("0 false", "session over\nslot reset ok\n")
    assert "exited, recreating" in err and "ended abnormally" not in err and "session over" not in err  # a normal end stays quiet
    err = end("137 true", "emulator started\nkilled\n")
    assert "ended abnormally (exit=137 oom_killed=true)" in err and "| emulator started" in err
    err = end("0 false", "x\nemulator is not running (exit -9): slot broken\n")
    assert "ended abnormally (exit=0 oom_killed=false)" in err and "slot broken" in err
    err = end("1 false", "bad \x1b[31m\x07line\n")
    assert "\x1b" not in err and "\x07" not in err and "line" in err


if __name__ == "__main__":
    test_reconcile()
    test_wrapper_only_touches_its_own_stack()
    test_tor_route_is_fixed_by_exec_into_tor_slots_only()
    test_needs_kvm_group_and_secret()
    test_an_abnormal_slot_end_is_logged_before_the_container_goes()
    print("ok")
