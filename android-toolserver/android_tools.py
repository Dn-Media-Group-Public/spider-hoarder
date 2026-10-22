"""Tool definitions and handlers of the Android tool server. Handlers are synchronous (uiautomator2 blocks)
and take a Device (device.py) plus the arguments; they return the text of the reply's sections."""

import os
import re
import time

import android_ui as ui
from mcp import types

OUT = os.environ.get("ANDROID_OUT_DIR", "/out")  # file outputs; slotd serves them to the gateway
IN = os.environ.get("ANDROID_IN_DIR", "/in")     # input files (APKs) the gateway pushed
KEYS = {"back", "home", "enter", "recent", "menu", "search", "delete", "tab", "space", "power", "volume_up",
        "volume_down", "volume_mute", "up", "down", "left", "right", "center", "camera", "escape"}
MAX_WAIT = 120


class ToolError(Exception):
    pass


s_, i_, b_, n_ = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}, {"type": "number"}
FILENAME = {**s_, "description": "Name of the file inside the job (default depends on the tool)"}
REF = {**s_, "description": "Element ref from device_snapshot / device_find"}


def schema(required=(), **props) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


# name -> (description, input schema)
TOOLS: dict = {
    "device_snapshot": (
        "Outline of what is on the Android screen (accessibility tree): visible and interactive elements with "
        "`[ref=eN]`, flags and tap coordinates. Refs stay valid while the element is on screen; if one fails with "
        "'ref not found', take a new snapshot. Webview content (Chrome, in-app browsers) is included.",
        schema(filename=FILENAME)),
    "device_find": (
        "Find on-screen elements matching ALL given criteria and return them with refs.",
        schema(text={**s_, "description": "Substring of text, description or hint (case-insensitive)"},
               regex={**s_, "description": "Python regex searched in text/description/hint"},
               resource_id={**s_, "description": "Substring of the resource id, e.g. 'search_box'"},
               description={**s_, "description": "Substring of the content description"},
               class_name={**s_, "description": "Substring of the class name, e.g. 'EditText'"},
               filename=FILENAME)),
    "device_click": ("Tap an element by ref.", schema(["ref"], ref=REF)),
    "device_long_click": ("Long-press an element by ref.", schema(["ref"], ref=REF, seconds={**n_, "description": "Hold time (default 1)"})),
    "device_tap": ("Tap screen coordinates (for canvas/game UIs without accessibility nodes).",
                   schema(["x", "y"], x=i_, y=i_)),
    "device_swipe": ("Swipe between two screen coordinates (scroll, pull, drag).",
                     schema(["x1", "y1", "x2", "y2"], x1=i_, y1=i_, x2=i_, y2=i_,
                            seconds={**n_, "description": "Duration (default 0.3)"})),
    "device_type": ("Type text (Unicode ok) into the element `ref` (it is tapped first to focus it).",
                    schema(["ref", "text"], ref=REF, text=s_,
                           clear={**b_, "description": "Clear the field first (default false)"},
                           submit={**b_, "description": "Press enter afterwards (default false)"})),
    "device_press_key": ("Press a key: " + ", ".join(sorted(KEYS)) + ".", schema(["key"], key={**s_, "enum": sorted(KEYS)})),
    "device_wait_for": (
        "Wait until `text` appears on screen, `text_gone` disappears, or `time` seconds passed (max 120). "
        "With text/text_gone it fails after `timeout` seconds (default 30).",
        schema(text=s_, text_gone=s_, time=n_, timeout=n_)),
    "device_take_screenshot": ("Take a screenshot (PNG, always a file).", schema(filename=FILENAME)),
    "device_open_url": ("Open a URL on the device (an intent VIEW; usually opens Chrome).", schema(["url"], url=s_)),
    "device_install_apk": (
        "Install an APK that is stored in a job (e.g. uploaded through get_upload_link).",
        schema(["file"], file={**s_, "description": "Name of an APK pushed to the device (set by the gateway)"})),
    "device_app_start": ("Start an app by package name.", schema(["package"], package=s_, activity=s_)),
    "device_app_stop": ("Force-stop an app.", schema(["package"], package=s_)),
    "device_app_list": ("List installed packages (third-party only unless `all`).", schema(all=b_, filename=FILENAME)),
    "device_current_app": ("Package and activity in the foreground.", schema()),
    "device_logcat": ("Recent logcat output.", schema(lines={**i_, "description": "Last N lines (default 500)"},
                                                      regex={**s_, "description": "Keep only matching lines"}, filename=FILENAME)),
    "device_shell": ("Run a command in the device's adb shell (the device is an isolated emulator).",
                     schema(["command"], command=s_, filename=FILENAME)),
}


def build_tools() -> list:
    return [types.Tool(name=n, description=d, inputSchema=sch) for n, (d, sch) in TOOLS.items()]


# ------------------------------------------------------------------ handlers

def _write(args: dict, default: str, text: str) -> str:
    """Handler output: inline, or into OUT/<filename> when a filename is given (the gateway always gives one
    to the tools it turns into jobs)."""
    name = args.get("filename")
    if not name:
        return text
    name = os.path.basename(str(name))
    if name in ("", ".", ".."):
        name = default
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w" if isinstance(text, str) else "wb") as f:
        f.write(text)
    return f"Saved {len(text)} characters to {name}"


def _device(dev) -> str:
    cur = dev.current_app()
    return f"\n### Device\n- Current app: {cur.get('package')}/{cur.get('activity')}\n- Screen: {dev.window_size()[0]}x{dev.window_size()[1]}"


def snapshot(dev, args):
    return "### Snapshot\n" + _write(args, "snapshot.txt", ui.outline(dev.nodes(), dev.refs)) + _device(dev)


def find(dev, args):
    q = {k: args.get(k) for k in ("text", "regex", "resource_id", "description", "class_name")}
    if not any(q.values()):
        raise ToolError("give at least one of text, regex, resource_id, description, class_name")
    try:
        return "### Result\n" + _write(args, "matches.txt", ui.find(dev.nodes(), dev.refs, **q))
    except re.error as e:
        raise ToolError(f"bad regex: {e}")


def _target(dev, ref):
    try:
        return dev.refs.resolve(ref, dev.nodes())
    except ui.RefNotFound as e:
        raise ToolError(str(e))


def click(dev, args):
    x, y = _target(dev, args["ref"]).center
    dev.click(x, y)
    return f"### Result\nClicked {args['ref']} at {x},{y}"


def long_click(dev, args):
    x, y = _target(dev, args["ref"]).center
    dev.long_click(x, y, float(args.get("seconds") or 1))
    return f"### Result\nLong-clicked {args['ref']} at {x},{y}"


def tap(dev, args):
    dev.click(int(args["x"]), int(args["y"]))
    return f"### Result\nTapped {args['x']},{args['y']}"


def swipe(dev, args):
    dev.swipe(int(args["x1"]), int(args["y1"]), int(args["x2"]), int(args["y2"]), float(args.get("seconds") or 0.3))
    return "### Result\nSwiped"


def type_(dev, args):
    x, y = _target(dev, args["ref"]).center
    dev.click(x, y)
    time.sleep(0.3)  # let the field take focus and the IME come up
    dev.send_keys(str(args["text"]), bool(args.get("clear")))
    if args.get("submit"):
        dev.press("enter")
    return f"### Result\nTyped into {args['ref']}"


def press_key(dev, args):
    key = str(args["key"]).lower()
    if key not in KEYS:
        raise ToolError(f"unknown key {key!r}; one of {', '.join(sorted(KEYS))}")
    dev.press(key)
    return f"### Result\nPressed {key}"


def wait_for(dev, args):
    text, gone = args.get("text"), args.get("text_gone")
    if not (text or gone):
        secs = min(float(args.get("time") or 1), MAX_WAIT)
        time.sleep(secs)
        return f"### Result\nWaited {secs:g}s"
    timeout = min(float(args.get("timeout") or 30), MAX_WAIT)
    deadline = time.time() + timeout
    while True:
        labels = " ".join(f"{n.text}\n{n.desc}" for n in dev.nodes()).lower()
        if (not text or text.lower() in labels) and (not gone or gone.lower() not in labels):
            return "### Result\nCondition met"
        if time.time() >= deadline:
            raise ToolError(f"timed out after {timeout:g}s waiting for text={text!r} text_gone={gone!r}")
        time.sleep(0.5)


def screenshot(dev, args):
    os.makedirs(OUT, exist_ok=True)
    name = os.path.basename(str(args.get("filename") or "screenshot.png")) or "screenshot.png"
    dev.screenshot(os.path.join(OUT, name))
    return "### Result\nSaved screenshot to " + name + _device(dev)


def open_url(dev, args):
    url = str(args["url"]).strip()
    if not url or url.startswith("-"):
        raise ToolError("bad url")
    return "### Result\n" + dev.shell(["am", "start", "-a", "android.intent.action.VIEW", "-d", url]).strip()


def install_apk(dev, args):
    name = str(args["file"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ToolError("bad file name")
    path = os.path.join(IN, name)
    if not os.path.isfile(path):
        raise ToolError("no such pushed file")
    try:
        dev.install(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return "### Result\nInstalled"


def app_start(dev, args):
    dev.app_start(str(args["package"]), args.get("activity"))
    return f"### Result\nStarted {args['package']}" + _device(dev)


def app_stop(dev, args):
    dev.app_stop(str(args["package"]))
    return f"### Result\nStopped {args['package']}"


def app_list(dev, args):
    return "### Result\n" + _write(args, "apps.txt", "\n".join(sorted(dev.app_list(bool(args.get("all"))))))


def current_app(dev, args):
    cur = dev.current_app()
    return f"### Result\n{cur.get('package')}/{cur.get('activity')}"


def logcat(dev, args):
    """Last N lines; with a regex the filter comes first (the matches are among the last N *matching* lines, not
    among the last N lines overall)."""
    n = max(1, min(int(args.get("lines") or 500), 20000))
    if args.get("regex"):
        try:
            rx = re.compile(args["regex"])
        except re.error as e:
            raise ToolError(f"bad regex: {e}")
        lines = [l for l in dev.shell(["logcat", "-d"]).splitlines() if rx.search(l)]
        text = "\n".join(lines[-n:])
    else:
        text = dev.shell(["logcat", "-d", "-t", str(n)])
    return "### Result\n" + _write(args, "logcat.txt", text)


def shell(dev, args):
    return "### Result\n" + _write(args, "shell.txt", dev.shell(str(args["command"])))


HANDLERS = {
    "device_snapshot": snapshot, "device_find": find, "device_click": click, "device_long_click": long_click,
    "device_tap": tap, "device_swipe": swipe, "device_type": type_, "device_press_key": press_key,
    "device_wait_for": wait_for, "device_take_screenshot": screenshot, "device_open_url": open_url,
    "device_install_apk": install_apk, "device_app_start": app_start, "device_app_stop": app_stop,
    "device_app_list": app_list, "device_current_app": current_app, "device_logcat": logcat, "device_shell": shell,
}
assert set(HANDLERS) == set(TOOLS)
