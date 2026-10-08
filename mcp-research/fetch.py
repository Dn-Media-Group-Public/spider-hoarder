"""Non-HTML downloads and the HTML-or-not preflight, via `requests` through the
same egress as the browsers: Tor SOCKS (socks5h, proxy resolves) or direct with
the SSRF guard. HTML itself is fetched by a browser session (see sessions.py)."""

import os

import requests

import netguard
import ports

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
EGRESS = os.environ.get("EGRESS", "clear")
_TOR = {"http": f"socks5h://tor:{ports.GATEWAY_SOCKS_PORT}", "https": f"socks5h://tor:{ports.GATEWAY_SOCKS_PORT}"}


def _request(method: str, url: str, **kw) -> requests.Response:
    kw["headers"] = {"User-Agent": CHROME_UA, **kw.get("headers", {})}
    if EGRESS == "tor":
        return requests.request(method, url, proxies=_TOR, **kw)
    return netguard.guarded_request(method, url, **kw)


def content_type(url: str) -> str:
    try:
        ctype = _request("HEAD", url, timeout=15).headers.get("Content-Type", "")
        if ctype:
            return ctype
    except (requests.RequestException, ValueError):
        pass
    # HEAD unsupported or empty content-type: small ranged GET
    resp = _request("GET", url, timeout=15, headers={"Range": "bytes=0-0"}, stream=True)
    resp.close()
    return resp.headers.get("Content-Type", "")


def download(url: str) -> tuple[bytes, str]:
    resp = _request("GET", url, timeout=60)
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "")
