"""Host-runnable checks for the Android slot: the firewall script's rules (with a fake iptables), slotd's file
handling and auth. The emulator itself is exercised by the e2e smoke test on a KVM host."""

import io
import json
import os
import subprocess
import sys
import tempfile
import socket
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAKE = """#!/bin/sh
echo "$(basename "$0") $*" >> "$RULES"
"""


def firewall(egress, resolv="nameserver 127.0.0.11\nnameserver 10.1.2.3\nnameserver 2001:db8::1\n"):
    with tempfile.TemporaryDirectory() as d:
        for n in ("iptables", "ip6tables"):
            p = os.path.join(d, n)
            open(p, "w").write(FAKE)
            os.chmod(p, 0o755)
        rs = os.path.join(d, "resolv.conf")
        open(rs, "w").write(resolv)
        rules = os.path.join(d, "rules")
        env = {**os.environ, "EGRESS": egress, "EMU_UID": "2000", "SVC_UID": "3000", "RULES": rules, "RESOLV": rs,
               "IPT": os.path.join(d, "iptables"), "IPT6": os.path.join(d, "ip6tables")}
        subprocess.run(["sh", os.path.join(HERE, "firewall.sh")], env=env, check=True)
        return open(rules).read().splitlines()


def idx(rules, *needles):
    """Index of the first rule containing all needles (None if missing)."""
    for i, r in enumerate(rules):
        if all(n in r for n in needles):
            return i
    return None


def test_firewall_clear():
    r = firewall("clear")
    assert r[0] == "iptables -F OUTPUT" and any("ip6tables -F OUTPUT" in x for x in r)
    ok = idx(r, "uid-owner 2000", "-d 127.0.0.11", "ACCEPT")
    dns = idx(r, "uid-owner 2000", "-d 10.1.2.3", "--dport 53", "ACCEPT")
    loop = idx(r, "uid-owner 2000", "-d 127.0.0.0/8", "REJECT")
    rfc = idx(r, "uid-owner 2000", "-d 10.0.0.0/8", "REJECT")
    last = r.index("iptables -A OUTPUT -m owner --uid-owner 2000 -j ACCEPT")
    assert None not in (ok, dns, loop, rfc, last)
    assert ok < loop and dns < rfc < last  # exceptions first, then the rejects, then the public internet
    assert idx(r, "-d 10.192.0.0/10") is None  # tor's virtual range is a tor-only exception
    for net in ("172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10"):
        assert idx(r, "uid-owner 2000", f"-d {net}", "REJECT") is not None
    assert idx(r, "ip6tables", "uid-owner 2000", "REJECT") is not None
    assert idx(r, "ip6tables", "2001:db8") is None  # only v4 resolvers are allowed
    # svc uid: loopback only
    assert idx(r, "uid-owner 3000", "-o lo", "ACCEPT") < idx(r, "uid-owner 3000", "-j REJECT")


def test_firewall_tor():
    r = firewall("tor")
    virt = idx(r, "uid-owner 2000", "-d 10.192.0.0/10", "ACCEPT")
    rfc = idx(r, "uid-owner 2000", "-d 10.0.0.0/8", "REJECT")
    assert virt is not None and virt < rfc  # onion addresses accepted before the private ranges are rejected
    assert idx(r, "-d 127.0.0.11") is None and idx(r, "--dport 53") is None  # no resolver exceptions: tor answers DNS
    assert idx(r, "uid-owner 2000", "-d 127.0.0.0/8", "REJECT") is not None


def test_slotd_files_and_auth():
    os.environ["SLOT_SECRET"] = "s3cret"
    import slotd
    with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as inp:
        slotd.OUT, slotd.IN = out, inp
        slotd.SVC_UID, slotd.SVC_GID = os.getuid(), os.getgid()
        # read_out: regular files only, no symlinks, no odd names; read once
        open(os.path.join(out, "a.png"), "wb").write(b"png")
        assert slotd.read_out("a.png") == b"png" and not os.path.exists(os.path.join(out, "a.png"))
        os.symlink("/etc/passwd", os.path.join(out, "link"))
        os.mkfifo(os.path.join(out, "fifo"))
        for bad in ("link", "fifo", "../x", ".hidden", "a/b", "missing"):
            try:
                slotd.read_out(bad)
                assert False, bad
            except (ValueError, OSError):
                pass
        slotd.MAX_FILE_BYTES = 4
        open(os.path.join(out, "big"), "wb").write(b"12345")
        try:
            slotd.read_out("big")
            assert False
        except ValueError:
            pass
        slotd.MAX_FILE_BYTES = 1000
        # write_in: exclusive, no overwrite, no symlink
        slotd.write_in("x.apk", b"apk")
        assert open(os.path.join(inp, "x.apk"), "rb").read() == b"apk"
        for bad in ("x.apk", "../y", ".z"):
            try:
                slotd.write_in(bad, b"")
                assert False, bad
            except (ValueError, OSError):
                pass

        srv = slotd.ThreadingHTTPServer(("127.0.0.1", 0), slotd.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(path, method="GET", secret="s3cret", data=None):
            req = urllib.request.Request(base + path, method=method, data=data, headers={"X-Slot-Secret": secret} if secret is not None else {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        assert call("/internal/health", secret=None)[0] == 403
        assert call("/internal/health", secret="wrong")[0] == 403
        assert call("/internal/out/a", method="GET", secret="")[0] == 403
        code, body = call("/internal/health")
        assert code == 200 and json.loads(body)["state"] == slotd.BOOTING
        open(os.path.join(out, "b.txt"), "wb").write(b"hello")
        assert call("/internal/out/b.txt") == (200, b"hello")
        assert call("/internal/out/b.txt")[0] == 404  # read once
        assert call("/internal/in/q.apk", "PUT", data=b"zz")[0] == 200
        assert call("/internal/in/q.apk", "PUT", data=b"zz")[0] == 409  # exists
        assert call("/internal/nope")[0] == 404
        # a second session cannot start on a busy/non-free slot
        slotd.state = slotd.BUSY
        assert call("/internal/start", "POST", data=b'{"vnc_password": "abcd1234"}')[0] == 409
        slotd.state = slotd.FREE
        assert call("/internal/start", "POST", data=b'{"vnc_password": "bad pass word"}')[0] == 400
        srv.shutdown()


class _Proc:
    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode


def test_watchdog_marks_dead_emulator_broken():
    os.environ["SLOT_SECRET"] = "s3cret"
    import slotd
    slotd.procs.clear()
    slotd.procs.update(emulator=_Proc(), toolserver=_Proc())
    slotd.state = slotd.FREE
    slotd.check_alive()
    assert slotd.state == slotd.FREE
    slotd.procs["emulator"] = _Proc(-9)  # OOM-killed
    slotd.state = slotd.BUSY
    slotd.check_alive()
    assert slotd.state == slotd.BROKEN
    # a slot that is booting/resetting is not judged (the processes are being replaced)
    slotd.state = slotd.RESETTING
    slotd.check_alive()
    assert slotd.state == slotd.RESETTING
    slotd.procs.pop("emulator")
    slotd.state = slotd.FREE
    slotd.check_alive()
    assert slotd.state == slotd.BROKEN
    slotd.procs.clear()
    assert "emulator not running" in slotd.verify()["problems"]


def test_verify_names_sdcard_differences_and_baseline_listing():
    os.environ["SLOT_SECRET"] = "s3cret"
    import slotd
    with tempfile.TemporaryDirectory() as d:
        for n, text in (("baseline-packages.txt", "package:a\n"), ("baseline-sdcard.txt", "/sdcard/.nomedia\n/sdcard/gone\n")):
            open(os.path.join(d, n), "w").write(text)
        listing = ["/sdcard/.nomedia", "/sdcard/leak.txt", ""]  # `find /sdcard/` output is split on newlines
        answers = {"getprop": "1\n", "packages": "package:a\n", "find": "\n".join(listing), "account": "Accounts: 0\n"}

        def fake_adb(*a, **k):
            return next(v for key, v in answers.items() if key in a)
        saved = (slotd.AVD, slotd.adb, dict(slotd.procs))
        slotd.AVD, slotd.adb = d, fake_adb
        slotd.procs["emulator"] = _Proc()
        try:
            r = slotd.verify()
            assert not r["ok"] and len(r["problems"]) == 1, r
            assert "leak.txt" in r["problems"][0] and "/sdcard/gone" in r["problems"][0], r
            answers["find"] = "/sdcard/.nomedia\n/sdcard/gone\n"
            assert slotd.verify() == {"ok": True, "problems": []}
        finally:
            slotd.AVD, slotd.adb = saved[0], saved[1]
            slotd.procs.clear()
            slotd.procs.update(saved[2])


def test_ensure_network_toggles_wifi_when_gateway_stays_silent():
    os.environ["SLOT_SECRET"] = "s3cret"
    import slotd
    calls, state = [], {"up": False}
    saved = (slotd.adb, slotd.has_net, time.sleep, time.time)
    clock = [0.0]

    def fake_adb(*a, **k):
        calls.append(a[-2:])
        if a[-2:] == ("wifi", "enable"):
            state["up"] = True  # asking again gets an answer
        return ""
    slotd.adb, slotd.has_net = fake_adb, lambda: state["up"]
    time.sleep = lambda n: clock.__setitem__(0, clock[0] + n)
    time.time = lambda: clock[0]
    try:
        slotd.ensure_network()
    finally:
        slotd.adb, slotd.has_net, time.sleep, time.time = saved
    assert calls == [("wifi", "disable"), ("wifi", "enable")], calls
    # an address that is up from the start needs no toggling
    calls.clear()
    state["up"] = True
    clock[0] = 0.0
    slotd.adb, slotd.has_net, time.sleep, time.time = fake_adb, lambda: True, lambda n: clock.__setitem__(0, clock[0] + n), lambda: clock[0]
    try:
        slotd.ensure_network()
    finally:
        slotd.adb, slotd.has_net, time.sleep, time.time = saved
    assert calls == []


def test_console_py_auth_and_ko():
    import console
    with tempfile.TemporaryDirectory() as d:
        tok = os.path.join(d, "tok")
        open(tok, "w").write("sekrit\n")
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        seen = []

        def serve():
            for _ in range(2):
                c, _ = srv.accept()
                f = c.makefile("rw", newline="")
                f.write("Android Console\r\nOK\r\n")
                f.flush()
                auth = f.readline().strip()
                seen.append(auth)
                if auth != "auth sekrit":
                    f.write("KO: bad token\r\n")
                    f.flush()
                else:
                    f.write("OK\r\n")
                    f.flush()
                    cmd = f.readline().strip()
                    seen.append(cmd)
                    f.write("KO: bad sub-command\r\n" if cmd.startswith("bogus") else "snapshot saved\r\nOK\r\n")
                    f.flush()
                c.close()
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        port = srv.getsockname()[1]
        ok, out = console.run("avd snapshot save clean", port, tok, 5)
        assert ok and out == ["snapshot saved"], (ok, out)
        ok, out = console.run("bogus", port, tok, 5)
        assert not ok and out == ["KO: bad sub-command"], (ok, out)
        t.join(5)
        assert seen == ["auth sekrit", "avd snapshot save clean", "auth sekrit", "bogus"], seen


if __name__ == "__main__":
    test_firewall_clear()
    test_firewall_tor()
    test_slotd_files_and_auth()
    test_watchdog_marks_dead_emulator_broken()
    test_verify_names_sdcard_differences_and_baseline_listing()
    test_ensure_network_toggles_wifi_when_gateway_stays_silent()
    test_console_py_auth_and_ko()
    print("ok")
