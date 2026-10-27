"""Host-runnable checks for the I2P egress: per-slot router pairing and the *.i2p-only filter."""

import os
import subprocess
import sys


def test_i2p_ports():
    code = "import ports; print(ports.SOCKS_HOST, ports.socks_port(ports.UIDS[0]), ports.GATEWAY_SOCKS_PORT)"
    env = {**os.environ, "EGRESS": "i2p", "REPLICA_SLOT": "7", "I2P_SOCKS_BASE": "4500", "MAX_SESSIONS_TOTAL": "18"}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=os.path.dirname(__file__) or ".")
    assert r.stdout.split() == ["i2pd-7", "4500", "4520"], r.stderr  # slot 7 > MAX_REPLICAS is fine here


def test_filter_i2p_only(monkeypatch):
    import filter_proxy
    assert filter_proxy.is_allowed("example.com", 3)  # no suffix configured = Tor behaviour
    monkeypatch.setattr(filter_proxy, "ALLOW_SUFFIX", ".i2p")
    assert filter_proxy.is_allowed("Stats.I2P", 3) and filter_proxy.is_allowed("abc.b32.i2p.", 3)
    assert not filter_proxy.is_allowed("example.com", 3)
    assert not filter_proxy.is_allowed("evili2p", 3) and not filter_proxy.is_allowed("1.2.3.4", 1)
