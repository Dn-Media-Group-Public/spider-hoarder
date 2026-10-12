"""Authenticated end-to-end test against a running gateway (see README): stub
Keycloak JWKS, test-signed tokens, real MCP client. Run inside the container as uid 1000
with the container started with OIDC_ISSUER=http://127.0.0.1:9999/realms/r PUBLIC_HOST=localhost:8000."""

import asyncio
import json
import os
import re
import threading
import time
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
            print("ingest_page:", err, ing["files"][0]["path"], "Example Domain" in ing.get("text", ""))

            err, text = await call(cs, "browser_navigate", url="https://example.org")
            err, text = await call(cs, "browser_save_page")
            saved = json.loads(text)
            print("save_page:", err, "Example Domain" in saved.get("text", ""))
            err, text = await call(cs, "browser_save_page", target="h1")
            saved = json.loads(text)
            print("save_page target:", err, saved.get("target"), saved.get("url"), "Example Domain" in saved.get("text", ""), "More information" not in saved.get("text", ""))

            err, text = await call(cs, "browser_request_human_help", reason="solve the captcha")
            view = re.search(r"/clear/view/([A-Za-z0-9_-]+)/start", text).group(1)
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
            print("file text:", json.loads(text))
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
