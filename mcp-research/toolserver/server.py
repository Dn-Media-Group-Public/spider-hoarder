"""MCP server (streamable HTTP) replacing @playwright/mcp: per-session process, one Chromium,
many browsers (windows). See docs/implemented-plans/python-browser-tool-server.md."""

import asyncio
import json
import os

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from playwright.async_api import Error as PWError

from . import tools
from .textcap import DEFAULT_TEXT_CAP
from .browsers import launch

GOLDEN = os.path.join(os.path.dirname(__file__), "..", "golden_tools.json")  # playwright-mcp 0.0.83
DROPPED = {"browser_run_code_unsafe"}  # JS snippets cannot run in a Python server; browser_evaluate stays

BROWSER = {"type": "string", "description": (
    "Browser id returned by browser_start_session. Omit to use the shared 'main' browser, which belongs to the "
    "main agent and the human: if you are a subagent, never use 'main'; call browser_start_session first and "
    "pass the id it returns on every call.")}
TEXT_CAP = {"type": ["integer", "null"], "minimum": 1, "default": DEFAULT_TEXT_CAP, "description": (
    f"Cut every pure text node (`- text: …`, `paragraph [ref=…]: …`) to this many characters, adding `...` "
    f"(default {DEFAULT_TEXT_CAP}); null = no cap. Names (`link \"…\"`) are never cut. For browser_find the match is "
    "searched in the full text and the shown text is cut to a window around it.")}
s_, i_, b_ = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}


def schema(required=(), **props) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


NEW = {
    "browser_start_session": ("Open a new, separate browser window (same logins as 'main') and return its id. Subagents: call this "
        "first and use ONLY the id it returns on every browser_* call (never 'main', never another agent's id); "
        "call browser_end_session with that id when done.", schema(), False),
    "browser_end_session": ("Close one browser (its window and tabs) when given `browser`; with no `browser`, close all browsers "
        "and end the session.", schema(), True),
    "browser_bring_to_front": ("Raise a browser's window.", schema(), True),
    "browser_cookie_list": ("List cookies, optionally only those applying to `url` or matching `domain` / `path`.",
        schema(url=s_, domain=s_, path=s_), True),
    "browser_cookie_get": ("Get one cookie by name.", schema(["name"], name=s_), True),
    "browser_cookie_set": ("Set a cookie.", schema(["name", "value"], name=s_, value=s_, domain=s_, path=s_,
        expires={"type": "number"}, httpOnly=b_, secure=b_, sameSite={"type": "string", "enum": ["Strict", "Lax", "None"]}), True),
    "browser_cookie_delete": ("Delete cookies by name (optionally domain/path).", schema(["name"], name=s_, domain=s_, path=s_), True),
    "browser_cookie_clear": ("Delete all cookies.", schema(), True),
    "browser_storage_state": ("Save cookies and localStorage of all browsers to a file (returned as a job file).",
        schema(filename=s_), True),
    "browser_set_storage_state": ("Restore cookies and localStorage from a storage state given inline as JSON.",
        schema(["state"], state={"description": "Storage state: {cookies: [...], origins: [{origin, localStorage: [{name, value}]}]}",
                                  "type": ["object", "string"]}), True),
}
for kind in ("localstorage", "sessionstorage"):
    k = kind.replace("storage", " storage")
    NEW |= {
        f"browser_{kind}_list": (f"List {k} items of the current page.", schema(), True),
        f"browser_{kind}_get": (f"Get a {k} item.", schema(["key"], key=s_), True),
        f"browser_{kind}_set": (f"Set a {k} item.", schema(["key", "value"], key=s_, value=s_), True),
        f"browser_{kind}_delete": (f"Delete a {k} item.", schema(["key"], key=s_), True),
        f"browser_{kind}_clear": (f"Clear {k}.", schema(), True),
    }

def build_tools() -> list[types.Tool]:
    out = []
    for t in json.load(open(GOLDEN)):
        if t["name"] in DROPPED:
            continue
        extra = {"text_cap": TEXT_CAP} if t["name"] in ("browser_snapshot", "browser_find") else {}
        sch = {**t["inputSchema"], "properties": {**t["inputSchema"].get("properties", {}), **extra, "browser": BROWSER}}
        out.append(types.Tool(name=t["name"], description=t.get("description", ""), inputSchema=sch))
    for name, (desc, sch, with_browser) in NEW.items():
        sch = {**sch, "properties": {**sch["properties"], **({"browser": BROWSER} if with_browser else {})}}
        out.append(types.Tool(name=name, description=desc, inputSchema=sch))
    return out


TOOLS = build_tools()
assert {t.name for t in TOOLS} <= set(tools.HANDLERS), {t.name for t in TOOLS} - set(tools.HANDLERS)

_reg = None
_reg_lock = asyncio.Lock()
_cfg: dict = {}


async def registry():
    global _reg
    async with _reg_lock:  # Chromium starts on first use, so tool listing needs no display
        if _reg is None:
            _, _reg = await launch(_cfg)
    return _reg


def reply(text: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=error)


def build(port: int) -> FastMCP:
    mcp = FastMCP(
        "toolserver", host="127.0.0.1", port=port, streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=["mcp-upstream"], allowed_origins=[]))
    server = mcp._mcp_server

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        args = arguments or {}
        try:
            if name not in tools.HANDLERS:
                raise tools.ToolError(f"unknown tool {name}")
            R = await registry()
            bid = None if name in ("browser_start_session", "browser_end_session") else await R.resolve(args.get("browser"))
            return reply(await tools.HANDLERS[name](R, bid, args))
        except Exception as e:  # noqa: BLE001
            msg = str(e).split("\nCall log:")[0].strip()
            return reply(f"### Error\nError: {msg[:2000]}", True)

    return mcp


def main(config_path: str) -> None:
    _cfg.update(json.load(open(config_path)))
    tools.OUT = _cfg["outDir"]
    build(_cfg["port"]).run(transport="streamable-http")
