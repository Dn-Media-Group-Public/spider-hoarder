import os
import subprocess
import sys

from spider_hoarder_browser_sdk import firewall, ports


def _layout(slot, max_replicas="4"):
    code = ("from spider_hoarder_browser_sdk import ports; from browser_tor.egress import TorEgress; e = TorEgress();"
            "print(e.router_host, e.socks_port(ports.UIDS[0]), e.socks_port(ports.UIDS[-1]), e.gateway_socks_port)")
    env = {**os.environ, "REPLICA_SLOT": str(slot), "MAX_REPLICAS": max_replicas, "MAX_SESSIONS_TOTAL": "18"}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    return r


def test_each_replica_gets_its_own_socks_port_slice():
    stride = 20 + 1  # POOL_SIZE + the gateway's own port
    assert _layout(1).stdout.split() == ["browser-tor-daemon", "9050", str(9050 + 19), str(9050 + 20)]
    assert _layout(3).stdout.split() == ["browser-tor-daemon", str(9050 + 2 * stride), str(9050 + 2 * stride + 19), str(9050 + 2 * stride + 20)]


def test_slot_outside_max_replicas_is_refused():
    r = _layout(5)
    assert r.returncode != 0 and "MAX_REPLICAS" in r.stderr


def test_tor_firewall_pins_the_router_per_uid():
    from browser_tor.egress import TorEgress
    e = TorEgress()
    assert e.proxied and not e.media_default and not e.watch_router
    cmds = firewall.rules(e, "10.9.8.7")
    flat = [" ".join(c) for c in cmds]
    for i in (0, 5, ports.POOL_SIZE - 1):
        assert (f"iptables -A OUTPUT -m owner --uid-owner {ports.UID_BASE + i} -d 10.9.8.7 -p tcp --dport {e.socks_base + i} -j ACCEPT") in flat
    rng = f"{ports.UID_BASE}-{ports.UID_BASE + ports.POOL_SIZE - 1}"
    assert flat[-2] == f"iptables -A OUTPUT -m owner --uid-owner {rng} -j REJECT" and flat[-1].startswith("ip6tables")
    assert not any("ACCEPT" in f and "-d 10." not in f and rng in f for f in flat)  # nothing opens the whole range
    assert e.session_env(ports.UIDS[2]) == {"UPSTREAM_SOCKS": f"browser-tor-daemon:{e.socks_base + 2}"}
