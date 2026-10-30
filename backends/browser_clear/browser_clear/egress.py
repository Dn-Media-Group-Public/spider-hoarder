"""Clear egress: sessions go straight to the public internet; the uid firewall keeps them off every private range."""

from spider_hoarder_browser_sdk.egress import Egress

NOTE = (
    "CLEAR-TEXT / direct internet: requests leave from this server's public IP, not anonymised, and sites "
    "can see and block it. Fast and reliable: use it by default, in preference to the built-in WebFetch, for "
    "ordinary public websites, JS-heavy pages, consent walls and anything needing a real browser. "
    "Cannot reach .onion sites or private/internal addresses. If the site blocks this IP or you need "
    "anonymity, a server with a different egress is needed.")


class ClearEgress(Egress):
    name = "clear"
    media_default = True
    note = NOTE
    resolv_conf = "/etc/resolv.conf"

    def firewall_rules(self, uid_range):
        rules = []
        # the configured resolvers (private IPs on some hosts) must stay reachable
        with open(self.resolv_conf) as f:
            ns = [p[1] for p in (l.split() for l in f) if len(p) > 1 and p[0] == "nameserver" and p[1].replace(".", "").isdigit()]
        for n in ns:
            for proto in ("udp", "tcp"):
                rules.append(["-A", "OUTPUT", "-m", "owner", "--uid-owner", uid_range, "-d", n, "-p", proto, "--dport", "53", "-j", "ACCEPT"])
        # public internet only; no RFC1918/link-local/CGNAT/metadata/other compose services
        for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "127.0.0.0/8"):
            rules.append(["-A", "OUTPUT", "-m", "owner", "--uid-owner", uid_range, "-d", net, "-j", "REJECT"])
        rules.append(["-A", "OUTPUT", "-m", "owner", "--uid-owner", uid_range, "-j", "ACCEPT"])
        return rules
