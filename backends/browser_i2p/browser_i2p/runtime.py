"""The gateway-side driver of browser_i2p, imported lazily inside the container ($BACKEND=browser_i2p)."""

from browser_i2p.egress import I2pEgress
from spider_hoarder_browser_sdk.driver import BrowserDriver

driver = BrowserDriver(I2pEgress())
