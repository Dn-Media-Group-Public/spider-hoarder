"""Self-check for the trust-boundary logic: blob tokens and zip-slip rejection."""

import io
import os
import time
import zipfile

os.environ.setdefault("BLOB_TOKEN_SECRET", "test-secret")

import blobtoken
import extract


def test_blobtoken_roundtrip():
    token = blobtoken.mint("job1", "parsed/a.md")
    data = blobtoken.verify(token)
    assert data == {"job_id": "job1", "path": "parsed/a.md"}


def test_blobtoken_tamper_rejected():
    token = blobtoken.mint("job1", "parsed/a.md")
    payload, sig = token.split(".", 1)
    tampered = payload + "x." + sig
    try:
        blobtoken.verify(tampered)
        assert False, "tampered token should not verify"
    except ValueError:
        pass


def test_blobtoken_expiry_rejected():
    token = blobtoken.mint("job1", "parsed/a.md", ttl=-1)
    try:
        blobtoken.verify(token)
        assert False, "expired token should not verify"
    except ValueError:
        pass


def test_zip_slip_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ok.txt", "fine")
        zf.writestr("../escape.txt", "malicious")
        zf.writestr("/absolute.txt", "malicious")
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        names = extract.safe_zip_members(zf)
    assert names == ["ok.txt"]


if __name__ == "__main__":
    test_blobtoken_roundtrip()
    test_blobtoken_tamper_rejected()
    test_blobtoken_expiry_rejected()
    test_zip_slip_rejected()
    print("ok")
