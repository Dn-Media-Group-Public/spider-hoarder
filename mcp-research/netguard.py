"""SSRF guard for the gateway's own `requests` fetches (non-HTML downloads):
resolve, reject non-public addresses, pin the IP, re-check every redirect.
Not used with Tor: there the proxy resolves and the egress is already Tor."""

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

MAX_REDIRECTS = 5


def check_host(host: str) -> str:
    """Return one public IP for host, or raise ValueError."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise ValueError(f"{host} resolves to non-public address {ip}")
    return infos[0][4][0]


class _Pinned(HTTPAdapter):
    """Connect to the URL's (pre-validated) IP; SNI and cert check use the real name."""

    def __init__(self, host: str):
        self.host = host
        super().__init__()

    def init_poolmanager(self, *args, **kw):
        super().init_poolmanager(*args, server_hostname=self.host, assert_hostname=self.host, **kw)


def guarded_request(method: str, url: str, **kw) -> requests.Response:
    """requests.request with the SSRF guard on every hop (redirects followed here)."""
    for _ in range(MAX_REDIRECTS + 1):
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ValueError("only http(s) URLs")
        ip = check_host(p.hostname)
        netloc = f"[{ip}]" if ":" in ip else ip
        if p.port:
            netloc += f":{p.port}"
        pinned = p._replace(netloc=netloc).geturl()
        s = requests.Session()
        s.mount("https://", _Pinned(p.hostname))
        headers = {**kw.pop("headers", {}), "Host": p.netloc}
        resp = s.request(method, pinned, headers=headers, allow_redirects=False, **kw)
        if resp.is_redirect and "Location" in resp.headers:
            resp.close()
            url = urljoin(url, resp.headers["Location"])
            kw["headers"] = {k: v for k, v in headers.items() if k != "Host"}
            continue
        return resp
    raise ValueError("too many redirects")
