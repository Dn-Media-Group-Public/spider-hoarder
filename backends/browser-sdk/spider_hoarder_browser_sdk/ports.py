"""Everything derived from the pool size, in one place (gateway, sessiond, the firewall and the egress helpers all
agree on it): uid -> index -> ports. The router's SocksPort layout is the egress policy's (egress.py)."""

import os

UID_BASE = int(os.environ.get("UID_BASE", "20000"))
MAX_SESSIONS_TOTAL = int(os.environ.get("MAX_SESSIONS_TOTAL", "18"))
POOL_SIZE = MAX_SESSIONS_TOTAL + 2  # + spare for ingest_page / tool-list probe
assert POOL_SIZE <= 50, "port ranges overlap above 50 sessions"

UIDS = range(UID_BASE, UID_BASE + POOL_SIZE)


def index(uid: int) -> int:
    assert uid in UIDS, uid
    return uid - UID_BASE


def display(uid): return index(uid) + 10
def vnc_port(uid): return 5900 + index(uid)
def ws_port(uid): return 6080 + index(uid)
def mcp_port(uid): return 9000 + index(uid)
def filter_port(uid): return 9100 + index(uid)


def home(uid: int) -> str:
    return f"/home/s{uid}"
