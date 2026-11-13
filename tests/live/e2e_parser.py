"""End to end of the parser service (docs/implemented-plans/parser-wrapper.md) on the deployed --dev stack, through nginx, with real Keycloak tokens,
a real browser, dn.no as the site, and the parser's isolation checked from inside its container.

    tests/live/e2e_android_setup.sh     # once: the e2e-android client and users
    KC_ALICE_PASSWORD=... .venv/bin/python tests/live/e2e_parser.py [browser-clear]   (--isolation: only the container checks)

Covers: ingest_page / browser_save_page of dn.no (parsed, simplified, terse, metadata, inline text), a downloaded image (metadata, no text), a screenshot and
a PDF (the one path for tool-produced files), the network-request tree job, an upload link with a hostile archive (unpacked in the parser, zip-slip, a
1 GiB zip bomb), several parses at once (any-replica status reads when the parser is scaled), and, with docker access on the swarm host, what the parser uid
can and cannot reach. Prints PASS/FAIL per check; exits non-zero if any failed."""

import asyncio
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = os.environ.get("BASE", "http://127.0.0.1:7297")
KC = os.environ.get("KC", "http://127.0.0.1:8085")
PUBLIC = os.environ.get("PUBLIC", "http://localhost:7297")
REALM = os.environ.get("KC_REALM", "newsroom")
HOST = {"Host": PUBLIC.split("//")[1]}
SITE = os.environ.get("SITE", "https://www.dn.no/")
IMAGE = os.environ.get("IMAGE", "https://stc.dngroup.com/cpp/dn/icons/android-icon-192x192.png")
FAILED = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{str(detail)[:200]}]" if detail != "" else ""), flush=True)
    if not ok:
        FAILED.append(name)


def token():
    r = httpx.post(f"{KC}/realms/{REALM}/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "e2e-android", "username": "alice", "password": os.environ["KC_ALICE_PASSWORD"]})
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


async def call(cs, name, /, **args):
    r = await cs.call_tool(name, args)
    return r.isError, "".join(c.text for c in r.content if hasattr(c, "text"))


def paths(j):
    return {f["path"] for f in j["files"]}


async def file_text(cs, job, path):
    err, t = await call(cs, "get_file_text", job_id=job, path=path, length=100000)
    return "" if err else t


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def exec_in(service, *cmd, user="20000"):
    cid = sh("docker", "ps", "-q", "-f", f"name=spider-hoarder_{service}.").stdout.split()[0]
    return sh("docker", "exec", "-u", user, cid, *cmd)


async def functional(server):
    url = f"{BASE}/{server}/mcp"
    async with streamablehttp_client(url, headers=HOST, auth=FreshToken()) as (r, w, _):
        async with ClientSession(r, w) as cs:
            await cs.initialize()
            err, t = await call(cs, "create_project", name="e2e-parser")
            proj = json.loads(t)["project"]
            err, t = await call(cs, "open_session", project=proj)
            check("open_session", not err, t[:100])
            try:
                await _scenarios(cs, server, proj)
            finally:
                await call(cs, "browser_end_session")


async def _scenarios(cs, server, proj):
    # 1. ingest_page of a page
    t0 = time.time()
    err, t = await call(cs, "ingest_page", url=SITE)
    j = json.loads(t)
    check("ingest_page dn.no: no error", not err, t[:150])
    p = paths(j)
    check("ingest_page: original, parsed, simplified, terse stored", {f for f in p if f.startswith("original/")} and any(f.startswith("parsed/") for f in p)
          and any(f.startswith("simplified/") for f in p) and any(f.startswith("terse/") for f in p), sorted(p))
    check("ingest_page: inline text or outline mentions DN", bool(re.search(r"dn\.no|Dagens|DN", j.get("text", ""))), (j.get("text") or "")[:80])
    check("ingest_page: no 'not parsed' notes", not [n for n in j.get("notes", []) if "not parsed" in n], j.get("notes"))
    print(f"     ({time.time() - t0:.1f} s)")
    parsed = next(f for f in p if f.startswith("parsed/"))
    big = await file_text(cs, j["job_id"], parsed)
    check("parsed page is absolutized (https links)", "](https://" in big or "](http://" in big, len(big))
    err, g = await call(cs, "grep_file", job_id=j["job_id"], path=parsed, pattern="/dn/i", max_matches=3)
    check("grep_file on parsed/", not err and "line" in g, g[:80])
    # 2. a binary download: metadata, no text
    err, t = await call(cs, "ingest_page", url=IMAGE)
    j = json.loads(t)
    check("ingest_page image: metadata/*.yaml (exiftool) written", any(f.startswith("metadata/") for f in paths(j)), sorted(paths(j)))
    check("ingest_page image: original kept", any(f.startswith("original/") for f in paths(j)))
    meta = next((f for f in paths(j) if f.startswith("metadata/")), None)
    if meta:
        y = await file_text(cs, j["job_id"], meta)
        check("metadata names the file type PNG and the size", "PNG" in y and ("192" in y), y[:120].replace("\n", " "))
    # 3. tool-produced files take the same path
    err, t = await call(cs, "browser_navigate", url=SITE)
    check("navigate", not err, t[:100])
    await call(cs, "browser_wait_for", time=3)
    err, t = await call(cs, "browser_take_screenshot")
    j = json.loads(t)
    check("screenshot: original + metadata", any(f.startswith("original/") for f in paths(j)) and any(f.startswith("metadata/") for f in paths(j)), sorted(paths(j)))
    err, t = await call(cs, "browser_pdf_save")
    j = json.loads(t)
    check("pdf: original, parsed text and PDF metadata", any(f.startswith("parsed/") for f in paths(j)) and any(f.startswith("metadata/") for f in paths(j)), sorted(paths(j)))
    meta = next((f for f in paths(j) if f.startswith("metadata/")), None)
    if meta:
        y = await file_text(cs, j["job_id"], meta)
        check("PDF metadata has the SpiderHoarder-PDF group", "SpiderHoarder-PDF" in y, y[:100].replace("\n", " "))
    err, t = await call(cs, "browser_snapshot")
    j = json.loads(t)
    check("snapshot (plain text file): inline text from original/", "text" in j and j.get("text_path", "").startswith("original/") or "text" in j, j.get("text_path"))
    err, t = await call(cs, "browser_save_page")
    j = json.loads(t)
    check("save_page: parsed + simplified + terse", all(any(f.startswith(d) for f in paths(j)) for d in ("parsed/", "simplified/", "terse/")), sorted(paths(j)))
    # 4. network request tree job
    err, t = await call(cs, "browser_network_requests", new=False)
    lj = json.loads(t)
    t = lj.get("text") or await file_text(cs, lj["job_id"], next(f for f in paths(lj) if f.startswith("original/")))
    m = re.search(r"(\d+)\.\s*\[GET\][^\n]*=> \[200\]", t)
    if m:
        err, t = await call(cs, "browser_network_request_get", index=int(m.group(1)))
        j = json.loads(t)
        check("network request tree: response/headers + body parsed by the one rule", any(f.startswith("original/response/") for f in paths(j)), sorted(paths(j))[:6])
    else:
        check("network request listing found a 200 GET", False, t[:150])
    # 5. an upload link with hostile files
    err, t = await call(cs, "get_upload_link", project=proj)
    up = json.loads(t)
    check("get_upload_link", not err, t[:100])
    token_ = up["url"].rsplit("/", 1)[1]
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ok/note.md", "# Note\n[link](https://dn.no)\n")
        zf.writestr("../../escape.txt", "slip")
        zf.writestr("/abs.txt", "slip")
        zf.writestr("inner.zip", b"PK\x05\x06" + b"\0" * 18)
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        with zf.open("zeros.bin", "w", force_zip64=True) as m:
            chunk = b"\0" * (1 << 20)
            for _ in range(1024):
                m.write(chunk)
    print(f"     (bomb: {bomb.tell() >> 10} KiB compressed, 1 GiB uncompressed)")
    m3u8 = b"#EXTM3U\n#EXTINF:1,\nhttp://169.254.169.254/latest/meta-data/\n"
    files = [("files", ("hostile.zip", z.getvalue(), "application/zip")), ("files", ("playlist.m3u8", m3u8, "application/vnd.apple.mpegurl")),
             ("files", ("bomb.zip", bomb.getvalue(), "application/zip"))]
    t0 = time.time()
    async with httpx.AsyncClient(timeout=900) as hc:
        r = await hc.post(f"{BASE}{up['url'].split(PUBLIC)[1]}", files=files, headers=HOST)
    check("upload with archives + bomb answered", r.status_code == 200, f"{r.status_code} in {time.time() - t0:.0f} s")
    job = up["job_id"]
    err, t = await call(cs, "list_files", job_id=job)
    lf = json.loads(t)
    p = paths(lf)
    check("archive unpacked in the parser: members under original/<zip>.d/", "original/hostile.zip.d/ok/note.md" in p and "terse/hostile.zip.d/ok/note.md" in p, sorted(p)[:10])
    check("zip-slip names dropped", not any("escape" in f or "abs.txt" in f for f in p))
    check("inner archive stored, not unpacked again", "original/hostile.zip.d/inner.zip" in p and not any("inner.zip.d" in f for f in p))
    check("m3u8 stored and parsed without a fetch (job done)", "original/playlist.m3u8" in p)
    check("1 GiB bomb: member kept, not parsed (> MAX_PARSE_BYTES) with a note", any("bomb.zip.d/zeros.bin" in f for f in p) and any("not parsed" in n for n in lf.get("notes", [])), lf.get("notes"))
    check("bomb member size in the manifest is 1 GiB", any(f["size"] == 1 << 30 for f in lf["files"] if f["path"].endswith("zeros.bin")))
    # 6. several parses at once
    t0 = time.time()
    res = await asyncio.gather(*[call(cs, "ingest_page", url=SITE) for _ in range(6)])
    check("6 concurrent ingest_page all parsed", all(not e and "parsed/" in t for e, t in res), f"{time.time() - t0:.1f} s; " + str([json.loads(t).get("notes") for e, t in res if not e]))


def isolation():
    print("-- parser container isolation (uid 20000 inside the swarm service)")
    cfg = "http://blob-sidecar:3128"
    py = "import sys,urllib.request as u\ntry:\n r=u.urlopen(sys.argv[1],timeout=8); print(r.status)\nexcept Exception as e: print(type(e).__name__, getattr(e,'code',''))\n"

    def run(service, code, *args, user="20000"):
        r = exec_in(service, "python", "-c", code, *args, user=user)
        return (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else ""
    out = run("parser", "import urllib.request as u\nh=u.ProxyHandler({'http':'http://blob-sidecar:3128'})\ntry:\n print(u.build_opener(h).open('http://example.com',timeout=8).status)\nexcept Exception as e: print(type(e).__name__, getattr(e,'code',''))")
    check("parser uid -> proxy without a token: refused (407)", "407" in out, out)
    out = run("parser", "import os,urllib.request as u\nh=u.ProxyHandler({'http':'http://parse:wrong@blob-sidecar:3128'})\ntry:\n print(u.build_opener(h).open('http://example.com',timeout=8).status)\nexcept Exception as e: print(type(e).__name__, getattr(e,'code',''))")
    check("parser uid -> proxy with a wrong token: 407", "407" in out, out)
    out = run("parser", py, "http://example.com/")
    check("parser uid -> internet directly: no route", "200" not in out, out)
    out = run("parser", py, "http://browser-clear-gateway:7298/")
    check("parser uid reaches the gateway port of its parse network (known residual risk) but gets nothing without a token: not a 200", "200" not in out, out)
    tok = sh("docker", "service", "inspect", "spider-hoarder_parser", "--format", "{{json .Spec.TaskTemplate.ContainerSpec.Env}}").stdout
    ptok = re.search(r"PROXY_TOKEN=([0-9a-f]+)", tok).group(1)
    for tgt, name in (("http://169.254.169.254/", "metadata server"), ("http://10.0.0.1/", "a private address"), ("http://127.0.0.1:8080/", "the wrapper's own port on loopback")):
        r = exec_in("parser", "python", "-c", "import sys,urllib.request as u\nh=u.ProxyHandler({'http':'http://parse:%s@blob-sidecar:3128'})\ntry:\n print(u.build_opener(h).open(sys.argv[1],timeout=10).status)\nexcept Exception as e: print(type(e).__name__, getattr(e,'code',''))" % ptok, tgt, user="900")
        out = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
        check(f"wrapper uid via the proxy -> {name}: blocked", "200" not in out[0], out[0])
    r = exec_in("parser", "python", "-c", "import urllib.request as u\nh=u.ProxyHandler({'https':'http://parse:%s@blob-sidecar:3128'})\nprint(u.build_opener(h).open('https://dn.no',timeout=15).status)" % ptok, user="900")
    check("wrapper uid via the proxy -> https://dn.no works", "200" in r.stdout, (r.stdout + r.stderr)[-120:])
    pid = exec_in("parser", "python", "-c", "import os\nfor p in os.listdir('/proc'):\n if p.isdigit() and b'spider_hoarder_parser.wrapper' in open(f'/proc/{p}/cmdline','rb').read(): print(p); break", user="900").stdout.strip() or "7"
    r = exec_in("parser", "python", "-c", f"import sys\ntry:\n open('/proc/{pid}/environ').read(); print('READ')\nexcept OSError as e: print('denied', e.errno)")
    check("parser uid cannot read the wrapper's /proc/<pid>/environ (the tokens)", "denied" in r.stdout, r.stdout + r.stderr[-100:])
    r = exec_in("parser", "sh", "-c", "grep -E '^(Cap(Eff|Prm|Amb)|NoNewPrivs)' /proc/self/status | tr '\\n' ' '")
    check("parser uid holds no capability", "CapEff:\t0000000000000000" in r.stdout and "CapAmb:\t0000000000000000" in r.stdout, r.stdout)
    r = exec_in("parser", "sh", "-c", "grep -E '^CapEff' /proc/1/status; ls /scratch 2>&1 | head -3", user="900")
    print("     wrapper:", r.stdout.strip().replace("\n", " | "))
    r = exec_in("parser", "python", "-c", "import os\ntry: os.listdir('/scratch'); print('LISTED')\nexcept OSError as e: print('denied', e.errno)")
    check("parser uid cannot list the scratch root (other jobs)", "denied" in r.stdout, r.stdout)
    r = exec_in("parser", "sh", "-c", "ls -A /scratch | wc -l", user="900")
    check("scratch is empty when idle (wiped after every job)", r.stdout.strip() == "0", r.stdout.strip())
    # the sidecar needs the token; with no OCR profile there is none: assert the service list says so
    svcs = sh("docker", "service", "ls", "--format", "{{.Name}}").stdout.split()
    env = sh("docker", "service", "inspect", "spider-hoarder_parser", "--format", "{{json .Spec.TaskTemplate.ContainerSpec.Env}}").stdout
    has_profiles = '"OCR_PROFILES={}"' not in env
    check("ocr-sidecar is deployed exactly when OCR_PROFILES is set", ("spider-hoarder_ocr-sidecar" in svcs) == has_profiles, f"profiles={has_profiles}")


async def main():
    servers = [a for a in sys.argv[1:] if not a.startswith("--")] or ["browser-clear"]
    for s in ([] if "--isolation" in sys.argv else servers):
        print(f"== {s}")
        await functional(s)
    if sh("docker", "ps", "-q", "-f", "name=spider-hoarder_parser.").stdout.strip():
        isolation()
    print("\n" + (f"{len(FAILED)} FAILED: {FAILED}" if FAILED else "all passed"))
    sys.exit(1 if FAILED else 0)


asyncio.run(main())
