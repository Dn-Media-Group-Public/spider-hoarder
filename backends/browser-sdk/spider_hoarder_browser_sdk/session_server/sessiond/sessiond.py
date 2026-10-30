"""sessiond: the only root process. Owns the uid pool's invariants.

Fixed verbs over /run/sessiond.sock (gateway uid only), one JSON line each:
  {"op":"start","uid":N,"env":{...},"files":{"name":"content"}}
  {"op":"stop","uid":N}
  {"op":"reset"}   (stop every pool uid; gateway start = crash recovery)
  {"op":"status"}
A uid is `free`, `busy` or `quarantined` (never reused). See
docs/implemented-plans/browser-sessions-plan.md, "The uid lifecycle".
"""

import json
import logging
import os
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time

from spider_hoarder_browser_sdk import ports

log = logging.getLogger("sessiond")

SOCKET = os.environ.get("SESSIOND_SOCKET", "/run/sessiond.sock")
GW_UID = int(os.environ.get("GW_UID", "1000"))
GW_COLLECT_GID = int(os.environ.get("GW_COLLECT_GID", "1500"))  # gateway is a member; sessions are not
SESSION_GROUP = os.environ.get("SESSION_GROUP", os.path.join(os.path.dirname(__file__), "..", "session", "session-group.sh"))
WIPE_ROOTS = ["/home", "/tmp", "/var/tmp", "/dev/shm", "/run"]
NPROC = os.environ.get("SESSION_NPROC", "1024")  # Chromium's process tree is hundreds
NOFILE = os.environ.get("SESSION_NOFILE", "8192")
ENV_KEYS = {"UPSTREAM_SOCKS", "ALLOW_SUFFIX"}  # the only gateway-supplied env a session may get

FREE, BUSY, QUARANTINED = "free", "busy", "quarantined"
state: dict[int, str] = {}
procs: dict[int, subprocess.Popen] = {}
lock = threading.Lock()  # ponytail: one lock for all verbs; stop takes seconds, per-uid locks if it matters


def uid_procs(uid: int) -> list[int]:
    """Live (non-zombie) pids with real/effective/saved uid == uid."""
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


def owned_paths(uid: int, roots=None) -> list[str]:
    """Top-most entries owned by uid under the wipe roots (not crossing mounts)."""
    found = []
    for root in roots or WIPE_ROOTS:
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
                if st.st_dev != dev:
                    continue
                if st.st_uid == uid:
                    found.append(e.path)
                elif stat.S_ISDIR(st.st_mode):
                    stack.append(e.path)
    return found


def _rm(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _kill_all(uid: int) -> None:
    # setuid to the uid and kill(-1): reaches anything that left the process group
    subprocess.run(["/bin/sh", "-c", "kill -9 -1"], user=uid, group=uid, extra_groups=[],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def _stop(uid: int, roots=None) -> bool:
    """Kill, wipe, verify. True only if the uid is provably clean."""
    try:
        p = procs.pop(uid, None)
        if p:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait(timeout=10)
        for _ in range(20):  # loop: a fork bomb may win a single sweep
            _kill_all(uid)
            time.sleep(0.1)
            if not uid_procs(uid):
                break
        for path in owned_paths(uid, roots):
            _rm(path)
        return not uid_procs(uid) and not owned_paths(uid, roots)
    except Exception:
        log.exception("stop(%s) failed", uid)
        return False


def stop(uid: int) -> None:
    if state.get(uid) == QUARANTINED:
        raise RuntimeError("quarantined")
    if _stop(uid):
        state[uid] = FREE
    else:
        state[uid] = QUARANTINED
        log.error("QUARANTINE uid %s: not provably clean, capacity shrinks until restart", uid)
        raise RuntimeError("stop failed, uid quarantined")


def start(uid: int, env: dict, files: dict) -> None:
    if state.get(uid) != FREE:
        raise RuntimeError(f"uid not free ({state.get(uid)})")
    home = ports.home(uid)
    if uid_procs(uid) or os.path.lexists(home):
        state[uid] = QUARANTINED
        log.error("QUARANTINE uid %s: dirty at start", uid)
        raise RuntimeError("uid dirty at start, quarantined")
    state[uid] = BUSY
    try:
        os.mkdir(home)
        os.chown(home, uid, GW_COLLECT_GID)
        os.chmod(home, 0o710)  # gateway may traverse to out/, not list the home
        out = os.path.join(home, "out")  # file outputs the gateway copies into blob storage
        os.mkdir(out)
        os.chmod(out, 0o2750)  # setgid: files created inside inherit gw-collect. Before the chown: while out/ is still in
        os.chown(out, uid, GW_COLLECT_GID)  # our own group the bit sticks (no CAP_FSETID needed); chown keeps it on directories
        inn = os.path.join(home, "in")  # files the gateway stages for the session (browser_network_request_make)
        os.mkdir(inn)
        os.chmod(inn, 0o2770)  # the gateway (gw-collect) adds files; the session owns the dir, so it can read and delete them
        os.chown(inn, uid, GW_COLLECT_GID)
        for name, content in files.items():
            if "/" in name or name.startswith("."):
                raise ValueError(f"bad file name {name!r}")
            path = os.path.join(home, name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.chown(path, uid, uid)
        child_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8",
            "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"),
            "DISPLAY_NUM": str(ports.display(uid)), "VNC_PORT": str(ports.vnc_port(uid)),
            "WS_PORT": str(ports.ws_port(uid)), "MCP_PORT": str(ports.mcp_port(uid)), "FILTER_PORT": str(ports.filter_port(uid)),
            **{k: v for k, v in env.items() if k in ENV_KEYS},
        }
        procs[uid] = subprocess.Popen(
            ["prlimit", f"--nproc={NPROC}", f"--nofile={NOFILE}", SESSION_GROUP],
            env=child_env, cwd=home, user=uid, group=uid, extra_groups=[], umask=0o027,
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=2, stderr=2,
        )
        log.info("started uid %s", uid)
    except Exception:
        try:
            stop(uid)
        except RuntimeError:
            pass
        raise


def reset_all() -> None:
    for uid in ports.UIDS:
        state[uid] = FREE
        try:
            stop(uid)
        except RuntimeError:
            pass


def handle(req: dict) -> dict:
    op = req.get("op")
    with lock:
        if op == "reset":
            reset_all()
            return {"ok": True}
        if op == "status":
            return {"ok": True, "state": {str(u): s for u, s in state.items()}}
        uid = req.get("uid")
        if uid not in ports.UIDS:
            return {"ok": False, "error": "bad uid"}
        try:
            if op == "start":
                start(uid, req.get("env", {}), req.get("files", {}))
            elif op == "stop":
                stop(uid)
            else:
                return {"ok": False, "error": "bad op"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
    return {"ok": True}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        _, _, peer_uid = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer_uid not in (0, GW_UID):
            return
        try:
            resp = handle(json.loads(self.rfile.readline()))
        except Exception as e:  # noqa: BLE001
            resp = {"ok": False, "error": str(e)}
        self.wfile.write(json.dumps(resp).encode() + b"\n")


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    logging.basicConfig(level="INFO", format="sessiond %(levelname)s %(message)s")
    reset_all()
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    old = os.umask(0o177)
    srv = Server(SOCKET, Handler)
    os.umask(old)
    os.chown(SOCKET, GW_UID, GW_UID)
    log.info("ready: uids %s-%s", ports.UIDS[0], ports.UIDS[-1])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    srv.serve_forever()


if __name__ == "__main__":
    main()
