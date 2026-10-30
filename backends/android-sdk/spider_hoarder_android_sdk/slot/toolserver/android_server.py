"""MCP server (streamable HTTP) of a slot: the Android tools over uiautomator2. Reachable only by the gateway:
every request needs X-Slot-Secret, and Host must be `mcp-upstream` (as in the browser tool server)."""

import asyncio
import hmac
import os

import uvicorn
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse

import android_tools as tools
from device import Device

SECRET = os.environ.get("SLOT_SECRET", "")


def reply(text: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=error)


class SecretGate:
    """ASGI middleware: no valid X-Slot-Secret, no service. A slot without a secret refuses everything."""

    def __init__(self, app, secret: str):
        self.app, self.secret = app, secret.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            given = dict(scope["headers"]).get(b"x-slot-secret", b"")
            if not self.secret or not hmac.compare_digest(given, self.secret):
                return await PlainTextResponse("forbidden", status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)


def build(port: int, device: Device | None = None) -> FastMCP:
    dev = device or Device()
    lock = asyncio.Lock()  # one UI action at a time: the screen is a single shared resource
    mcp = FastMCP(
        "android-toolserver", host="0.0.0.0", port=port, streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=["mcp-upstream"], allowed_origins=[]))
    server = mcp._mcp_server
    listed = tools.build_tools()

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return listed

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        try:
            if name not in tools.HANDLERS:
                raise tools.ToolError(f"unknown tool {name}")
            async with lock:
                return reply(await asyncio.to_thread(tools.HANDLERS[name], dev, arguments or {}))
        except Exception as e:  # noqa: BLE001
            return reply(f"### Error\nError: {str(e)[:2000]}", True)

    return mcp


def main(port: int) -> None:
    app = SecretGate(build(port).streamable_http_app(), SECRET)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    import sys

    main(int(sys.argv[1]) if len(sys.argv) > 1 else 9000)
