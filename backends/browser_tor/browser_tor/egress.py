"""Tor egress: sessions can reach only their own SocksPort of this backend's Tor daemon (stream isolation per port)."""

import os

from spider_hoarder_browser_sdk import ports
from spider_hoarder_browser_sdk.egress import Egress

NOTE = (
    "TOR: all traffic is routed through the Tor network, so the destination does not see this server's IP. "
    "Use it for .onion sites, for sensitive or politically exposed research where the request origin must "
    "not be traceable, or when direct access is blocked or geo-restricted. "
    "Slower, and many sites show CAPTCHAs or block Tor exit nodes; use browser_request_human_help for those.")


class TorEgress(Egress):
    name = "tor"
    media_default = False
    note = NOTE

    def __init__(self, router_host: str = "browser-tor-daemon"):
        self.router_host = router_host
        # Replica `REPLICA_SLOT` (1..MAX_REPLICAS, Swarm's {{.Task.Slot}}) gets its own slice of the daemon's SocksPorts: slot 1 is
        # exactly the single-container layout. Same formula as daemon/entrypoint.sh.
        slot, max_replicas = int(os.environ.get("REPLICA_SLOT", "1")), int(os.environ.get("MAX_REPLICAS", "4"))
        assert 1 <= slot <= max_replicas, f"REPLICA_SLOT {slot} outside 1..MAX_REPLICAS ({max_replicas}); raise MAX_REPLICAS and redeploy the Tor daemon"
        self.socks_base = 9050 + (slot - 1) * (ports.POOL_SIZE + 1)  # + the gateway's own port
