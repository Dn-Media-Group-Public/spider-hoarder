"""Authenticated end-to-end test against a running gateway (see README): stub
Keycloak JWKS, test-signed tokens, real MCP client. Run inside the container as uid 1000
with the container started with OIDC_ISSUER=http://127.0.0.1:9999/realms/r PUBLIC_HOST=localhost:8000."""

import asyncio
import json
import os
import re
import threading
import time
from urllib.parse import quote
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ISS, AUD, HOST = "http://127.0.0.1:9999/realms/r", "a", "http://localhost:8000"
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
jwk.update(kid="k1", alg="RS256", use="sig")
d = "/tmp/kc/realms/r/protocol/openid-connect"
os.makedirs(d, exist_ok=True)
json.dump({"keys": [jwk]}, open(f"{d}/certs", "w"))
os.makedirs("/tmp/kc/protocol/openid-connect", exist_ok=True)
json.dump({"keys": [jwk]}, open("/tmp/kc/protocol/openid-connect/certs", "w"))  # issuer path variants
srv = HTTPServer(("127.0.0.1", 9999), partial(SimpleHTTPRequestHandler, directory="/tmp/kc"))
threading.Thread(target=srv.serve_forever, daemon=True).start()


def token(email):
    return jwt.encode({"iss": ISS, "aud": AUD, "sub": email, "email": email, "exp": int(time.time()) + 600}, key, algorithm="RS256", headers={"kid": "k1"})


async def call(cs, name, **args):
    r = await cs.call_tool(name, args)
    text = "".join(c.text for c in r.content if hasattr(c, "text"))
    if r.isError:
        print("ERROR", name, text[:300])
    return r.isError, text


async def main():
    assert requests.post(f"{HOST}/clear/mcp", json={}).status_code == 401, "unauthenticated must be 401"
    assert requests.get(f"{HOST}/.well-known/oauth-protected-resource/clear/mcp").status_code == 200
    alice, bob = "alice@example.org", "bob@example.org"
    async with streamablehttp_client(f"{HOST}/clear/mcp", headers={"Authorization": f"Bearer {token(alice)}"}) as (r, w, _):
        async with ClientSession(r, w) as cs:
            await cs.initialize()
            names = {t.name for t in (await cs.list_tools()).tools}
            print("tools:", len(names), {"browser_navigate", "browser_save_page", "ingest_page"} <= names, "browser_install" in names)

            err, text = await call(cs, "ingest_page", url="https://example.com")
            ing = json.loads(text)
            print("ingest_page:", err, ing["files"][0]["path"], "This domain" in ing.get("text", ""))

            err, text = await call(cs, "browser_navigate", url="https://example.org")
            err, text = await call(cs, "browser_save_page")
            saved = json.loads(text)
            print("save_page:", err, "This domain" in saved.get("text", ""))
            err, text = await call(cs, "browser_save_page", target="p:nth-of-type(1)")
            saved = json.loads(text)
            print("save_page target:", err, saved.get("target"), saved.get("url"), "This domain" in saved.get("text", ""), "Данный" not in saved.get("text", ""))

            # jobs for all tools (a data: page: no internet dependence): navigate, find, click by ref, find again, grep
            page = "data:text/html," + quote("<title>E2E</title><p>" + "word " * 100 + "NEEDLE " + "tail " * 100 + "</p><button onclick=\"document.title='clicked';document.body.insertAdjacentHTML('beforeend','<a href=#x>Fresh link</a>')\">Go</button>")
            err, text = await call(cs, "browser_navigate", url=page)
            assert not err and "### Page" in text and "Snapshot" not in text, text  # no post-action snapshot
            err, text = await call(cs, "browser_find", text="needle")
            found = json.loads(text)
            m = json.loads((await call(cs, "list_files", job_id=found["job_id"]))[1])
            print("find job:", found["files"][0]["path"], m["url"][:20], m["text_cap"], m["query"])
            assert found["files"][0]["path"] == "original/matches.txt" and m["text_cap"] == 128 and m["query"] == {"text": "needle"}
            assert m["url"].startswith("data:text/html") and "NEEDLE" in found["text"] and "..." in found["text"]  # window around the match
            err, text = await call(cs, "browser_find", text="go")
            ref = re.search(r'button "Go".*?\[ref=((?:f\d+)?e\d+)\]', json.loads(text)["text"]).group(1)
            err, text = await call(cs, "browser_snapshot", text_cap=None, depth=3)
            snap = json.loads(text)
            m = json.loads((await call(cs, "list_files", job_id=snap["job_id"]))[1])
            assert snap["files"][0]["path"] == "original/snapshot.yml" and m["text_cap"] is None and m["depth"] == 3, m
            assert "NEEDLE" in snap["text"]
            err, text = await call(cs, "browser_snapshot")
            capped = json.loads(text)
            assert "NEEDLE" not in capped["text"] and "..." in capped["text"]
            err, text = await call(cs, "grep_file", job_id=snap["job_id"], path="original/snapshot.yml", pattern="/needle/i", context=0)
            print("grep_file:", err, text[:120].replace("\n", " | "))
            assert not err and "NEEDLE" in text and text.startswith("-- line 2, offset ")
            err, text = await call(cs, "grep_file", job_id=snap["job_id"], path="original/snapshot.yml", pattern="(")
            assert err
            err, text = await call(cs, "browser_click", target=ref)
            assert not err and "Page Title: clicked" in text and "Snapshot" not in text, text
            err, text = await call(cs, "browser_find", text="fresh link")
            assert "Fresh link" in json.loads(text)["text"]
            err, text = await call(cs, "browser_click", target=ref)  # a ref survives an action that changes the page
            assert not err
            err, text = await call(cs, "browser_navigate", url="data:text/html,<title>other</title><p>x</p>")
            err, text = await call(cs, "browser_click", target=ref)  # stale after navigation
            print("stale ref:", err, text[:80])
            assert err and "not found" in text
            err, text = await call(cs, "browser_evaluate", function="() => 1+1")
            assert json.loads(text)["text"] == "2"
            err, text = await call(cs, "get_file_text", job_id=snap["job_id"], path="original/snapshot.yml", offset=0, length=30)
            assert text.startswith("[Showing characters 0-30")
            err, text = await call(cs, "browser_navigate", url="https://example.org")

            err, text = await call(cs, "browser_request_human_help", reason="solve the captcha")
            view = re.search(r"/clear/view/([0-9-]+/[A-Za-z0-9_-]+)/start", text).group(1)
            h = lambda email, **x: {"x-auth-request-email": email, **x}
            uri = f"/clear/view/{view}/vnc.html"
            ok = requests.get(f"{HOST}/authz", headers=h(alice, **{"x-forwarded-uri": uri}))
            bad = requests.get(f"{HOST}/authz", headers=h(bob, **{"x-forwarded-uri": uri}))
            none = requests.get(f"{HOST}/authz", headers={"x-forwarded-uri": uri})
            print("authz owner/other/none:", ok.status_code, ok.headers.get("X-Upstream"), bad.status_code, none.status_code)
            st = requests.get(f"{HOST}/clear/view/{view}/start", headers=h(alice), allow_redirects=False)
            stb = requests.get(f"{HOST}/clear/view/{view}/start", headers=h(bob), allow_redirects=False)
            print("start owner/other:", st.status_code, "password=" in st.headers.get("location", ""), stb.status_code)

            err, text = await call(cs, "get_upload_link")
            up = json.loads(text)
            url = up["url"].replace("https://localhost:8000", HOST)
            requests.post(url, files={"files": ("note.txt", b"hello upload", "text/plain")}).raise_for_status()
            err, text = await call(cs, "list_files", job_id=up["job_id"])
            print("uploaded:", [f["path"] for f in json.loads(text)["files"]])
            err, text = await call(cs, "get_file_text", job_id=up["job_id"], path="parsed/note.txt.md")
            print("file text:", text)
            err, text = await call(cs, "get_file_url", job_id=up["job_id"], path="parsed/note.txt.md")
            blob = requests.get(json.loads(text)["url"].replace("https://localhost:8000", HOST))
            print("blob:", blob.status_code, blob.text)
            print("upload token as blob:", requests.get(url.replace("/upload/", "/blob/")).status_code)

            # owner scoping: bob cannot see alice's jobs
            async with streamablehttp_client(f"{HOST}/clear/mcp", headers={"Authorization": f"Bearer {token(bob)}"}) as (r2, w2, _):
                async with ClientSession(r2, w2) as cb:
                    await cb.initialize()
                    print("bob list_files:", await call(cb, "list_files", job_id=up["job_id"]))
                    print("bob get_file_text:", await call(cb, "get_file_text", job_id=up["job_id"], path="parsed/note.txt.md"))
            print("end:", await call(cs, "browser_end_session"))


asyncio.run(main())
