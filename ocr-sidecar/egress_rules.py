"""Egress rules of the OCR sidecar: pinned to the configured profile endpoints (and the metadata server only when a `google` profile uses
it), not the proxy's "public internet only" rules, so a LAN-local profile is possible. Resolved at start; a profile whose address changes
needs a restart. Own file, not shared with blob-sidecar/egress_rules.py (docs/plans/parser-wrapper.md). Pure: tests compare the commands."""


def rules(uid: int, resolvers: list[str], endpoints: list[tuple[str, int]]) -> list[list[str]]:
    own = ["-m", "owner", "--uid-owner", str(uid)]
    cmds = [[t, "-F", "OUTPUT"] for t in ("iptables", "ip6tables")]
    cmds += [[t, "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"] for t in ("iptables", "ip6tables")]
    for n in resolvers:
        if n == "127.0.0.11":  # Docker's embedded DNS: NAT rewrites the port before this table sees the packet, so any port
            cmds.append(["iptables", "-A", "OUTPUT", *own, "-d", n, "-j", "ACCEPT"])
            continue
        for proto in ("udp", "tcp"):
            cmds.append(["iptables", "-A", "OUTPUT", *own, "-d", n, "-p", proto, "--dport", "53", "-j", "ACCEPT"])
    for ip, port in endpoints:
        cmds.append(["iptables", "-A", "OUTPUT", *own, "-d", ip, "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"])
    cmds.append(["iptables", "-A", "OUTPUT", *own, "-j", "REJECT"])
    cmds.append(["ip6tables", "-A", "OUTPUT", *own, "-j", "REJECT"])
    return cmds


def endpoints(profiles: dict, resolve) -> list[tuple[str, int]]:
    """(ip, port) of every profile's base_url, plus the metadata server for `google` profiles; `resolve(host) -> ip`."""
    from urllib.parse import urlparse
    out = []
    for p in profiles.values():
        u = urlparse(p["base_url"])
        out.append((resolve(u.hostname), u.port or (443 if u.scheme == "https" else 80)))
        if p.get("auth") == "google":
            out.append(("169.254.169.254", 80))
    return list(dict.fromkeys(out))
