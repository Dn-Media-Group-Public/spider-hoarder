"""Shared Playwright browser (one process-lifetime instance, started/stopped
from app.py's startup/shutdown events) plus an in-memory store of paused
"interactive" sessions for the cookie-consent fallback.

Every job/session gets its own fresh incognito BrowserContext with no
storage_state — the shared Browser process is stateless; nothing about a
job/session (cookies included) carries over to the next one.

ponytail: single in-process dict, only correct with one uvicorn worker.
Add a shared store (e.g. redis) if this ever runs with >1 worker/replica.
"""

import time
import uuid

from playwright.async_api import Browser, async_playwright

import cmp_block
import consent_click

SESSION_TTL = 300

_playwright = None
_browser: Browser | None = None
_sessions: dict[str, dict] = {}


async def startup():
    global _playwright, _browser
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch()


async def shutdown():
    global _playwright, _browser
    for session_id in list(_sessions):
        await _close(session_id)
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()


async def _route_handler(route):
    if cmp_block.should_block(route.request.url):
        await route.abort()
    else:
        await route.continue_()


async def new_context(user_agent: str):
    context = await _browser.new_context(user_agent=user_agent)
    await context.route("**/*", _route_handler)
    return context


async def tag_clickable(page) -> list[dict]:
    return await page.evaluate("""
() => {
    const sel = 'button, a, [role="button"], input[type="submit"], input[type="button"], [onclick]';
    const els = Array.from(document.querySelectorAll(sel));
    const out = [];
    let i = 0;
    for (const el of els) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const id = String(i++);
        el.setAttribute('data-mcp-id', id);
        out.push({id, tag: el.tagName.toLowerCase(), text: (el.innerText || el.value || '').trim().slice(0, 80)});
    }
    return out;
}
""")


async def screenshot(page) -> bytes:
    return await page.screenshot()


def sweep():
    now = time.time()
    for session_id, session in list(_sessions.items()):
        if session["expires_at"] < now:
            _sessions.pop(session_id, None)


async def create_session(url: str, user_agent: str) -> tuple[str, dict]:
    sweep()
    context = await new_context(user_agent)
    page = await context.new_page()
    await page.goto(url, wait_until="networkidle", timeout=60_000)
    await consent_click.try_dismiss(page)

    session_id = str(uuid.uuid4())
    session = {"context": context, "page": page, "expires_at": time.time() + SESSION_TTL}
    _sessions[session_id] = session
    return session_id, session


def get_session(session_id: str) -> dict | None:
    sweep()
    session = _sessions.get(session_id)
    if session:
        session["expires_at"] = time.time() + SESSION_TTL
    return session


async def _close(session_id: str):
    session = _sessions.pop(session_id, None)
    if session:
        await session["context"].close()


async def close_session(session_id: str):
    await _close(session_id)
