"""The wrapper against a real object store (scripts/test.sh starts a SeaweedFS), with scoped credentials from the gateway's creds.py, the
gateway's parse_client and, for the hostile-parser cases, scripts standing in for the parser. Not root here: slots run as the test's own uid."""

import io
import json
import os
import sys
import tempfile
import time
import zipfile

import httpx
import pytest

from spider_hoarder_sdk import testing

testing.gateway_env()

from spider_hoarder_parser import wrapper
from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway import creds, jobs, parse_client

TOKEN = testing.PARSE_TOKEN
PROFILES = {"remote": {"url": "http://ocr-remote", "token": "tok-remote"}, "lan": {"url": "http://ocr-lan", "token": "tok-lan"}}


class Service(testing.ParserService):
    def __init__(self, **cfg):
        super().__init__(PROFILES, **cfg)

    def job(self, files: dict):
        """(ref, key, scoped credential) of a new job whose originals are stored."""
        job_id, key = testing.new_job()
        for name, data in files.items():
            storage.write_bytes(key, f"original/{name}", data, "application/octet-stream")
        proj, session, job = job_id.split("/")
        return job_id, key, creds.mint(proj, session, job)

    def post(self, ref, cred, token=TOKEN, **body):
        return httpx.post(f"{self.url}/jobs/{ref}", json={"credentials": cred.options, **body}, headers={"Authorization": f"Bearer {token}"})

    def get(self, ref, cred):
        return httpx.get(f"{self.url}/jobs/{ref}", headers={"Authorization": f"Bearer {TOKEN}", "X-Credentials": json.dumps(cred.options)}).json()

    def wait(self, ref, cred, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            s = self.get(ref, cred)
            if s["status"] in ("done", "failed"):
                return s
            time.sleep(0.1)
        raise AssertionError("timeout")


@pytest.fixture
def service():
    made = []

    def make(**cfg):
        made.append(Service(**cfg))
        return made[-1]

    yield make
    for s in made:
        s.stop()


def script(code: str) -> list:
    """A stand-in parser: `code` runs with fd 1 = the protocol pipe, jobdir = sys.argv[1]."""
    return [sys.executable, "-I", "-u", "-c", "import os, sys, json, time\njobdir = sys.argv[1]\n" + code]


NO_METADATA = testing.NO_METADATA


def test_end_to_end_through_the_gateway_client(service, monkeypatch):
    s = service(parser_cmd=NO_METADATA)
    monkeypatch.setenv("PARSE_URL", s.url)
    monkeypatch.setenv("PARSE_TOKEN", TOKEN)
    monkeypatch.setattr(jobs, "INLINE_LIMIT", 50_000)
    job_id, key = testing.new_job()
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("a/n.md", "# H\n[l](u)")
        z.writestr("in.zip", b"PK")
    page = b"<html><body><article><h1>Title</h1><p>" + b"Some long article text here. " * 20 + b"</p><h2>Part</h2></article></body></html>"
    jobs._store_original(key, "page.html", page, "text/html")
    jobs._store_original(key, "b.zip", zb.getvalue(), "application/zip")
    storage.set_meta(key, url="http://u", target="main")
    r = jobs._parse_job(key, "http://h.test/", "page.html")
    paths = {f["path"] for f in r["files"]}
    assert {"original/page.html", "parsed/page.html.md", "simplified/page.html.md", "terse/page.html.md", "original/b.zip",
            "original/b.zip.d/a/n.md", "terse/b.zip.d/a/n.md", "original/b.zip.d/in.zip"} <= paths, (paths, r.get("notes"))
    assert "notes" not in r and r["text_path"] == "parsed/page.html.md" and "Title" in r["text"]
    assert not any(p.endswith(("status.json", "index.json")) for p in paths)
    assert {f["path"]: f for f in r["files"]}["original/b.zip.d/a/n.md"]["text"] is True
    m = storage.get_manifest(key)
    assert m["url"] == "http://u" and m["target"] == "main"  # the gateway's own manifest entries survive
    assert json.loads(storage.read_bytes(key, "status.json"))["status"] == "done"
    assert storage.read_bytes(key, "terse/page.html.md") == b"# Title\n## Part"
    monkeypatch.setattr(jobs, "INLINE_LIMIT", 16)  # parsed too big for inline, terse fits
    assert jobs._job_result(key, "page.html")["text_path"] == "terse/page.html.md"
    # a second parse of the same, done job is allowed (an upload link adds files over time) and gives the same artefacts
    jobs._store_original(key, "second.md", b"# Two", "text/markdown")
    r2 = jobs._parse_job(key, "http://h.test/", "second.md")
    assert paths <= {f["path"] for f in r2["files"]} and r2.get("text_path") == "original/second.md", r2
    assert not any(p.startswith("original/b.zip.d/in.zip.d") for p in {f["path"] for f in r2["files"]})


def test_gateway_client_reports_problems_as_notes(monkeypatch):
    testing.fresh_store()
    key = testing.new_job()[1]
    jobs._store_original(key, "x.md", b"# x", "text/markdown")
    monkeypatch.delenv("PARSE_URL", raising=False)
    assert "no parser service configured" in jobs._parse_job(key, "", "x.md")["notes"][0]
    monkeypatch.setenv("PARSE_URL", "http://127.0.0.1:9")
    r = jobs._parse_job(key, "", "x.md")
    assert "parser unavailable" in r["notes"][0] and any(f["path"] == "original/x.md" for f in r["files"])  # the original is never lost


def test_auth_and_validation(service):
    s = service()
    ref, key, cred = s.job({"a.md": b"# a"})
    assert s.post(ref, cred, token="wrong").status_code == 401
    assert httpx.get(f"{s.url}/jobs/{ref}").status_code == 401
    assert s.post("../../x/y", cred).status_code in (400, 404)
    assert s.post(ref.replace("-", "", 1), cred).status_code in (400, 404)
    assert s.post(ref, cred, features={"nope": 1}).status_code == 400
    assert s.post(ref, cred, features={"ocr": "unknown"}).status_code == 400
    assert s.get(ref, cred) == {"status": "not_started"}
    assert httpx.post(f"{s.url}/jobs/{ref}", content=b"{", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 400


def test_credential_scope_limits_what_the_wrapper_can_write(service):
    s = service(parser_cmd=script("open(jobdir + '/out/parsed/x', 'wb') if False else None"))
    ref, key, cred = s.job({"a.md": b"# a"})
    other = testing.new_job()[1]
    fs, root = s.wrapper.fs(cred.options)
    with pytest.raises(Exception):  # the job's credential cannot write into another job
        fs.pipe_file(f"{root}/{other}/parsed/x", b"x")
    with pytest.raises(Exception):
        fs.pipe_file(f"{root}/projects/x/access.json", b"{}")
    fs.pipe_file(f"{root}/{key}/parsed/ok", b"ok")


def hostile(code: str):
    return script("os.makedirs(jobdir + '/out/parsed', exist_ok=True)\n" + code)


CASES = {
    "bad json": ("os.write(1, b'not json\\n')", "not JSON"),
    "not an object": ("os.write(1, b'[1]\\n')", "not an object"),
    "long record": ("os.write(1, b'{\"note\": \"' + b'x' * 5000 + b'\"}\\n')", "too long"),
    "dotdot": ("os.write(1, b'{\"path\": \"parsed/../../x\", \"content_type\": \"a/b\"}\\n')", "bad path"),
    "absolute": ("os.write(1, b'{\"path\": \"/etc/passwd\", \"content_type\": \"a/b\"}\\n')", "bad path"),
    "reserved top dir": ("os.write(1, b'{\"path\": \"index.json\", \"content_type\": \"a/b\"}\\n')", "bad path"),
    "top-level file": ("os.write(1, b'{\"path\": \"parsed\", \"content_type\": \"a/b\"}\\n')", "bad path"),
    "no path": ("os.write(1, b'{\"content_type\": \"a/b\"}\\n')", "bad path"),
    "text mark outside original": ("os.write(1, b'{\"path\": \"parsed/x\", \"text\": true}\\n')", "bad path"),
    "symlink": ("os.symlink('/etc/passwd', jobdir + '/out/parsed/l'); os.write(1, b'{\"path\": \"parsed/l\", \"content_type\": \"a/b\"}\\n')", None),
    "symlinked dir": ("os.symlink('/etc', jobdir + '/out/parsed/d'); os.write(1, b'{\"path\": \"parsed/d/passwd\", \"content_type\": \"a/b\"}\\n')", None),
    "directory": ("os.mkdir(jobdir + '/out/parsed/dir'); os.write(1, b'{\"path\": \"parsed/dir\", \"content_type\": \"a/b\"}\\n')", "not a regular"),
    "fifo": ("os.mkfifo(jobdir + '/out/parsed/f'); os.write(1, b'{\"path\": \"parsed/f\", \"content_type\": \"a/b\"}\\n')", "not a regular"),
    "missing file": ("os.write(1, b'{\"path\": \"parsed/none\", \"content_type\": \"a/b\"}\\n')", None),
    "nonzero exit": ("sys.exit(3)", "exited with 3"),
    "killed": ("os.kill(os.getpid(), 9)", "exited with -9"),
    "truncated record": ("os.write(1, b'{\"path\": \"parsed/x\", \"cont')", "truncated"),
}


@pytest.mark.parametrize("name", CASES)
def test_hostile_parser(service, name):
    code, want = CASES[name]
    s = service(parser_cmd=hostile(code))
    ref, key, cred = s.job({"a.md": b"# a"})
    assert s.post(ref, cred).status_code == 202
    st = s.wait(ref, cred)
    assert st["status"] == "failed", st
    if want:
        assert want in st["error"], st
    m = storage.get_manifest(key)
    assert [f["path"] for f in m["files"]] == ["original/a.md"]  # nothing hostile reached the store, the original is still listed
    assert any("not parsed" in n for n in m["notes"])  # index.json says why
    assert not any("passwd" in p for p in s.wrapper.fs(cred.options)[0].find(s.wrapper.fs(cred.options)[1] + "/" + key))
    deadline = time.time() + 5
    while "busy" in s.wrapper.pool.state.values() and time.time() < deadline:
        time.sleep(0.05)
    assert set(s.wrapper.pool.state.values()) == {"free"} and not os.listdir(s.cfg.scratch)  # slot wiped and freed


def test_size_comes_from_fstat_and_artefacts_are_deleted_after_upload(service):
    s = service(parser_cmd=hostile(
        "open(jobdir + '/out/parsed/a.md', 'wb').write(b'hello')\n"
        "os.write(1, b'{\"path\": \"parsed/a.md\", \"content_type\": \"text/markdown\", \"size\": 999999}\\n')\n"
        "time.sleep(0.5)\nprint(os.path.exists(jobdir + '/out/parsed/a.md'), file=sys.stderr)\n"
        "os.write(1, b'{\"path\": \"original/a.md\", \"text\": true}\\n{\"path\": \"original/ghost\", \"text\": true}\\n{\"note\": \"hi\"}\\n')"))
    ref, key, cred = s.job({"a.md": b"# a"})
    s.post(ref, cred)
    assert s.wait(ref, cred)["status"] == "done"
    files = {f["path"]: f for f in storage.get_manifest(key)["files"]}
    assert files["parsed/a.md"]["size"] == 5 and files["parsed/a.md"]["content_type"] == "text/markdown"
    assert files["original/a.md"]["text"] is True and "original/ghost" not in files and storage.get_manifest(key)["notes"] == ["hi"]
    assert storage.read_bytes(key, "parsed/a.md") == b"hello"


def test_caps_and_timeout(service):
    s = service(parser_cmd=hostile("for i in range(10): os.write(1, b'{\"note\": \"n\"}\\n')"), max_records=3)
    ref, key, cred = s.job({"a.md": b"# a"})
    s.post(ref, cred)
    assert "too many records" in s.wait(ref, cred)["error"]
    s = service(parser_cmd=hostile("open(jobdir + '/out/parsed/a', 'wb').write(b'x' * 100)\nos.write(1, b'{\"path\": \"parsed/a\", \"content_type\": \"a/b\"}\\n')"),
                max_bytes=10)
    ref, key, cred = s.job({"a.md": b"# a"})
    s.post(ref, cred)
    assert "output too big" in s.wait(ref, cred)["error"]
    s = service(parser_cmd=hostile("time.sleep(60)"), job_timeout=1)
    ref, key, cred = s.job({"a.md": b"# a"})
    s.post(ref, cred)
    assert "timed out" in s.wait(ref, cred)["error"]


def test_parser_environment_has_no_secrets_and_only_the_requested_profile(service, monkeypatch):
    monkeypatch.setenv("WRAPPER_TOKEN", "leak-me")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
    s = service(parser_cmd=hostile("os.write(1, json.dumps({'note': json.dumps(dict(os.environ))}).encode() + b'\\n')"))
    ref, key, cred = s.job({"a.md": b"# a"})
    s.post(ref, cred, features={"ocr": "lan"})
    assert s.wait(ref, cred)["status"] == "done"
    env = json.loads(storage.get_manifest(key)["notes"][0])
    assert env["OCR_URL"] == "http://ocr-lan" and env["OCR_TOKEN"] == "tok-lan" and "tok-remote" not in json.dumps(env)
    assert "leak-me" not in json.dumps(env) and "KEY" not in "".join(env) and env["PYTHONUNBUFFERED"] == "1"
    ref, key, cred = s.job({"a.md": b"# a"})
    assert s.post(ref, cred).status_code == 202  # no feature: no OCR variables at all
    s.wait(ref, cred)
    assert not [k for k in json.loads(storage.get_manifest(key)["notes"][0]) if k.startswith("OCR")]
    # the operator's limits reach the parser (the only part of the wrapper's own environment that does), a lookalike secret does not
    monkeypatch.setenv("MAX_PARSE_BYTES", "12345")
    monkeypatch.setenv("METADATA_TIMEOUT", "7")
    monkeypatch.setenv("MAX_ARCHIVE_BYTESX", "leak-me")
    ref, key, cred = s.job({"a.md": b"# a"})
    assert s.post(ref, cred).status_code == 202  # the slot of the job just seen done is already free
    s.wait(ref, cred)
    env = json.loads(storage.get_manifest(key)["notes"][0])
    assert env["MAX_PARSE_BYTES"] == "12345" and env["METADATA_TIMEOUT"] == "7" and "MAX_ARCHIVE_BYTESX" not in env


def test_duplicate_post_busy_pool_and_stale_heartbeat(service):
    s = service(parser_cmd=hostile("time.sleep(2)"), pool_size=1, heartbeat=0.2)
    ref, key, cred = s.job({"a.md": b"# a"})
    assert s.post(ref, cred).status_code == 202
    assert s.get(ref, cred)["status"] == "running"
    assert s.post(ref, cred).status_code == 409  # same job, running with a fresh heartbeat
    ref2, key2, cred2 = s.job({"a.md": b"# a"})
    assert s.post(ref2, cred2).status_code == 503  # the only uid is busy
    st = json.loads(storage.read_bytes(key, "status.json"))
    assert st["status"] == "running" and st["owner_replica"] == "wrapper"
    assert s.wait(ref, cred)["status"] == "done"
    assert s.post(ref2, cred2).status_code == 202  # a freed slot takes the next job
    s.wait(ref2, cred2)
    # another replica (no memory) reads status.json from the store; an old heartbeat means the owner is lost
    ref3, key3, cred3 = s.job({})
    fs, root = s.wrapper.fs(cred3.options)
    fs.pipe_file(f"{root}/{key3}/status.json", json.dumps({"status": "running", "owner_replica": "x", "updated": time.time() - 60}).encode())
    assert s.get(ref3, cred3) == {"status": "failed", "error": "wrapper lost"}
    assert s.post(ref3, cred3).status_code == 202  # ... and the job is restarted
    s.wait(ref3, cred3)
    fs.pipe_file(f"{root}/{key3}/status.json", json.dumps({"status": "running", "owner_replica": "x", "updated": time.time()}).encode())
    s.wrapper.memory.clear()
    assert s.get(ref3, cred3)["status"] == "running"
    assert s.post(ref3, cred3).status_code == 409


def test_pull_failure_fails_only_that_job(service, monkeypatch):
    s = service()
    ref, key, cred = s.job({"a.md": b"# a"})
    monkeypatch.setattr(wrapper.Job, "pull", lambda self, indir, manifest: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    s.post(ref, cred)
    st = s.wait(ref, cred)
    assert st["status"] == "failed" and "No space" in st["error"]
    assert [f["path"] for f in storage.get_manifest(key)["files"]] == ["original/a.md"] and not os.listdir(s.cfg.scratch)


@pytest.mark.skipif(not os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "..", "ocr-sidecar")), reason="ocr-sidecar/ not next to parser/")
def test_ocr_client_talks_to_the_sidecar(monkeypatch):
    """parser/ocr.py against the real sidecar app (upstream faked), with the variables the wrapper would hand a parser."""
    import httpx
    import threading

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "ocr-sidecar"))
    import app as sidecar
    import socket
    import uvicorn
    from spider_hoarder_parser import ocr

    prof = {"p": {"token": "tk", "base_url": "http://up/v1", "model": "m"}}
    up = httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "OCR text"}}]}))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(sidecar.make_app(prof, transport=up), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    try:
        monkeypatch.delenv("OCR_URL", raising=False)
        assert not ocr.enabled()
        monkeypatch.setenv("OCR_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("OCR_TOKEN", "tk")
        assert ocr.enabled() and ocr.remote_ocr(b"\x89PNG") == "OCR text"
        monkeypatch.setenv("OCR_TOKEN", "other")
        with pytest.raises(Exception):
            ocr.remote_ocr(b"\x89PNG")
    finally:
        srv.should_exit = True


root_only = pytest.mark.skipif(not os.environ.get("PARSE_CAPS_TEST"), reason="run in the parser container as the wrapper uid with its capabilities")


@root_only
def test_parser_uid_holds_nothing_and_cannot_read_the_wrapper(service):
    """With real pool uids (the container run, as uid 900 with CAP_SETUID/SETGID/CHOWN/KILL): who the parser is and what it can touch."""
    import shutil
    s = service(parser_cmd=hostile(
        "ppid = os.getppid()\n"
        "def can(f):\n"
        "    try:\n        f(); return True\n    except OSError:\n        return False\n"
        "st = open('/proc/self/status').read()\n"
        "info = {'uid': os.getuid(), 'euid': os.geteuid(), 'gid': os.getgid(), 'groups': os.getgroups(),\n"
        "        'capeff': [l for l in st.splitlines() if l.startswith('CapEff')][0].split()[1], 'nnp': [l for l in st.splitlines() if l.startswith('NoNewPrivs')][0].split()[1],\n"
        "        'environ': can(lambda: open(f'/proc/{ppid}/environ').read()), 'setuid': can(lambda: os.setuid(0)),\n"
        "        'other_slot': can(lambda: os.listdir(os.path.dirname(jobdir) + '/slot-1')),\n"
        "        'kill_wrapper': can(lambda: os.kill(ppid, 0)) and can(lambda: os.kill(ppid, 15)),\n"
        "        'write_etc': can(lambda: open('/etc/x', 'w')), 'write_venv': can(lambda: open('/venv/x', 'w')),\n"
        "        'nofile': __import__('resource').getrlimit(7)[0], 'nproc': __import__('resource').getrlimit(6)[0], 'as': __import__('resource').getrlimit(9)[0]}\n"
        "os.write(1, json.dumps({'note': json.dumps(info)}).encode() + b'\\n')"), pool_size=2)
    ref, key, cred = s.job({"a.md": b"# a"})
    s.post(ref, cred)
    assert s.wait(ref, cred)["status"] == "done"
    info = json.loads(storage.get_manifest(key)["notes"][0])
    assert info["uid"] == info["euid"] == 20000 and info["gid"] == 20000 and info["groups"] == []
    assert info["capeff"] == "0000000000000000" and info["nnp"] == "1" and not info["setuid"]
    assert not info["environ"] and not info["other_slot"] and not info["write_etc"] and not info["write_venv"]  # the wrapper's environ (tokens), files and the image are out of reach
    assert info["nofile"] == 1024 and info["nproc"] == 1024 and info["as"] == 2 << 30
    assert os.listdir(s.cfg.scratch) == [] and s.wrapper.pool.state == {0: "free", 1: "free"}  # wiped and freed


@root_only
def test_a_parser_that_leaves_files_and_processes_is_cleaned_up(service):
    s = service(parser_cmd=hostile(
        "open('/tmp/leftover', 'w').write('x')\nos.mkdir('/tmp/leftdir'); os.chmod('/tmp/leftdir', 0)\n"
        "if os.fork() == 0:\n    os.setsid(); time.sleep(120)\n"
        "os.write(1, b'{\"note\": \"ok\"}\\n')"), pool_size=1)
    ref, key, cred = s.job({"a.md": b"# a"})
    s.post(ref, cred)
    assert s.wait(ref, cred)["status"] == "done"
    deadline = time.time() + 5
    while s.wrapper.pool.state[0] == "busy" and time.time() < deadline:
        time.sleep(0.05)
    assert s.wrapper.pool.state == {0: "free"} and not os.path.exists("/tmp/leftover") and not os.path.exists("/tmp/leftdir")
    assert wrapper.owned_by(20000, ["/tmp", s.cfg.scratch]) is None
    def alive(p):  # a zombie is dead (pytest is pid 1 in the container run and does not reap)
        try:
            return os.stat(f"/proc/{p}").st_uid == 20000 and open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False
    assert not [p for p in os.listdir("/proc") if p.isdigit() and alive(p)]  # the setsid'ed child is dead too


@pytest.mark.skipif(not __import__("shutil").which("ffprobe"), reason="ffprobe not installed (the parser image has it)")
def test_hostile_media_and_raster_files_open_no_connection(service):
    """An .m3u8 that makes ffprobe fetch URLs and a .vrt pointing at /vsicurl/: the real worker, the real extractors, nothing connects."""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    s = service(parser_cmd=None)
    ref, key, cred = s.job({
        "a.m3u8": f"#EXTM3U\n#EXTINF:1,\nhttp://127.0.0.1:{port}/seg.ts\n".encode(),
        "b.vrt": f'<VRTDataset rasterXSize="1" rasterYSize="1"><VRTRasterBand dataType="Byte" band="1"><SimpleSource><SourceFilename relativeToVRT="0">/vsicurl/http://127.0.0.1:{port}/x.tif</SourceFilename></SimpleSource></VRTRasterBand></VRTDataset>'.encode(),
        "c.mp4": b"\0\0\0\x18ftypmp42\0\0\0\0mp42isom"})
    s.post(ref, cred)
    assert s.wait(ref, cred)["status"] == "done"
    try:
        conn, _ = srv.accept()
        conn.close()
        raise AssertionError("a parser opened a connection to a URL from an untrusted file")
    except socket.timeout:
        pass
