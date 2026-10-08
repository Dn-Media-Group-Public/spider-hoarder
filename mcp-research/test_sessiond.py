"""Needs root + the image's tools (run inside the container, see README).
Reuse is clean, cross-uid isolation, crash recovery, quarantine."""

import os
import subprocess
import sys
import time

os.environ.setdefault("MAX_SESSIONS_TOTAL", "3")
os.environ["SESSION_GROUP"] = "/tmp/fake-group.sh"
import ports
import sessiond

assert os.geteuid() == 0, "run as root"
A, B = ports.UIDS[0], ports.UIDS[1]

# stand-in session: leaves files in home, /tmp, /dev/shm and a background process that
# detaches into its own session (escapes the process group)
open("/tmp/fake-group.sh", "w").write("""#!/bin/sh
echo secret > $HOME/profile; echo x > /tmp/leak-$$; echo x > /dev/shm/leak-$$
mkdir -p /tmp/.X11-unix; : > /tmp/.X11-unix/X$DISPLAY_NUM
setsid sleep 1000 &
exec sleep 1000
""")
os.chmod("/tmp/fake-group.sh", 0o755)
os.makedirs("/tmp/.X11-unix", exist_ok=True); os.chmod("/tmp/.X11-unix", 0o1777)


def clean(uid):
    return not sessiond.uid_procs(uid) and not sessiond.owned_paths(uid) and not os.path.exists(ports.home(uid))


def test_reuse_is_clean():
    sessiond.reset_all()
    sessiond.start(A, {}, {"vncpass": "pw"})
    time.sleep(1)
    assert sessiond.uid_procs(A) and os.path.exists(ports.home(A) + "/profile")
    assert oct(os.stat(ports.home(A)).st_mode & 0o777) == "0o700" and os.stat(ports.home(A)).st_uid == A
    # B cannot read A's home or signal A
    r = subprocess.run(["sh", "-c", f"cat {ports.home(A)}/profile"], user=B, group=B, extra_groups=[], capture_output=True)
    assert r.returncode != 0
    pid = sessiond.uid_procs(A)[0]
    r = subprocess.run(["sh", "-c", f"kill -9 {pid}"], user=B, group=B, extra_groups=[], capture_output=True)
    assert r.returncode != 0 and sessiond.uid_procs(A)
    r = subprocess.run(["sh", "-c", f"cat /proc/{pid}/environ"], user=B, group=B, extra_groups=[], capture_output=True)
    assert r.returncode != 0
    sessiond.stop(A)
    assert clean(A) and sessiond.state[A] == sessiond.FREE
    sessiond.start(A, {}, {})  # same uid again, sees nothing of the previous session
    assert not os.path.exists(ports.home(A) + "/vncpass")
    sessiond.stop(A)


def test_refuses_dirty_and_busy():
    sessiond.reset_all()
    sessiond.start(A, {}, {})
    try:
        sessiond.start(A, {}, {})
        assert False
    except RuntimeError:
        pass
    sessiond.stop(A)
    os.mkdir(ports.home(B))  # pre-existing homedir -> quarantine, never reused
    try:
        sessiond.start(B, {}, {})
        assert False
    except RuntimeError:
        pass
    assert sessiond.state[B] == sessiond.QUARANTINED
    os.rmdir(ports.home(B))


def test_crash_recovery():
    sessiond.reset_all()
    sessiond.start(A, {}, {})
    sessiond.procs.clear()  # simulate sessiond restart: it forgot the Popen handle
    sessiond.state.clear()
    sessiond.reset_all()
    assert clean(A)


if __name__ == "__main__":
    test_reuse_is_clean(); test_refuses_dirty_and_busy(); test_crash_recovery()
    print("ok")
