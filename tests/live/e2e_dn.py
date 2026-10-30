"""End to end through nginx on the deployed --dev stack, with real Keycloak tokens: open dn.no in each backend and look at it.

    source .env-less; eval "$(tests/live/e2e_android_setup.sh)"
    .venv/bin/python tests/live/e2e_dn.py browser-clear browser-tor android-clear [android-tor]

browser-*: open_session, browser_navigate, browser_save_page (the page as a job), get_file_text.
android-*: device_open_url (the emulator's browser), device_snapshot, get_file_text.
Prints what it found; exits non-zero if the page text never mentions dn.no / DN content."""

import asyncio
import json
import os
import re
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = os.environ.get("BASE", "http://127.0.0.1:7297")  # 127.0.0.1: localhost resolves to ::1 first here
KC = os.environ.get("KC", "http://127.0.0.1:8085")
PUBLIC = os.environ.get("PUBLIC", "http://localhost:7297")
REALM = os.environ.get("KC_REALM", "newsroom")
HOST = {"Host": PUBLIC.split("//")[1]}
SITE = os.environ.get("SITE", "https://www.dn.no/")


def token():
    r = httpx.post(f"{KC}/realms/{REALM}/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "e2e-android", "username": "alice",
        "password": os.environ["KC_ALICE_PASSWORD"], "scope": os.environ.get("MCP_SCOPE", "spider-hoarder")})
    r.raise_for_status()
    return r.json()["access_token"]


class FreshToken(httpx.Auth):
    def __init__(self):
        self.tok, self.at = None, 0

    def auth_flow(self, request):
        if time.time() - self.at > 120:
            self.tok, self.at = token(), time.time()
        request.headers["Authorization"] = f"Bearer {self.tok}"
        yield request


def text_of(r):
    return "".join(c.text for c in r.content if hasattr(c, "text"))


async def call(cs, name, **args):
    r = await cs.call_tool(name, args)
    return r.isError, text_of(r)


async def job_text(cs, res: str) -> str:
    """The text of a {job_id, files, text?} result: inline `text`, else the first parsed/ or original/ text file."""
    j = json.loads(res)
    if j.get("text"):
        return j["text"]
    for f in j.get("files", []):
        if f["path"].startswith(("parsed/", "original/")) and not f["path"].endswith((".png", ".pdf")):
            err, t = await call(cs, "get_file_text", job_id=j["job_id"], path=f["path"], length=6000)
            if not err:
                return t
    return ""


async def run(server):
    url = f"{BASE}/{server}/mcp"
    out = {"server": server, "ok": False}
    async with streamablehttp_client(url, headers=HOST, auth=FreshToken()) as (r, w, _):
        async with ClientSession(r, w) as cs:
            init = await cs.initialize()
            tools = [t.name for t in (await cs.list_tools()).tools]
            out["server_name"] = init.serverInfo.name
            out["tools"] = len(tools)
            out["instructions_tail"] = (init.instructions or "")[-420:]
            try:
                if server.startswith("browser-"):
                    err, t = await call(cs, "open_session")
                    out["open_session"] = t[:200]
                    err, t = await call(cs, "browser_navigate", url=SITE)
                    out["navigate"] = ("ERR " if err else "") + t[:300]
                    await call(cs, "browser_wait_for", time=3)
                    err, t = await call(cs, "browser_save_page")
                    out["save_page_err"] = err
                    body = await job_text(cs, t) if not err else t
                else:
                    err, t = await call(cs, "device_open_url", url=SITE)
                    out["open_url"] = ("ERR " if err else "") + t[:300]
                    await asyncio.sleep(12)  # Chrome's first run + page load on a cold snapshot
                    for _ in range(4):
                        err, t = await call(cs, "device_snapshot")
                        body = await job_text(cs, t) if not err else t
                        if re.search(r"dn\.no|Dagens", body, re.I):
                            break
                        await asyncio.sleep(8)
                out["chars"] = len(body)
                out["sample"] = re.sub(r"\s+", " ", body)[:500]
                out["ok"] = bool(re.search(r"dn\.no|Dagens N", body, re.I))
            finally:
                await call(cs, "browser_end_session" if server.startswith("browser-") else "device_end_session")
    return out


async def main():
    ok = True
    for s in sys.argv[1:] or ["browser-clear"]:
        try:
            o = await run(s)
        except BaseException as e:  # noqa: BLE001
            o = {"server": s, "ok": False, "error": repr(e)[:400]}
        print(json.dumps(o, indent=1, ensure_ascii=False), flush=True)
        ok &= bool(o.get("ok"))
    sys.exit(0 if ok else 1)


asyncio.run(main())
