"""The gateway-side driver of android_tor, imported lazily inside the container ($BACKEND=android_tor)."""

from spider_hoarder_android_sdk.driver import AndroidDriver

NOTE = (
        'ANDROID, TOR: a real Android emulator (Google Play image) whose traffic, DNS included, is routed through Tor; '
        'everything but TCP and DNS is dropped (no QUIC/UDP). Apps are slow and many services block Tor exits or show '
        'CAPTCHAs; Google sign-in / Play Store are often refused.')

driver = AndroidDriver(NOTE)
