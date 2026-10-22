"""slotd: the one privileged process of an Android slot (root, narrow caps; the emulator and everything else it
starts has none). Boots the emulator from the golden snapshot, runs the tool server, and serves the gateway's
control verbs on CONTROL_PORT, each needing X-Slot-Secret:

  GET  /internal/health        {state, active}
  POST /internal/start         {"vnc_password": "..."}   session begins: VNC + noVNC come up
  POST /internal/reset         session over: answers, then slotd exits and the container stops; the wrapper recreates it from
                               scratch (fresh overlay, nothing of the session survives) and the new slotd boots from the snapshot
  GET  /internal/verify        {ok, problems}: is the guest what the snapshot was (packages, accounts, /sdcard)
  GET  /internal/out/<name>    a file a tool wrote (read once, then deleted)
  PUT  /internal/in/<name>     a file for a tool (an APK)

States: booting -> free -> busy -> resetting (then the container is gone); broken after a failed boot (reset replaces the slot).
See docs/implemented-plans/android-hoarder.md, "Slot lifecycle and the gateway-to-slot interface".
"""

import hmac
import json
import logging
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("slotd")

SECRET = os.environ.get("SLOT_SECRET", "")
EGRESS = os.environ.get("EGRESS", "clear")
EMU_UID = int(os.environ.get("EMU_UID", "2000"))
SVC_UID = int(os.environ.get("SVC_UID", "3000"))
SVC_GID = int(os.environ.get("SVC_GID", SVC_UID))
KVM_GID = os.environ.get("KVM_GID", "")
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "9001"))
TOOL_PORT = int(os.environ.get("TOOL_PORT", "9000"))
WS_PORT = int(os.environ.get("WS_PORT", "6080"))
VNC_PORT = 5900
DISPLAY = ":99"
EMU_PORT = 5554
SERIAL = f"emulator-{EMU_PORT}"
OUT, IN = "/out", "/in"
AVD = "/avd"
GATEWAY = "10.0.2.2"  # the emulator's slirp gateway/DHCP server
BOOT_TIMEOUT = int(os.environ.get("BOOT_TIMEOUT", "240"))
TOOLSERVER_TIMEOUT = int(os.environ.get("TOOLSERVER_TIMEOUT", "180"))  # python + mcp imports: slow on a starved host
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", 50 * 1024 * 1024))
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")
SDK = "/opt/android-sdk"
ADB = f"{SDK}/platform-tools/adb"
PATH = f"/venv/bin:{SDK}/platform-tools:{SDK}/emulator:/usr/local/bin:/usr/bin:/bin"
SWEEP = ["/tmp", "/var/tmp", "/dev/shm", "/run"]  # tmpfs: anything the emulator uid or svc uid left there goes

BOOTING, FREE, BUSY, RESETTING, BROKEN = "booting", "free", "busy", "resetting", "broken"
state = BOOTING
procs: dict = {}      # name -> Popen: xvfb, emulator, toolserver, x11vnc, websockify
sdcard_at_free: list | None = None  # the /sdcard listing when this boot became free: what verify compares with
lock = threading.Lock()  # one lifecycle verb at a time


# ------------------------------------------------------------- processes ----
def spawn(name: str, uid: int, argv: list, env: dict | None = None, groups: str = "") -> subprocess.Popen:
    """Start argv as uid with an empty capability set, in its own process group."""
    cmd = ["setpriv", "--no-new-privs", f"--reuid={uid}", f"--regid={uid}", f"--groups={groups}" if groups else "--clear-groups",
           "--bounding-set=-all", "--inh-caps=-all", "--", *argv]
    home = "/home/emu" if uid == EMU_UID else "/home/svc"
    penv = {"PATH": PATH, "HOME": home, "LANG": "C.UTF-8", "DISPLAY": DISPLAY, "ANDROID_SDK_ROOT": SDK,
            "ANDROID_AVD_HOME": AVD, **(env or {})}
    p = subprocess.Popen(cmd, env=penv, cwd=home, start_new_session=True, umask=0o027,
                         stdin=subprocess.DEVNULL, stdout=2, stderr=2)
    procs[name] = p
    log.info("started %s (pid %s, uid %s)", name, p.pid, uid)
    return p


def kill(name: str) -> None:
    p = procs.pop(name, None)
    if p:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.error("%s would not die", name)


def uid_procs(uid: int) -> list:
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/status") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        uids, zombie = [], False
        for line in lines:
            if line.startswith("Uid:"):
                uids = [int(x) for x in line.split()[1:4]]
            elif line.startswith("State:"):
                zombie = line.split()[1] == "Z"
        if uid in uids and not zombie:
            out.append(int(d))
    return out


def kill_uid(uid: int) -> bool:
    """SIGKILL everything of uid (CAP_KILL); True once nothing is left."""
    for _ in range(50):
        pids = uid_procs(uid)
        if not pids:
            return True
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.1)
    return not uid_procs(uid)


def adb(*args: str, timeout: int = 30) -> str:
    """adb as the svc uid (its server runs there, firewalled to loopback). Stdout, stripped of CRs."""
    cmd = ["setpriv", f"--reuid={SVC_UID}", f"--regid={SVC_UID}", "--clear-groups", "--bounding-set=-all", "--inh-caps=-all",
           "--no-new-privs", "--", ADB, *args]
    r = subprocess.run(cmd, env={"PATH": PATH, "HOME": "/home/svc", "ADB_MDNS_AUTO_CONNECT": "0"},
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, cwd="/home/svc")
    return r.stdout.replace("\r", "")


def wipe(uid: int) -> None:
    """Delete everything owned by uid under the tmpfs roots, its home and (svc) the file dirs."""
    roots = list(SWEEP)
    for root in roots:
        try:
            dev = os.lstat(root).st_dev
        except OSError:
            continue
        stack = [root]
        while stack:
            try:
                entries = list(os.scandir(stack.pop()))
            except OSError:
                continue
            for e in entries:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_uid == uid:
                    shutil.rmtree(e.path, ignore_errors=True) if stat.S_ISDIR(st.st_mode) else _unlink(e.path)
                elif stat.S_ISDIR(st.st_mode) and st.st_dev == dev:
                    stack.append(e.path)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def clear_dir(path: str) -> None:
    for e in os.scandir(path):
        if e.is_dir(follow_symlinks=False):
            shutil.rmtree(e.path, ignore_errors=True)
        else:
            _unlink(e.path)


# --------------------------------------------------------------- lifecycle ---
def start_xvfb() -> None:
    os.makedirs("/tmp/.X11-unix", exist_ok=True)
    os.chmod("/tmp/.X11-unix", 0o1777)
    spawn("xvfb", SVC_UID, ["Xvfb", DISPLAY, "-screen", "0", "800x1000x24", "-nolisten", "tcp", "-ac"])  # -ac: the emulator uid draws too
    for _ in range(100):
        if os.path.exists("/tmp/.X11-unix/X99"):
            return
        time.sleep(0.1)
    raise RuntimeError("Xvfb did not start")


def emulator_cmd() -> list:
    cmd = ["emulator", "-avd", "slot", "-port", str(EMU_PORT), "-read-only", "-snapshot", "clean", "-no-snapshot-save",
           "-gpu", "swiftshader_indirect", "-no-audio", "-no-boot-anim", "-no-metrics"]
    if EGRESS == "tor":
        cmd += ["-dns-server", "8.8.8.8"]  # a dummy public address: tor's DNSPort redirect answers it, no private-range exception needed
    return cmd


def boot() -> None:
    """Emulator from snapshot `clean`, then (S6) the adb server only once the emulator listens: it finds the device
    by scanning, so the firewall can reject the emulator uid's loopback entirely."""
    if "xvfb" not in procs or procs["xvfb"].poll() is not None:
        kill("xvfb")
        start_xvfb()
    spawn("emulator", EMU_UID, emulator_cmd(), env={"ANDROID_EMU_TMP": "/tmp"}, groups=KVM_GID)
    deadline = time.time() + BOOT_TIMEOUT
    while time.time() < deadline:
        if procs["emulator"].poll() is not None:
            raise RuntimeError("emulator exited during boot")
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", EMU_PORT)) == 0:
                break
        time.sleep(1)
    else:
        raise RuntimeError("emulator console never came up")
    while time.time() < deadline:
        if procs["emulator"].poll() is not None:  # e.g. OOM-killed while loading the snapshot
            raise RuntimeError("emulator exited during boot")
        adb("kill-server")
        adb("start-server")
        devices = adb("devices")
        if SERIAL in devices and "device" in devices.split(SERIAL, 1)[1][:12]:
            break
        time.sleep(2)
    while time.time() < deadline:
        if procs["emulator"].poll() is not None:
            raise RuntimeError("emulator exited during boot")
        if adb("-s", SERIAL, "shell", "getprop", "sys.boot_completed").strip() == "1":
            ensure_network()
            set_guest_time()
            return
        time.sleep(1)
    raise RuntimeError("guest did not finish booting")


def has_net() -> bool:
    """wlan0 has an address AND the emulator's gateway answers (right after a restore the stale address is still there
    for a few seconds, then the guest drops it)."""
    return "1 received" in adb("-s", SERIAL, "shell", "ping", "-c", "1", "-W", "1", GATEWAY)


def ensure_network() -> None:
    """After a snapshot restore the guest loses its address and its DHCP discovers go unanswered (seen on most restores):
    toggling wifi makes it ask again. Best effort: an egress that is down on purpose (Tor not up) is not a boot failure."""
    stable = 0
    deadline = time.time() + 25  # the stale address disappears within ~10 s of the restore
    while time.time() < deadline:
        stable = stable + 1 if has_net() else 0
        if stable >= 5:
            return
        time.sleep(1)
    for _ in range(3):
        adb("-s", SERIAL, "shell", "svc", "wifi", "disable")
        time.sleep(2)
        adb("-s", SERIAL, "shell", "svc", "wifi", "enable")
        for _ in range(20):
            time.sleep(1)
            if has_net():
                log.info("guest network recovered by toggling wifi")
                return
    log.warning("guest cannot reach its gateway after restore")


def set_guest_time() -> None:
    """The guest's clock is the snapshot's time plus the time since the restore, so it lags by the snapshot's age (an hour
    or more: TLS dates, 'today' in apps). `cmd alarm set-time` works as the shell uid. Best effort."""
    adb("-s", SERIAL, "shell", "cmd", "alarm", "set-time", str(int(time.time() * 1000)))


def start_toolserver() -> None:
    spawn("toolserver", SVC_UID, ["python", "/opt/toolserver/android_server.py", str(TOOL_PORT)],
          env={"SLOT_SECRET": SECRET, "ANDROID_OUT_DIR": OUT, "ANDROID_IN_DIR": IN}, groups="")
    deadline = time.time() + TOOLSERVER_TIMEOUT
    while time.time() < deadline:
        if procs["toolserver"].poll() is not None:
            raise RuntimeError("tool server exited")
        try:  # a 403 (no secret) means it is up
            urllib.request.urlopen(f"http://127.0.0.1:{TOOL_PORT}/mcp", timeout=2)
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("tool server did not start")


def sdcard_listing() -> list:
    """Every regular file on /sdcard (the trailing slash matters: /sdcard is a symlink and `find /sdcard` lists nothing)."""
    out = adb("-s", SERIAL, "shell", "find", "/sdcard/", "-type", "f")
    return sorted(x for x in out.split("\n") if x.strip())


def settled_sdcard_listing(timeout: float = 30, pause: float = 3) -> list:
    """System apps create and delete files on /sdcard for a while after a snapshot restore (the obb .nomedia, YouTube's
    randomly named exo/*.uid, GMS's speech model download): take the listing once it stops changing. A baseline fixed at
    image time cannot match that, this one is what *this* boot looked like at rest."""
    deadline = time.time() + timeout
    last = sdcard_listing()
    while time.time() < deadline:
        time.sleep(pause)
        now = sdcard_listing()
        if now == last:
            return now
        last = now
    return last


def stop_session_processes() -> None:
    for name in ("websockify", "x11vnc", "toolserver", "emulator"):
        kill(name)
    kill_uid(EMU_UID)  # the process group is not enough: anything that left it dies here
    adb("kill-server")


def boot_slot() -> None:
    """First (and only) boot of this container: a fresh container is the reset, so this is the same wipe and boot the old
    in-place reset did, with nothing left over to clean."""
    global state, sdcard_at_free
    state = BOOTING  # not "resetting": a gateway starting meanwhile must see a booting slot (clean), not one to replace
    try:
        stop_session_processes()
        kill("xvfb")  # also drops the display's contents (clipboard, windows)
        kill_uid(SVC_UID)
        wipe(EMU_UID)
        wipe(SVC_UID)
        shutil.rmtree("/home/emu", ignore_errors=True)
        shutil.rmtree("/home/svc", ignore_errors=True)
        os.makedirs("/home/emu", exist_ok=True)
        os.makedirs("/home/svc", exist_ok=True)
        subprocess.run(["/app/prep-homes.sh"], check=True, env={"PATH": PATH, "EMU_UID": str(EMU_UID), "SVC_UID": str(SVC_UID)})
        for d in (OUT, IN):
            os.makedirs(d, exist_ok=True)
            clear_dir(d)
            os.chown(d, SVC_UID, SVC_GID)
        boot()
        start_toolserver()
        sdcard_at_free = settled_sdcard_listing()
        state = FREE
        log.info("slot free")
    except BaseException:
        state = BROKEN
        log.exception("boot failed")
        raise


def end_session() -> None:
    """Session over: the slot is not reset in place, it is thrown away. Answer first, then exit; no restart policy, so the
    container stops and the wrapper (which only looks at whether its slots run) recreates it with an empty overlay."""
    global state
    state = RESETTING  # nothing starts on it from here on
    threading.Timer(0.5, os._exit, args=(0,)).start()


def check_alive() -> None:
    """A slot that is free or busy has a running emulator and tool server: if one died (OOM kill, crash) the slot is
    broken now, not when the next tool call fails. The gateway's reset after the session then reboots it."""
    global state
    if state not in (FREE, BUSY):
        return
    for name in ("emulator", "toolserver"):
        p = procs.get(name)
        if p is None or p.poll() is not None:
            log.error("%s is not running (%s): slot broken", name, "never started" if p is None else f"exit {p.returncode}")
            state = BROKEN
            return


def watchdog() -> None:
    while True:
        time.sleep(2)
        check_alive()


def start_session(vnc_password: str) -> None:
    global state
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,8}", vnc_password):  # VNC auth uses at most 8 chars
        raise ValueError("bad vnc_password")
    if state != FREE:
        raise Busy(f"slot is {state}")
    pw = "/home/svc/vncpass"
    fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(vnc_password)
    os.chown(pw, SVC_UID, SVC_GID)
    set_guest_time()  # the slot may have been free for hours since boot
    spawn("x11vnc", SVC_UID, ["x11vnc", "-display", DISPLAY, "-passwdfile", pw, "-localhost", "-rfbport", str(VNC_PORT),
                              "-forever", "-shared", "-quiet"])
    spawn("websockify", SVC_UID, ["websockify", "--web", "/usr/share/novnc", f"0.0.0.0:{WS_PORT}", f"127.0.0.1:{VNC_PORT}"])
    state = BUSY


def verify() -> dict:
    """The guest is what the snapshot was: same third-party packages as the baseline, no accounts, the same files on /sdcard
    as when this boot became free (not the image-time baseline: see settled_sdcard_listing), adb up."""
    problems = []
    try:
        if procs.get("emulator") is None or procs["emulator"].poll() is not None:
            problems.append("emulator not running")
        if adb("-s", SERIAL, "shell", "getprop", "sys.boot_completed").strip() != "1":
            problems.append("guest not booted")
        base = lambda n: sorted(open(f"{AVD}/{n}").read().split())
        pk = sorted(adb("-s", SERIAL, "shell", "pm", "list", "packages", "-3").split())
        if pk != base("baseline-packages.txt"):
            problems.append("third-party packages differ from the baseline")
        sd, want = sdcard_listing(), sdcard_at_free
        if want is None:
            problems.append("no /sdcard listing from when the slot became free")
        elif sd != want:
            extra, missing = sorted(set(sd) - set(want)), sorted(set(want) - set(sd))
            problems.append(f"/sdcard changed since the slot became free: extra {extra[:10]}, missing {missing[:10]}")
        acc = adb("-s", SERIAL, "shell", "dumpsys", "account")
        if not re.search(r"Accounts: 0\b", acc):
            problems.append("an account exists")
    except Exception as e:  # noqa: BLE001
        problems.append(f"verify failed: {e!r}")
    return {"ok": not problems, "problems": problems}


# ------------------------------------------------------------------ files ----
def _name(raw: str) -> str:
    if not NAME.fullmatch(raw):
        raise ValueError("bad file name")
    return raw


def read_out(name: str) -> bytes:
    """/out/<name> without trusting what svc controls: O_NOFOLLOW at every step, O_NONBLOCK against planted
    FIFOs, regular files only, size-capped. The file is deleted after reading."""
    dfd = os.open(OUT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(_name(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("output is not a regular file")
            if st.st_size > MAX_FILE_BYTES:
                raise ValueError("output file too large")
            with os.fdopen(os.dup(fd), "rb") as f:
                data = f.read(MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
        os.unlink(name, dir_fd=dfd)
        return data
    finally:
        os.close(dfd)


def write_in(name: str, data: bytes) -> None:
    dfd = os.open(IN, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(_name(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            os.fchown(f.fileno(), SVC_UID, SVC_GID)
    finally:
        os.close(dfd)


# ------------------------------------------------------------------- http ----
class Busy(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "slotd"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body=b"", ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        given = self.headers.get("X-Slot-Secret", "").encode()
        if SECRET and hmac.compare_digest(given, SECRET.encode()):
            return True
        self._send(403, {"error": "forbidden"})
        return False

    def _body(self, cap=MAX_FILE_BYTES) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > cap:
            raise ValueError("body too large")
        return self.rfile.read(n)

    def _run(self, fn):
        try:
            self._send(200, fn())
        except Busy as e:
            self._send(409, {"error": str(e)})
        except FileExistsError as e:
            self._send(409, {"error": str(e)})
        except (ValueError, FileNotFoundError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log.exception("request failed")
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        if not self._authed():
            return
        if self.path == "/internal/health":
            return self._send(200, {"state": state, "active": state == BUSY})
        if self.path == "/internal/verify":
            return self._run(verify)
        if self.path.startswith("/internal/out/"):
            try:
                self._send(200, read_out(self.path[len("/internal/out/"):]), "application/octet-stream")
            except (ValueError, OSError) as e:
                self._send(404, {"error": str(e)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return
        if self.path == "/internal/start":
            def go():
                with lock:
                    start_session(json.loads(self._body(4096) or b"{}").get("vnc_password", ""))
                return {"ok": True}
            return self._run(go)
        if self.path == "/internal/reset":
            def go():
                with lock:
                    end_session()
                return {"ok": True}
            return self._run(go)
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        if not self._authed():
            return
        if self.path.startswith("/internal/in/"):
            return self._run(lambda: (write_in(self.path[len("/internal/in/"):], self._body()), {"ok": True})[1])
        self._send(404, {"error": "not found"})


def main() -> None:
    global state
    logging.basicConfig(level="INFO", format="slotd %(levelname)s %(message)s")
    if not SECRET:
        sys.exit("SLOT_SECRET is required")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: os._exit(0))  # children die with the container
    srv = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()  # health answers "booting" meanwhile
    try:
        with lock:
            boot_slot()
    except BaseException:  # noqa: BLE001
        log.error("boot failed; the gateway's reset replaces this slot")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
