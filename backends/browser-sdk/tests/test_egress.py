"""The Egress base class: SOCKS layout, session env, firewall rule list, and start.py being safe to import."""

import importlib

from spider_hoarder_browser_sdk import firewall, ports
from spider_hoarder_browser_sdk.egress import Egress


class Direct(Egress):
    name = "clear"


class Routed(Egress):
    name = "tor"
    router_host = "router.test"
    socks_base = 9100
    allow_suffix = ".x"


def test_direct_has_no_socks_and_no_session_env():
    e = Direct()
    assert not e.proxied and e.session_env(ports.UIDS[0]) == {}


def test_socks_layout_and_session_env():
    e = Routed()
    u0, u3 = ports.UIDS[0], ports.UIDS[3]
    assert e.socks_port(u0) == 9100 and e.socks_port(u3) == 9103
    assert e.gateway_socks_port == 9100 + ports.POOL_SIZE
    assert e.session_env(u3) == {"UPSTREAM_SOCKS": "router.test:9103", "ALLOW_SUFFIX": ".x"}


def test_router_moved_only_when_ip_changes(monkeypatch):
    import spider_hoarder_browser_sdk.egress as m
    e = Routed()
    e.router_pinned("10.0.0.1")
    monkeypatch.setattr(m, "resolve_v4", lambda h: "10.0.0.1")
    assert not e.router_moved()
    monkeypatch.setattr(m, "resolve_v4", lambda h: "")
    assert not e.router_moved()  # unresolvable for a moment is not a move
    monkeypatch.setattr(m, "resolve_v4", lambda h: "10.0.0.9")
    assert e.router_moved()


def test_base_firewall_rules_structure():
    cmds = firewall.rules(Routed(), "10.0.0.1")
    rng = f"{ports.UID_BASE}-{ports.UID_BASE + ports.POOL_SIZE - 1}"
    assert cmds[0] == ["iptables", "-F", "OUTPUT"] and ["ip6tables", "-F", "OUTPUT"] in cmds
    assert cmds[-2] == ["iptables", "-A", "OUTPUT", "-m", "owner", "--uid-owner", rng, "-j", "REJECT"]
    assert cmds[-1] == ["ip6tables", "-A", "OUTPUT", "-m", "owner", "--uid-owner", rng, "-j", "REJECT"]
    router = [c for c in cmds if "10.0.0.1" in c]
    assert len(router) == ports.POOL_SIZE
    assert " ".join(router[1]) == f"iptables -A OUTPUT -m owner --uid-owner {ports.UID_BASE + 1} -d 10.0.0.1 -p tcp --dport 9101 -j ACCEPT"
    assert all(c[0] == "iptables" for c in router)


def test_direct_firewall_has_no_router_rules():
    cmds = firewall.rules(Direct(), "")
    assert not any("-d" in c and c[0] == "iptables" and "127.0.0.11" not in c for c in cmds)  # only loopback + resolver rules per uid


def test_start_is_importable_without_side_effects():
    m = importlib.import_module("spider_hoarder_browser_sdk.start")
    assert callable(m.main) and callable(m.watch_router)
