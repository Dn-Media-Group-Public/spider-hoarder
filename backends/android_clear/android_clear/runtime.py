"""The gateway-side driver of android_clear, imported lazily inside the container ($BACKEND=android_clear)."""

from spider_hoarder_android_sdk.driver import AndroidDriver

NOTE = (
        'ANDROID, CLEAR-TEXT / direct internet: a real Android emulator (Google Play image) whose traffic leaves from '
        "this server's public IP, not anonymised. Apps can see and block it. Cannot reach .onion sites or "
        'private/internal addresses.')

driver = AndroidDriver(NOTE)
