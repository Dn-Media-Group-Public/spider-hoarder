"""The Android driver: one emulator slot per session. The slots are containers made by the backend's own android-wrapper;
the gateway starts and resets them over their control channel (slots.py) and proxies MCP tool calls to the tool server
inside. No uid pool, sessiond or firewall in the gateway container. See docs/implemented-plans/android-hoarder.md."""

import json
import os
import socket
import uuid

import httpx
from mcp import types

from spider_hoarder_android_sdk import slots
from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway.driver import Driver, sessions
from spider_hoarder_sdk.gateway.jobs import (MAX_FILE_BYTES, _check_rel_path, _job_name, _key, _new_job_id, _schema, _store_file,
                                             _text)
from spider_hoarder_sdk.gateway.pool import Pool

FILE_TOOLS = {"device_snapshot": "snapshot.txt", "device_find": "matches.txt", "device_take_screenshot": "screenshot.png",
              "device_logcat": "logcat.txt", "device_shell": "shell.txt", "device_app_list": "apps.txt"}

# Gateway -> slot connections never legitimately stall: they stay inside the stack. A slot container that dies takes its network
# namespace with it and nothing answers on the old address (no FIN, RST or ICMP), so without these the gateway waits on the
# dead socket for the kernel's retransmission limit (many minutes). Keepalive covers an idle wait for a reply (20 s + 3 x 10 s),
# TCP_USER_TIMEOUT unacknowledged data (a call sent after the death); both give up within about a minute.
STACK_SOCKET_OPTIONS = [
    (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 20),
    (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10),
    (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3),
    (socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 60_000),
]


def _stack_client(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    """The MCP SDK's default client (create_mcp_http_client) plus STACK_SOCKET_OPTIONS."""
    return httpx.AsyncClient(headers=headers, auth=auth, follow_redirects=True, timeout=timeout or httpx.Timeout(30.0),
                             transport=httpx.AsyncHTTPTransport(socket_options=STACK_SOCKET_OPTIONS))


def extra_tools() -> list[types.Tool]:
    return [
        types.Tool(name="device_request_human_help", inputSchema=_schema(reason="What the user needs to do, e.g. 'sign in to Google'"),
                   description="Get a link that lets the user see and control THIS conversation's device in a new tab. "
                   "Use it for a Google / Play Store sign-in, CAPTCHA, 2FA, a permission or anything else a human should do. "
                   "Give the link to the user, explain what to do, then STOP until they confirm they are done."),
        types.Tool(name="device_end_session", inputSchema=_schema(),
                   description="End this conversation's device session: the emulator is wiped (apps, accounts, files) and freed."),
        types.Tool(name="device_install_apk", inputSchema=_schema(job_id="Job id of the job holding the APK",
                                                                  path="File path from the job's manifest, e.g. original/app.apk"),
                   description="Install an APK stored in a job (e.g. uploaded by a human through get_upload_link). "
                   "F-Droid is preinstalled for open-source apps."),
    ]


class AndroidDriver(Driver):
    noun = "device"
    file_tools = FILE_TOOLS
    capped_tools = ()

    def __init__(self, note: str):
        self.note = note  # the egress note: what this backend's traffic looks like
        self.idle_timeout = int(os.environ.get("ANDROID_IDLE_TIMEOUT", "600"))
        self.help_timeout = int(os.environ.get("ANDROID_HELP_TIMEOUT", "3600"))
        self.max_per_user = int(os.environ.get("MAX_SESSIONS_PER_USER", "1"))
        # an android slot is recreated and verified after every session (slots.RESET_TIMEOUT at most): closing must outlast that,
        # or the waiter is cancelled mid-reset (pool.release finishes on its own, but the caller sees a timeout)
        self.close_timeout = slots.RESET_TIMEOUT + 90
        self.extra_tools = extra_tools()
        self.names = {t["name"] for t in json.load(open(slots.TOOLS_FILE))} | {"device_request_human_help"}
        self.instructions = (
            f"{note} Each conversation gets its own private, isolated emulator (one device, wiped when the session ends; "
            "if all devices are busy you get a 'busy' error: try again later). Use the device_* tools: device_snapshot shows the "
            "accessibility tree with refs, device_click / device_type / device_swipe act on it, device_take_screenshot returns a "
            "screenshot file, device_install_apk installs an APK from a job (get_upload_link lets a human upload one; F-Droid is "
            "preinstalled). There is NO pre-logged-in account: at a Google / Play Store sign-in, a CAPTCHA, 2FA or any login, call "
            "device_request_human_help, show the user the link, and wait for them to confirm before continuing. Refs stay valid "
            "while the element is on screen; actions do not return a snapshot, so call device_snapshot or device_find again after "
            "one. Bulk output (snapshots, finds, screenshots, logcat, shell, app lists) returns {job_id, files, text?}; text is "
            "inline only when small, otherwise use get_file_text to page or grep_file to search, or list_files / get_file_url. "
            f"The session ends after {self.idle_timeout // 60} minutes without a tool call (waiting for the user does not count). "
            "Call device_end_session when finished.")

    # ---- pool and upstream session
    def make_pool(self):
        return Pool(slots.backend, uids=slots.SLOTS)

    async def prepare(self, bs):
        return {}, {"vncpass": bs.vnc_password}

    def upstream_mcp(self, uid):
        # the slot is a container: its tool server is reached by name, with the slot secret
        return slots.mcp_url(uid), slots.mcp_headers(), {"httpx_client_factory": _stack_client}

    def view_upstream(self, uid, rid):
        return slots.view_upstream(uid)

    # ---- tools
    async def upstream_tools(self):
        """The static list: no device is booked just to list tools."""
        return slots.static_tools()

    def decorate_tools(self, tools):
        return [t.model_copy(update={"description": f"[{self.prefix.upper()}] {self.note}\n\n{t.description or ''}"})
                if t.name == "device_open_url" else t for t in tools]

    def known(self, name):
        return name in self.names  # the static list (the toolserver's) plus the gateway's own

    async def pre_session(self, name, args, ctx):
        if name == "device_end_session":
            bs = await sessions.get_session(ctx.mcp_session, ctx.owner, create=False)
            if bs:
                await sessions.close(bs)
            return _text("Device session ended; the emulator is being wiped.")

    async def with_session(self, name, args, ctx, bs):
        if name == "device_request_human_help":
            bs.waiting_human = bs.last_used  # the idle timer is suspended while the user is at the device
            return _text(
                f"Live view of this conversation's device: {ctx.base}/view/{ctx.rid}/{bs.id}/start\n"
                f"Ask the user to open it (they sign in with their newsroom account) and to: {args.get('reason', 'help on the device')}. "
                "Only the user who owns this conversation can open it. "
                "Do not take further device actions until the user confirms they are done.")
        if name == "device_install_apk":
            return await self._install_apk(bs, args)

    async def _install_apk(self, bs, args: dict) -> types.CallToolResult:
        """The APK comes from a job (storage), is pushed into the slot under a name of our choosing, and the tool server
        installs it by that name: the model never names a path on the device side."""
        key = _key(bs.owner, args["job_id"])
        path = _check_rel_path(args["path"])
        if not storage.exists(key, path):
            return _text("file not found", True)
        raw = storage.read_bytes(key, path)
        if len(raw) > MAX_FILE_BYTES:
            return _text("APK too large", True)
        if raw[:2] != b"PK":
            return _text("not an APK (not a zip archive)", True)
        private = f"{uuid.uuid4()}.apk"
        await slots.backend.push_in(bs.uid, private, raw)
        return await bs.client.call_tool("device_install_apk", {"file": private})

    async def file_tool(self, bs, name, arguments):
        """Run a file-producing upstream tool; the file ends up under a new job id."""
        jname = _job_name(arguments.get("filename"), FILE_TOOLS[name])
        job_id = _new_job_id()
        # the slot only serves plain names; the output comes back over the slot's control channel
        private = f"{uuid.uuid4()}{os.path.splitext(FILE_TOOLS[name])[1]}"
        res = await bs.client.call_tool(name, {**arguments, "filename": private})
        if res.isError:
            return res
        try:
            raw = await slots.backend.fetch_out(bs.uid, private, MAX_FILE_BYTES)
        except (OSError, ValueError, httpx.HTTPError) as e:
            return _text(f"{name} produced no readable output file ({e}). Upstream said: {sessions.result_text(res)}", True)
        key = _key(bs.owner, job_id)
        result = _store_file(key, jname, raw)
        storage.set_meta(key, keep_none=("text_cap",))
        return _text(json.dumps({"job_id": job_id, **result}))
