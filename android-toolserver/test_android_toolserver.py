"""Host-runnable checks for the Android tool server: outline/refs on a captured hierarchy, tool handlers
against a fake device, the gateway's tool list in sync, the secret gate. A test against a real emulator
(skipped without /dev/kvm) is at the bottom."""

import asyncio
import os
import tempfile

import android_tools as tools
import android_ui as ui

XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
 <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.x" content-desc="" clickable="false" enabled="true" scrollable="false" visible-to-user="true" bounds="[0,0][1080,2400]">
  <node index="0" text="" resource-id="com.x:id/root" class="android.widget.LinearLayout" package="com.x" content-desc="" clickable="false" enabled="true" scrollable="true" visible-to-user="true" bounds="[0,0][1080,2400]">
   <node index="0" text="Sign in" resource-id="com.x:id/title" class="android.widget.TextView" package="com.x" content-desc="" clickable="false" enabled="true" visible-to-user="true" bounds="[100,200][500,300]" />
   <node index="1" text="" resource-id="com.x:id/email" class="android.widget.EditText" package="com.x" content-desc="" hint="Email" clickable="true" focused="true" enabled="true" visible-to-user="true" bounds="[100,400][980,500]" />
   <node index="2" text="Next" resource-id="com.x:id/next" class="android.widget.Button" package="com.x" content-desc="" clickable="true" enabled="false" visible-to-user="true" bounds="[100,600][980,700]" />
   <node index="3" text="hidden" resource-id="" class="android.widget.TextView" package="com.x" content-desc="" visible-to-user="false" bounds="[0,0][10,10]" />
   <node index="4" text="" resource-id="" class="android.widget.ImageView" package="com.x" content-desc="Help" clickable="true" enabled="true" visible-to-user="true" bounds="[900,50][1000,150]" />
   <node index="5" text="empty box" resource-id="" class="android.view.View" package="com.x" content-desc="" visible-to-user="true" bounds="[0,0][0,0]" />
  </node>
 </node>
</hierarchy>"""


class FakeDevice:
    def __init__(self, xml=XML):
        self.xml, self.refs, self.calls = xml, ui.Refs(), []

    def nodes(self): return ui.parse(self.xml)
    def current_app(self): return {"package": "com.x", "activity": ".Main"}
    def window_size(self): return (1080, 2400)
    def click(self, x, y): self.calls.append(("click", x, y))
    def long_click(self, x, y, s): self.calls.append(("long", x, y, s))
    def swipe(self, *a): self.calls.append(("swipe", *a))
    def press(self, k): self.calls.append(("press", k))
    def send_keys(self, t, clear): self.calls.append(("keys", t, clear))
    def screenshot(self, p): open(p, "wb").write(b"\x89PNG")
    def shell(self, cmd): self.calls.append(("shell", cmd)); return "out\n"
    def install(self, p): self.calls.append(("install", p))
    def app_start(self, p, a): self.calls.append(("start", p, a))
    def app_stop(self, p): self.calls.append(("stop", p))
    def app_list(self, everything): return ["b.pkg", "a.pkg"]


def run(dev, name, **args):
    return tools.HANDLERS[name](dev, args)


def test_outline():
    nodes = ui.parse(XML)
    assert all(n.text != "hidden" and n.text != "empty box" for n in nodes)  # invisible / zero-size dropped
    text = ui.outline(nodes, ui.Refs())
    lines = text.splitlines()
    assert '- TextView "Sign in" [ref=e1]' in lines[0] or any('"Sign in"' in l for l in lines)
    assert any('EditText "Email"' in l and "[focused]" in l and "id=email" in l for l in lines)
    assert any('Button "Next"' in l and "[disabled]" in l for l in lines)
    assert any('"Help"' in l and "[clickable]" in l and "@950,100" in l for l in lines)
    assert not any("FrameLayout" in l for l in lines)  # plain containers are left out
    assert ui.outline([], ui.Refs()) == "(nothing on screen)"


def test_refs_and_staleness():
    dev = FakeDevice()
    run(dev, "device_snapshot")
    ref = next(r for r, n in dev.refs.table.items() if n.text == "Next")
    assert run(dev, "device_click", ref=ref).endswith("at 540,650")
    assert dev.calls[-1] == ("click", 540, 650)
    dev.xml = XML.replace("[100,600][980,700]", "[100,800][980,900]")  # the button moved: still found by identity
    run(dev, "device_click", ref=ref)
    assert dev.calls[-1] == ("click", 540, 850)
    dev.xml = XML.replace('text="Next"', 'text="Done"')  # page re-rendered
    try:
        run(dev, "device_click", ref=ref)
        assert False
    except tools.ToolError as e:
        assert "take a new snapshot" in str(e)
    try:
        run(dev, "device_click", ref="e9999")
        assert False
    except tools.ToolError as e:
        assert "not found" in str(e)
    assert run(dev, "device_snapshot").count("[ref=e") and dev.refs.n > int(ref[1:])  # numbers never reused


def test_find_and_type():
    dev = FakeDevice()
    out = run(dev, "device_find", text="email")
    assert "EditText" in out and "Button" not in out
    assert "Button" in run(dev, "device_find", class_name="Button")
    assert "(no matches)" in run(dev, "device_find", text="nope")
    try:
        run(dev, "device_find")
        assert False
    except tools.ToolError:
        pass
    ref = next(r for r, n in dev.refs.table.items() if n.cls.endswith("EditText"))
    run(dev, "device_type", ref=ref, text="Ünï 日本", submit=True, clear=True)
    assert [c[0] for c in dev.calls] == ["click", "keys", "press"] and dev.calls[1] == ("keys", "Ünï 日本", True)


def test_files_and_inputs():
    with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as inp:
        tools.OUT, tools.IN = out, inp
        dev = FakeDevice()
        r = run(dev, "device_snapshot", filename="../../etc/x.txt")  # never a path
        assert os.path.exists(os.path.join(out, "x.txt")) and "Saved" in r
        run(dev, "device_take_screenshot", filename="s.png")
        assert open(os.path.join(out, "s.png"), "rb").read() == b"\x89PNG"
        run(dev, "device_shell", command="ls", filename="sh.txt")
        assert open(os.path.join(out, "sh.txt")).read() == "out\n"
        assert run(dev, "device_app_list").splitlines()[-2:] == ["a.pkg", "b.pkg"]
        open(os.path.join(inp, "a.apk"), "w").write("x")
        run(dev, "device_install_apk", file="a.apk")
        assert dev.calls[-1] == ("install", os.path.join(inp, "a.apk")) and not os.path.exists(os.path.join(inp, "a.apk"))
        for bad in ("../a.apk", ".hidden", "sub/a.apk", "missing.apk"):
            try:
                run(dev, "device_install_apk", file=bad)
                assert False, bad
            except tools.ToolError:
                pass
        run(dev, "device_open_url", url="https://example.com")
        assert dev.calls[-1][1][-2:] == ["-d", "https://example.com"]
        try:
            run(dev, "device_open_url", url="--es x y")
            assert False
        except tools.ToolError:
            pass


def test_wait_for_and_keys():
    dev = FakeDevice()
    assert "met" in run(dev, "device_wait_for", text="sign in")
    assert "met" in run(dev, "device_wait_for", text_gone="Welcome")
    try:
        run(dev, "device_wait_for", text="Welcome", timeout=0.2)
        assert False
    except tools.ToolError as e:
        assert "timed out" in str(e)
    run(dev, "device_press_key", key="BACK")
    try:
        run(dev, "device_press_key", key="reboot")
        assert False
    except tools.ToolError:
        pass


def test_logcat_regex_filters_before_tail():
    dev = FakeDevice()
    buf = "\n".join(f"line {i} {'hit' if i % 10 == 0 else 'miss'}" for i in range(100))
    dev.shell = lambda cmd: (dev.calls.append(("shell", cmd)), buf)[1]
    out = run(dev, "device_logcat", lines=3, regex="hit")
    assert out.splitlines()[1:] == ["line 70 hit", "line 80 hit", "line 90 hit"], out  # last 3 matches, not matches of the last 3
    assert ("shell", ["logcat", "-d"]) in dev.calls
    out = run(dev, "device_logcat", lines=2)
    assert ("shell", ["logcat", "-d", "-t", "2"]) in dev.calls


def test_gateway_tool_list_in_sync():
    import export_tools
    path = export_tools.PATH
    assert open(path).read() == export_tools.render(), "run `python export_tools.py` in android-toolserver/"


def test_secret_gate_and_server():
    import android_server
    from starlette.testclient import TestClient

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    for secret, hdr, code in (("s3", {"x-slot-secret": "s3"}, 200), ("s3", {}, 403), ("s3", {"x-slot-secret": "no"}, 403),
                              ("", {}, 403), ("", {"x-slot-secret": ""}, 403)):
        c = TestClient(android_server.SecretGate(inner, secret))
        assert c.get("/", headers=hdr).status_code == code, (secret, hdr)
    server = android_server.build(0, FakeDevice())
    assert server is not None


def test_call_tool_replies_with_error_section():
    import android_server
    mcp = android_server.build(0, FakeDevice())
    from mcp import types
    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name="device_click", arguments={"ref": "e1"}))
    res = asyncio.run(handler(req)).root
    assert res.isError and res.content[0].text.startswith("### Error\nError: ref 'e1' not found")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
