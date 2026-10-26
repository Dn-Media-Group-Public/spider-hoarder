"""The tool set. Each handler is `async (R, bid, args) -> str` returning response sections
(`### Result`, `### Page`; errors are raised and formatted by server.py).
Schemas for the playwright-mcp tools come from tools.json (from tools.json.upstream, 0.0.83), see server.py."""

import asyncio
import base64
import json
import mimetypes
import os
import re
import time

from playwright.async_api import Error as PWError

from .browsers import MAIN, Registry, ToolError, locator
from .textcap import DEFAULT_TEXT_CAP, cap_found_line, cap_snapshot

OUT = os.environ.get("TOOLSERVER_OUT", "out")
ACT_TIMEOUT = 10_000  # ms; a stale ref or hidden element fails fast
HANDLERS: dict = {}


def tool(name):
    def deco(f):
        HANDLERS[name] = f
        return f
    return deco


def out_path(name: str) -> str:
    """Files only ever land directly in the out dir, whatever the agent asks for."""
    base = os.path.basename(str(name).replace("\\", "/"))
    if base in ("", ".", ".."):
        raise ToolError("invalid filename")
    return os.path.join(OUT, base)


def write_out(name: str, data: bytes | str) -> str:
    path = out_path(name)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    return os.path.basename(path)


def stamp(ext: str) -> str:
    return f"page-{time.strftime('%Y-%m-%dT%H-%M-%S')}-{int(time.time() * 1000) % 1000:03d}.{ext}"


def result(text: str) -> str:
    return f"### Result\n{text}"


def file_result(label: str, name: str) -> str:
    return result(f"- [{label}](out/{name})")


async def page_info(page) -> str:
    title = await page.title()
    return f"### Page\n- Page URL: {page.url}\n" + (f"- Page Title: {title}\n" if title else "")


async def snapshot_text(page, target=None, depth=None, boxes=None) -> str:
    kw = {"mode": "ai", **({"depth": int(depth)} if depth else {}), **({"boxes": True} if boxes else {})}
    return await (locator(page, target) if target else page).aria_snapshot(**kw)


async def post(R: Registry, bid: str, text: str = "") -> str:
    """Response after an action: optional result, page and modal state. No snapshot: refs come from
    browser_snapshot / browser_find."""
    await R.settle()
    page = R.page(bid)
    st = R.state.get(page)
    out = (text + "\n" if text else "") + await page_info(page)
    if st and (st.dialog or st.chooser):
        out += "### Modal state\n" + (f"- [{st.dialog.type} dialog]: {st.dialog.message!r} (use browser_handle_dialog)\n" if st.dialog else "") \
            + ("- [file chooser] (use browser_file_upload)\n" if st.chooser else "")
    return out


def loc(page, target):
    if not target:
        raise ToolError("target is required")
    return locator(page, target)


async def act(page, target, fn):
    """Run fn(locator); a missing ref is reported as such rather than as a timeout."""
    l = loc(page, target)
    try:
        return await fn(l.first if not re.match(r"^(f\d+)?e\d+$", target) else l)
    except PWError as e:
        is_ref = re.match(r"^(f\d+)?e\d+$", target)
        if is_ref and ("Invalid frame in aria-ref" in str(e) or "Timeout" in str(e) and await l.count() == 0):  # stale ref: its frame is gone, or nothing matches
            raise ToolError(f"Ref {target} not found in the current page snapshot. Try capturing new snapshot.")
        raise


# ---------------------------------------------------------------- navigation
@tool("browser_navigate")
async def navigate(R, bid, a):
    url = a["url"]
    if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
        url = "https://" + url
    await R.page(bid).goto(url)
    return await post(R, bid)


@tool("browser_navigate_back")
async def navigate_back(R, bid, a):
    await R.page(bid).go_back()
    return await post(R, bid)


@tool("browser_wait_for")
async def wait_for(R, bid, a):
    page = R.page(bid)
    if a.get("time"):
        await asyncio.sleep(min(float(a["time"]), 30))
    if a.get("text"):
        await page.get_by_text(a["text"]).first.wait_for(state="visible", timeout=30_000)
    if a.get("textGone"):
        await page.get_by_text(a["textGone"]).first.wait_for(state="hidden", timeout=30_000)
    return await post(R, bid)


@tool("browser_resize")
async def resize(R, bid, a):
    cdp, wid = await R.window_id(bid)
    await cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"width": int(a["width"]), "height": int(a["height"]), "windowState": "normal"}})
    await cdp.detach()
    return await post(R, bid)


# --------------------------------------------------------------- interaction
def text_cap(a) -> int | None:
    """`text_cap`: omitted = default, null = no cap."""
    cap = a.get("text_cap", DEFAULT_TEXT_CAP)
    if cap is None:
        return None
    if int(cap) < 1:
        raise ToolError("text_cap must be at least 1, or null for no cap")
    return int(cap)


@tool("browser_snapshot")
async def snapshot(R, bid, a):
    page = R.page(bid)
    snap = cap_snapshot(await snapshot_text(page, a.get("target"), a.get("depth"), a.get("boxes")), text_cap(a))
    if a.get("filename"):
        return await page_info(page) + f"### Snapshot\n- [Snapshot](out/{write_out(a['filename'], snap)})\n"
    return await page_info(page) + f"### Snapshot\n```yaml\n{snap}\n```\n"


FIND_CONTEXT = 3  # lines of context around each match (as playwright-mcp's browser_find)


def _ancestors(lines: list[str], indents: list[int], index: int) -> list[int]:
    """Indices of the lines above `index` that enclose it (strictly smaller indent), outermost first."""
    out, indent = [], indents[index]
    for i in range(index - 1, -1, -1):
        if indent <= 0:
            break
        if lines[i].strip() and indents[i] < indent:
            out.append(i)
            indent = indents[i]
    return out[::-1]


def find_snippets(snap: str, match, query: str, cap: int | None = None) -> str:
    """Matching snapshot lines with FIND_CONTEXT lines of context, each window under its ancestor chain
    (the lines that enclose it, so the refs of its containers are visible); `...` marks skipped lines.
    `match(line)` is falsy, or truthy (a (start, end) tuple centers the `cap` window on the match)."""
    lines = snap.split("\n")
    indents = [len(l) - len(l.lstrip()) for l in lines]
    found = {i: match(l) for i, l in enumerate(lines)}
    hits = [i for i, r in found.items() if r]
    spans = {i: found[i] if isinstance(found[i], tuple) else None for i in hits}
    if not hits:
        return f"No matches found for {query}."
    windows: list[list[int]] = []
    for h in hits:
        lo, hi = max(0, h - FIND_CONTEXT), min(len(lines) - 1, h + FIND_CONTEXT)
        if windows and lo <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])
    on_path = set(hits).union(*(_ancestors(lines, indents, h) for h in hits))
    snippets = []
    for lo, hi in windows:
        idx = _ancestors(lines, indents, lo) + list(range(lo, hi + 1))
        rows = []
        for k, i in enumerate(idx):
            if k and i > idx[k - 1] + 1 and i not in on_path and idx[k - 1] not in on_path:
                rows.append(" " * indents[i] + "...")
            rows.append(cap_found_line(lines[i], spans.get(i), cap))
        snippets.append("\n".join(rows))
    return f"Found {len(hits)} {'match' if len(hits) == 1 else 'matches'} for {query}:\n\n" + "\n\n----\n\n".join(snippets)


@tool("browser_find")
async def find(R, bid, a):
    if bool(a.get("text")) == bool(a.get("regex")):
        raise ToolError("Provide either text or regex, not both.")
    snap = await snapshot_text(R.page(bid))
    if a.get("text"):
        needle = a["text"].lower()
        match, query = (lambda l: (j, j + len(needle)) if (j := l.lower().find(needle)) >= 0 else None), f'"{a["text"]}"'
    else:
        m = re.fullmatch(r"/(.*)/([a-z]*)", a["regex"], re.S)
        rx = re.compile(m.group(1) if m else a["regex"], re.I if m and "i" in m.group(2) else 0)
        match, query = (lambda l: m_.span() if (m_ := rx.search(l)) else None), f"/{m.group(1) if m else a['regex']}/{m.group(2) if m else ''}"
    body = find_snippets(snap, match, query, text_cap(a))
    page = await page_info(R.page(bid))
    if a.get("filename"):
        return page + file_result("Matches", write_out(a["filename"], body))
    return page + result(body)


@tool("browser_click")
async def click(R, bid, a):
    await act(R.page(bid), a.get("target"), lambda l: l.click(
        button=a.get("button", "left"), click_count=2 if a.get("doubleClick") else 1,
        modifiers=a.get("modifiers"), timeout=ACT_TIMEOUT))
    return await post(R, bid)


@tool("browser_hover")
async def hover(R, bid, a):
    await act(R.page(bid), a.get("target"), lambda l: l.hover(timeout=ACT_TIMEOUT))
    return await post(R, bid)


@tool("browser_type")
async def type_(R, bid, a):
    async def go(l):
        if a.get("slowly"):
            await l.press_sequentially(a["text"], timeout=ACT_TIMEOUT)
        else:
            await l.fill(a["text"], timeout=ACT_TIMEOUT)
        if a.get("submit"):
            await l.press("Enter", timeout=ACT_TIMEOUT)
    await act(R.page(bid), a.get("target"), go)
    return await post(R, bid)


@tool("browser_fill_form")
async def fill_form(R, bid, a):
    for f in a["fields"]:
        async def go(l, f=f):
            t, v = f["type"], f["value"]
            if t in ("checkbox", "radio"):
                await l.set_checked(v == "true" or t == "radio", timeout=ACT_TIMEOUT)
            elif t == "combobox":
                await l.select_option(label=v, timeout=ACT_TIMEOUT)
            else:
                await l.fill(v, timeout=ACT_TIMEOUT)
        await act(R.page(bid), f.get("target"), go)
    return await post(R, bid)


@tool("browser_press_key")
async def press_key(R, bid, a):
    await R.page(bid).keyboard.press(a["key"])
    return await post(R, bid)


@tool("browser_select_option")
async def select_option(R, bid, a):
    await act(R.page(bid), a.get("target"), lambda l: l.select_option(a["values"], timeout=ACT_TIMEOUT))
    return await post(R, bid)


@tool("browser_drag")
async def drag(R, bid, a):
    page = R.page(bid)
    await loc(page, a.get("startTarget")).drag_to(loc(page, a.get("endTarget")), timeout=ACT_TIMEOUT)
    return await post(R, bid)


DROP_JS = """(el, {files, data}) => {
  const dt = new DataTransfer();
  for (const f of files) dt.items.add(new File([Uint8Array.from(atob(f.b64), c => c.charCodeAt(0))], f.name));
  for (const [k, v] of Object.entries(data)) dt.setData(k, v);
  for (const t of ['dragenter', 'dragover', 'drop']) el.dispatchEvent(new DragEvent(t, {dataTransfer: dt, bubbles: true, cancelable: true}));
}"""


@tool("browser_drop")
async def drop(R, bid, a):
    files = [{"name": os.path.basename(p), "b64": base64.b64encode(open(p, "rb").read()).decode()} for p in a.get("paths") or []]
    await act(R.page(bid), a.get("target"), lambda l: l.evaluate(DROP_JS, {"files": files, "data": a.get("data") or {}}, timeout=ACT_TIMEOUT))
    return await post(R, bid)


@tool("browser_evaluate")
async def evaluate(R, bid, a):
    page = R.page(bid)
    if a.get("target"):
        v = await act(page, a["target"], lambda l: l.evaluate(a["function"]))
    else:
        v = await page.evaluate(a["function"])
    text = json.dumps(v) if v is not None else "undefined"
    if a.get("filename"):
        return file_result("Result", write_out(a["filename"], text))
    return result(text)


@tool("browser_handle_dialog")
async def handle_dialog(R, bid, a):
    st = R.state[R.page(bid)]
    if not st.dialog:
        raise ToolError("No dialog visible")
    d, st.dialog = st.dialog, None
    await (d.accept(a.get("promptText")) if a["accept"] else d.dismiss())
    return await post(R, bid)


@tool("browser_file_upload")
async def file_upload(R, bid, a):
    st = R.state[R.page(bid)]
    if not st.chooser:
        raise ToolError("No file chooser visible")
    c, st.chooser = st.chooser, None
    await c.set_files(a.get("paths") or [])
    return await post(R, bid)


@tool("browser_emulate_media")
async def emulate_media(R, bid, a):
    keys = {"colorScheme": "color_scheme", "reducedMotion": "reduced_motion", "forcedColors": "forced_colors", "contrast": "contrast", "media": "media"}
    await R.page(bid).emulate_media(**{py: ("null" if a[k] is None else a[k]) for k, py in keys.items() if k in a})
    return await post(R, bid)


# ------------------------------------------------------------------- outputs
@tool("browser_take_screenshot")
async def screenshot(R, bid, a):
    page, typ = R.page(bid), a.get("type")
    name = a.get("filename") or stamp(typ or "png")
    kw = {"type": typ or (os.path.splitext(name)[1][1:].replace("jpg", "jpeg") if os.path.splitext(name)[1][1:] in ("jpeg", "jpg", "webp") else "png"),
          "scale": a.get("scale", "css")}
    if a.get("target"):
        data = await act(page, a["target"], lambda l: l.screenshot(**kw))
    else:
        data = await page.screenshot(full_page=bool(a.get("fullPage")), **kw)
    return file_result("Screenshot", write_out(name, data))


@tool("browser_pdf_save")
async def pdf_save(R, bid, a):
    cdp = await R.ctx.new_cdp_session(R.page(bid))
    try:
        data = base64.b64decode((await cdp.send("Page.printToPDF", {"printBackground": True}))["data"])
    finally:
        await cdp.detach()
    return file_result("PDF", write_out(a.get("filename") or stamp("pdf"), data))


LEVELS = ["error", "warning", "info", "debug"]


@tool("browser_console_messages")
async def console_messages(R, bid, a):
    st = R.state[R.page(bid)]
    msgs = st.console if a.get("all") else st.console[st.console_mark:]
    lvl = LEVELS.index(a.get("level", "info"))
    norm = lambda t: {"warn": "warning", "log": "info"}.get(t, t)
    shown = [m for m in msgs if norm(m[0]) in LEVELS and LEVELS.index(norm(m[0])) <= lvl]
    head = f"Total messages: {len(msgs)} (Errors: {sum(norm(m[0]) == 'error' for m in msgs)}, Warnings: {sum(norm(m[0]) == 'warning' for m in msgs)})"
    body = head + "\n\n" + "\n".join(f"[{norm(l).upper()}] {t}" + (f" @ {loc_}" if loc_ else "") for l, t, loc_ in shown)
    if a.get("filename"):
        return file_result("Console messages", write_out(a["filename"], body))
    return result(body)


STATIC = {"image", "font", "stylesheet", "script", "media"}


def net_line(i, e):
    r, resp = e["req"], e["resp"]
    return f"{i}. [{r.method}] {r.url} => " + (f"[{resp.status}] {resp.status_text}" if resp else f"[FAILED] {r.failure}")


@tool("browser_network_requests")
async def network_requests(R, bid, a):
    rx = None
    if a.get("filter"):
        m = re.fullmatch(r"/(.*)/([a-z]*)", a["filter"], re.S)
        rx = re.compile(m.group(1) if m else a["filter"], re.I if m and "i" in m.group(2) else 0)
    st = R.state[R.page(bid)]
    start, end = (st.net_mark if a.get("new") else 0), len(st.network)
    if a.get("new"):
        st.net_mark = end  # moves whether or not filter/static hide some of them
    lines = [net_line(i, e) for i, e in enumerate(st.network[start:end], start + 1)
             if (a.get("static") or not e["resp"] or e["resp"].ok is False or e["req"].resource_type not in STATIC)
             and (not rx or rx.search(e["req"].url))]
    if a.get("new") and not lines:
        return result("No new requests.")
    body = "\n".join(lines)
    if a.get("filename"):
        return file_result("Network requests", write_out(a["filename"], body))
    return result(body)


EXT_FIX = {"application/xml": ".xml", "application/javascript": ".js"}  # what mimetypes gets wrong or lacks


def body_ext(headers: dict) -> str:
    ct = headers.get("content-type", "").split(";")[0].strip().lower()
    return EXT_FIX.get(ct) or mimetypes.guess_extension(ct) or ".bin"


@tool("browser_network_request")
async def network_request(R, bid, a):
    """Writes a directory (named by `filename`): request/ and response/, each headers.json and body.<ext>,
    only the parts that exist."""
    net = R.state[R.page(bid)].network
    if not 1 <= a["index"] <= len(net):
        raise ToolError(f"No request with index {a['index']}")
    if not a.get("filename"):
        raise ToolError("filename is required")
    e = net[a["index"] - 1]
    req, resp = e["req"], e["resp"]
    root = out_path(a["filename"])
    parts = {}  # relative path -> bytes
    hdrs = await req.all_headers()
    parts["request/headers.json"] = json.dumps(hdrs, indent=1).encode()
    if (body := req.post_data_buffer) is not None:
        parts[f"request/body{body_ext(hdrs)}"] = body
    if resp:
        hdrs = await resp.all_headers()
        parts["response/headers.json"] = json.dumps(hdrs, indent=1).encode()
        try:
            parts[f"response/body{body_ext(hdrs)}"] = await resp.body()
        except PWError:  # e.g. redirects and evicted bodies have none
            pass
    for rel, data in parts.items():
        os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(root, rel), "wb") as f:
            f.write(data)
    return file_result("Network request", os.path.basename(root)) + "\n" + net_line(a["index"], e)


# --------------------------------------------------------------------- tabs
@tool("browser_tabs")
async def tabs(R, bid, a):
    act_, idx = a["action"], a.get("index")
    page = None
    if idx is not None:
        if not 0 <= int(idx) < len(R.tabs[bid]):
            raise ToolError(f"Tab {idx} not found")
        page = R.tabs[bid][int(idx)]
    if act_ == "new":
        await R.new_tab(bid, a.get("url"))
    elif act_ == "select":
        R.cur[bid] = page or R.cur[bid]
        await R.cur[bid].bring_to_front()
    elif act_ == "close":
        await close_tab(R, bid, page)
    if bid not in R.tabs:
        return result("Browser closed.")
    out = await tab_list(R, bid)
    return out if act_ == "list" else out + "\n" + await post(R, bid)


async def tab_list(R, bid) -> str:
    rows = []
    for i, p in enumerate(R.tabs[bid]):
        rows.append(f"- {i}:{' (current)' if p is R.cur[bid] else ''} [{await p.title()}]({p.url})")
    return result("\n".join(rows))


async def close_tab(R, bid, page=None):
    page = page or R.cur[bid]
    if len(R.tabs[bid]) == 1 and len(R.tabs) == 1:
        # the last tab of the last browser stays (Chromium would exit); browser_end_session ends it
        await page.goto("about:blank")
        return
    await page.close()
    await R.settle()


@tool("browser_close")
async def close(R, bid, a):
    await close_tab(R, bid)
    return await tab_list(R, bid) if bid in R.tabs else result("Browser closed.")


# ------------------------------------------------------------------ sessions
@tool("browser_start_session")
async def start_session(R, bid, a):
    return result(json.dumps(await R.start()))


@tool("browser_end_session")
async def end_session(R, bid, a):
    remaining = await R.end(a.get("browser"))
    return result(json.dumps({"remaining": remaining}))


@tool("browser_bring_to_front")
async def bring_to_front(R, bid, a):
    await R.page(bid).bring_to_front()
    return result("ok")


# ------------------------------------------------------------------ storage
@tool("browser_cookie_list")
async def cookie_list(R, bid, a):
    cs = await R.ctx.cookies(a["url"]) if a.get("url") else await R.ctx.cookies()
    for k in ("domain", "path"):
        if a.get(k):
            cs = [c for c in cs if c[k] == a[k]]
    return result(json.dumps(cs))


@tool("browser_cookie_get")
async def cookie_get(R, bid, a):
    cs = [c for c in await R.ctx.cookies() if c["name"] == a["name"]]
    return result(json.dumps(cs[0] if cs else None))


@tool("browser_cookie_set")
async def cookie_set(R, bid, a):
    c = {k: a[k] for k in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite") if k in a}
    if "domain" not in c and "path" not in c:
        c["url"] = R.page(bid).url
    await R.ctx.add_cookies([c])
    return result("ok")


@tool("browser_cookie_delete")
async def cookie_delete(R, bid, a):
    await R.ctx.clear_cookies(name=a["name"], **{k: a[k] for k in ("domain", "path") if k in a})
    return result("ok")


@tool("browser_cookie_clear")
async def cookie_clear(R, bid, a):
    await R.ctx.clear_cookies()
    return result("ok")


def web_storage(kind):
    """localStorage / sessionStorage of the browser's current page: list, get, set, delete, clear."""
    js = {
        "list": f"() => Object.fromEntries(Object.entries({kind}))",
        "get": f"k => {kind}.getItem(k)",
        "set": f"([k, v]) => {kind}.setItem(k, v)",
        "delete": f"k => {kind}.removeItem(k)",
        "clear": f"() => {kind}.clear()",
    }
    argf = {"list": lambda a: None, "get": lambda a: a["key"], "set": lambda a: [a["key"], a["value"]],
            "delete": lambda a: a["key"], "clear": lambda a: None}
    for op in js:
        async def h(R, bid, a, op=op):
            v = await R.page(bid).evaluate(js[op], argf[op](a))
            return result(json.dumps(v) if op in ("list", "get") else "ok")
        HANDLERS[f"browser_{kind.lower()}_{op}"] = h


web_storage("localStorage")
web_storage("sessionStorage")


@tool("browser_storage_state")
async def storage_state(R, bid, a):
    return file_result("Storage state", write_out(a.get("filename") or "storage-state.json", json.dumps(await R.ctx.storage_state())))


@tool("browser_set_storage_state")
async def set_storage_state(R, bid, a):
    state = a["state"]
    if isinstance(state, str):
        state = json.loads(state)
    if state.get("cookies"):
        await R.ctx.add_cookies(state["cookies"])
    R._ignore = True  # temporary pages are not part of any browser
    try:
        for o in state.get("origins", []):
            page = await R.ctx.new_page()
            try:
                await page.goto(o["origin"])
                await page.evaluate("items => items.forEach(({name, value}) => localStorage.setItem(name, value))", o.get("localStorage", []))
            finally:
                await page.close()
    finally:
        R._ignore = False
    return result("ok")
