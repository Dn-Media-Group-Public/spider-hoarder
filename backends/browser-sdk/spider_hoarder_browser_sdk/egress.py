"""Egress policy of a browser backend: the one place that says how its sessions reach the network. A `browser_*`
backend package supplies one object (a subclass of Egress) to BrowserDriver; the driver, the start script (firewall) and
the gateway's own downloads all go through it, so no code branches on the egress kind.

  clear   sessions go straight to the public internet (the uid firewall keeps them off private ranges)
  tor     sessions can reach only their own SocksPort of the backend's Tor daemon
  i2p     sessions can reach only their own SocksPort of the replica's i2pd router, and only *.i2p names
"""

import socket

from spider_hoarder_browser_sdk import netguard, ports


class Egress:
    name = ""                 # short name: clear | tor | i2p (policy summaries, descriptions)
    media_default = True      # open_session's default for `media`
    note = ""                 # LLM-readable note about this egress, in the instructions and tool descriptions
    router_host: str | None = None   # the SOCKS router sessions go through (None: direct)
    socks_base = 0            # the router's first SocksPort for this replica; session uid index i uses socks_base + i
    allow_suffix = ""         # only names with this suffix are reachable (the filter relay refuses everything else)

    # ---- SOCKS layout
    @property
    def proxied(self) -> bool:
        return self.router_host is not None

    def socks_port(self, uid: int) -> int:
        return self.socks_base + ports.index(uid)

    @property
    def gateway_socks_port(self) -> int:
        """The gateway's own `requests` get a SocksPort of their own (own circuits)."""
        return self.socks_base + ports.POOL_SIZE

    # ---- session start (gateway side)
    def session_env(self, uid: int) -> dict:
        """The only gateway-supplied env a session may get (sessiond.ENV_KEYS)."""
        if not self.proxied:
            return {}
        env = {"UPSTREAM_SOCKS": f"{self.router_host}:{self.socks_port(uid)}"}
        if self.allow_suffix:
            env["ALLOW_SUFFIX"] = self.allow_suffix
        return env

    async def ready(self, uid: int) -> None:
        """Raise RuntimeError if a session started now could not reach its network yet (readiness probe)."""

    # ---- firewall (start script, as root)
    def firewall_rules(self, uid_range: str) -> list[list[str]]:
        """iptables arguments (after `iptables`) appended to OUTPUT after the per-uid loopback/router rules and before
        the final REJECT of the session uids. uid_range is `first-last`."""
        return []

    watch_router = False      # the rules pin the router's IP: restart this replica if it changes (i2p: one router per replica)
    _pinned = ""

    def router_pinned(self, ip: str) -> None:
        self._pinned = ip

    def router_moved(self) -> bool:
        """Polled by the start script when watch_router: True = the firewall rules are stale, restart."""
        ip = resolve_v4(self.router_host) if self.proxied else ""
        return bool(ip) and ip != self._pinned

    # ---- the gateway's own downloads (ingest_page, content type checks)
    def request(self, method: str, url: str, socks_port: int | None = None, **kw):
        """A `requests` call through this egress; `socks_port` = a session's own port (same circuit / destination)."""
        import requests
        if not self.proxied:
            return netguard.guarded_request(method, url, **kw)
        p = f"socks5h://{self.router_host}:{socks_port or self.gateway_socks_port}"
        return requests.request(method, url, proxies={"http": p, "https": p}, **kw)


def resolve_v4(host: str) -> str:
    """First IPv4 address of host, "" if it does not resolve (yet)."""
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except OSError:
        return ""
