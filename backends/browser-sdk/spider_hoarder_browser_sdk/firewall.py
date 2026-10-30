"""The session-uid firewall, built from the backend's egress policy and applied as root at container start (before
sessiond and the gateway drop the capability). Per session uid: only its own loopback ports (so sessions cannot reach the
gateway or each other), replies on accepted connections, the Docker resolver, its own SocksPort of the router, and
whatever the egress policy adds; everything else is rejected. ponytail: IPv6 is rejected outright for session uids; add
rules if the compose networks ever get v6."""

import subprocess

from spider_hoarder_browser_sdk import ports
from spider_hoarder_browser_sdk.egress import Egress, resolve_v4


def rules(egress: Egress, router_ip: str = "") -> list[list[str]]:
    """The full command list, each `[binary, args...]`, in order (pure: tests run it against expected rules)."""
    cmds = []
    for t in ("iptables", "ip6tables"):
        cmds += [[t, "-F", "OUTPUT"], [t, "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"]]
    for i in range(ports.POOL_SIZE):
        u = str(ports.UID_BASE + i)
        owner = ["-m", "owner", "--uid-owner", u]
        cmds.append(["iptables", "-A", "OUTPUT", *owner, "-o", "lo", "-p", "tcp", "-m", "multiport", "--dports", f"{5900 + i},{9100 + i}", "-j", "ACCEPT"])
        cmds.append(["iptables", "-A", "OUTPUT", *owner, "-o", "lo", "-d", "127.0.0.11", "-j", "ACCEPT"])
        if egress.proxied:
            cmds.append(["iptables", "-A", "OUTPUT", *owner, "-d", router_ip, "-p", "tcp", "--dport", str(egress.socks_base + i), "-j", "ACCEPT"])
    rng = f"{ports.UID_BASE}-{ports.UID_BASE + ports.POOL_SIZE - 1}"
    cmds += [["iptables", *r] for r in egress.firewall_rules(rng)]
    cmds.append(["iptables", "-A", "OUTPUT", "-m", "owner", "--uid-owner", rng, "-j", "REJECT"])
    cmds.append(["ip6tables", "-A", "OUTPUT", "-m", "owner", "--uid-owner", rng, "-j", "REJECT"])
    return cmds


def apply(egress: Egress, wait=lambda s: __import__("time").sleep(s)) -> str:
    """Wait for the router to resolve (it may still be starting), then install the rules. Returns the router's IP."""
    router_ip = ""
    if egress.proxied:
        while not (router_ip := resolve_v4(egress.router_host)):
            wait(2)
    for cmd in rules(egress, router_ip):
        subprocess.run(cmd, check=True)
    return router_ip
