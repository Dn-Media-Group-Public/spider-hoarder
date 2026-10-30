"""The gateway-side driver of browser_clear, imported lazily inside the container ($BACKEND=browser_clear)."""

from browser_clear.egress import ClearEgress
from spider_hoarder_browser_sdk.driver import BrowserDriver

driver = BrowserDriver(ClearEgress())
