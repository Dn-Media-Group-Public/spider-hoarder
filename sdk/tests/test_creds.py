"""Per-job store credentials, this project's policy (project-wide read, job-scoped write) on the cadmus-parser minting helper: the grants, and against
the real test store (SeaweedFS, scripts/test.sh) what a minted credential can read, write and list, that releasing / revoking / sweeping ends it,
and that a failure never leaves a role or a fallback behind."""

import json
import urllib.request
import uuid

import pytest

from spider_hoarder_sdk import testing

testing.gateway_env()

from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway import creds

P, Q, S, J, J2 = (str(uuid.uuid4()) for _ in range(5))


def denied(fn):
    try:
        fn()
    except PermissionError:
        return True
    except OSError as e:  # s3fs maps most AccessDenied to PermissionError; a few surface as the raw ClientError text
        return "AccessDenied" in str(e) or "Access Denied" in str(e)
    return False


def test_grants_and_urls():
    key = storage.job_key(P, S, J)
    root = storage.BLOB_URL.rstrip("/")
    assert creds.urls(key) == (f"{root}/projects/{P}/data", f"{root}/{key}/original", f"{root}/{key}")
    assert creds.grants(key) == [(f"{root}/projects/{P}/data", "read"), (f"{root}/{key}", "write")]  # project-wide read, the job's own directory for write


@pytest.fixture
def store():
    creds.active.clear()
    testing.fresh_store()  # s3://bucket/<uuid>: also proves a key prefix inside the bucket is honoured
    return storage


def reader(h, key):
    """(fs, path-of-key) with the job credential: the read side for project data, the write side for the job directory."""
    fs, root = storage._abs("", h[creds.urls(key)[2]])
    return fs, lambda k: f"{root.rstrip('/')}/{k}"


def test_matrix(store):
    store.write_object(f"projects/{P}/access.json", b"{}")
    store.write_object(f"projects/{P}/data/other/job/seen.txt", b"seen")
    store.write_object(f"projects/{Q}/data/s/j/foreign.txt", b"x")
    store.write_object(f"users/a@x.y/{P}", b"admin")
    key = storage.job_key(P, S, J)
    c = creds.mint(key)
    try:
        fs, p = reader(c, key)
        base = f"projects/{P}"
        # read the project's data side, nothing else
        assert fs.cat_file(p(f"{base}/data/other/job/seen.txt")) == b"seen"
        for k in (f"{base}/access.json", f"projects/{Q}/data/s/j/foreign.txt", f"users/a@x.y/{P}"):
            assert denied(lambda: fs.cat_file(p(k))), k
        # write and delete only inside the job, multipart included
        fs.pipe_file(p(f"{base}/data/{S}/{J}/out.txt"), b"hi")
        with fs.open(p(f"{base}/data/{S}/{J}/big.bin"), "wb") as f:
            f.write(b"x" * (12 << 20))
        assert fs.size(p(f"{base}/data/{S}/{J}/big.bin")) == 12 << 20
        for k in (f"{base}/data/{S}/{J2}/n.txt", f"{base}/data/other/job/n.txt", f"{base}/access.json", f"{base}/n.txt",
                  f"projects/{Q}/data/{S}/{J}/n.txt", f"users/a@x.y/{P}", "evil.txt"):
            assert denied(lambda: fs.pipe_file(p(k), b"no")), k
        assert denied(lambda: fs.rm_file(p(f"{base}/data/other/job/seen.txt")))
        fs.rm_file(p(f"{base}/data/{S}/{J}/out.txt"))
        # listing: the data side yes; the project root, the bare uuid, a sibling project, users/ and the bucket root no
        assert fs.ls(p(f"{base}/data/other/job"), detail=False) and fs.ls(p(f"{base}/data"), detail=False)
        for k in (base, "projects", f"projects/{Q}", f"projects/{Q}/data", "users", ""):
            assert denied(lambda: fs.ls(p(k), detail=False)), k
    finally:
        creds.release(c)
    # the gateway's own credential is unaffected, and presigned URLs still work
    assert store.read_object(f"projects/{P}/access.json") == b"{}"
    import boto3
    o = storage.BLOB_OPTIONS
    bucket = storage.BLOB_URL.split("/")[2]
    url = boto3.client("s3", endpoint_url=o["endpoint_url"], region_name="us-east-1", aws_access_key_id=o["key"], aws_secret_access_key=o["secret"]
                       ).generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": f"{storage.BLOB_URL.split('/', 3)[3]}/projects/{P}/data/other/job/seen.txt"}, ExpiresIn=60)
    assert urllib.request.urlopen(url).read() == b"seen"


def test_release_revoke_and_sweep_end_the_credential(store):
    store.write_object(f"projects/{P}/data/o/j/a.txt", b"a")
    store.write_object(f"projects/{Q}/data/o/j/a.txt", b"a")
    ka, kb, kq = storage.job_key(P, S, J), storage.job_key(P, S, J2), storage.job_key(Q, S, str(uuid.uuid4()))
    ok = lambda c, k, proj: reader(c, k)[0].cat_file(reader(c, k)[1](f"projects/{proj}/data/o/j/a.txt")) == b"a"
    a, b, other = creds.mint(ka), creds.mint(kb), creds.mint(kq)
    assert ok(a, ka, P) and ok(b, kb, P)
    creds.release(a)
    creds.release(a)  # idempotent
    assert denied(lambda: ok(a, ka, P)) and ok(b, kb, P)  # the role is gone: so is the credential
    assert creds.revoke_project(P) == 1 and denied(lambda: ok(b, kb, P)) and not [h for p, h in creds.active.values() if p == P]
    assert ok(other, kq, Q)  # another project is untouched
    assert creds.sweep() == 0  # nothing is old enough
    assert creds.sweep(max_age=-1) >= 1  # a role no one released (e.g. a crashed gateway's) is deleted
    assert denied(lambda: ok(other, kq, Q))


def test_a_persisted_handle_revokes_in_another_process(store):
    key = storage.job_key(P, S, J)
    h = creds.mint(key)
    saved = json.loads(json.dumps(h.to_dict()))
    assert "secret" not in json.dumps(saved)
    from cadmus_parser import minting
    minting.Handle.from_dict(saved, storage.BLOB_OPTIONS).revoke()
    fs, p = reader(h, key)
    assert denied(lambda: fs.cat_file(p(f"projects/{P}/data/x")))
    creds.release(h)  # revoking twice is fine


def test_a_failed_mint_leaves_no_role_and_never_falls_back(store, monkeypatch):
    from cadmus_parser import minting
    real = minting._aws

    class NoAssume:
        def __init__(self, c):
            self.c = c

        def __getattr__(self, n):
            if n == "assume_role":
                raise RuntimeError("sts down")
            return getattr(self.c, n)

    monkeypatch.setattr(minting, "_aws", lambda svc, o: NoAssume(real(svc, o)) if svc == "sts" else real(svc, o))
    with pytest.raises(RuntimeError, match="sts down"):
        creds.mint(storage.job_key(P, S, J))
    assert not [r["RoleName"] for r in real("iam", storage.BLOB_OPTIONS).list_roles()["Roles"] if r["RoleName"].startswith(minting.ROLE_PREFIX)] and not creds.active
    monkeypatch.setattr(storage, "BLOB_URL", "https://example.org/x")
    with pytest.raises(RuntimeError, match="no scoped credentials"):
        creds.mint(storage.job_key(P, S, J))


def test_listing_is_not_cached(store):
    """s3fs caches directory listings per process; the shared bucket is written by other replicas, so storage never uses the cache."""
    import fsspec
    o = storage.BLOB_OPTIONS
    other = fsspec.filesystem("s3", skip_instance_cache=True, **o)
    root = storage.BLOB_URL.split("://", 1)[1]
    u = f"users/stale-{uuid.uuid4().hex[:6]}@x.y"
    other.pipe_file(f"{root}/{u}/one", b"x")
    assert storage.list_names(u) == ["one"]
    other.pipe_file(f"{root}/{u}/two", b"x")
    assert storage.list_names(u) == ["one", "two"]
