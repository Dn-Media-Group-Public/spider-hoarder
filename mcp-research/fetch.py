"""Non-HTML downloads and the HTML-or-not preflight, via `requests` through the
same egress as the browsers: Tor SOCKS (socks5h, proxy resolves) or direct with
the SSRF guard. HTML itself is fetched by a browser session (see sessions.py)."""

import os
import time
from urllib.parse import urlsplit

import requests

import netguard
import ports

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
EGRESS = os.environ.get("EGRESS", "clear")
I2P_PROBE_HOST = os.environ.get("I2P_PROBE_HOST", "stats.i2p")  # any always-up eepsite


def _request(method: str, url: str, socks_port: int | None = None, cookies: dict | None = None, **kw) -> requests.Response:
    kw["headers"] = {"User-Agent": CHROME_UA, **kw.get("headers", {})}
    if cookies:
        kw["cookies"] = cookies
    if EGRESS in ("tor", "i2p"):
        if EGRESS == "i2p":  # the router would refuse anyway; fail early and clearly
            if not (urlsplit(url).hostname or "").endswith(".i2p"):
                raise ValueError("only *.i2p addresses are reachable here")
            kw["timeout"] = kw.get("timeout", 60) * 4  # eepsites are slow
        # a session's own SocksPort = same Tor circuit (exit IP) / I2P destination as its browser
        p = f"socks5h://{ports.SOCKS_HOST}:{socks_port or ports.GATEWAY_SOCKS_PORT}"
        return requests.request(method, url, proxies={"http": p, "https": p}, **kw)
    return netguard.guarded_request(method, url, **kw)


def content_type(url: str, **kw) -> str:
    try:
        ctype = _request("HEAD", url, timeout=15, **kw).headers.get("Content-Type", "")
        if ctype:
            return ctype
    except (requests.RequestException, ValueError):
        pass
    # HEAD unsupported or empty content-type: small ranged GET
    resp = _request("GET", url, timeout=15, **kw, headers={"Range": "bytes=0-0"}, stream=True)
    resp.close()
    return resp.headers.get("Content-Type", "")


def download(url: str, **kw) -> tuple[bytes, str]:
    resp = _request("GET", url, timeout=60, **kw)
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "")


def i2p_ready(socks_port: int, wait: float = 240) -> bool:
    """A fresh session destination needs about a minute to build tunnels: retry until an eepsite answers."""
    deadline, delay = time.time() + wait, 5
    while True:
        try:
            _request("HEAD", f"http://{I2P_PROBE_HOST}/", socks_port=socks_port, timeout=60)
            return True
        except requests.RequestException:
            if time.time() + delay > deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 30)
