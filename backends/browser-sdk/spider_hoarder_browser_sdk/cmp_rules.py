"""Chromium --host-rules value that blackholes known cookie-consent (CMP)
vendors, from Fanboy's Cookiemonster list fetched at image build time
(cmp_blocklist.txt). Only host-wide network rules (``||host^``) are kept; path- or domain=-scoped ones are skipped."""

import os

PATH = os.environ.get("CMP_BLOCKLIST_PATH", os.path.join(os.path.dirname(__file__), "cmp_blocklist.txt"))


def hosts(path: str = PATH) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    out = set()
    for line in lines:
        line = line.strip()
        if line.startswith("||") and "#" not in line:
            host = line[2:].split("^", 1)[0].split("$", 1)[0]
            # host-wide rules only: a path (||alicdn.com/cookie-consent-sdk/) or domain= scope can't be
            # expressed as a host block, and would blackhole the whole site (alicdn.com, googletagmanager.com)
            if host and "*" not in host and "/" not in host and "domain=" not in line:
                out.add(host.lower())
    return sorted(out)


def host_rules(path: str = PATH) -> str:
    return ", ".join(f"MAP {h} ~NOTFOUND, MAP *.{h} ~NOTFOUND" for h in hosts(path))
