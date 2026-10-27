"""Per-tool checks against a real headless Chromium (needs playwright + its Chromium; the
container smoke test covers headed mode). Run: pytest test_toolserver.py"""

import asyncio
import json
import os
import tempfile

import pytest

from toolserver import tools
from toolserver.browsers import launch
from toolserver.mediapolicy import Policy
from toolserver.server import GOLDEN, TOOLS

PAGE = ("data:text/html,<title>T</title><input id=i><button onclick=\"document.title='clicked'\">Go</button>"
        "<a href='data:text/html,two'>link</a>")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path):
    tools.OUT = str(tmp_path)

    async def mk():
        return await launch({"userDataDir": str(tmp_path / "p"), "args": ["--no-sandbox"], "headless": True})
    pw, R = run_loop.run_until_complete(mk())
    run_loop.run_until_complete(R.set_policy(Policy.make("clear")))
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
    call(R, "browser_network_request", index=i, filename="img")
    assert open(tools.OUT + "/img/response/body.png", "rb").read() == png


def val(text):
    return json.loads(text.split("### Result\n", 1)[1].split("\n### ", 1)[0])


def test_schemas_match_golden():
    golden = {t["name"]: t for t in json.load(open(GOLDEN))}
    ours = {t.name: t for t in TOOLS}
    assert set(golden) - set(ours) == {"browser_run_code_unsafe"}
    for n, g in golden.items():
        if n in ours:
            props = dict(ours[n].inputSchema["properties"])
            assert props.pop("browser")
            if n in ("browser_snapshot", "browser_find"):
                assert props.pop("text_cap")["default"] == 128
            want = dict(g["inputSchema"].get("properties", {}))
            if n == "browser_network_requests":
                assert props.pop("new")["type"] == "boolean"
            if n == "browser_network_request":
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
    assert "](out/net)" in call(R, "browser_network_request", index=1, filename="net") and "s.test" in call(R, "browser_network_request", index=1, filename="net")
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
    call(R, "browser_set_policy", policy=Policy.make("tor", **kw).to_json())


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
    call(R, "browser_set_policy", policy=Policy.make("clear", deny=["text/plain"]).to_json())
    call(R, "browser_navigate", url=base + "/a.png")  # media allowed
    with pytest.raises(Exception, match="blocked"):
        call(R, "browser_navigate", url=base + "/plain")
