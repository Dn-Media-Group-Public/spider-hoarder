"""Per-job credentials on real GCS (a Credential Access Boundary): what a minted token can read, write and list. Needs a scratch bucket
and application-default credentials: TEST_GCS_URL=gs://bucket pytest sdk/tests/test_creds_gcs.py (skipped otherwise). Not revocable:
release() is a no-op, the token just expires."""

import os
import uuid

import pytest

from spider_hoarder_sdk import testing

testing.gateway_env()

from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway import creds

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_GCS_URL"), reason="TEST_GCS_URL (gs://scratch-bucket) not set")
P, Q, S, J, J2 = (str(uuid.uuid4()) for _ in range(5))


def denied(fn):
    try:
        fn()
    except (PermissionError, OSError) as e:
        return isinstance(e, PermissionError) or any(w in str(e).lower() for w in ("403", "forbidden", "denied", "permission"))
    return False


def test_matrix(monkeypatch):
    prefix = f"t-{uuid.uuid4()}"
    monkeypatch.setattr(storage, "BLOB_URL", f"{os.environ['TEST_GCS_URL'].rstrip('/')}/{prefix}")
    monkeypatch.setattr(storage, "BLOB_OPTIONS", {})
    base = f"projects/{P}"
    try:
        storage.write_object(f"{base}/access.json", b"{}")
        storage.write_object(f"{base}/data/other/job/seen.txt", b"seen")
        storage.write_object(f"projects/{Q}/data/s/j/foreign.txt", b"x")
        c = creds.mint(P, S, J)
        fs, root = storage._abs("", c.options)
        p = lambda k: f"{root.rstrip('/')}/{k}"
        assert fs.cat_file(p(f"{base}/data/other/job/seen.txt")) == b"seen"
        for k in (f"{base}/access.json", f"projects/{Q}/data/s/j/foreign.txt"):
            assert denied(lambda: fs.cat_file(p(k))), k
        fs.pipe_file(p(f"{base}/data/{S}/{J}/out.txt"), b"hi")
        with fs.open(p(f"{base}/data/{S}/{J}/big.bin"), "wb") as f:
            f.write(b"x" * (12 << 20))
        assert fs.size(p(f"{base}/data/{S}/{J}/big.bin")) == 12 << 20
        for k in (f"{base}/data/{S}/{J2}/n.txt", f"{base}/data/other/job/n.txt", f"{base}/access.json", f"{base}/n.txt",
                  f"projects/{Q}/data/{S}/{J}/n.txt", "evil.txt"):
            assert denied(lambda: fs.pipe_file(p(k), b"no")), k
        assert denied(lambda: fs.rm_file(p(f"{base}/data/other/job/seen.txt")))
        fs.rm_file(p(f"{base}/data/{S}/{J}/out.txt"))
        assert fs.ls(p(f"{base}/data/other/job"), detail=False) and fs.ls(p(f"{base}/data"), detail=False)
        for k in (base, "projects", f"projects/{Q}", f"projects/{Q}/data", ""):
            assert denied(lambda: fs.ls(p(k), detail=False)), k
        c.release()
        assert storage.read_object(f"{base}/access.json") == b"{}"   # the gateway's own credential is unaffected
    finally:
        storage.delete_tree("")
