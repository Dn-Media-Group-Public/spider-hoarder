"""Block known cookie-consent (CMP) vendor scripts at the network level.

The blocklist is Fanboy's Cookiemonster list, fetched once at image build
time into cmp_blocklist.txt (see Dockerfile) — no runtime network
dependency. We only care about its network-block rules (``||host^``);
cosmetic (``##``) and exception (``@@``) rules are irrelevant to us.
"""

import functools
import os
from urllib.parse import urlparse

BLOCKLIST_PATH = os.environ.get("CMP_BLOCKLIST_PATH", os.path.join(os.path.dirname(__file__), "cmp_blocklist.txt"))


@functools.cache
def blocked_hosts() -> frozenset[str]:
    hosts = set()
    try:
        with open(BLOCKLIST_PATH, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return frozenset()

    for line in lines:
        line = line.strip()
        if not line or line.startswith("!") or line.startswith("@@") or "#" in line:
            continue
        if line.startswith("||"):
            host = line[2:].split("^", 1)[0].split("/", 1)[0]
            if host:
                hosts.add(host.lower())
    return frozenset(hosts)


def should_block(url: str) -> bool:
    host = urlparse(url).hostname
    if not host:
        return False
    host = host.lower()
    hosts = blocked_hosts()
    return any(host == h or host.endswith("." + h) for h in hosts)
