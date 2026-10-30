import os
import subprocess
import sys

import pytest

from spider_hoarder_browser_sdk import firewall, ports


def test_router_per_replica_slot_and_ports():
    code = ("from spider_hoarder_browser_sdk import ports; from browser_i2p.egress import I2pEgress; e = I2pEgress();"
            "print(e.router_host, e.socks_port(ports.UIDS[0]), e.gateway_socks_port)")
    env = {**os.environ, "REPLICA_SLOT": "7", "I2P_SOCKS_BASE": "4500", "MAX_SESSIONS_TOTAL": "18"}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout.split()
    assert out == ["browser-i2p-router-7", "4500", "4520"]


def test_i2p_session_env_firewall_and_request():
    from browser_i2p.egress import I2pEgress
    e = I2pEgress()
    assert e.allow_suffix == ".i2p" and e.watch_router and not e.media_default
    assert e.session_env(ports.UIDS[1])["ALLOW_SUFFIX"] == ".i2p" and e.session_env(ports.UIDS[1])["UPSTREAM_SOCKS"].startswith("browser-i2p-router-")
    flat = [" ".join(c) for c in firewall.rules(e, "10.1.1.1")]
    assert f"iptables -A OUTPUT -m owner --uid-owner {ports.UID_BASE} -d 10.1.1.1 -p tcp --dport {e.socks_base} -j ACCEPT" in flat
    assert flat[-1].startswith("ip6tables") and flat[-2].endswith("-j REJECT")
    for url in ("http://example.org/", "http://x.onion/", "http://1.2.3.4/"):
        with pytest.raises(ValueError, match=r"only \*\.i2p"):
            e.request("GET", url)
