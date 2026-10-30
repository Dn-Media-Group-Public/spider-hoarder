"""Per-tool checks against a real headless Chromium (needs playwright + its Chromium; the
container smoke test covers headed mode). Run: pytest test_toolserver.py"""

import asyncio
import json
import os
import shutil
import tempfile
import time

import pytest

from spider_hoarder_browser_sdk.session_server.toolserver import tools
from spider_hoarder_browser_sdk.session_server.toolserver.browsers import launch
from spider_hoarder_browser_sdk.mediapolicy import Policy
from spider_hoarder_browser_sdk.session_server.toolserver.server import GOLDEN, TOOLS

PAGE = ("data:text/html,<title>T</title><input id=i><button onclick=\"document.title='clicked'\">Go</button>"
        "<a href='data:text/html,two'>link</a>")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _need_chromium():
    import glob
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")
    if not glob.glob(f"{root}/chromium_headless_shell-*/*/chrome-headless-shell"):
        pytest.skip("Playwright Chromium (headless shell) is not installed")


@pytest.fixture
def env(tmp_path):
    tools.OUT = str(tmp_path)

    async def mk():
        # the generated origin page has no address space of its own, so Local Network Access would block cross-origin xhr to the
        # loopback test server (a public target is unaffected)
        return await launch({"userDataDir": str(tmp_path / "p"), "args": ["--no-sandbox", "--disable-features=LocalNetworkAccessChecks"], "headless": True})
    pw, R = run_loop.run_until_complete(mk())
    run_loop.run_until_complete(R.set_policy(Policy.make(True)))
    yield R
    run_loop.run_until_complete(R.ctx.close())
    run_loop.run_until_complete(pw.stop())


run_loop = asyncio.new_event_loop()


def call(R, name, /, bid=None, **a):
    async def go():
        b = None if name in ("browser_start_session", "browser_end_session", "browser_set_policy") else await R.resolve(bid)
        return await tools.HANDLERS[name](R, b, a)
    return run_loop.run_until_complete(go())


def tmp_path_out():
    return tools.OUT


def test_network_new_cursor_and_binary_body(env):
    R = env
    png = bytes(range(256)) * 4
    run_loop.run_until_complete(R.ctx.route("https://n.test/**", lambda r: r.fulfill(
        body=png if r.request.url.endswith(".png") else "<title>n</title>",
        content_type="image/png" if r.request.url.endswith(".png") else "text/html")))
    call(R, "browser_navigate", url="https://n.test/a")
    first = call(R, "browser_network_requests", new=True)
    assert "n.test/a" in first
    assert call(R, "browser_network_requests", new=True) == "### Result\nNo new requests."
    call(R, "browser_evaluate", function="() => fetch('/b.png').then(r => r.arrayBuffer())")
    call(R, "browser_navigate", url="about:blank")
    assert "n.test/a" in call(R, "browser_network_requests")  # no `new`: whole log, mark untouched
    call(R, "browser_navigate", url="https://n.test/c")
    second = call(R, "browser_network_requests", new=True, filter="/zzz/")  # filtered out, mark still moves
    assert second == "### Result\nNo new requests."
    assert call(R, "browser_network_requests", new=True) == "### Result\nNo new requests."
    i = next(int(l.split(".")[0]) for l in call(R, "browser_network_requests", static=True).splitlines() if "b.png" in l)
    assert i > 1  # indexes stay global
    call(R, "browser_network_request_get", index=i, filename="img")
    assert open(tools.OUT + "/img/response/body.png", "rb").read() == png


def val(text):
    return json.loads(text.split("### Result\n", 1)[1].split("\n### ", 1)[0])


def test_schemas_match_golden():
    golden = {t["name"]: t for t in json.load(open(GOLDEN))}
    ours = {t.name: t for t in TOOLS}
    assert set(golden) - set(ours) == {"browser_run_code_unsafe", "browser_network_request"}  # the latter is renamed _get
    golden["browser_network_request_get"] = golden.pop("browser_network_request")
    for n, g in golden.items():
        if n in ours:
            props = dict(ours[n].inputSchema["properties"])
            assert props.pop("browser")
            if n in ("browser_snapshot", "browser_find"):
                assert props.pop("text_cap")["default"] == 128
            want = dict(g["inputSchema"].get("properties", {}))
            if n == "browser_network_requests":
                assert props.pop("new")["type"] == "boolean"
            if n == "browser_network_request_get":
                want.pop("part")  # replaced by a job with a file per part
            assert props == want, n
    assert {"browser_start_session", "browser_cookie_list", "browser_localstorage_set", "browser_set_storage_state"} <= set(ours)
    assert len([n for n in ours if "cookie" in n or "storage" in n]) == 17
    assert set(ours) <= set(tools.HANDLERS)


def test_interaction_and_refs(env):
    R = env
    out = call(R, "browser_navigate", url=PAGE)
    assert "### Page" in out and "Snapshot" not in out and "Ran Playwright" not in out
    assert not [f for f in os.listdir(tools.OUT) if f.startswith("page-")]  # no post-action snapshot file
    snap = call(R, "browser_snapshot")
    assert '```yaml' in snap and 'button "Go"' in snap
    ref = [l for l in snap.splitlines() if 'button "Go"' in l][0].split("[ref=")[1].split("]")[0]
    assert "Page Title: clicked" in call(R, "browser_click", target=ref)
    iref = [l for l in snap.splitlines() if "textbox" in l][0].split("[ref=")[1].split("]")[0]
    call(R, "browser_type", target=iref, text="hello")
    assert val(call(R, "browser_evaluate", function="() => document.getElementById('i').value")) == "hello"
    with pytest.raises(tools.ToolError, match="not found in the current page snapshot"):
        call(R, "browser_click", target="e999")
    assert "link" in call(R, "browser_find", text="LINK")
    assert val(call(R, "browser_evaluate", function="() => 'a'")) == "a"


FIND_SNAP = """- generic [ref=e1]:
  - article [ref=e2]:
    - heading "Title" [level=1] [ref=e3]
    - paragraph [ref=e4]: a
    - paragraph [ref=e5]: b
    - paragraph [ref=e6]: c
    - paragraph [ref=e7]: d
    - paragraph [ref=e8]: e
    - paragraph [ref=e9]: f
    - link "Needle" [ref=e10]
  - navigation [ref=e11]:
    - link "Home" [ref=e12]
    - link "needle again" [ref=e13]"""


def test_find_snippets_context_and_ancestors():
    out = tools.find_snippets(FIND_SNAP, lambda l: "needle" in l.lower(), '"needle"')
    assert out.startswith('Found 2 matches for "needle":\n\n')
    assert out.count("\n\n----\n\n") == 0  # the two windows overlap and are merged
    lines = out.split("\n\n", 1)[1].split("\n")
    assert lines[:2] == ["- generic [ref=e1]:", "  - article [ref=e2]:"]  # ancestors of the window, with refs
    assert 'paragraph [ref=e4]' not in out and 'paragraph [ref=e7]: d' in out  # 3 lines of context, not more
    assert lines[-1] == '    - link "needle again" [ref=e13]'
    one = tools.find_snippets(FIND_SNAP, lambda l: "title" in l.lower(), '"title"')
    assert one.startswith('Found 1 match for "title":')
    assert tools.find_snippets(FIND_SNAP, lambda l: False, '"x"') == 'No matches found for "x".'
    far = "\n".join(["- root:"] + [f"  - item {i}" for i in range(30)])
    out = tools.find_snippets(far, lambda l: l.endswith(("item 2", "item 25")), "q")
    assert out.count("\n\n----\n\n") == 1 and out.startswith("Found 2 matches for q:")


def test_find_via_tool(env):
    R = env
    call(R, "browser_navigate", url=PAGE)
    out = call(R, "browser_find", text="go")
    assert out.startswith("### Page\n- Page URL: ") and "### Result\nFound 1 match for \"go\":" in out and 'button "Go"' in out
    assert "Found 1 match for /GO/i:" in call(R, "browser_find", regex="/GO/i")
    assert "No matches found" in call(R, "browser_find", text="nothing-like-this")


def test_find_refs_survive_actions_and_fail_after_navigation(env):
    R = env
    call(R, "browser_navigate", url=PAGE)
    ref = lambda out, label: out.split(label, 1)[1].split("[ref=")[1].split("]")[0]
    go = ref(call(R, "browser_find", text="Go"), 'button "Go"')
    call(R, "browser_evaluate", function="() => document.body.insertAdjacentHTML('beforeend', '<button>New</button>')")
    assert 'button "New"' in call(R, "browser_find", text="New")
    assert "Page Title: clicked" in call(R, "browser_click", target=go)  # the old ref still resolves
    call(R, "browser_navigate", url="data:text/html,<title>other</title><button>x</button>")
    with pytest.raises(tools.ToolError, match="not found in the current page snapshot"):
        call(R, "browser_click", target=go)


def test_text_cap_via_tools(env):
    R = env
    call(R, "browser_navigate", url="data:text/html,<p>" + "word " * 100 + "NEEDLE " + "tail " * 100 + "</p>")
    snap = lambda **a: call(R, "browser_snapshot", **a).split("### Snapshot", 1)[1]  # ### Page echoes the data: URL
    assert "..." in snap() and "NEEDLE" not in snap()
    assert "NEEDLE" in snap(text_cap=None)
    out = call(R, "browser_find", text="needle", text_cap=40)
    assert "...NEEDLE" in out or "NEEDLE" in out and out.count("...") == 2, out  # window around the match
    with pytest.raises(tools.ToolError):
        call(R, "browser_snapshot", text_cap=0)


def test_file_outputs(env, tmp_path):
    R = env
    call(R, "browser_navigate", url=PAGE)
    assert "[Screenshot](out/s.png)" in call(R, "browser_take_screenshot", filename="../../s.png")
    assert (tmp_path / "s.png").read_bytes()[:4] == b"\x89PNG"
    call(R, "browser_pdf_save", filename="p.pdf")
    assert (tmp_path / "p.pdf").read_bytes()[:4] == b"%PDF"
    call(R, "browser_evaluate", function="() => 1+1", filename="e.json")
    assert (tmp_path / "e.json").read_text() == "2"
    call(R, "browser_snapshot", filename="s.yml")
    assert "button" in (tmp_path / "s.yml").read_text()
    call(R, "browser_storage_state")
    assert "cookies" in (tmp_path / "storage-state.json").read_text()


def test_two_browsers_are_isolated_and_share_logins(env):
    R = env
    other = val(call(R, "browser_start_session"))
    call(R, "browser_navigate", url="data:text/html,<title>main</title>")
    call(R, "browser_navigate", bid=other, url="data:text/html,<title>other</title>")
    assert "main" in call(R, "browser_tabs", action="list") and "other" not in call(R, "browser_tabs", action="list")
    assert "other" in call(R, "browser_tabs", bid=other, action="list")
    with pytest.raises(tools.ToolError, match="^unknown browser id$"):
        call(R, "browser_tabs", bid="nope", action="list")
    # one context: a cookie set via one browser is visible to the other
    call(R, "browser_cookie_set", name="sid", value="1", domain="example.org", path="/")
    assert val(call(R, "browser_cookie_get", bid=other, name="sid"))["value"] == "1"
    assert val(call(R, "browser_cookie_list", url="https://example.org/"))[0]["name"] == "sid"
    assert val(call(R, "browser_end_session", browser=other)) == {"remaining": 1, "blocked": 0}
    with pytest.raises(tools.ToolError):
        call(R, "browser_tabs", bid=other, action="list")
    assert val(call(R, "browser_end_session")) == {"remaining": 0, "blocked": 0}
    call(R, "browser_navigate", url="data:text/html,<title>again</title>")  # main comes back on demand


def test_tabs_and_popups(env):
    R = env
    other = val(call(R, "browser_start_session"))
    call(R, "browser_navigate", bid=other, url="data:text/html,<a target=_blank href='data:text/html,pop'>x</a>")
    call(R, "browser_tabs", bid=other, action="new", url="data:text/html,<title>t2</title>")
    assert call(R, "browser_tabs", bid=other, action="list").count("- ") == 2
    assert call(R, "browser_tabs", action="list").count("- ") == 1  # main did not gain the tab
    run_loop.run_until_complete(R.cur[other].evaluate("window.open('about:blank')"))
    run_loop.run_until_complete(R.settle())
    assert call(R, "browser_tabs", bid=other, action="list").count("- ") == 3  # popup joined its opener's browser
    call(R, "browser_tabs", bid=other, action="close", index=0)
    assert call(R, "browser_tabs", bid=other, action="list").count("- ") == 2
    call(R, "browser_close")  # last tab of main, but another browser exists -> main closes
    assert "main" not in R.tabs


def test_storage_and_console(env):
    R = env
    call(R, "browser_navigate", url="about:blank")
    call(R, "browser_evaluate", function="() => console.error('boom')")
    assert "boom" in call(R, "browser_console_messages", level="info", all=True)
    run_loop.run_until_complete(R.ctx.route("https://s.test/**", lambda r: r.fulfill(body="<title>s</title>", content_type="text/html")))
    call(R, "browser_navigate", url="https://s.test/")
    call(R, "browser_localstorage_set", key="k", value="v")
    assert val(call(R, "browser_localstorage_list")) == {"k": "v"}
    call(R, "browser_sessionstorage_set", key="a", value="b")
    assert val(call(R, "browser_sessionstorage_get", key="a")) == "b"
    call(R, "browser_localstorage_clear")
    assert val(call(R, "browser_localstorage_list")) == {}
    assert "s.test" in call(R, "browser_network_requests", static=True)
    d = tmp_path_out()
    assert "](out/net)" in call(R, "browser_network_request_get", index=1, filename="net") and "s.test" in call(R, "browser_network_request_get", index=1, filename="net")
    tree = sorted(os.path.relpath(os.path.join(r, f), d + "/net") for r, _, fs in os.walk(d + "/net") for f in fs)
    assert tree == ["request/headers.json", "response/body.html", "response/headers.json"], tree  # no request body: no file
    assert open(d + "/net/response/body.html", "rb").read() == b"<title>s</title>"
    assert json.load(open(d + "/net/response/headers.json"))["content-type"] == "text/html"
    state = {"cookies": [], "origins": [{"origin": "https://s.test", "localStorage": [{"name": "x", "value": "y"}]}]}
    call(R, "browser_set_storage_state", state=state)
    assert val(call(R, "browser_localstorage_get", key="x")) == "y"


# ------------------------------------------------------------ no-media policy
@pytest.fixture
def media_site():
    """Local site serving a page full of media plus direct media URLs; records which paths were requested."""
    import http.server
    import threading
    hits = []
    PNG = bytes(range(64))
    pages = {
        "/": ("text/html", b"<link rel=icon href=/fav.png><style>@font-face{font-family:f;src:url(/f.woff2)}"
              b"div{background:url(/bg.png);font-family:f}</style><title>site</title><img src=/a.png><img src=/s.svg>"
              b"<video src=/v.mp4></video><div>hello</div><iframe src=/i.png></iframe>"),
        "/a.png": ("image/png", PNG), "/i.png": ("image/png", PNG), "/bg.png": ("image/png", PNG), "/fav.png": ("image/png", PNG), "/v.mp4": ("video/mp4", PNG),
        "/f.woff2": ("font/woff2", PNG), "/s.svg": ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
        "/data.json": ("application/json", b"{}"), "/plain": ("text/plain", b"x"),
    }

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            ct, body = pages.get(self.path, ("text/html", b"nope"))
            self.send_response(200 if self.path in pages else 404)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", hits
    srv.shutdown()


@pytest.fixture
def nomedia(tmp_path):
    """Like env, but the policy is chosen by the test (R.policy is unset on entry)."""
    tools.OUT = str(tmp_path)
    pw, R = run_loop.run_until_complete(launch({"userDataDir": str(tmp_path / "p"), "args": ["--no-sandbox"], "headless": True}))
    yield R
    run_loop.run_until_complete(R.ctx.close())
    run_loop.run_until_complete(pw.stop())


def set_policy(R, **kw):
    call(R, "browser_set_policy", policy=Policy.make(False, **kw).to_json())


def test_policy_is_one_shot_and_not_listed(nomedia):
    set_policy(nomedia)
    with pytest.raises(Exception, match="already set"):
        set_policy(nomedia)
    assert "browser_set_policy" not in {t.name for t in TOOLS}


def test_nomedia_blocks_subresources_and_media_navigation(nomedia, media_site):
    R, (base, hits) = nomedia, media_site
    set_policy(R)
    call(R, "browser_navigate", url=base + "/")
    call(R, "browser_wait_for", time=1)
    assert "hello" in call(R, "browser_snapshot")
    assert set(hits) <= {"/", "/i.png"}, hits  # img, video, css background, favicon, font: never requested; the iframe is stopped at its headers (layer D)
    with pytest.raises(Exception, match=r"blocked: media disabled for this session \(image/png\)"):
        call(R, "browser_navigate", url=base + "/a.png")
    assert "/a.png" in hits  # layer D sees headers, so the request itself is made (documented limit)
    call(R, "browser_navigate", url=base + "/data.json")
    call(R, "browser_navigate", url=base + "/plain")
    assert "x" in call(R, "browser_evaluate", function="() => document.body.innerText")
    call(R, "browser_navigate", url=base + "/")
    net = call(R, "browser_network_requests", static=True)
    assert "/a.png => [BLOCKED] blocked: media policy" in net
    assert val(call(R, "browser_end_session"))["blocked"] >= 6


def test_allow_svg_navigation_not_img_tag(nomedia, media_site):
    R, (base, hits) = nomedia, media_site
    set_policy(R, allow=["image/svg+xml"])
    call(R, "browser_navigate", url=base + "/s.svg")  # navigation: allowed at layer D
    call(R, "browser_navigate", url=base + "/")
    call(R, "browser_wait_for", time=1)
    assert hits.count("/s.svg") == 1  # the <img src=s.svg> at layer A never loaded
    with pytest.raises(Exception, match="blocked"):
        call(R, "browser_navigate", url=base + "/a.png")


def test_second_window_inherits_policy(nomedia, media_site):
    R, (base, hits) = nomedia, media_site
    set_policy(R)
    b2 = val(call(R, "browser_start_session"))
    call(R, "browser_navigate", bid=b2, url=base + "/")
    call(R, "browser_wait_for", bid=b2, time=1)
    assert set(hits) <= {"/", "/i.png"}
    with pytest.raises(Exception, match="blocked"):
        call(R, "browser_navigate", bid=b2, url=base + "/v.mp4")


def test_deny_with_media_true(nomedia, media_site):
    R, (base, hits) = nomedia, media_site
    call(R, "browser_set_policy", policy=Policy.make(True, deny=["text/plain"]).to_json())
    call(R, "browser_navigate", url=base + "/a.png")  # media allowed
    with pytest.raises(Exception, match="blocked"):
        call(R, "browser_navigate", url=base + "/plain")


# ------------------------------------------------- browser_network_request_make
@pytest.fixture
def echo():
    import hashlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def cors(self):
            if o := self.headers.get("Origin"):
                if not self.path.startswith("/nocors"):
                    self.send_header("Access-Control-Allow-Origin", o)
                    self.send_header("Access-Control-Allow-Credentials", "true")

        def do_OPTIONS(self):
            self.send_response(204); self.cors()
            self.send_header("Access-Control-Allow-Methods", self.headers.get("Access-Control-Request-Method", "*"))
            self.send_header("Access-Control-Allow-Headers", self.headers.get("Access-Control-Request-Headers", "*")); self.end_headers()

        def do(self):
            n = int(self.headers.get("content-length") or 0)
            if self.path == "/hash":  # big bodies are only measured
                h, left = hashlib.sha256(), n
                while left:
                    c = self.rfile.read(min(left, 1 << 20)); h.update(c); left -= len(c)
                body = json.dumps({"len": n, "sha": h.hexdigest(), "ct": self.headers.get("content-type")}).encode()
                self.send_response(200); self.cors(); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            body = self.rfile.read(n)
            if self.path == "/redir":
                self.send_response(302); self.send_header("Location", "/echo"); self.end_headers(); return
            if self.path == "/redir307":
                self.send_response(307); self.send_header("Location", "/echo"); self.end_headers(); return
            if self.path == "/slow":
                time.sleep(3)
            ct, extra, data = "application/json", {}, json.dumps(
                {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body.decode("latin1")}).encode()
            if self.path == "/att":
                ct, extra, data = "text/csv", {"Content-Disposition": "attachment; filename=a.csv"}, b"a,b\n1,2\n"
            if self.path == "/img":
                ct, data = "image/png", b"\x89PNG" + bytes(50)
            if self.path == "/big":  # 3 MiB of response, to cross the chunk size of the xhr transport
                ct, data = "application/octet-stream", bytes(range(256)) * 12288
            self.send_response(200); self.cors(); self.send_header("Content-Type", ct)
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def stage(tmp_path):
    """stage(name, data) leaves a file as the gateway would: in/<random>/<name>; returns the {file, filename} part."""
    tools.IN = str(tmp_path / "in"); os.mkdir(tools.IN)
    n = [0]

    def f(name, data, **kw):
        n[0] += 1
        os.mkdir(f"{tools.IN}/d{n[0]}")
        with open(f"{tools.IN}/d{n[0]}/{name}", "wb") as fh:
            fh.write(data)
        return {"file": f"d{n[0]}/{name}", "filename": name, **kw}
    return f


def make(R, **a):
    out = call(R, "browser_network_request_make", filename="m", **a)
    d = tools.OUT + "/m"
    tree = {os.path.relpath(os.path.join(r, f), d): open(os.path.join(r, f), "rb").read() for r, _, fs in os.walk(d) for f in fs}
    shutil.rmtree(d)
    return out, tree


def test_make_post_headers_cookie_and_hidden_tab(env, echo):
    R = env
    run_loop.run_until_complete(R.ctx.add_cookies([{"name": "sid", "value": "1", "url": echo, "sameSite": "Lax"}]))
    before = len(R.tabs.get("main", []))
    out, tree = make(R, url=echo + "/echo", method="post", headers={"Content-Type": "application/json", "X-Foo": "bar"}, body='{"x":1}')
    assert out.splitlines()[2:] == [f"1. [POST] {echo}/echo => [200] OK"], out
    got = json.loads(tree["response/body.json"])
    assert (got["method"], got["body"], got["headers"]["x-foo"], got["headers"]["cookie"]) == ("POST", '{"x":1}', "bar", "sid=1"), got
    assert json.loads(tree["request/headers.json"])["content-type"] == "application/json" and tree["request/body.json"] == b'{"x":1}'
    assert len(R.tabs.get("main", [])) == before and len(R.ctx.pages) == before  # the tab belonged to no browser and is gone
    _, tree = make(R, url=echo + "/echo", headers={"cookie": "own=2"})  # an explicit Cookie wins
    assert json.loads(tree["response/body.json"])["headers"]["cookie"] == "own=2" and "request/body.json" not in tree


def test_make_redirects_attachment_blocked_errors(env, echo):
    R = env
    out, tree = make(R, url=echo + "/redir", method="POST", headers={"Content-Type": "text/plain"}, body="hi")
    assert f"1. [GET] {echo}/echo => [200]" in out and f'Redirects: [{{"url": "{echo}/redir", "status": 302}}]' in out, out  # POST -> GET
    assert "request/body.txt" not in tree and json.loads(tree["response/body.json"])["method"] == "GET"
    out, tree = make(R, url=echo + "/redir307", method="POST", headers={"Content-Type": "text/plain"}, body="hi")
    assert "[POST]" in out and tree["request/body.bin"] == b"hi" and json.loads(tree["response/body.json"])["body"] == "hi"  # 307 keeps method and body, not our headers (only the first request is rewritten)
    out, tree = make(R, url=echo + "/att")
    assert tree["response/body.csv"] == b"a,b\n1,2\n" and "[200]" in out, (out, list(tree))
    with pytest.raises(tools.ToolError, match="ERR_"):
        make(R, url="http://127.0.0.1:1/")
    with pytest.raises(tools.ToolError, match="Timeout"):
        make(R, url=echo + "/slow", timeout_ms=500)
    for bad in ({"url": "file:///etc/passwd"}, {"url": echo, "method": "G E T"}, {"url": echo, "headers": {"a": 1}},
                {"url": echo, "body": "x", "body_file": "y"}, {"url": echo, "type": "nope"}, {"url": echo, "origin": echo},
                {"url": echo, "credentials": "omit"}, {"url": echo, "headers": {"Host": "x"}}, {"url": echo, "cookies": {"a": "b"}, "headers": {"Cookie": "x"}}):
        with pytest.raises(tools.ToolError):
            make(R, **bad)


def test_make_staged_files_are_read_and_deleted(env, echo, stage):
    R = env
    f = stage("b.bin", b"\x00\x01bytes")
    _, tree = make(R, url=echo + "/echo", method="PUT", body_file=f["file"])
    assert json.loads(tree["response/body.json"])["body"] == "\x00\x01bytes" and not os.listdir(tools.IN)
    assert "request/body.bin" not in tree  # a job-sourced body is not stored a second time
    for bad in ("nope", "../x", "a/../b", "/etc/passwd", "d1/missing"):
        with pytest.raises(tools.ToolError, match="not found"):
            make(R, url=echo + "/echo", body_file=bad)
    g = stage("g.txt", b"x")
    with pytest.raises(tools.ToolError, match="fields only"):  # staged files are removed even when validation fails
        make(R, type="form", url=echo + "/echo", method="POST", body_file=g["file"])
    assert not os.listdir(tools.IN)


def test_make_navigation_fields_and_cookies(env, echo, stage):
    R = env
    run_loop.run_until_complete(R.ctx.add_cookies([{"name": "sid", "value": "1", "url": echo}, {"name": "x", "value": "jar", "url": echo}]))
    _, tree = make(R, url=echo + "/echo", method="POST", body={"a": "1 2", "n": 3, "t": True, "l": ["x", "y"]}, cookies={"x": "own", "k": 2.0})
    got = json.loads(tree["response/body.json"])
    assert got["body"] == "a=1+2&n=3&t=true&l=x&l=y" and tree["request/body.bin"] == b"a=1+2&n=3&t=true&l=x&l=y"  # JS String() of values
    assert got["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert got["headers"]["cookie"] == "sid=1; x=own; k=2", got["headers"]  # jar plus cookies, winning by name, never stored
    assert not [c for c in run_loop.run_until_complete(R.ctx.cookies(echo)) if c["name"] == "k"]
    _, tree = make(R, url=echo + "/echo?q=1", body={"a": "b"})  # GET: query string
    assert json.loads(tree["response/body.json"])["path"] == "/echo?q=1&a=b"
    up = stage("up.txt", b"hello")
    _, tree = make(R, url=echo + "/echo", method="POST", body={"f": {**up, "content_type": "text/x-foo"}, "k": "v"})
    got = json.loads(tree["response/body.json"])
    assert got["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    assert 'name="f"; filename="up.txt"\r\nContent-Type: text/x-foo\r\n\r\nhello' in got["body"] and 'name="k"\r\n\r\nv' in got["body"]
    _, tree = make(R, url=echo + "/echo", method="POST", headers={"Content-Type": "text/plain"}, body={"a": "b", "c": 1})
    assert json.loads(tree["response/body.json"])["body"] == "a=b\r\nc=1\r\n"
    for bad in ({"body": {"a": None}}, {"body": {"f": stage("z", b"")}, "headers": {"Content-Type": "text/plain"}, "method": "POST"},
                {"body": {"a": "b"}, "headers": {"Content-Type": "application/json"}, "method": "POST"}):
        with pytest.raises(tools.ToolError):
            make(R, url=echo + "/echo", **bad)


def test_make_form(env, echo, stage):
    R = env
    run_loop.run_until_complete(R.ctx.add_cookies([{"name": "sid", "value": "1", "url": echo}]))
    out, tree = make(R, type="form", origin=echo + "/page#frag", url=echo + "/echo", method="POST", body={"a": "1", "l": ["x", "y"]})
    got = json.loads(tree["response/body.json"])
    assert got["body"] == "a=1&l=x&l=y" and got["headers"]["origin"] == echo and got["headers"]["referer"] == echo + "/page"
    assert got["headers"]["cookie"] == "sid=1" and f"Origin: {echo}/page" in out and "[POST]" in out, (out, got)
    _, tree = make(R, type="form", url=echo + "/echo?old=1", body={"a": "b"})  # GET: replaces the query string, like a browser
    assert json.loads(tree["response/body.json"])["path"] == "/echo?a=b"
    up = stage("up.txt", b"hello")
    _, tree = make(R, type="form", url=echo + "/echo", method="POST", body={"f": {**up, "filename": up["filename"], "content_type": "text/x-foo"}})
    assert 'filename="up.txt"\r\nContent-Type: text/x-foo\r\n\r\nhello' in json.loads(tree["response/body.json"])["body"]
    up = stage("up2.txt", b"hello2")  # rewritten request: method, header and cookie; the browser's multipart Content-Type survives
    out, tree = make(R, type="form", url=echo + "/echo", method="PUT", headers={"X-Foo": "bar"}, cookies={"c": "2"}, body={"f": up, "k": "v"})
    got = json.loads(tree["response/body.json"])
    assert (got["method"], got["headers"]["x-foo"], got["headers"]["cookie"]) == ("PUT", "bar", "sid=1; c=2"), got
    assert got["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    assert 'filename="up2.txt"' in got["body"] and "hello2" in got["body"] and 'name="k"\r\n\r\nv' in got["body"] and "[PUT]" in out, got
    _, tree = make(R, type="form", url=echo + "/redir", method="POST", body={"a": "b"})
    assert json.loads(tree["response/body.json"])["method"] == "GET"
    with pytest.raises(tools.ToolError, match="content_type"):
        make(R, type="form", url=echo + "/echo", method="PUT", body={"f": {**stage("c.txt", b"c"), "content_type": "text/x-foo"}})
    with pytest.raises(tools.ToolError, match="sends only the filename"):
        make(R, type="form", url=echo + "/echo", body={"f": stage("d.txt", b"d")})
    assert not os.listdir(tools.IN)


def test_make_xhr(env, echo, stage):
    R = env
    run_loop.run_until_complete(R.ctx.add_cookies([{"name": "sid", "value": "1", "url": echo}]))
    out, tree = make(R, type="xhr", url=echo + "/echo", method="POST", body="hi")  # raw text: no Content-Type inferred
    got = json.loads(tree["response/body.json"])
    assert got["body"] == "hi" and "content-type" not in got["headers"] and got["headers"]["cookie"] == "sid=1", got
    assert got["headers"]["origin"] == echo and "[POST]" in out and tree["request/body.bin"] == b"hi"
    _, tree = make(R, type="xhr", url=echo + "/echo", method="PUT", headers={"Content-Type": "application/json", "X-Foo": "bar"}, body='{"x":1}')
    got = json.loads(tree["response/body.json"])
    assert (got["method"], got["headers"]["content-type"], got["headers"]["x-foo"], got["body"]) == ("PUT", "application/json", "bar", '{"x":1}')
    f = stage("raw.bin", b"\x00\xffraw")
    _, tree = make(R, type="xhr", url=echo + "/echo", method="PUT", body_file=f["file"])
    got = json.loads(tree["response/body.json"])
    assert got["body"] == "\x00\xffraw" and "content-type" not in got["headers"]
    up = stage("up.txt", b"hello")
    _, tree = make(R, type="xhr", url=echo + "/echo", method="POST", body={"f": {**up, "filename": "n.txt", "content_type": "text/x-foo"}, "k": "v", "l": ["1", "2"]})
    got = json.loads(tree["response/body.json"])
    assert 'name="f"; filename="n.txt"\r\nContent-Type: text/x-foo\r\n\r\nhello' in got["body"] and 'name="l"\r\n\r\n2' in got["body"], got
    _, tree = make(R, type="xhr", url=echo + "/echo", method="POST", body={"f": stage("e.txt", b"e")})  # type from the extension
    assert "filename=\"e.txt\"\r\nContent-Type: text/plain" in json.loads(tree["response/body.json"])["body"]
    _, tree = make(R, type="xhr", url=echo + "/echo", method="POST", body={"a": "b c"})
    got = json.loads(tree["response/body.json"])
    assert got["body"] == "a=b+c" and got["headers"]["content-type"] == "application/x-www-form-urlencoded;charset=UTF-8"
    _, tree = make(R, type="xhr", url=echo + "/echo", method="POST", headers={"Content-Type": "text/plain"}, body={"a": "b"})
    assert json.loads(tree["response/body.json"])["body"] == "a=b\r\n"
    _, tree = make(R, type="xhr", url=echo + "/echo?q=1", body={"a": "b"})
    assert json.loads(tree["response/body.json"])["path"] == "/echo?q=1&a=b"
    out, tree = make(R, type="xhr", url=echo + "/big")  # more than one transport chunk, byte exact
    assert tree["response/body.bin"] == bytes(range(256)) * 12288 and "[200]" in out
    out, tree = make(R, type="xhr", url=echo + "/redir", method="POST", body="hi")
    assert f"{echo}/echo => [200]" in out and "Redirects:" in out and json.loads(tree["response/body.json"])["method"] == "GET"
    # cross origin: localhost vs 127.0.0.1
    other = echo.replace("127.0.0.1", "localhost")
    _, tree = make(R, type="xhr", url=other + "/echo", origin=echo + "/", method="PUT", headers={"X-A": "1"}, body="z")
    got = json.loads(tree["response/body.json"])
    assert got["headers"]["origin"] == echo and got["headers"]["x-a"] == "1" and got["method"] == "PUT"
    with pytest.raises(tools.ToolError, match="not readable"):
        make(R, type="xhr", url=other + "/nocors", origin=echo + "/")
    with pytest.raises(tools.ToolError, match="ERR_"):
        make(R, type="xhr", url="http://127.0.0.1:1/", origin=echo + "/")
    with pytest.raises(tools.ToolError, match="Timeout"):
        make(R, type="xhr", url=echo + "/slow", timeout_ms=500)
    for bad in ({"headers": {"Cookie": "a=b"}}, {"headers": {"User-Agent": "x"}}, {"headers": {"Sec-Foo": "x"}}, {"cookies": {"a": "b"}},
                {"method": "TRACE"}, {"body": "x"}, {"credentials": "sometimes"}):
        with pytest.raises(tools.ToolError):
            make(R, type="xhr", url=echo + "/echo", **bad)
    assert not os.listdir(tools.IN) and len(R.ctx.pages) == len(R.tabs.get("main", []))


def test_make_large_files(env, echo, stage):
    R = env
    n = 150 << 20
    f = stage("big.bin", b"")
    with open(f"{tools.IN}/{f['file']}", "wb") as fh:
        fh.truncate(n)  # zeros: the sha is that of n zero bytes
    import hashlib
    h = hashlib.sha256()
    for _ in range(n >> 20):
        h.update(bytes(1 << 20))
    _, tree = make(R, type="xhr", url=echo + "/hash", method="PUT", body_file=f["file"], timeout_ms=120_000)
    assert json.loads(tree["response/body.json"]) == {"len": n, "sha": h.hexdigest(), "ct": None}
    g = stage("big2.bin", b"")
    with open(f"{tools.IN}/{g['file']}", "wb") as fh:
        fh.truncate(n)
    _, tree = make(R, type="form", url=echo + "/hash", method="POST", body={"f": g}, timeout_ms=120_000)
    got = json.loads(tree["response/body.json"])
    assert n < got["len"] < n + 1000 and got["ct"].startswith("multipart/form-data")
    h2 = stage("big3.bin", b"")
    with open(f"{tools.IN}/{h2['file']}", "wb") as fh:
        fh.truncate(n)
    _, tree = make(R, type="form", url=echo + "/hash", method="PUT", headers={"X-A": "1"}, body={"f": h2}, timeout_ms=120_000)  # rewritten under the pause
    assert n < json.loads(tree["response/body.json"])["len"] < n + 1000


def test_make_navigation_body_cap(env, echo, stage):
    f = stage("big.bin", b"")
    with open(f"{tools.IN}/{f['file']}", "wb") as fh:
        fh.truncate(tools.CDP_BODY_MAX + 1)
    with pytest.raises(tools.ToolError, match="too large"):
        make(env, url=echo + "/echo", method="POST", body_file=f["file"])
    assert not os.listdir(tools.IN)


def test_make_under_media_policy(nomedia, echo, stage):
    R = nomedia
    set_policy(R)  # layer A (route) and D (Fetch on the response) are both on the hidden tab
    _, tree = make(R, url=echo + "/echo", method="POST", headers={"Content-Type": "text/plain"}, body="hi")
    assert json.loads(tree["response/body.json"])["method"] == "POST" and tree["request/body.txt"] == b"hi"
    out, tree = make(R, url=echo + "/img")
    assert "[BLOCKED] blocked: media policy" in out and not any(p.startswith("response/body") for p in tree), (out, list(tree))
    assert json.loads(tree["response/headers.json"])["content-type"] == "image/png" and "request/headers.json" in tree
    assert R.blocked[-1]["layer"] == "D" and R.blocked[-1]["url"].endswith("/img")
    for kw in ({"type": "form", "method": "POST", "body": {"a": "b"}}, {"type": "xhr"}):  # layer D for every type
        out, tree = make(R, url=echo + "/img", **kw)
        assert "[BLOCKED] blocked: media policy" in out and not any(p.startswith("response/body") for p in tree), (kw, out, list(tree))
        assert json.loads(tree["response/headers.json"])["content-type"] == "image/png" and "request/headers.json" in tree
    assert [b["layer"] for b in R.blocked].count("D") >= 3
    _, tree = make(R, type="xhr", url=echo + "/echo", method="POST", body="hi")  # allowed ones still work
    assert json.loads(tree["response/body.json"])["body"] == "hi"
    big = stage("big.bin", b"")  # layer A pauses every request: a big upload would kill the browser, so it is an error
    with open(f"{tools.IN}/{big['file']}", "wb") as fh:
        fh.truncate(tools.ROUTED_UPLOAD_CAP + 1)
    with pytest.raises(tools.ToolError, match="limited to"):
        make(R, type="xhr", url=echo + "/hash", method="PUT", body_file=big["file"])
