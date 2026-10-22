"""The one uiautomator2 connection of a slot: thin wrapper so tool handlers (android_tools.py) can be
tested against a fake. Created lazily, so listing tools needs no booted device."""

import android_ui as ui


class Device:
    def __init__(self, serial: str = "emulator-5554"):
        self.serial = serial
        self._d = None
        self.refs = ui.Refs()

    @property
    def d(self):
        if self._d is None:
            import uiautomator2 as u2  # imported late: the first connect also installs the on-device APKs
            self._d = u2.connect(self.serial)
        return self._d

    def reset(self) -> None:
        """New emulator session: forget the connection and every ref."""
        self._d = None
        self.refs = ui.Refs()

    def nodes(self) -> list:
        return ui.parse(self.d.dump_hierarchy())

    def current_app(self) -> dict:
        return self.d.app_current()

    def window_size(self) -> tuple:
        return tuple(self.d.window_size())

    def click(self, x, y): self.d.click(x, y)
    def long_click(self, x, y, seconds): self.d.long_click(x, y, seconds)
    def swipe(self, x1, y1, x2, y2, seconds): self.d.swipe(x1, y1, x2, y2, seconds)
    def press(self, key): self.d.press(key)
    def screenshot(self, path): self.d.screenshot(path)

    def send_keys(self, text, clear):
        self.d.send_keys(text, clear=clear)

    def shell(self, cmd) -> str:
        r = self.d.shell(cmd, timeout=60)
        return r.output if hasattr(r, "output") else str(r)

    def install(self, path): self.d.adb_device.install(path, nolaunch=True)
    def app_start(self, pkg, activity): self.d.app_start(pkg, activity)
    def app_stop(self, pkg): self.d.app_stop(pkg)

    def app_list(self, everything: bool) -> list:
        return self.d.app_list() if everything else self.d.app_list("-3")
