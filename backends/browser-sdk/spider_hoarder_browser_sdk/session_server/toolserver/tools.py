"""The tool set. Each handler is `async (R, bid, args) -> str` returning response sections
(`### Result`, `### Page`; errors are raised and formatted by server.py).
Schemas for the playwright-mcp tools come from tools.json (from tools.json.upstream, 0.0.83), see server.py."""

import asyncio
import base64
import contextlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import time
import urllib.parse

from playwright.async_api import Error as PWError

from .browsers import MAIN, Registry, ToolError, locator
from spider_hoarder_browser_sdk.mediapolicy import ROUTED_UPLOAD_CAP, Policy, blocked_message
from spider_hoarder_sdk.textcap import DEFAULT_TEXT_CAP, cap_found_line, cap_snapshot

OUT = os.environ.get("TOOLSERVER_OUT", "out")
IN = os.environ.get("TOOLSERVER_IN", "in")  # files the gateway stages (in/<random>/<filename>) for browser_network_request_make
ACT_TIMEOUT = 10_000  # ms; a stale ref or hidden element fails fast
HANDLERS: dict = {}


def tool(name):
    def deco(f):
        HANDLERS[name] = f
        return f
    return deco


def out_path(name: str) -> str:
    """Files only ever land directly in the out dir, whatever the MCP client asks for."""
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
    try:
        await R.page(bid).goto(url)
    except PWError as e:
        if "ERR_BLOCKED_BY_CLIENT" in str(e) and (b := next((b for b in reversed(R.blocked) if b["url"] == url), None)):
            if not R.page(bid).url.startswith("chrome-error:"):  # let Chromium commit its error page, or the next goto is "interrupted"
                with contextlib.suppress(PWError):
                    await R.page(bid).wait_for_url("chrome-error://*", timeout=3000)
            raise ToolError(blocked_message(b["kind"]))
        raise
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


def net_line(i, e, blocked=()):
    r, resp = e["req"], e["resp"]
    return f"{i}. [{r.method}] {r.url} => " + (f"[{resp.status}] {resp.status_text}" if resp
                                              else "[BLOCKED] blocked: media policy" if r.url in blocked else f"[FAILED] {r.failure}")


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
    lines = [net_line(i, e, R.blocked_urls) for i, e in enumerate(st.network[start:end], start + 1)
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


async def response_parts(resp, parts: dict) -> None:
    """response/headers.json and response/body.<ext> of a Playwright Response, whatever of them exists."""
    hdrs = await resp.all_headers()
    parts["response/headers.json"] = json.dumps(hdrs, indent=1).encode()
    try:
        parts[f"response/body{body_ext(hdrs)}"] = await resp.body()
    except PWError:  # e.g. redirects and evicted bodies have none
        pass


def write_tree(filename: str, parts: dict) -> str:
    """Writes {relative path: bytes} below the out dir, returns the directory's name."""
    root = out_path(filename)
    for rel, data in parts.items():
        os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(root, rel), "wb") as f:
            f.write(data)
    return os.path.basename(root)


@tool("browser_network_request_get")
async def network_request_get(R, bid, a):
    """Writes a directory (named by `filename`): request/ and response/, each headers.json and body.<ext>,
    only the parts that exist."""
    net = R.state[R.page(bid)].network
    if not 1 <= a["index"] <= len(net):
        raise ToolError(f"No request with index {a['index']}")
    if not a.get("filename"):
        raise ToolError("filename is required")
    e = net[a["index"] - 1]
    req, resp = e["req"], e["resp"]
    parts = {}  # relative path -> bytes
    hdrs = await req.all_headers()
    parts["request/headers.json"] = json.dumps(hdrs, indent=1).encode()
    if (body := req.post_data_buffer) is not None:
        parts[f"request/body{body_ext(hdrs)}"] = body
    if resp:
        await response_parts(resp, parts)
    name = write_tree(a["filename"], parts)
    return file_result("Network request", name) + "\n" + net_line(a["index"], e, R.blocked_urls)


# ------------------------------------------------------- browser_network_request_make
CDP_BODY_MAX = 72 * 1024 * 1024  # a navigation body is one base64 CDP message; Chromium kills the context above ~75 MiB raw
NO_FRAMING = {"host", "content-length", "transfer-encoding", "connection"}  # they break the framing (a Host rewrite hung a fetch)
XHR_FORBIDDEN = {"accept-charset", "accept-encoding", "access-control-request-headers", "access-control-request-method", "cookie",
                 "cookie2", "date", "dnt", "expect", "keep-alive", "origin", "referer", "set-cookie", "te", "trailer", "upgrade",
                 "user-agent", "via"}  # fetch silently drops these (plus Sec-* and Proxy-*)
ENCODINGS = ("application/x-www-form-urlencoded", "multipart/form-data", "text/plain")
ORIGIN_PAGE = "<!doctype html><meta charset=utf-8><link rel=icon href='data:,'><title>request</title><body>"


def js_str(v) -> str:
    """A field or cookie value as JS String() would show it."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (int, float, str)):
        return str(v)
    raise ToolError("values are text, numbers or booleans")


def staged_path(name) -> str:
    """The file the gateway staged as in/<random>/<filename>; never a path the MCP client chose (the gateway discards its own hidden
    arguments and only ever writes these two components)."""
    parts = str(name).split("/")
    if len(parts) != 2 or any(p in ("", ".", "..") for p in parts):
        raise ToolError("request body file not found")
    path = os.path.join(IN, *parts)
    if not os.path.isfile(path):
        raise ToolError("request body file not found")
    return path


def staged_names(a: dict):
    body = a.get("body")
    yield a.get("body_file")
    if isinstance(body, dict):
        for v in body.values():
            for i in v if isinstance(v, list) else [v]:
                if isinstance(i, dict):
                    yield i.get("file")


def drop_staged(a: dict) -> None:
    for n in staged_names(a):
        if isinstance(n, str) and n.split("/")[0] not in ("", ".", ".."):
            shutil.rmtree(os.path.join(IN, n.split("/")[0]), ignore_errors=True)


def merge_headers(base: dict, over: dict) -> list[dict]:
    low = {k.lower() for k in over}
    return [{"name": k, "value": v} for k, v in {**base, **over}.items() if k in over or k.lower() not in low]


def add_query(url: str, pairs: list) -> str:
    u = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(u._replace(query="&".join(filter(None, [u.query, urllib.parse.urlencode(pairs)]))))


class Req:
    """One validated browser_network_request_make call."""

    def __init__(self, R, bid, a):
        self.url, self.filename = a["url"], a.get("filename")
        if not re.match(r"https?://", self.url):
            raise ToolError("url must be http(s)")
        self.method = str(a.get("method") or "GET").upper()
        if not re.fullmatch(r"[A-Z]+", self.method):
            raise ToolError("invalid method")
        self.type = a.get("type") or "navigation"
        if self.type not in ("navigation", "form", "xhr"):
            raise ToolError("type must be navigation, form or xhr")
        self.timeout = int(a.get("timeout_ms") or 30_000)
        hs = a.get("headers") or {}
        if not isinstance(hs, dict) or not all(isinstance(v, str) for v in hs.values()):
            raise ToolError("headers must be an object of strings")
        self.headers = dict(hs)
        for k in self.headers:
            lk = k.lower()
            if lk in NO_FRAMING:
                raise ToolError(f"header {k} breaks the request framing and cannot be set")
            if self.type == "xhr" and (lk in XHR_FORBIDDEN or lk.startswith(("sec-", "proxy-"))):
                raise ToolError(f"header {k} cannot be set by a page script: fetch would silently drop it (use type=navigation or form)")
        if self.type == "xhr" and self.method in ("CONNECT", "TRACE", "TRACK"):
            raise ToolError(f"fetch cannot send {self.method}")
        cs = a.get("cookies")
        if cs is not None and not isinstance(cs, dict):
            raise ToolError("cookies must be an object {name: value}")
        self.cookies = {k: js_str(v) for k, v in (cs or {}).items()}
        if self.cookies and any(k.lower() == "cookie" for k in self.headers):
            raise ToolError("headers.Cookie and cookies are mutually exclusive")
        if self.cookies and self.type == "xhr":
            raise ToolError("cookies cannot be set per request by a page script: set them with browser_cookie_set beforehand, or use type=form or navigation")
        self.credentials = a.get("credentials")
        if self.credentials is not None and (self.type != "xhr" or self.credentials not in ("include", "omit")):
            raise ToolError("credentials is include or omit, and for type=xhr only")
        self.credentials = self.credentials or "include"
        self.origin = None
        if a.get("origin") is not None and self.type == "navigation":
            raise ToolError("origin is for type=form and xhr")
        if self.type != "navigation":
            cur = urllib.parse.urldefrag(R.page(bid).url).url if bid else ""
            self.origin = str(a["origin"]) if a.get("origin") else cur if re.match(r"https?://", cur) else self.url
            if not re.match(r"https?://", self.origin):
                raise ToolError("origin must be http(s)")
        # ---- body
        body, self.kind, self.text, self.fields, self.file = a.get("body"), "none", None, [], None
        if a.get("body_file") and body is not None:
            raise ToolError("body and a job file body are mutually exclusive")
        if a.get("body_file"):
            self.kind, self.file = "file", staged_path(a["body_file"])
        elif isinstance(body, str):
            self.kind, self.text = "text", body
        elif isinstance(body, dict):
            self.kind = "fields"
            for n, v in body.items():
                for i in v if isinstance(v, list) else [v]:
                    if isinstance(i, dict):
                        self.fields.append((n, {**i, "path": staged_path(i.get("file"))}))
                    elif i is None:
                        raise ToolError(f"field {n!r}: null is not a value")
                    else:
                        self.fields.append((n, js_str(i)))
        elif body is not None:
            raise ToolError("body must be text, a job file or a fields object")
        self.has_file = any(isinstance(v, dict) for _, v in self.fields)
        bodyless = self.method in ("GET", "HEAD")
        if self.type == "form" and self.kind in ("text", "file"):
            raise ToolError("type=form sends fields only")
        if self.type == "xhr" and bodyless and self.kind in ("text", "file"):
            raise ToolError("fetch cannot send a body with GET/HEAD")
        if bodyless and self.has_file:
            raise ToolError("a GET/HEAD form sends only the filename: use another method for a file part")
        self.enc = self.encoding() if self.kind == "fields" and not bodyless else None
        ct_parts = [v for _, v in self.fields if isinstance(v, dict) and v.get("content_type")]
        self.rewrites = self.method not in ("GET", "POST") or bool(self.cookies) or any(k.lower() != "content-type" for k in self.headers)
        if self.type == "form" and ct_parts and self.rewrites:
            raise ToolError("content_type of a file part needs a plain GET/POST with no headers and no cookies (use type=xhr)")
        self.files = [v["path"] for _, v in self.fields if isinstance(v, dict)] + ([self.file] if self.file else [])
        routed = R.policy is not None and R.policy.active and not R.policy.media  # layer A pauses every request
        if routed and (self.type == "xhr" or ct_parts) and sum(map(os.path.getsize, self.files)) > ROUTED_UPLOAD_CAP:
            raise ToolError(f"this session's media policy filters every request, so uploads are limited to {ROUTED_UPLOAD_CAP} bytes here")

    def encoding(self) -> str:
        ct = next((v for k, v in self.headers.items() if k.lower() == "content-type"), None)
        if ct is None:
            return "multipart/form-data" if self.has_file else "application/x-www-form-urlencoded"
        mt = ct.split(";")[0].strip().lower()
        if mt not in ENCODINGS:
            raise ToolError(f"with fields, Content-Type is one of {', '.join(ENCODINGS)}")
        if self.has_file and mt != "multipart/form-data":
            raise ToolError("a file part needs multipart/form-data")
        return mt

    def text_plain(self) -> str:
        return "".join(f"{n}={v}\r\n" for n, v in self.fields)

    def encoded(self) -> tuple[bytes, str]:
        """type=navigation: the fields as a body we build, and its Content-Type."""
        if self.enc == "text/plain":
            return self.text_plain().encode(), "text/plain"
        if self.enc == "application/x-www-form-urlencoded":
            return urllib.parse.urlencode(self.fields).encode(), self.enc
        if sum(map(os.path.getsize, self.files)) > CDP_BODY_MAX:
            raise ToolError(f"request body too large for type=navigation (max {CDP_BODY_MAX} bytes): use type=form or xhr")
        b, esc, out = "----spiderhoarder" + secrets.token_hex(16), lambda s: s.replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A"), []
        for n, v in self.fields:
            if isinstance(v, str):
                out += [f'--{b}\r\nContent-Disposition: form-data; name="{esc(n)}"\r\n\r\n'.encode(), v.encode(), b"\r\n"]
            else:
                ct = v.get("content_type") or mimetypes.guess_type(v["filename"])[0] or "application/octet-stream"
                out += [f'--{b}\r\nContent-Disposition: form-data; name="{esc(n)}"; filename="{esc(v["filename"])}"\r\nContent-Type: {ct}\r\n\r\n'.encode(),
                        open(v["path"], "rb").read(), b"\r\n"]
        return b"".join(out + [f"--{b}--\r\n".encode()]), f"multipart/form-data; boundary={b}"


async def cookie_header(R, url: str, extra: dict) -> str:
    jar = {c["name"]: c["value"] for c in await R.ctx.cookies(url)} | extra
    return "; ".join(f"{k}={v}" for k, v in jar.items())


@tool("browser_network_request_make")
async def network_request_make(R, bid, a):
    """Sends one request from a hidden tab, as a navigation (CDP Fetch rewrites its method, headers and body), a form submit or
    a page script's fetch, so cookies, TLS fingerprint, proxy and the media policy are the real browser's. Writes the same
    directory as network_request_get for the final request/response, then `Origin:` (form, xhr) and `Redirects:` lines."""
    try:
        q = Req(R, bid, a)
        page, cdp = await R.hidden_page()
        try:
            return await (_xhr if q.type == "xhr" else _document)(R, q, page, cdp)
        finally:
            await page.close()
    finally:
        drop_staged(a)  # also when validation failed


def tail(q, line: str, redirects: list) -> str:
    return (f"\n{line}" + (f"\nOrigin: {q.origin}" if q.origin else "")) + (f"\nRedirects: {json.dumps(redirects)}" if redirects else "")


def hops_of(net: list, method: str, kept: bool):
    redirects = []
    for r in net:
        redirects.append({"url": r.url, "status": r.status})
        if r.status == 303 and method != "HEAD" or r.status in (301, 302) and method == "POST":
            method, kept = "GET", False
    return redirects, method, kept


async def _document(R, q, page, cdp):
    """type=navigation and form: the request is a main-frame document request, rewritten while paused."""
    timeout = q.timeout
    net, downloads, failed = [], [], []
    blocked0, outcome = len(R.blocked), asyncio.Event()
    st = {"phase": "origin" if q.type == "form" else "go"}  # which Document request is next: origin page -> submit -> the rest untouched
    headers, record, post = dict(q.headers), None, None
    if q.type == "navigation":
        if q.kind == "fields" and q.enc is None:
            q.url = add_query(q.url, [(n, v) for n, v in q.fields])
        elif q.kind == "fields":
            post, ct = q.encoded()
            headers = {k: v for k, v in headers.items() if k.lower() != "content-type"} | {"Content-Type": ct}
            record = post
        elif q.kind == "text":
            post = record = q.text.encode()
        elif q.kind == "file":
            post = open(q.file, "rb").read()
        if post is not None and len(post) > CDP_BODY_MAX:
            raise ToolError(f"request body too large for type=navigation (max {CDP_BODY_MAX} bytes): use type=form or xhr")
        if not any(k.lower() == "cookie" for k in headers) and (c := await cookie_header(R, q.url, q.cookies)):
            headers["Cookie"] = c
    else:
        headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}  # the browser owns it: the form's enctype
        if q.cookies:
            headers["Cookie"] = await cookie_header(R, q.url, q.cookies)
    # Playwright's own Request objects do not know about the override (method GET, no post data): ours comes from the arguments
    def on_response(r):
        if r.frame == page.main_frame and r.request.resource_type == "document" and st["phase"] in ("go", "passed"):
            net.append(r)
            if not (300 <= r.status < 400 and r.headers.get("location")) and "attachment" not in r.headers.get("content-disposition", "").lower():
                outcome.set()  # (an attachment ends with its download event)
    page.on("response", on_response)
    page.on("requestfailed", lambda r: (failed.append(r), r.resource_type == "document" and r.failure != "net::ERR_ABORTED" and st["phase"] in ("go", "passed") and outcome.set()))  # a download aborts its navigation: its event ends the wait
    page.on("download", lambda d: (downloads.append(d), outcome.set()))

    async def paused(ev):
        args = {"requestId": ev["requestId"]}
        if ev.get("resourceType") == "Document":
            if st["phase"] == "origin":
                st["phase"] = "wait"
                with contextlib.suppress(PWError):
                    await cdp.send("Fetch.fulfillRequest", {"requestId": ev["requestId"], "responseCode": 200, "body": base64.b64encode(ORIGIN_PAGE.encode()).decode(),
                                                            "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}]})
                return
            if st["phase"] in ("go", "submit"):  # the request itself; later ones (redirect hops, subresources) pass untouched
                st["phase"] = "passed"
                if q.type == "navigation":
                    args |= {"method": q.method, "headers": [{"name": k, "value": v} for k, v in headers.items()]}
                    if post is not None:
                        args["postData"] = base64.b64encode(post).decode()
                else:  # a form: keep the browser's own headers (multipart boundary, Origin, Referer) and add ours
                    if q.method not in ("GET", "POST"):
                        args["method"] = q.method
                    args["headers"] = merge_headers(ev["request"]["headers"], headers)
        with contextlib.suppress(PWError):
            await cdp.send("Fetch.continueRequest", args)

    cdp.on("Fetch.requestPaused", lambda ev: R._spawn(paused(ev)))
    err = None
    if q.type == "navigation":
        await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        try:
            await page.goto(q.url, wait_until="commit", timeout=timeout)  # redirects are followed before the commit
        except PWError as e:
            err = e
            if "Download is starting" in str(e):  # the event may trail the error
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(outcome.wait(), timeout / 1000)
    else:
        await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        await page.goto(q.origin, wait_until="load", timeout=timeout)
        st["phase"] = "submit" if q.rewrites else "wait"
        if not q.rewrites:  # a file swapped in with DataTransfer is unusable under a request-stage pause
            await cdp.send("Fetch.disable")
            st["phase"] = "passed"
        await page.evaluate(FORM_JS, {"action": q.url, "method": "get" if q.method in ("GET", "HEAD") else "post", "enc": q.enc or "application/x-www-form-urlencoded",
                                      "fields": [[n, None if isinstance(v, dict) else v] for n, v in q.fields]})
        files = [v for _, v in q.fields if isinstance(v, dict)]
        for i, f in enumerate(files):
            await page.locator("input[type=file]").nth(i).set_input_files(f["path"])
        if swaps := [[i, f["filename"], f["content_type"]] for i, f in enumerate(files) if f.get("content_type")]:
            await page.evaluate(SWAP_JS, swaps)
        net.clear()
        await page.evaluate("setTimeout(() => document.getElementById('f').submit())")
        try:
            await asyncio.wait_for(outcome.wait(), timeout / 1000)
        except asyncio.TimeoutError:
            err = ToolError(f"Timeout {timeout}ms exceeded")
    parts = {}
    blocked = [b for b in R.blocked[blocked0:] if any(f.url == b["url"] for f in failed)]
    hops = net if blocked else net[:-1]  # a blocked response never becomes a Response: every one in `net` was a redirect
    redirects, m, kept = hops_of(hops, q.method, record is not None)
    if downloads:  # Content-Disposition: attachment; the navigation "fails" with "Download is starting"
        path = await asyncio.wait_for(downloads[0].path(), timeout / 1000)
        body_bytes = await asyncio.to_thread(lambda: open(path, "rb").read())
    elif not blocked and (err or not net):
        raise ToolError(str(err or "no response").split("\n")[0])
    if blocked:  # Playwright reports no response for it: the headers are the registry's record of what was blocked
        req = next(f for f in failed if f.url == blocked[0]["url"])
        hdrs = await req.all_headers()
        parts["response/headers.json"] = json.dumps({"content-type": blocked[0]["kind"]}, indent=1).encode()
        line = f"1. [{m}] {req.url} => [BLOCKED] blocked: media policy"
    else:
        final = net[-1] if net else None
        hdrs = await final.request.all_headers()
        if downloads:
            fh = await final.all_headers()
            parts["response/headers.json"] = json.dumps(fh, indent=1).encode()
            parts[f"response/body{body_ext(fh)}"] = body_bytes
        else:
            await response_parts(final, parts)
        line = f"1. [{m}] {final.url} => [{final.status}] {final.status_text}"
    parts["request/headers.json"] = json.dumps(hdrs, indent=1).encode()
    if kept:
        parts[f"request/body{body_ext(hdrs)}"] = record
    return file_result("Network request", write_tree(q.filename, parts)) + tail(q, line, redirects)


FORM_JS = """({action, method, enc, fields}) => {
  const f = document.createElement('form'); f.id = 'f'; f.action = action; f.method = method; f.enctype = enc;
  for (const [name, v] of fields) {
    const i = document.createElement('input'); i.name = name;
    if (v === null) i.type = 'file'; else { i.type = 'hidden'; i.value = v; }
    f.append(i);
  }
  document.body.append(f);
}"""
SWAP_JS = """swaps => swaps.forEach(([i, name, type]) => {
  const inp = document.querySelectorAll('input[type=file]')[i], dt = new DataTransfer();
  dt.items.add(new File([inp.files[0]], name, {type})); inp.files = dt.files;
})"""
# One fetch in the generated page. Raw bodies are typeless Blobs so fetch infers no Content-Type; the response body goes to
# Python in ~1 MiB base64 chunks (getResponseBody is unusable for fetch with a File body), after the media policy saw its headers.
XHR_JS = """async ({url, method, headers, credentials, kind, text, enc, fields}) => {
  const files = [...document.querySelectorAll('input[type=file]')].map(i => i.files[0]);
  const init = {method, headers, credentials};
  if (kind === 'text') init.body = new Blob([text]);
  else if (kind === 'file') init.body = files[0].slice();
  else if (kind === 'fields' && enc === 'text/plain') init.body = text;
  else if (kind === 'fields') {
    const d = enc === 'multipart/form-data' ? new FormData() : new URLSearchParams();
    for (const [n, v] of fields) {
      if (typeof v === 'string') d.append(n, v);
      else { const f = files[v.i]; d.append(n, v.type != null || v.filename !== f.name ? new File([f], v.filename, {type: v.type ?? f.type}) : f, v.filename); }
    }
    init.body = d;
  }
  let resp;
  try { resp = await fetch(url, init); } catch (e) { return {error: String(e)}; }
  const out = {status: resp.status, statusText: resp.statusText, url: resp.url};
  if (await window.__blocked(resp.headers.get('content-type'), resp.status, resp.url)) {
    try { await resp.body?.cancel(); } catch (e) {}
    return {...out, blocked: true};
  }
  if (!resp.body) return out;
  const reader = resp.body.getReader();
  let buf = [], n = 0;
  const flush = async () => {
    if (!n) return;
    const b64 = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result.slice(fr.result.indexOf(',') + 1)); fr.readAsDataURL(new Blob(buf)); });
    buf = []; n = 0; await window.__chunk(b64);
  };
  try {
    for (;;) { const {done, value} = await reader.read(); if (done) break; buf.push(value); n += value.length; if (n >= (1 << 20)) await flush(); }
    await flush();
  } catch (e) { return {...out, error: String(e)}; }
  return out;
}"""


async def _xhr(R, q, page, cdp):
    """type=xhr: a fetch run by the generated page at q.origin, so CORS, cookies and Origin are a page script's. Request-stage
    Fetch is only on while the origin page loads: a pause makes big File bodies hang."""
    timeout, net, failed = q.timeout, [], []
    root = out_path(q.filename)
    os.makedirs(os.path.join(root, "response"), exist_ok=True)
    body_path, fh, blocked = os.path.join(root, "response", "body.part"), [None], []

    def chunk(b64):
        if fh[0] is None:
            fh[0] = open(body_path, "wb")
        fh[0].write(base64.b64decode(b64))

    def policy(ctype, status, url):
        if status in (204, 205) or not R.policy.content_type_blocked(ctype):
            return False
        R._block(url, ctype or "", "D")
        blocked.append(ctype or "")
        return True

    first = [True]

    async def paused(ev):
        args = {"requestId": ev["requestId"]}
        if first[0] and ev.get("resourceType") == "Document":
            first[0] = False
            with contextlib.suppress(PWError):
                await cdp.send("Fetch.fulfillRequest", {"requestId": ev["requestId"], "responseCode": 200, "body": base64.b64encode(ORIGIN_PAGE.encode()).decode(),
                                                        "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}]})
            return
        with contextlib.suppress(PWError):
            await cdp.send("Fetch.continueRequest", args)

    try:
        page.on("response", lambda r: r.request.resource_type == "fetch" and net.append(r))
        page.on("requestfailed", lambda r: r.resource_type == "fetch" and failed.append(r))
        await page.expose_function("__chunk", chunk)
        await page.expose_function("__blocked", policy)
        cdp.on("Fetch.requestPaused", lambda ev: R._spawn(paused(ev)))
        await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        await page.goto(q.origin, wait_until="load", timeout=timeout)
        await cdp.send("Fetch.disable")
        await page.evaluate("n => { for (let i = 0; i < n; i++) { const e = document.createElement('input'); e.type = 'file'; document.body.append(e); } }", len(q.files))
        for i, path in enumerate(q.files):
            await page.locator("input[type=file]").nth(i).set_input_files(path)
        spec = {"url": q.url, "method": q.method, "credentials": q.credentials, "kind": q.kind, "text": None, "enc": q.enc, "fields": [],
                "headers": {k: v for k, v in q.headers.items() if not (q.kind == "fields" and k.lower() == "content-type")}}
        if q.kind == "text":
            spec["text"] = q.text
        elif q.kind == "fields" and q.method in ("GET", "HEAD"):
            spec["kind"], spec["url"] = "none", add_query(q.url, [(n, v) for n, v in q.fields])
        elif q.kind == "fields" and q.enc == "text/plain":
            spec["text"] = q.text_plain()
        elif q.kind == "fields":
            i = 0
            for n, v in q.fields:
                if isinstance(v, dict):
                    spec["fields"].append([n, {"i": i, "filename": v["filename"], "type": v.get("content_type")}])
                    i += 1
                else:
                    spec["fields"].append([n, v])
        try:
            res = await asyncio.wait_for(page.evaluate(XHR_JS, spec), timeout / 1000)
        except asyncio.TimeoutError:
            raise ToolError(f"Timeout {timeout}ms exceeded") from None
    finally:
        if fh[0]:
            fh[0].close()
    if "status" not in res:
        reasons = [f.failure for f in failed if f.failure and f.failure != "net::ERR_FAILED"]
        if reasons:
            raise ToolError(reasons[0])
        if res["error"] == "TypeError: Failed to fetch":
            raise ToolError("the response is not readable by type=xhr (blocked by CORS, a cross-origin redirect or mixed content; the request "
                            f"itself may have been sent): try origin={q.url.split('?')[0]!r} (same origin), credentials=\"omit\", or type=navigation/form")
        raise ToolError(res["error"])
    if res.get("error"):
        raise ToolError("the response body could not be read: " + res["error"])
    final = net[-1]
    hdrs = await final.request.all_headers()
    parts = {"request/headers.json": json.dumps(hdrs, indent=1).encode()}
    kept = q.kind == "text"
    redirects, m, kept = hops_of(net[:-1], q.method, kept)
    if blocked:
        parts["response/headers.json"] = json.dumps({"content-type": blocked[0]}, indent=1).encode()
        line = f"1. [{m}] {res['url']} => [BLOCKED] blocked: media policy"
    else:
        fhdrs = await final.all_headers()
        parts["response/headers.json"] = json.dumps(fhdrs, indent=1).encode()
        if os.path.exists(body_path):
            os.rename(body_path, os.path.join(root, "response", f"body{body_ext(fhdrs)}"))
        line = f"1. [{m}] {res['url']} => [{res['status']}] {res['statusText']}"
    if kept:
        parts[f"request/body{body_ext(hdrs)}"] = q.text.encode()
    return file_result("Network request", write_tree(q.filename, parts)) + tail(q, line, redirects)


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
    return result(json.dumps({"remaining": remaining, "blocked": len(R.blocked)}))


@tool("browser_set_policy")
async def set_policy(R, bid, a):
    """Internal (the gateway calls it once at session start; not in the tool list). Locks the media policy."""
    await R.set_policy(Policy.from_json(a["policy"]))
    return result("ok")


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
