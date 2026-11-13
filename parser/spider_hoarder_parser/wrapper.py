"""The parser wrapper (docs/plans/parser-wrapper.md): the trusted half of the parser container. It authenticates the gateway, pulls a job's
`original/` tree from the blob store with the job's scoped credential (through the egress proxy), runs the parser as a pool uid that
can read nothing and authenticate to nothing, validates and uploads what the parser announces on its stdout pipe, and writes
`index.json` and `status.json`. The parser never sees a credential or a token other than (if the job asked for OCR) one profile's.

  POST /jobs/<proj>/<session>/<job>   {credentials, base_url?, features?}   -> 202 | 400 | 401 | 409 | 503
  GET  /jobs/<proj>/<session>/<job>   header X-Credentials (JSON)           -> {status, error?}

Runs as its own uid with CAP_SETUID/SETGID/CHOWN/KILL: pool uids are UID_BASE.. . Without CAP_SETUID (tests): one pseudo uid per slot, no setuid."""

import asyncio
import collections
import contextlib
import hmac
import json
import logging
import os
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

import fsspec
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger("wrapper")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FEATURES = {"ocr"}
TOP_DIRS = {"original", "parsed", "simplified", "terse", "metadata"}  # what a parser may write; never index.json / status.json
LINE_MAX = 4096  # PIPE_BUF: the parser writes each record with one write
HEARTBEAT, STALE = 10, 30  # seconds
# the operator settings the parser process enforces (limits, no secret): the only part of the wrapper's own environment a parser gets
PARSER_LIMIT_ENV = ("MAX_PARSE_BYTES", "MAX_ARCHIVE_BYTES", "MAX_ARCHIVE_MEMBERS", "METADATA_TIMEOUT", "METADATA_MAX_MEM", "METADATA_MAX_BYTES", "OCR_TIMEOUT")


class ProtocolError(Exception):
    """The parser sent something no honest parser sends: the job fails and the parser is killed."""


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


class Config:
    def __init__(self, token: str, blob_url: str, blob_options: dict | None = None, profiles: dict | None = None,
                 scratch: str = "/scratch", uid_base: int = 20000, pool_size: int = 4, replica: str = "wrapper",
                 job_timeout: int = 600, max_records: int = 500_000, max_bytes: int = 64 << 30, max_notes: int = 1000,
                 parser_cmd: list | None = None, rlimit_as: int = 2 << 30, rlimit_fsize: int = 4 << 30, heartbeat: float = HEARTBEAT):
        self.token, self.blob_url, self.blob_options, self.profiles = token, blob_url, blob_options or {}, profiles or {}
        self.scratch, self.uid_base, self.pool_size, self.replica = scratch, uid_base, pool_size, replica
        self.job_timeout, self.max_records, self.max_bytes, self.max_notes = job_timeout, max_records, max_bytes, max_notes
        self.parser_cmd = parser_cmd or [sys.executable, "-I", "-u", "-m", "spider_hoarder_parser.worker"]
        self.rlimit_as, self.rlimit_fsize, self.heartbeat = rlimit_as, rlimit_fsize, heartbeat

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ
        return cls(e["WRAPPER_TOKEN"], e["RESEARCH_BLOB_URL"], json.loads(e.get("PARSE_BLOB_OPTIONS") or "{}"),
                   json.loads(e.get("OCR_PROFILES") or "{}"), e.get("PARSE_SCRATCH", "/scratch"), env_int("PARSE_UID_BASE", 20000),
                   env_int("PARSE_POOL_SIZE", 4), e.get("REPLICA_ID") or e.get("HOSTNAME") or "wrapper", env_int("PARSE_JOB_TIMEOUT", 600),
                   rlimit_as=env_int("PARSE_RLIMIT_AS", 6 << 30))


# ---- the uid pool ---------------------------------------------------------------------------------------------

def _reclaim(path: str, uid: int) -> None:
    """Make a tree the wrapper's own again (the parser uid owned it): lchown, and owner-rwx on directories so rmtree can enter."""
    with contextlib.suppress(PermissionError):  # chown needs CAP_CHOWN; without it (tests) the tree is already ours
        os.lchown(path, os.geteuid(), os.getegid())
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        os.chmod(path, 0o700)
        for e in os.scandir(path):
            _reclaim(e.path, uid)


def owned_by(uid: int, roots) -> str | None:
    """First path under `roots` owned by `uid` (None if there is none): a pool uid is only freed when it owns nothing."""
    for root in roots:
        stack = [root]
        while stack:
            p = stack.pop()
            try:
                st = os.lstat(p)
                if st.st_uid == uid:
                    return p
                if stat.S_ISDIR(st.st_mode):
                    stack.extend(e.path for e in os.scandir(p))
            except OSError:
                continue
    return None


def purge_owned(uid: int, roots) -> None:
    """Delete what a pool uid left in shared places (/tmp ...): lchown it to ourselves first (CAP_CHOWN), then it is ours to remove."""
    for _ in range(1000):
        if not (p := owned_by(uid, roots)):
            return
        _reclaim(p, uid)
        shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)


def drop_ambient() -> None:
    """For a child's preexec_fn: the wrapper's capabilities are ambient, and an ambient capability survives setuid() and exec."""
    import ctypes
    ctypes.CDLL(None, use_errno=True).prctl(47, 4, 0, 0, 0)  # PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL


def kill_uid(uid: int) -> None:
    """Kill every process of `uid` (also those that left the process group): a helper running as the uid signals everything it may signal."""
    subprocess.run([sys.executable, "-I", "-c", "import os,signal; os.kill(-1, signal.SIGKILL)"], user=uid, group=uid, extra_groups=[], preexec_fn=drop_ambient,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def can_setuid() -> bool:
    """The wrapper runs as its own uid with CAP_SETUID (not as root): that, not euid 0, decides whether pool uids are real."""
    try:
        caps = next(int(l.split()[1], 16) for l in open("/proc/self/status") if l.startswith("CapEff"))
    except (OSError, StopIteration):
        return os.geteuid() == 0
    return bool(caps & (1 << 7))  # CAP_SETUID


class Pool:
    """Slots `free | busy | quarantined`; a slot is a uid (root) or a scratch dir run as the wrapper's own uid (tests)."""

    def __init__(self, cfg: Config):
        self.cfg, self.lock = cfg, threading.Lock()
        self.state = {i: "free" for i in range(cfg.pool_size)}
        self.real = can_setuid()
        os.makedirs(cfg.scratch, exist_ok=True)
        os.chmod(cfg.scratch, 0o711)

    def uid(self, slot: int) -> int:
        return self.cfg.uid_base + slot if self.real else os.geteuid()

    def dir(self, slot: int) -> str:
        return os.path.join(self.cfg.scratch, f"slot-{slot}")

    def acquire(self) -> int | None:
        with self.lock:
            for slot, s in self.state.items():
                if s == "free":
                    self.state[slot] = "busy"
                    return slot
        return None

    def release(self, slot: int, quarantine: bool = False) -> None:
        with self.lock:
            self.state[slot] = "quarantined" if quarantine else "free"

    def clean(self, slot: int, proc=None) -> bool:
        """Kill, wipe and verify; False = the slot must be quarantined."""
        d, uid = self.dir(slot), self.uid(slot)
        try:
            if proc is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            if self.real:
                kill_uid(uid)
            if os.path.lexists(d):
                _reclaim(d, uid)
                shutil.rmtree(d)
            roots = [self.cfg.scratch, "/tmp", "/var/tmp", "/dev/shm"]
            if self.real:
                purge_owned(uid, roots)
            left = owned_by(uid, roots) if self.real else None
            if left:
                log.error("uid %s still owns %s: quarantined", uid, left)
            return left is None
        except Exception:  # noqa: BLE001
            log.exception("wipe of slot %s failed: quarantined", slot)
            return False


# ---- one job --------------------------------------------------------------------------------------------------

def open_nofollow(root: str, rel: str) -> tuple[int, int, str]:
    """(fd, parent dir fd, name) of the regular file root/rel with no symlink followed in any component."""
    *dirs, name = rel.split("/")
    dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for d in dirs:
            nfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd), dfd, name  # NONBLOCK: a fifo must not hang us
    except BaseException:
        os.close(dfd)
        raise


def valid_rel(rel, first=TOP_DIRS) -> str:
    if (not isinstance(rel, str) or not rel or len(rel) > 1024 or "\0" in rel or "\\" in rel or rel.startswith("/")
            or os.path.normpath(rel) != rel or rel.split("/")[0] in ("", ".", "..") or rel.split("/")[0] not in first or "/" not in rel):
        raise ProtocolError(f"bad path {str(rel)[:80]!r}")
    return rel


def drain_stderr(proc, cap: int = 64 << 10) -> None:
    """The parser's stderr goes to the wrapper's log, capped, never to the client."""
    n = 0
    for line in proc.stderr:
        if n < cap:
            log.info("parser: %s", line.decode("utf-8", "replace").rstrip()[:500])
        n += len(line)


class Job:
    def __init__(self, w: "Wrapper", ref: tuple, slot: int, creds: dict, req: dict):
        self.w, self.ref, self.slot, self.creds, self.req = w, ref, slot, creds, req
        self.fs, root = w.fs(creds)
        self.prefix = f"{root.rstrip('/')}/projects/{ref[0]}/data/{ref[1]}/{ref[2]}"
        self.notes, self.marks, self.uploaded = [], set(), {}
        self.timed_out = False

    def write_json(self, name: str, obj) -> None:
        with self.fs.open(f"{self.prefix}/{name}", "wb") as f:
            f.write(json.dumps(obj).encode())

    def status(self, status: str, error: str | None = None) -> None:
        s = {"status": status, "owner_replica": self.w.cfg.replica, "updated": time.time(), **({"error": error} if error else {})}
        self.w.remember(self.ref, s)
        with contextlib.suppress(Exception):  # a heartbeat that cannot be written must not kill the parse
            self.write_json("status.json", s)

    # -- steps --
    def manifest(self) -> dict:
        """The existing index.json: the gateway wrote it when it stored the originals (read first, so that a failed pull keeps it)."""
        try:
            return json.loads(self.fs.cat_file(f"{self.prefix}/index.json"))
        except FileNotFoundError:
            return {"files": []}

    def pull(self, indir: str, manifest: dict) -> None:
        """original/ -> indir/original/."""
        base = f"{self.prefix}/original/"
        os.makedirs(os.path.join(indir, "original"))
        for p in self.fs.find(base.rstrip("/")):
            rel = p[len(base):] if p.startswith(base) else None
            if not rel or os.path.normpath(rel) != rel or rel.startswith(("/", "..")):
                continue
            dest = os.path.join(indir, "original", rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            self.fs.get_file(p, dest)
        ctypes = {e["path"][len("original/"):]: e.get("content_type") for e in manifest.get("files", []) if e["path"].startswith("original/")}
        with open(os.path.join(indir, "job.json"), "w") as f:
            json.dump({"base_url": self.req.get("base_url") or "", "ctypes": ctypes}, f)

    def env(self, work: str) -> dict:
        env = {"PYTHONUNBUFFERED": "1", "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": work, "TMPDIR": work,
               "LANG": "C.UTF-8", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
        env |= {k: os.environ[k] for k in PARSER_LIMIT_ENV if os.environ.get(k)}
        if profile := (self.req.get("features") or {}).get("ocr"):  # only the requested profile: a job never holds another's token
            env |= {"OCR_URL": self.w.cfg.profiles[profile]["url"], "OCR_TOKEN": self.w.cfg.profiles[profile]["token"]}
        return env

    def launch(self, jobdir: str, uid: int) -> subprocess.Popen:
        cfg = self.w.cfg

        def limits():
            # NPROC counts a uid's threads, and is per uid: meaningless when the parser shares the wrapper's uid (tests)
            for r, v in ((resource.RLIMIT_AS, cfg.rlimit_as), *([(resource.RLIMIT_NPROC, 1024)] if self.w.pool.real else []),
                         (resource.RLIMIT_NOFILE, 1024), (resource.RLIMIT_FSIZE, cfg.rlimit_fsize), (resource.RLIMIT_CORE, 0)):
                resource.setrlimit(r, (v, v))
            import ctypes
            ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS
            drop_ambient()  # the parser must hold no capability at all

        extra = {"user": uid, "group": uid, "extra_groups": []} if self.w.pool.real else {}
        return subprocess.Popen([*cfg.parser_cmd, jobdir], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self.env(os.path.join(jobdir, "work")), close_fds=True,
                                start_new_session=True, umask=0o007, preexec_fn=limits, **extra)

    def upload(self, out: str, rel: str, ctype: str) -> None:
        fd, dfd, name = open_nofollow(out, rel)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ProtocolError(f"{rel} is not a regular file")
            self.total += st.st_size  # the size comes from our own fstat, never from the record
            if self.total > self.w.cfg.max_bytes:
                raise ProtocolError("output too big")
            with os.fdopen(fd, "rb", closefd=False) as src, self.fs.open(f"{self.prefix}/{rel}", "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            os.unlink(name, dir_fd=dfd)
        finally:
            os.close(fd)
            os.close(dfd)
        self.uploaded[rel] = {"path": rel, "size": st.st_size, "content_type": ctype}

    def record(self, out: str, line: bytes) -> None:
        try:
            rec = json.loads(line)
        except ValueError:
            raise ProtocolError("record is not JSON") from None
        if not isinstance(rec, dict):
            raise ProtocolError("record is not an object")
        if "note" in rec:
            if len(self.notes) < self.w.cfg.max_notes and isinstance(rec["note"], str):
                self.notes.append(rec["note"][:1000])
        elif rec.get("text") is True:
            self.marks.add(valid_rel(rec.get("path"), {"original"}))
        else:
            ctype = rec.get("content_type")
            self.upload(out, valid_rel(rec.get("path")), ctype[:200] if isinstance(ctype, str) and ctype else "application/octet-stream")

    def finish(self, manifest: dict, error: str | None) -> None:
        """index.json first (artefacts already uploaded stay, a failure is a note), then status.json."""
        files = {e["path"]: e for e in manifest.get("files", [])}
        files.update(self.uploaded)
        for p in self.marks:
            if p in files:
                files[p] = {**files[p], "text": True}
        notes = self.notes + ([f"not parsed ({error})"] if error else [])
        manifest = {**manifest, "files": list(files.values())}
        manifest.pop("notes", None)
        if notes:
            manifest["notes"] = notes
        self.write_json("index.json", manifest)
        self.status("failed" if error else "done", error)

    def run(self) -> None:
        w, error, manifest, proc = self.w, None, {"files": []}, None
        jobdir, uid = w.pool.dir(self.slot), w.pool.uid(self.slot)
        stop, beat, timer = threading.Event(), None, None
        self.total = 0
        try:
            def heartbeat():
                while not stop.wait(w.cfg.heartbeat):
                    self.status("running")
            beat = threading.Thread(target=heartbeat, daemon=True)
            beat.start()
            w.pool.clean(self.slot)
            os.mkdir(jobdir, 0o700)
            for d in ("in", "out", "work"):
                os.mkdir(os.path.join(jobdir, d), 0o700)
            manifest = self.manifest()
            self.pull(os.path.join(jobdir, "in"), manifest)
            if w.pool.real:
                # list everything first and chown the job dir last: once a directory is the uid's, the wrapper cannot reach what is in it
                paths = [jobdir] + [os.path.join(d, x) for d, dirs, names in os.walk(jobdir) for x in dirs + names]
                out = os.path.join(jobdir, "out")
                # the wrapper has no DAC_OVERRIDE: it reaches the artefacts through its group (setgid out/, parser umask 007), nothing else of the uid's
                os.chmod(jobdir, 0o710)  # before the chown: afterwards the wrapper is not the owner
                os.chmod(out, 0o2770)
                for n in reversed(paths):
                    os.lchown(n, uid, os.getegid() if n in (jobdir, out) else uid)
            proc = self.launch(jobdir, uid)
            threading.Thread(target=drain_stderr, args=(proc,), daemon=True).start()

            def reap():  # whatever the parser left running (a setsid'ed child holding the pipe) must not outlive it: EOF would never come
                proc.wait()
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                if w.pool.real:
                    kill_uid(uid)
            threading.Thread(target=reap, daemon=True).start()

            def expire():
                self.timed_out = True
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
            timer = threading.Timer(w.cfg.job_timeout, expire)
            timer.start()
            out, count, clean_eof = os.path.join(jobdir, "out"), 0, True
            while line := proc.stdout.readline(LINE_MAX + 1):
                if len(line) > LINE_MAX or not line.endswith(b"\n"):
                    clean_eof = False  # a too-long line, or a record cut off by a kill
                    if len(line) > LINE_MAX:
                        raise ProtocolError("record too long")
                    break
                count += 1
                if count > w.cfg.max_records:
                    raise ProtocolError("too many records")
                self.record(out, line)
            rc = proc.wait()
            if self.timed_out:
                error = f"parser timed out after {w.cfg.job_timeout} s"
            elif rc != 0:
                error = f"parser exited with {rc}"
            elif not clean_eof:
                error = "parser output truncated"
        except ProtocolError as e:
            error = f"parser protocol violation: {e}"
        except Exception as e:  # noqa: BLE001  a full disk or a pull error fails this job only
            log.exception("job %s failed", self.ref)
            error = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            if timer:
                timer.cancel()
            stop.set()
            if beat:
                beat.join()
            ok = w.pool.clean(self.slot, proc)  # after the process group is dead, before the uid is freed again
            w.pool.release(self.slot, quarantine=not ok)  # before the result: a client that re-POSTs the moment it sees `done` must find the slot free
            try:
                self.finish(manifest, error)
            except Exception:  # noqa: BLE001
                log.exception("could not write the result of %s", self.ref)


class Wrapper:
    def __init__(self, cfg: Config):
        self.cfg, self.pool = cfg, Pool(cfg)
        self.memory: collections.OrderedDict = collections.OrderedDict()

    def remember(self, ref, state: dict) -> None:
        self.memory[ref] = state
        self.memory.move_to_end(ref)
        while len(self.memory) > 1000:
            self.memory.popitem(last=False)

    def fs(self, creds: dict):
        creds = dict(creds)
        if "gcs_access_token" in creds:  # GCS: a downscoped access token travels as a string
            from google.oauth2.credentials import Credentials
            creds["token"] = Credentials(creds.pop("gcs_access_token"))
        opts = {**self.cfg.blob_options, **creds, "skip_instance_cache": True}
        if self.cfg.blob_url.startswith(("gs://", "gcs://")):
            opts["session_kwargs"] = {"trust_env": True}  # gcsfs (aiohttp) ignores HTTP(S)_PROXY, the only way out of this container, unless told to look
        return fsspec.core.url_to_fs(self.cfg.blob_url, use_listings_cache=False, **opts)

    def read_status(self, ref, creds: dict) -> dict:
        s = self.memory.get(ref)
        if s is None:
            fs, root = self.fs(creds)
            try:
                s = json.loads(fs.cat_file(f"{root.rstrip('/')}/projects/{ref[0]}/data/{ref[1]}/{ref[2]}/status.json"))
            except FileNotFoundError:
                return {"status": "not_started"}
        if s["status"] == "running" and time.time() - s["updated"] > STALE:
            return {"status": "failed", "error": "wrapper lost"}
        return {k: v for k, v in s.items() if k in ("status", "error")}


def make_app(cfg: Config) -> Starlette:
    w = Wrapper(cfg)

    def denied(request: Request):
        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth.encode(), f"Bearer {cfg.token}".encode()):
            return JSONResponse({"error": "unauthorized"}, 401)

    def ref_of(request: Request):
        ref = tuple(request.path_params[k] for k in ("proj", "session", "job"))
        return ref if all(UUID_RE.match(p) for p in ref) else None

    async def post(request: Request):
        if r := denied(request):
            return r
        if not (ref := ref_of(request)):
            return JSONResponse({"error": "invalid job reference"}, 400)
        try:
            body = await request.json()
            creds, features = body["credentials"], body.get("features") or {}
            assert isinstance(creds, dict) and isinstance(features, dict) and isinstance(body.get("base_url", ""), str)
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "bad request body"}, 400)
        if set(features) - FEATURES:
            return JSONResponse({"error": f"unknown feature {sorted(set(features) - FEATURES)[0]!r}"}, 400)
        if "ocr" in features and features["ocr"] not in cfg.profiles:
            return JSONResponse({"error": "unknown OCR profile"}, 400)
        try:
            cur = await asyncio.to_thread(w.read_status, ref, creds)  # a running job with a stale heartbeat reads as failed: restarted
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"storage credential rejected ({type(e).__name__})"}, 400)
        if cur["status"] == "running":
            return JSONResponse({"error": "job is running"}, 409)
        if (slot := w.pool.acquire()) is None:
            return JSONResponse({"error": "no free parser"}, 503)
        try:
            job = Job(w, ref, slot, creds, body)
            await asyncio.to_thread(job.status, "running")
        except BaseException:
            w.pool.release(slot)
            raise
        threading.Thread(target=job.run, daemon=True).start()
        return JSONResponse({"status": "running"}, 202)

    async def get(request: Request):
        if r := denied(request):
            return r
        if not (ref := ref_of(request)):
            return JSONResponse({"error": "invalid job reference"}, 400)
        try:
            creds = json.loads(request.headers.get("x-credentials") or "{}")
            return JSONResponse(await asyncio.to_thread(w.read_status, ref, creds))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"cannot read status ({type(e).__name__})"}, 502)

    app = Starlette(routes=[Route("/jobs/{proj}/{session}/{job}", post, methods=["POST"]),
                            Route("/jobs/{proj}/{session}/{job}", get, methods=["GET"]),
                            Route("/health", lambda r: JSONResponse({"ok": True}))])
    app.state.wrapper = w
    return app


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    if token := os.environ.get("PROXY_TOKEN"):  # only the wrapper's own storage traffic uses the proxy; the parser env is built by hand
        url = f"http://parse:{token}@{os.environ['PARSE_PROXY_HOST']}"
        os.environ.update(HTTP_PROXY=url, HTTPS_PROXY=url, http_proxy=url, https_proxy=url)
    uvicorn.run(make_app(Config.from_env()), host="0.0.0.0", port=env_int("PARSE_PORT", 8080))


if __name__ == "__main__":
    main()
