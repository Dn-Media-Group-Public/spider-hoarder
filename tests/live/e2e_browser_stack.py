"""Full-stack browser end-to-end test: a deployed --dev stack (`./deploy.sh --dev`, BACKENDS with browser_clear/tor/i2p), a real MCP
client, real Keycloak tokens, everything THROUGH nginx. Not a unit test: it needs the whole stack and (for the real-network checks)
internet, Tor and I2P reachability.

    tests/live/e2e_android_setup.sh          # once: client + throwaway users alice, bob (prints the passwords)
    KC_ALICE_PASSWORD=... KC_BOB_PASSWORD=... .venv/bin/python tests/live/e2e_browser_stack.py [browser-clear browser-tor ...]
"""

import asyncio
import json
import os
import re
import sys
import time
from urllib.parse import quote

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = os.environ.get("BASE", "http://127.0.0.1:7297")   # 127.0.0.1: localhost may resolve to ::1 and hang
KC = os.environ.get("KC", "http://127.0.0.1:8085")
PUBLIC = os.environ.get("PUBLIC", "http://localhost:7297")
REALM, CLIENT = os.environ.get("KC_REALM", "newsroom"), os.environ.get("KC_CLIENT", "e2e-android")
HOSTHDR = {"Host": PUBLIC.split("//")[1]}   # the gateways only accept their public host name
FAILED = []
# per backend: a page that proves the egress, and text it must contain
REAL = {"browser-clear": ("https://example.org/", "This domain"),
        "browser-tor": ("https://check.torproject.org/", "Congratulations"),
        "browser-i2p": ("http://i2p-projekt.i2p/", "Invisible Internet")}   # (stats.i2p answers 403 to us: its own policy)


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def token(user):
    r = httpx.post(f"{KC}/realms/{REALM}/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": CLIENT, "username": user,
        "password": os.environ[f"KC_{user.upper()}_PASSWORD"], **({"scope": os.environ["MCP_SCOPE"]} if os.environ.get("MCP_SCOPE") else {})})
    r.raise_for_status()
    return r.json()["access_token"]


class Conn:
    def __init__(self, backend, user):
        self.url, self.user = f"{BASE}/{backend}/mcp", user

    async def __aenter__(self):
        self._http = streamablehttp_client(self.url, headers={"Authorization": f"Bearer {token(self.user)}", **HOSTHDR})
        r, w, self.sid = await self._http.__aenter__()
        self.cs = ClientSession(r, w)
        await self.cs.__aenter__()
        await self.cs.initialize()
        return self

    async def __aexit__(self, *a):
        try:
            await self.call("browser_end_session")
        except BaseException:  # noqa: BLE001
            pass
        await self.cs.__aexit__(*a)
        await self._http.__aexit__(*a)

    async def call(self, name, **args):
        r = await self.cs.call_tool(name, args)
        return r.isError, "".join(c.text for c in r.content if hasattr(c, "text"))


async def one(backend):
    print(f"--- {backend}")
    r = httpx.post(f"{BASE}/{backend}/mcp", json={}, headers=HOSTHDR)
    check(f"{backend}: unauthenticated MCP is 401", r.status_code == 401, r.status_code)
    r = httpx.get(f"{BASE}/.well-known/oauth-protected-resource/{backend}/mcp", headers=HOSTHDR)
    check(f"{backend}: protected-resource metadata", r.status_code == 200 and r.json()["resource"].endswith(f"/{backend}/mcp"), r.status_code)
    async with Conn(backend, "alice") as c:
        sid = c.sid()
        check(f"{backend}: session id carries the replica ip", bool(re.match(r"\d+-\d+-\d+-\d+\.", sid or "")), sid)
        names = {t.name for t in (await c.cs.list_tools()).tools}
        check(f"{backend}: tools", {"open_session", "browser_navigate", "ingest_page", "browser_save_page"} <= names, len(names))
        err, text = await c.call("browser_navigate", url="about:blank")
        check(f"{backend}: tools fail closed before open_session", err and "open_session" in text, text[:80])
        err, text = await c.call("open_session")
        check(f"{backend}: open_session", not err, text[:120])
        # no internet needed: a data: page, jobs, files
        page = "data:text/html," + quote("<title>E2E</title><p>" + "word " * 50 + "NEEDLE</p>")
        err, text = await c.call("browser_navigate", url=page)
        check(f"{backend}: navigate data: page", not err and "E2E" in text, text[:80])
        err, text = await c.call("browser_find", text="needle")
        found = json.loads(text) if not err else {}
        check(f"{backend}: browser_find job", "NEEDLE" in found.get("text", ""), text[:100])
        err, text = await c.call("browser_evaluate", function="() => 1+1")
        check(f"{backend}: browser_evaluate", not err and json.loads(text)["text"] == "2", text[:80])
        # the real network egress of this backend (an unreachable network is a FAIL: the stack is meant to work)
        url, want = REAL[backend]
        err, text = await c.call("browser_navigate", url=url)
        err2, snap = await c.call("browser_save_page")
        check(f"{backend}: reaches {url}", not err and not err2 and want in snap, (text if err else snap)[:150].replace("\n", " "))
        # i2p has no outproxy: clearnet names must be refused (Tor reaches them through exit nodes, that is its job)
        if backend == "browser-i2p":
            err, text = await c.call("browser_navigate", url="https://example.org/")
            check(f"{backend}: clearnet name is refused", err or "This domain" not in text, text[:100].replace("\n", " "))
        # uploads and blobs through nginx; bob cannot see alice's job
        err, text = await c.call("get_upload_link")
        up = json.loads(text)
        up_url = up["url"].replace(PUBLIC, BASE)
        pr = httpx.post(up_url, files={"files": ("note.txt", b"hello upload", "text/plain")}, headers=HOSTHDR)
        check(f"{backend}: upload via nginx", pr.status_code < 300, pr.status_code)
        err, text = await c.call("get_file_url", job_id=up["job_id"], path="parsed/note.txt.md")
        blob = httpx.get(json.loads(text)["url"].replace(PUBLIC, BASE), headers=HOSTHDR) if not err else None
        check(f"{backend}: blob via nginx", blob is not None and blob.status_code == 200 and "hello upload" in blob.text, text[:100])
        async with Conn(backend, "bob") as b:
            await b.call("open_session")
            err, text = await b.call("list_files", job_id=up["job_id"])
            check(f"{backend}: other user cannot see the job", err or "note.txt" not in text, text[:100])
        # human live view needs an SSO login: unauthenticated must be redirected to the login, not served
        v = httpx.get(f"{BASE}/{backend}/view/1-2-3-4/x/vnc.html", headers=HOSTHDR, follow_redirects=False)
        check(f"{backend}: live view without SSO redirects to login", v.status_code == 302 and "/oauth2/start" in v.headers.get("location", ""), v.status_code)


async def main():
    backends = [a for a in sys.argv[1:] if a.startswith("browser-")] or list(REAL)
    r = httpx.get(f"{BASE}/healthz")
    check("nginx healthz", r.status_code == 200)
    for b in backends:
        t = time.time()
        try:
            await asyncio.wait_for(one(b), 600)
        except BaseException as e:  # noqa: BLE001
            check(f"{b}: ran to the end", False, f"{type(e).__name__}: {e}"[:300])
        print(f"({b}: {time.time() - t:.0f}s)")
    print("FAILED:", FAILED or "none")
    sys.exit(1 if FAILED else 0)


asyncio.run(main())
