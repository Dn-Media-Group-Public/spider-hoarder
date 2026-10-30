"""I2P egress: sessions can reach only their own SocksPort of the replica's i2pd router, and only *.i2p names."""

import asyncio
import os
import time
from urllib.parse import urlsplit

import requests

from spider_hoarder_browser_sdk.egress import Egress

NOTE = (
    "I2P: traffic goes through the I2P network and ONLY *.i2p addresses (eepsites, .b32.i2p) are reachable; "
    "clearnet and .onion sites are not. Use it for I2P-hosted content. Slow: the first request on a fresh "
    "session can take a minute while tunnels are built, and eepsites are often offline.")


class I2pEgress(Egress):
    name = "i2p"
    media_default = False
    note = NOTE
    allow_suffix = ".i2p"
    watch_router = True   # the firewall rules pin the router's IP: if it moves, restart this replica

    def __init__(self):
        slot = int(os.environ.get("REPLICA_SLOT", "1"))
        assert slot >= 1, f"REPLICA_SLOT {slot}"
        # slot k pairs with its own router `browser-i2p-router-k` (one router per replica, no stride, no MAX_REPLICAS)
        self.router_host = f"browser-i2p-router-{slot}"
        self.socks_base = int(os.environ.get("I2P_SOCKS_BASE", "4500"))
        self.probe_host = os.environ.get("I2P_PROBE_HOST", "stats.i2p")  # any always-up eepsite

    async def ready(self, uid):
        if not await asyncio.to_thread(self.i2p_ready, self.socks_port(uid)):
            raise RuntimeError("I2P router/tunnels not ready yet; retry in a minute")

    def request(self, method, url, socks_port=None, **kw):
        if not (urlsplit(url).hostname or "").endswith(".i2p"):  # the router would refuse anyway; fail early and clearly
            raise ValueError("only *.i2p addresses are reachable here")
        kw["timeout"] = kw.get("timeout", 60) * 4  # eepsites are slow
        return super().request(method, url, socks_port, **kw)

    def i2p_ready(self, socks_port: int, wait: float = 240) -> bool:
        """A fresh session destination needs about a minute to build tunnels: retry until an eepsite answers."""
        deadline, delay = time.time() + wait, 5
        while True:
            try:
                self.request("HEAD", f"http://{self.probe_host}/", socks_port=socks_port, timeout=60 / 4)
                return True
            except requests.RequestException:
                if time.time() + delay > deadline:
                    return False
                time.sleep(delay)
                delay = min(delay * 2, 30)
