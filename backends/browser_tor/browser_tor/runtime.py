"""The gateway-side driver of browser_tor, imported lazily inside the container ($BACKEND=browser_tor)."""

from browser_tor.egress import TorEgress
from spider_hoarder_browser_sdk.driver import BrowserDriver

driver = BrowserDriver(TorEgress())
