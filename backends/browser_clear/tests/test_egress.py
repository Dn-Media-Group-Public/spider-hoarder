import os

from spider_hoarder_browser_sdk import firewall, ports
from browser_clear.egress import ClearEgress


def test_clear_firewall_rules(tmp_path):
    rc = tmp_path / "resolv.conf"
    rc.write_text("nameserver 127.0.0.11\nnameserver 10.1.2.3\nnameserver 2001:db8::1\n")
    e = ClearEgress()
    e.resolv_conf = str(rc)
    assert e.media_default and not e.proxied and e.session_env(ports.UIDS[0]) == {}
    rng = f"{ports.UID_BASE}-{ports.UID_BASE + ports.POOL_SIZE - 1}"
    cmds = firewall.rules(e, "")
    flat = [" ".join(c) for c in cmds]
    own = f"-m owner --uid-owner {rng}"
    for ns in ("127.0.0.11", "10.1.2.3"):  # IPv4 resolvers stay reachable, over udp and tcp
        for p in ("udp", "tcp"):
            assert f"iptables -A OUTPUT {own} -d {ns} -p {p} --dport 53 -j ACCEPT" in flat
    assert not any("2001:db8" in f for f in flat)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "127.0.0.0/8"):
        assert f"iptables -A OUTPUT {own} -d {net} -j REJECT" in flat
    i_accept = flat.index(f"iptables -A OUTPUT {own} -j ACCEPT")
    assert i_accept < flat.index(f"iptables -A OUTPUT {own} -j REJECT") and i_accept > flat.index(f"iptables -A OUTPUT {own} -d 127.0.0.0/8 -j REJECT")
    assert flat[-1] == f"ip6tables -A OUTPUT {own} -j REJECT"
    # per uid: own loopback ports and the Docker resolver, no router rule
    assert f"iptables -A OUTPUT -m owner --uid-owner {ports.UID_BASE} -o lo -p tcp -m multiport --dports 5900,9100 -j ACCEPT" in flat
