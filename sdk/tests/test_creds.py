"""Per-job store credentials: the policies, and against the real test store (SeaweedFS, scripts/test.sh) what a minted credential can
read, write and list, that releasing / revoking / sweeping ends it, and that a failure never leaves a role or a fallback behind."""

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


def test_policies():
    pol = creds.s3_policy("bkt", "pre/fix", P, S, J)
    rd, wr, ls = pol["Statement"]
    assert rd == {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::bkt/pre/fix/projects/{P}/data/*"}
    assert wr["Resource"] == f"arn:aws:s3:::bkt/pre/fix/projects/{P}/data/{S}/{J}/*" and "s3:GetObject" not in wr["Action"]
    assert ls["Action"] == "s3:ListBucket" and ls["Resource"] == "arn:aws:s3:::bkt" and ls["Condition"] == {"StringLike": {"s3:prefix": f"pre/fix/projects/{P}/data/*"}}
    assert all(a.startswith("s3:") for st in pol["Statement"] for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"]))
    assert len(json.dumps(pol, separators=(",", ":"))) < 2048 and "access.json" not in json.dumps(pol)
    assert f"projects/{P}/data/" in creds.s3_policy("bkt", "", P, S, J)["Statement"][0]["Resource"]
    rules = creds.cab_rules("bkt", "", P, S, J)
    assert len(rules) <= 10 and f"objects/projects/{P}/data/{S}/{J}/" in rules[1]["condition"] and "objectListPrefix" in rules[0]["condition"]


@pytest.fixture
def store():
    creds.active.clear()
    testing.fresh_store()  # s3://bucket/<uuid>: also proves a key prefix inside the bucket is honoured
    return storage


def reader(c):
    fs, root = storage._abs("", c.options)
    return fs, lambda k: f"{root.rstrip('/')}/{k}"


def test_matrix(store):
    store.write_object(f"projects/{P}/access.json", b"{}")
    store.write_object(f"projects/{P}/data/other/job/seen.txt", b"seen")
    store.write_object(f"projects/{Q}/data/s/j/foreign.txt", b"x")
    store.write_object(f"users/a@x.y/{P}", b"admin")
    c = creds.mint(P, S, J)
    try:
        fs, p = reader(c)
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
        c.release()
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
    ok = lambda c: reader(c)[0].cat_file(reader(c)[1](f"projects/{P}/data/o/j/a.txt")) == b"a"
    a, b, other = creds.mint(P, S, J), creds.mint(P, S, J2), creds.mint(Q, S, str(uuid.uuid4()))
    assert ok(a) and ok(b)
    a.release(); a.release()  # idempotent
    assert denied(lambda: ok(a)) and ok(b)  # the role is gone: so is the credential
    assert creds.revoke_project(P) == 1 and denied(lambda: ok(b)) and not [n for n, (p, _) in creds.active.items() if p == P]
    assert reader(other)[0].cat_file(reader(other)[1](f"projects/{Q}/data/o/j/a.txt")) == b"a"  # another project is untouched
    assert creds.sweep() == 0  # nothing is old enough
    assert creds.sweep(max_age=-1) >= 1  # a role no one released (e.g. a crashed gateway's) is deleted
    assert denied(lambda: reader(other)[0].cat_file(reader(other)[1](f"projects/{Q}/data/o/j/a.txt")))


def test_a_failed_mint_leaves_no_role_and_never_falls_back(store, monkeypatch):
    real = creds._aws

    class NoAssume:
        def __init__(self, c):
            self.c = c

        def __getattr__(self, n):
            if n == "assume_role":
                raise RuntimeError("sts down")
            return getattr(self.c, n)

    monkeypatch.setattr(creds, "_aws", lambda svc: NoAssume(real(svc)) if svc == "sts" else real(svc))
    with pytest.raises(RuntimeError, match="sts down"):
        creds.mint(P, S, J)
    names = [r["RoleName"] for r in real("iam").list_roles()["Roles"]]
    assert f"{creds.ROLE_PREFIX}{J}" not in names and not creds.active
    monkeypatch.setattr(storage, "BLOB_URL", "https://example.org/x")
    with pytest.raises(RuntimeError, match="no scoped credentials"):
        creds.mint(P, S, J)


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
