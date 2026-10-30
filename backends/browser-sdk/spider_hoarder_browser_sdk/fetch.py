"""Non-HTML downloads and the HTML-or-not preflight, via `requests` through the same egress as the browsers (the backend's
egress policy decides how: Tor/I2P SOCKS, or direct with the SSRF guard). HTML itself is fetched by a browser session
(see pages.py)."""

import requests

from spider_hoarder_browser_sdk.mediapolicy import Policy, blocked_message

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _request(egress, method: str, url: str, socks_port: int | None = None, cookies: dict | None = None, **kw) -> requests.Response:
    kw["headers"] = {"User-Agent": CHROME_UA, **kw.get("headers", {})}
    if cookies:
        kw["cookies"] = cookies
    return egress.request(method, url, socks_port=socks_port, **kw)


def content_type(egress, url: str, **kw) -> str:
    try:
        ctype = _request(egress, "HEAD", url, timeout=15, **kw).headers.get("Content-Type", "")
        if ctype:
            return ctype
    except (requests.RequestException, ValueError):
        pass
    # HEAD unsupported or empty content-type: small ranged GET
    resp = _request(egress, "GET", url, timeout=15, **kw, headers={"Range": "bytes=0-0"}, stream=True)
    resp.close()
    return resp.headers.get("Content-Type", "")


def download(egress, url: str, policy: Policy | None = None, **kw) -> tuple[bytes, str]:
    """With a no-media `policy`, a blocked Content-Type closes the connection before the body is read."""
    resp = _request(egress, "GET", url, timeout=60, stream=True, **kw)
    try:
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if policy and policy.content_type_blocked(ctype):
            raise ValueError(blocked_message(ctype))
        return resp.content, ctype
    finally:
        resp.close()
