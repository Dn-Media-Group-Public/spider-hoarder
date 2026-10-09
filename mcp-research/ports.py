"""Everything derived from the pool size, in one place (gateway, sessiond,
entrypoint firewall and torrc all agree on it): uid -> index -> ports."""

import os

UID_BASE = int(os.environ.get("UID_BASE", "20000"))
MAX_SESSIONS_TOTAL = int(os.environ.get("MAX_SESSIONS_TOTAL", "18"))
POOL_SIZE = MAX_SESSIONS_TOTAL + 2  # + spare for ingest_page / tool-list probe
assert POOL_SIZE <= 50, "port ranges overlap above 50 sessions"

# Replica `REPLICA_SLOT` (1..MAX_REPLICAS, Swarm's {{.Task.Slot}}) of this egress service gets its own
# slice of the shared `tor` service's SocksPorts: slot 1 is exactly the single-container layout.
MAX_REPLICAS = int(os.environ.get("MAX_REPLICAS", "4"))
SLOT = int(os.environ.get("REPLICA_SLOT", "1"))
assert 1 <= SLOT <= MAX_REPLICAS, f"REPLICA_SLOT {SLOT} outside 1..MAX_REPLICAS ({MAX_REPLICAS}); raise MAX_REPLICAS and redeploy tor"
SOCKS_STRIDE = POOL_SIZE + 1  # + the gateway's own port
SOCKS_BASE = 9050 + (SLOT - 1) * SOCKS_STRIDE

UIDS = range(UID_BASE, UID_BASE + POOL_SIZE)


def index(uid: int) -> int:
    assert uid in UIDS, uid
    return uid - UID_BASE


def display(uid): return index(uid) + 10
def vnc_port(uid): return 5900 + index(uid)
def ws_port(uid): return 6080 + index(uid)
def mcp_port(uid): return 9000 + index(uid)
def filter_port(uid): return 9100 + index(uid)
def socks_port(uid): return SOCKS_BASE + index(uid)


# The gateway's own `requests` get a SocksPort of their own (own Tor circuits).
GATEWAY_SOCKS_PORT = SOCKS_BASE + POOL_SIZE


def home(uid: int) -> str:
    return f"/home/s{uid}"
