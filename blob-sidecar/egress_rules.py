"""Egress rules of the blob-sidecar (squid): public internet only. COPIED from backends/browser_clear/browser_clear/egress.py
(ClearEgress.firewall_rules) and backends/browser-sdk/spider_hoarder_browser_sdk/firewall.py on 2026-11-12, deliberately not imported:
the two are allowed to drift (docs/plans/parser-wrapper.md, "The proxy").

Rules apply to the proxy's own uid. `allow` (PARSE_PROXY_ALLOW, empty in production) are (ip, port) pairs accepted *before* the
rejects: in dev the SeaweedFS endpoint, exactly that address and port and not its whole private range. Rules act at connect time, so
DNS rebinding cannot get around them. Pure function: tests compare the command list."""


def rules(uid: int, resolvers: list[str], allow: list[tuple[str, int]]) -> list[list[str]]:
    own = ["-m", "owner", "--uid-owner", str(uid)]
    cmds = [[t, "-F", "OUTPUT"] for t in ("iptables", "ip6tables")]
    cmds += [[t, "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"] for t in ("iptables", "ip6tables")]
    # squid's own helper channel is UDP on loopback (v4 and v6); the container runs nothing else there, so a request for 127.x only reaches squid itself
    cmds.append(["iptables", "-A", "OUTPUT", *own, "-o", "lo", "-j", "ACCEPT"])
    for n in resolvers:  # the configured resolvers (private IPs on some hosts) stay reachable
        for proto in ("udp", "tcp"):
            cmds.append(["iptables", "-A", "OUTPUT", *own, "-d", n, "-p", proto, "--dport", "53", "-j", "ACCEPT"])
    for ip, port in allow:
        cmds.append(["iptables", "-A", "OUTPUT", *own, "-d", ip, "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"])
    # public internet only; no RFC1918, link-local (the metadata server), CGNAT, loopback, no other stack service
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "127.0.0.0/8"):
        cmds.append(["iptables", "-A", "OUTPUT", *own, "-d", net, "-j", "REJECT"])
    cmds.append(["iptables", "-A", "OUTPUT", *own, "-j", "ACCEPT"])
    cmds.insert(cmds.index(["iptables", "-A", "OUTPUT", *own, "-o", "lo", "-j", "ACCEPT"]) + 1, ["ip6tables", "-A", "OUTPUT", *own, "-o", "lo", "-j", "ACCEPT"])
    cmds.append(["ip6tables", "-A", "OUTPUT", *own, "-j", "REJECT"])  # ponytail: no IPv6 on the stack networks
    return cmds
