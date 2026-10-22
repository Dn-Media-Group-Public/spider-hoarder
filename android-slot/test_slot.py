"""Host-runnable checks for the Android slot: the firewall script's rules (with a fake iptables), slotd's file
handling and auth. The emulator itself is exercised by the e2e smoke test on a KVM host."""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
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


if __name__ == "__main__":
    test_firewall_clear()
    test_firewall_tor()
    test_slotd_files_and_auth()
    test_watchdog_marks_dead_emulator_broken()
    print("ok")
