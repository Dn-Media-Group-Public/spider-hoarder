"""Page operations on a browser session: reading the rendered page, cookies, one-shot fetches, and the page-to-job
tools (browser_save_page, ingest_page). The session registry is imported inside the functions (it imports the driver,
which imports this)."""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING

from spider_hoarder_browser_sdk import fetch
from spider_hoarder_browser_sdk.mediapolicy import Policy
from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway.driver import sessions
from spider_hoarder_sdk.gateway.jobs import _key, _meta, _new_job_id, _store_and_parse

if TYPE_CHECKING:
    from spider_hoarder_sdk.gateway.sessions import Session


def parse_result(text: str):
    """Unwrap a toolserver reply: '### Result\\n<json>' (up to the next section)."""
    body = text.split("### Result", 1)[-1].lstrip("\n")
    return json.loads(body.split("\n### ", 1)[0].strip())


def parse_eval_result(text: str) -> tuple[str, str]:
    value = parse_result(text)
    if not isinstance(value, dict) or not isinstance(value.get("html"), str):
        raise ValueError("browser_evaluate did not return {html, url}")
    return value["html"], value.get("url", "")


# Scroll to trigger lazy content, then wait until the DOM has been quiet for 1s (max 10s) so
# client-side rendered pages are complete before we read them.
# The function gets the `target` element as its argument (none = whole document).
SETTLE_JS = """async (el) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let last = Date.now();
  const obs = new MutationObserver(() => { last = Date.now(); });
  obs.observe(document.documentElement, {subtree: true, childList: true, characterData: true});
  const y0 = window.scrollY;
  for (let y = 0, i = 0; y < document.body.scrollHeight && i < 30; y += window.innerHeight, i++) {
    window.scrollTo(0, y);
    await sleep(150);
  }
  window.scrollTo(0, y0);
  const t0 = Date.now();
  while (Date.now() - last < 1000 && Date.now() - t0 < 10000) await sleep(100);
  obs.disconnect();
  return {html: (el || document.documentElement).outerHTML, url: location.href};
}"""


async def page_html(bs: Session, browser: str | None = None, target: str | None = None) -> tuple[str, str]:
    """(outerHTML of target or the whole page, location.href)."""
    res = await bs.client.call_tool("browser_evaluate", {"function": SETTLE_JS, **({"target": target} if target else {}), **_on(browser)})
    if res.isError:
        raise RuntimeError(sessions.result_text(res))
    return parse_eval_result(sessions.result_text(res))


async def cookies_for(bs: Session, url: str) -> dict:
    """The session's browser cookies applicable to url, as a name->value dict for `requests`."""
    res = await bs.client.call_tool("browser_cookie_list", {"url": url})
    if res.isError:
        raise RuntimeError(sessions.result_text(res))
    return {c["name"]: c["value"] for c in parse_result(sessions.result_text(res))}


async def fetch_html(owner: str, url: str, live: Session | None = None, target: str | None = None,
                     policy: Policy | None = None) -> tuple[str, str]:
    """One-shot: navigate, read HTML. Without `live`: ephemeral session (own uid), destroyed after.
    With `live`: a temporary browser (window) in that session, so its cookies apply but nobody's page is clobbered."""
    if live:
        res = await live.client.call_tool("browser_start_session", {})
        if res.isError:
            raise RuntimeError(sessions.result_text(res))
        browser = parse_result(sessions.result_text(res))
        try:
            return await _navigate_read(live, url, browser, target)
        finally:
            await live.client.call_tool("browser_end_session", {"browser": browser})
    bs = await sessions.new_session(owner, opts=policy)
    try:
        return await _navigate_read(bs, url, None, target)
    finally:
        await sessions.close(bs)


def _on(browser: str | None) -> dict:
    return {"browser": browser} if browser else {}


async def _navigate_read(bs: Session, url: str, browser: str | None = None, target: str | None = None) -> tuple[str, str]:
    for tool, args in (("browser_navigate", {"url": url}), ("browser_wait_for", {"time": 2})):
        res = await bs.client.call_tool(tool, {**args, **_on(browser)})
        if res.isError:
            raise RuntimeError(sessions.result_text(res))
    return await page_html(bs, browser, target)


async def ingest_page(egress, owner: str, url: str, live: Session | None = None, target: str | None = None,
                       policy: Policy | None = None) -> dict:
    job_id = _new_job_id()
    kw = {}
    if live:  # preflight/download as the real browser session: its cookies, its Tor circuit / I2P destination
        kw = {"cookies": await cookies_for(live, url), "socks_port": egress.socks_port(live.uid)}
    ctype = await asyncio.to_thread(fetch.content_type, egress, url, **kw)
    if "text/html" in ctype or ctype == "":
        html, _ = await fetch_html(owner, url, live, target, policy)
        raw, ctype = html.encode(), "text/html"
    else:
        raw, ctype = await asyncio.to_thread(fetch.download, egress, url, policy, **kw)
    filename = os.path.basename(url.split("?")[0]) or "page"
    key = _key(owner, job_id)
    result = _store_and_parse(key, filename, raw, ctype, url)
    storage.set_meta(key, url=url, target=target if "html" in ctype else None)
    return {"job_id": job_id, **result, **_meta(key)}


async def save_page(bs: Session, browser: str | None = None, target: str | None = None) -> dict:
    job_id = _new_job_id()
    html, url = await page_html(bs, browser, target)
    key = _key(bs.owner, job_id)
    result = _store_and_parse(key, "page.html", html.encode(), "text/html", url)
    storage.set_meta(key, url=url, target=target)
    return {"job_id": job_id, **result, **_meta(key)}
