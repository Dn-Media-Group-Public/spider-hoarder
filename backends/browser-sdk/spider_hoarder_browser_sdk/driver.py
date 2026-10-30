"""The browser driver: one uid + process group per session (Xvfb, x11vnc, websockify, Chromium and the tool server, started
by sessiond), the gateway proxying MCP tool calls to the session's tool server. The egress policy (egress.py) is the only
thing that differs between browser backends. See docs/implemented-plans/browser-sessions-plan.md."""

import asyncio
import json
import os
import re
import uuid

from mcp import types

from spider_hoarder_browser_sdk import cmp_rules, files, pages, ports
from spider_hoarder_browser_sdk.mediapolicy import Policy
from spider_hoarder_browser_sdk.sessiond_client import Sessiond
from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway.driver import Driver, sessions
from spider_hoarder_sdk.gateway.jobs import _job_name, _key, _new_job_id, _schema, _text
from spider_hoarder_sdk.gateway.pool import Pool
from spider_hoarder_sdk.textcap import DEFAULT_TEXT_CAP

UPSTREAM_HOST = "mcp-upstream"  # must match the allow-list in toolserver/server.py
# Proxied tools whose output is a file in a job: name -> default job-internal file name when the MCP client gives
# no `filename`.
FILE_TOOLS = {
    "browser_take_screenshot": "screenshot.png", "browser_pdf_save": "page.pdf",
    "browser_snapshot": "snapshot.yml", "browser_find": "matches.txt", "browser_evaluate": "evaluate.json",
    "browser_console_messages": "console.txt", "browser_network_request_get": "network-request",
    "browser_network_request_make": "network-request",
    "browser_network_requests": "network-requests.txt", "browser_storage_state": "storage-state.json",
}
TREE_TOOLS = ("browser_network_request_get", "browser_network_request_make")  # output is a directory, named by the gateway alone
CAPPED_TOOLS = ("browser_snapshot", "browser_find")  # take `text_cap`
HIDDEN = frozenset({"browser_install", "browser_bring_to_front", "browser_set_policy"})  # the last is the gateway's own call at session start


BROWSER_ARG = {"browser": "Browser id from browser_start_session; omit for the shared 'main' browser"}
SAVE_TARGET_ARG = "Element to convert: a ref from browser_snapshot/browser_find, or a unique selector; omit for the whole page"


_MIME_LIST = {"type": "array", "items": {"type": "string"}}

def extra_tools(egress) -> list[types.Tool]:
    EGRESS, EGRESS_NOTE = egress.name, egress.note
    return [
        types.Tool(name="open_session", inputSchema={"type": "object", "properties": {
                       "media": {"type": "boolean", "description": "true = load images, video, audio and other media; false = never request or download them. "
                                 "Default: false on Tor and I2P, true on clear."},
                       "allow": {**_MIME_LIST, "description": "Extra content types allowed for direct navigation/downloads, e.g. ['image/svg+xml']"},
                       "deny": {**_MIME_LIST, "description": "Content types always blocked for direct navigation/downloads, e.g. ['text/geojson']"}}},
                   description="Set this session's media policy. Must be the FIRST tool call of a conversation; every other tool fails until it has been called. "
                   "Calling it again ENDS the existing session (all browsers, cookies, tabs) and starts a new one with the new policy, so call it only once, "
                   "at the start, never in the middle of a task. Browsing the dark web can expose the user to legal "
                   "risk if images, video or audio are even loaded, so BEFORE calling this, explicitly ask the user about their security "
                   "settings. Call it before any browser call. Pass `allow` / `deny` only if the user asked for a specific security setup; "
                   "otherwise call it with no arguments for the defaults. Returns the effective policy (same as get_session)."),
        types.Tool(name="get_session", inputSchema={"type": "object", "properties": {}},
                   description="Read-only: the session's effective media policy (`opened` is false until open_session has been called, "
                   "and the rest are then the defaults open_session() would apply). Subagents: call this to see what was decided; do not call open_session (it would end the session)."),
        types.Tool(name="browser_request_human_help", inputSchema=_schema(BROWSER_ARG, reason="What the user needs to do, e.g. 'solve the CAPTCHA'"),
                   description="Get a link that lets the user see and control THIS conversation's browser in a new tab. "
                   "Use it for a CAPTCHA, bot challenge, login, 2FA or anything else a human should do. Give the link to "
                   "the user, explain what to do, then STOP until they confirm they are done."),
        types.Tool(name="browser_save_page", inputSchema=_schema({**BROWSER_ARG, "target": SAVE_TARGET_ARG}),
                   description=f"[{EGRESS.upper()}] Convert the page currently open in the browser to Markdown and store it. "
                   "Returns {job_id, files, text?}. With `target` (an element ref from browser_snapshot or browser_find, or a "
                   "unique selector) only that element is converted; without it, the whole page, including navigation, teasers "
                   "and footers. To extract an article, first call browser_find (e.g. for `article`, `main`, or the headline) "
                   "to get the ref of the element that contains the content, then pass it as `target`. "
                   "`simplified/` holds an automatic main-content guess that may omit text. "
                   "Use after getting past consent walls/CAPTCHAs. "
                   "Prefer this over browser_snapshot for extracting page content; use browser_snapshot only for "
                   "navigation and interaction (finding refs to click or type into)."),
        types.Tool(name="browser_end_session", inputSchema=_schema(BROWSER_ARG),
                   description="With `browser`: close just that browser (window and tabs). Without: close all browsers and end "
                   "this conversation's session, discarding its state (cookies, tabs, downloads). Subagents: pass the id "
                   "from browser_start_session when done. Closing the last browser ends the session."),
        types.Tool(name="ingest_page", inputSchema=_schema({"target": "Unique selector of the element to convert (refs do not work here: no snapshot exists); omit for the whole page"}, url="URL to fetch"),
                   description=f"[{EGRESS.upper()}] {EGRESS_NOTE}\n\nOne-shot: fetch a URL (HTML via a throwaway browser, anything else directly) and parse it "
                   "to Markdown. Returns {job_id, files, text?}. `target` (HTML only) converts just that element, and must be a selector. `simplified/` holds an automatic main-content guess that may omit text. If the text looks like a consent/CAPTCHA wall, use the "
                   "browser_* tools and browser_request_human_help instead."),
    ]


def _without_filename(t: types.Tool) -> types.Tool:
    """browser_network_request_get/_make fill a whole job: the MCP client has no filename to choose."""
    sch = {**t.inputSchema, "properties": {k: v for k, v in t.inputSchema.get("properties", {}).items() if k != "filename"}}
    desc = (t.description or "").replace(" `filename` only names the file inside the job.", "")
    return t.model_copy(update={"inputSchema": sch, "description": desc})


class BrowserDriver(Driver):
    noun = "browser"
    file_tools = FILE_TOOLS
    capped_tools = CAPPED_TOOLS
    hidden = HIDDEN

    def __init__(self, egress):
        self.egress = egress
        self.idle_timeout = int(os.environ.get("IDLE_TIMEOUT", "1800"))
        self.max_per_user = int(os.environ.get("MAX_SESSIONS_PER_USER", "3"))
        self.max_total = ports.MAX_SESSIONS_TOTAL
        self.extra_tools = extra_tools(egress)
        self.instructions = (
            f"{egress.note} Prefer this server over the built-in WebFetch. "
            "Each conversation gets its own private, isolated browser. "
            "Use the browser_* tools to browse. When a page shows a CAPTCHA, bot check, login wall or cookie wall "
            "you cannot get past, call browser_request_human_help, show the user the link, and wait for them to "
            "confirm before continuing. browser_save_page parses and stores the current page; ingest_page does a "
            "one-shot fetch of a URL. Tools with bulk output (snapshots, finds, screenshots, PDFs, evaluate, console, network) return "
            "{job_id, files, text?}; text is inline only when small, otherwise use get_file_text to page or grep_file to "
            "search, or list_files / get_file_url. Refs from browser_snapshot / browser_find stay valid until the page "
            "re-renders or navigates; actions do not return a snapshot, so call browser_find again after one. "
            "terse/<name>.md is an outline (headings, links, images) of parsed/<name>.md; simplified/<name>.md is an "
            "automatic main-content extraction that may omit text. Before browser_save_page, use browser_find to "
            "locate the main content element and pass its ref as `target`. "
            "Call browser_end_session when finished. "
            "FIRST call open_session (after asking the user about security settings): every other tool fails until then.")
        self._host_rules = cmp_rules.host_rules()

    # ---- pool and upstream session
    def make_pool(self):
        return Pool(Sessiond(os.environ.get("SESSIOND_SOCKET", "/run/sessiond.sock")), uids=ports.UIDS)

    async def prepare(self, bs):
        env = self.egress.session_env(bs.uid)
        await self.egress.ready(bs.uid)
        # direct: Chromium --host-rules; proxied: the filter relay (host-rules don't apply when a proxy resolves names)
        return env, {"vncpass": bs.vnc_password, "hostrules": "" if self.egress.proxied else self._host_rules}

    def upstream_mcp(self, uid):
        return f"http://127.0.0.1:{ports.mcp_port(uid)}/mcp", {"Host": UPSTREAM_HOST}, {}

    async def after_initialize(self, cs, bs):
        if bs.opts:
            res = await cs.call_tool("browser_set_policy", {"policy": bs.opts.to_json()})
            if res.isError:
                raise RuntimeError(sessions.result_text(res))

    def view_upstream(self, uid, rid):
        return f"{rid.replace('-', '.')}:{ports.ws_port(uid)}"

    # ---- tools
    async def upstream_tools(self):
        """Fetched once from a throwaway session."""
        probe = await sessions.new_session("probe")
        try:
            return (await probe.client.list_tools()).tools
        finally:
            await sessions.close(probe)

    def decorate_tools(self, tools):
        tools = [_without_filename(t) if t.name in TREE_TOOLS else t for t in tools]
        return [t.model_copy(update={"description": f"[{self.egress.name.upper()}] {self.egress.note}\n\n{t.description or ''}"})
                if t.name == "browser_navigate" else t for t in tools]

    async def pre_session(self, name, args, ctx):
        sid, mcp_session, owner = ctx.sid, ctx.mcp_session, ctx.owner
        egress = self.egress.name
        if name == "open_session":
            if not sid:
                return _text("no MCP session", True)
            policy = Policy.make(self.egress.media_default, args.get("media"), args.get("allow"), args.get("deny"))  # validate before ending anything
            if sid in sessions.opened:  # a client may reuse one MCP session across conversations: a repeat call starts over
                if bs := await sessions.get_session(mcp_session, owner, create=False):
                    await sessions.close(bs)
            sessions.opened[sid] = policy
            return _text(json.dumps(policy.summary(egress, True)))
        if name == "get_session":
            return _text(json.dumps((sessions.opened.get(sid or "") or Policy.make(self.egress.media_default)).summary(egress, sid in sessions.opened)))
        if sid not in sessions.opened:  # fail closed: the MCP client cannot skip the question
            return _text("session not opened: ask the user about security settings, then call open_session first", True)
        if name == "browser_end_session":
            bs = await sessions.get_session(mcp_session, owner, create=False)
            blocked = None
            if bs:
                res = await bs.client.call_tool("browser_end_session", {k: args[k] for k in ("browser",) if args.get(k)})
                if res.isError and args.get("browser"):
                    return res
                if not res.isError:
                    info = pages.parse_result(sessions.result_text(res))
                    if args.get("browser") and info["remaining"] > 0:
                        return res
                    blocked = info["blocked"]
                await sessions.close(bs)  # no id, or the last browser was closed
            sessions.opened.pop(sid, None)  # close() already dropped it; the session is over: the next open_session asks the user again (clients reuse one MCP session across /clear)
            return _text("Browser session closed." + (f" Media policy blocked {blocked} request(s)." if blocked else ""))
        if name == "ingest_page":
            return _text(json.dumps(await pages.ingest_page(
                self.egress, owner, args["url"], await sessions.get_session(mcp_session, owner, create=False), args.get("target"),
                sessions.opened[sid])))

    async def with_session(self, name, args, ctx, bs):
        if name == "browser_request_human_help":
            reason = args.get("reason", "help with the page")
            if args.get("browser"):  # raise that browser's window in the live view
                res = await bs.client.call_tool("browser_bring_to_front", {"browser": args["browser"]})
                if res.isError:
                    return res
            return _text(
                f"Live view of this conversation's browser: {ctx.base}/view/{ctx.rid}/{bs.id}/start\n"
                f"Ask the user to open it (they sign in with their newsroom account) and to: {reason}. "
                "Only the user who owns this conversation can open it. "
                "Do not take further browser actions until the user confirms they are done.")
        if name == "browser_save_page":
            return _text(json.dumps(await pages.save_page(bs, args.get("browser"), args.get("target"))))

    async def file_tool(self, bs, name, arguments):
        """Run a file-producing upstream tool; the file ends up under a new job id."""
        tree = name in TREE_TOOLS
        jname = FILE_TOOLS[name] if tree else _job_name(arguments.get("filename"), FILE_TOOLS[name])
        job_id = _new_job_id()
        private = f"{uuid.uuid4()}-{jname}"  # gateway-chosen, relative: lands in out/, never a client-chosen path
        staged, given = [], arguments
        if name == "browser_network_request_make":
            pol = bs.opts
            arguments, staged = await asyncio.to_thread(files._request_body, bs.owner, bs.uid, arguments, bool(pol and pol.active and not pol.media))
        try:
            res = await bs.client.call_tool(name, {**arguments, "filename": private})
        finally:
            for s_ in staged:
                await asyncio.to_thread(files._drop_in, bs.uid, s_)
        up = sessions.result_text(res)
        if res.isError or "](out/" not in up:  # an error, or a plain text answer such as "No new requests."
            return res
        meta = {}
        if tree:  # the toolserver's request line: `<n>. [METHOD] url => [status] text`
            m = re.search(r"^\d+\. \[(\S+)\] (\S+) => \[(\w+)\]", up, re.M)
            if m:
                meta = {"url": m.group(2), "method": m.group(1), "status": int(m.group(3)) if m.group(3).isdigit() else None}
            if m and m.group(3) == "BLOCKED":
                meta["blocked"] = "media policy"
            if r := re.search(r"^Redirects: (.+)$", up, re.M):
                meta["redirects"] = json.loads(r.group(1))
            if name == "browser_network_request_make":
                meta["type"] = arguments.get("type") or "navigation"
                if r := re.search(r"^Origin: (.+)$", up, re.M):
                    meta["origin"] = r.group(1)
                if isinstance(given.get("body"), dict):  # the argument as given, with job references: never a second copy of a big upload
                    meta["body"] = given["body"]
        key = _key(bs.owner, job_id)
        try:
            result = await asyncio.to_thread(files._ingest_out, bs.uid, private, key, jname, tree, meta.get("url", ""))
        except (OSError, ValueError) as e:
            return _text(f"{name} produced no readable output file ({e}). Upstream said: {up}", True)
        if name in CAPPED_TOOLS:
            m = re.search(r"^- Page URL: (.+)$", up, re.M)
            meta = {"url": m and m.group(1), "target": arguments.get("target"), "depth": arguments.get("depth"),
                    "text_cap": arguments.get("text_cap", DEFAULT_TEXT_CAP)}
            if name == "browser_find":
                meta["query"] = {k: arguments[k] for k in ("text", "regex") if arguments.get(k)}
        storage.set_meta(key, keep_none=("text_cap",), **meta)
        if meta.get("type") == "form" and arguments.get("cookies"):
            result["notes"] = [*result.get("notes", []), "cookies were set as a Cookie header, which bypasses SameSite filtering of the session's own cookies"]
        if meta.get("blocked"):
            result["notes"] = [*result.get("notes", []), "blocked by the media policy: the response body was never read"]
        return _text(json.dumps({"job_id": job_id, **result}))
