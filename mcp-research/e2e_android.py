"""End-to-end test of the Android MCP servers THROUGH NGINX on a deployed (dev) stack, with a real MCP client and real
Keycloak tokens. Unlike e2e_auth.py (one gateway, stub Keycloak) this needs the whole stack and a KVM host, so it is not
part of the host tests. Run it with host networking so nginx (127.0.0.1:7297) and Keycloak (127.0.0.1:8085) are the
public addresses the gateways were deployed with:

    docker run --rm --network host -v $PWD:/app -w /app -e KC_ALICE_PASSWORD=... -e KC_BOB_PASSWORD=... \
        --entrypoint python mcp-research:local e2e_android.py [android-clear|android-tor]

Two throwaway Keycloak users (alice, bob) and a client with direct grants are created by `e2e_android_setup.sh`.
Covers the slot-independent part of docs/plans/android-kvm-bringup.md stage 4 step 5. The noVNC handoff in a browser is
`e2e_android_view.py` (needs a human-free Chromium, also in this image)."""

import asyncio
import io
import json
import os
import re
import sys
import time
import urllib.request

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

EGRESS = next((a for a in sys.argv[1:] if a.startswith("android-")), "android-clear")
# 127.0.0.1, not localhost: on this host localhost resolves to ::1 first and hangs. The links the gateway hands out say
# PUBLIC_HOST (localhost:7297), which local() rewrites.
BASE = os.environ.get("BASE", "http://127.0.0.1:7297")
KC = os.environ.get("KC", "http://127.0.0.1:8085")
PUBLIC = os.environ.get("PUBLIC", "http://localhost:7297")
REALM = os.environ.get("KC_REALM", "newsroom")
CLIENT = os.environ.get("KC_CLIENT", "e2e-android")
APK = os.environ.get("APK_URL", "https://f-droid.org/repo/org.woheller69.arity_159.apk")
PKG = "org.woheller69.arity"
URL = f"{BASE}/{EGRESS}/mcp"
RESET_WAIT = int(os.environ.get("RESET_WAIT", "300"))
FAILED = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def local(url):
    return url.replace(PUBLIC, BASE)


def token(user):
    r = httpx.post(f"{KC}/realms/{REALM}/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": CLIENT, "username": user,
        "password": os.environ[f"KC_{user.upper()}_PASSWORD"], "scope": os.environ.get("MCP_SCOPE", "spider-hoarder")})
    r.raise_for_status()
    return r.json()["access_token"]


HOSTHDR = {"Host": PUBLIC.split("//")[1]}  # the gateway only accepts its public host name


def headers(user):
    return {"Authorization": f"Bearer {token(user)}", **HOSTHDR}


class FreshToken(httpx.Auth):
    """Bearer token that is renewed before it expires (Keycloak's access tokens live 5 minutes; a long test outlasts one)."""

    def __init__(self, user):
        self.user, self.tok, self.at = user, None, 0

    def auth_flow(self, request):
        if time.time() - self.at > 120:
            self.tok, self.at = token(self.user), time.time()
        request.headers["Authorization"] = f"Bearer {self.tok}"
        yield request


class Conn:
    """One MCP connection (= one conversation = one Android session)."""

    def __init__(self, user, owns_session=True):
        self.user = user
        self.owns_session = owns_session

    async def __aenter__(self):
        self._http = streamablehttp_client(URL, headers=HOSTHDR, auth=FreshToken(self.user))
        r, w, self.get_sid = await self._http.__aenter__()
        self.cs = ClientSession(r, w)
        await self.cs.__aenter__()
        self.init = await self.cs.initialize()
        return self

    async def __aexit__(self, *a):
        if self.owns_session:
            try:
                await self.call("device_end_session")  # never leave the slot held: the next run would find it busy
            except BaseException:  # noqa: BLE001
                pass
        try:
            await self.cs.__aexit__(*a)
        finally:
            await self._http.__aexit__(*a)

    async def call(self, name, **args):
        r = await self.cs.call_tool(name, args)
        text = "".join(c.text for c in r.content if hasattr(c, "text"))
        return r.isError, text


async def wait_slot_free(timeout=RESET_WAIT):
    """A new session works again once the replaced slot is free: poll with initialize+snapshot until one is served."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            async with Conn("alice") as c:
                err, text = await c.call("device_current_app")
                if not err:
                    await c.call("device_end_session")
                    return True
                last = text
        except Exception as e:  # noqa: BLE001 - busy / booting: 503 before the session exists
            last = repr(e)
        await asyncio.sleep(5)
    print("  slot never became usable:", last[:200])
    return False


def raw_initialize(user):
    return httpx.post(URL, headers={**headers(user), "Accept": "application/json, text/event-stream"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}})


async def wait_capacity():
    """A previous run's session end recreates the slot (~1-2 min): initialize answers 503 until it is free again. A
    successful initialize RESERVES the slot (nginx relies on that), so the probe ends the session it got."""
    deadline = time.time() + RESET_WAIT
    while time.time() < deadline:
        r = raw_initialize("alice")
        if r.status_code != 503:
            sid = r.headers.get("mcp-session-id")
            if sid:
                httpx.delete(URL, headers={**headers("alice"), "mcp-session-id": sid})
            return True
        await asyncio.sleep(5)
    return False


async def main():
    await wait_capacity()
    # 1. auth gate and discovery
    r = httpx.post(URL, json={}, headers=HOSTHDR)
    check("unauthenticated initialize is 401", r.status_code == 401, str(r.status_code))
    r = httpx.get(f"{BASE}/.well-known/oauth-protected-resource/{EGRESS}/mcp", headers=HOSTHDR)
    check("protected-resource metadata", r.status_code == 200 and EGRESS in r.json().get("resource", ""), r.text[:120])

    async with Conn("alice") as a:
        # 2. tools and instructions
        names = {t.name for t in (await a.cs.list_tools()).tools}
        dev = {n for n in names if n.startswith("device_")}
        check("tool list: device tools + file tools, no browser tools",
              {"device_snapshot", "device_click", "device_type", "device_take_screenshot", "device_install_apk",
               "device_request_human_help", "device_end_session", "get_upload_link", "get_file_url", "grep_file",
               "list_files", "get_file_text"} <= names and not any(n.startswith("browser_") for n in names),
              f"{len(names)} tools, {len(dev)} device_*")
        ins = a.init.instructions or ""
        check("instructions present", "device_request_human_help" in ins and "ANDROID" in ins, ins[:60])

        # 3. snapshot / find / click / type / screenshot job / get_file_url / grep
        err, text = await a.call("device_snapshot")
        snap = json.loads(text)
        check("device_snapshot job", not err and snap["files"][0]["path"].endswith("snapshot.txt"), str(snap["files"]))
        err, text = await a.call("grep_file", job_id=snap["job_id"], path=snap["files"][0]["path"], pattern="/chrome|settings|play store/i", context=0)
        check("grep_file over a snapshot job", not err and "-- line" in text, text[:100].replace("\n", " | "))
        err, text = await a.call("device_open_url", url="https://example.com")
        check("device_open_url", not err, text[:100])
        err, text = await a.call("device_wait_for", text="Example Domain", timeout=60)
        check("device_wait_for 'Example Domain'", not err, text[:100])
        if err:  # what is on the screen? (a fresh slot's first page load has failed before)
            e2, t2 = await a.call("device_snapshot")
            print("  screen:", json.loads(t2).get("text", t2)[:600].replace("\n", " ; "))
            e3, t3 = await a.call("device_shell", command="ping -c1 -W3 example.com | tail -2; dumpsys connectivity | grep -i 'validated' | head -3; logcat -d -s chromium:E cr_X:E | tail -5")
            print("  net:", t3[:500].replace("\\n", " ; "))
        err, text = await a.call("device_find", text="Example Domain")
        found = json.loads(text)
        ref = re.search(r"\[ref=([^\]]+)\]", found.get("text", ""))
        check("device_find returns a ref", not err and bool(ref), found.get("text", "")[:100])
        if ref:
            err, text = await a.call("device_click", ref=ref.group(1))
            check("device_click by ref", not err, text[:100])
        err, text = await a.call("device_take_screenshot", filename="e2e.png")
        shot = json.loads(text)
        check("screenshot job", not err and shot["files"][0]["path"].endswith("e2e.png"), str(shot["files"]))
        err, text = await a.call("get_file_url", job_id=shot["job_id"], path=shot["files"][0]["path"])
        url = json.loads(text)["url"]
        blob = httpx.get(local(url), headers=HOSTHDR)
        check("get_file_url download is a PNG (through nginx)", blob.status_code == 200 and blob.content[:4] == b"\x89PNG",
              f"{blob.status_code} {len(blob.content)} bytes")
        # the link is a signed capability: tampering must fail
        check("tampered blob link rejected", httpx.get(local(url)[:-2] + ("aa" if not url.endswith("aa") else "bb"), headers=HOSTHDR).status_code in (400, 403, 404))

        # 4. upload an APK through the upload link, install it
        err, text = await a.call("get_upload_link")
        up = json.loads(text)
        apk = urllib.request.urlopen(APK, timeout=120).read()
        r = httpx.post(local(up["url"]), headers=HOSTHDR, files={"files": ("e2e.apk", apk, "application/vnd.android.package-archive")}, timeout=120)
        check("upload page accepts an APK (through nginx)", r.status_code < 300, str(r.status_code))
        err, text = await a.call("list_files", job_id=up["job_id"])
        paths = [f["path"] for f in json.loads(text)["files"]]
        check("uploaded APK in the job", "original/e2e.apk" in paths, str(paths))
        err, text = await a.call("device_install_apk", job_id=up["job_id"], path="original/e2e.apk")
        check("device_install_apk", not err, text[:150])
        err, text = await a.call("device_app_list")
        check("installed package is listed", PKG in text or PKG in json.loads(text).get("text", ""), text[:80])
        err, text = await a.call("device_install_apk", job_id=up["job_id"], path="original/nope.apk")
        check("install of a missing file fails cleanly", err, text[:100])

        # 5. a second session of the same user and of another user while the slot is busy
        for who, label in (("alice", "second session of the same user (per-user limit)"), ("bob", "other user, all slots busy")):
            t0 = time.time()
            r = raw_initialize(who)
            check(f"{label}: 503 at once with a readable message", r.status_code == 503 and ("try again later" in r.text or "end one first" in r.text) and time.time() - t0 < 10,
                  f"{r.status_code} {r.text[:100]} ({time.time() - t0:.1f}s)")
        err, text = await a.call("device_current_app")
        check("first session unaffected", not err, text[:100])

    return


async def main_wrapper():
    try:
        await main()
    except* Exception as eg:  # noqa: BLE001
        for e in eg.exceptions:
            print("EXCEPTION", repr(e)[:300])
            FAILED.append(repr(e)[:80])
    print("\nFAILED:" if FAILED else "\nALL PASSED", FAILED or "")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main_wrapper())
