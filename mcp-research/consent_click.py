"""Best-effort auto-click of common cookie-consent accept/reject buttons.

Cheap and silent: harmless no-op when there's no banner, never raises.
"""

KEYWORDS = [
    # English
    "accept all", "accept cookies", "accept", "agree", "i agree", "got it", "allow all",
    # Norwegian
    "godta alle", "godta", "aksepter alle", "aksepter", "jeg godtar",
]

_SELECTOR = ", ".join(f'button:has-text("{kw}"), a:has-text("{kw}"), [role="button"]:has-text("{kw}")' for kw in KEYWORDS)


async def try_dismiss(page) -> bool:
    try:
        locator = page.locator(_SELECTOR).first
        await locator.click(timeout=2000)
        await page.wait_for_timeout(500)
        return True
    except Exception:
        return False
