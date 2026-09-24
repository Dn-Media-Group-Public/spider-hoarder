"""Fetch a URL: HTML goes through Playwright + settle-wait, everything else is a
direct download. Returns (raw_bytes, content_type)."""

import requests

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _content_type(url: str) -> str:
    try:
        resp = requests.head(url, allow_redirects=True, timeout=15, headers={"User-Agent": CHROME_UA})
        ctype = resp.headers.get("Content-Type", "")
        if ctype:
            return ctype
    except requests.RequestException:
        pass
    # HEAD unsupported or empty content-type — fall back to a small ranged GET
    resp = requests.get(
        url, timeout=15, headers={"User-Agent": CHROME_UA, "Range": "bytes=0-0"}, stream=True
    )
    resp.close()
    return resp.headers.get("Content-Type", "")


def fetch(url: str) -> tuple[bytes, str]:
    ctype = _content_type(url)
    if "text/html" in ctype or ctype == "":
        return _fetch_html(url), "text/html"
    resp = requests.get(url, timeout=60, headers={"User-Agent": CHROME_UA})
    resp.raise_for_status()
    return resp.content, ctype


def _fetch_html(url: str) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=CHROME_UA)
        page.goto(url, wait_until="networkidle", timeout=60_000)
        # short quiescence check beyond networkidle for client-rendered content
        page.wait_for_timeout(1000)
        html = page.content()
        browser.close()
        return html.encode("utf-8")
