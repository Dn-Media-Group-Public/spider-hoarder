"""Self-check for the trust-boundary logic: blob tokens and zip-slip rejection."""

import io
import os
import time
import tarfile
import zipfile

os.environ.setdefault("BLOB_TOKEN_SECRET", "test-secret")

from mcp_research.gateway import blobtoken
from mcp_research.gateway.content import extract


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


def _tar_with(*members):
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as tf:
        for name, type_, data in members:
            ti = tarfile.TarInfo(name); ti.type = type_; ti.linkname = "/etc/passwd"
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return io.BytesIO(b.getvalue())


def test_tar_unsafe_members_skipped():
    notes = []
    f = _tar_with(("ok.txt", tarfile.REGTYPE, b"fine"), ("../escape", tarfile.REGTYPE, b"x"), ("/abs", tarfile.REGTYPE, b"x"),
                  ("sym", tarfile.SYMTYPE, b""), ("hard", tarfile.LNKTYPE, b""), ("dev", tarfile.CHRTYPE, b""),
                  ("fifo", tarfile.FIFOTYPE, b""))
    got = [(n, m.read()) for n, m in extract.unpack_archive(f, "t.tar", "", 100, 1000, notes)]
    assert got == [("ok.txt", b"fine")], got
    assert notes == ["sym skipped (symlink)", "hard skipped (hardlink)", "dev skipped (special file)", "fifo skipped (special file)"], notes


def test_archive_bomb_limits():
    f = _tar_with(*[(f"f{i}", tarfile.REGTYPE, b"x" * 10) for i in range(5)])
    try:
        list(extract.unpack_archive(f, "t.tar", "", 4, 1000))
        assert False
    except extract.ArchiveLimit:
        pass
    f = _tar_with(("big", tarfile.REGTYPE, b"x" * 100))
    try:
        [m.read() for _, m in extract.unpack_archive(f, "t.tar", "", 10, 50)]
        assert False
    except extract.ArchiveLimit:
        pass


def test_safe_name():  # py7zr/rarfile cannot be made to write slip names; 7z/rar members go through this same check
    assert [extract.safe_name(n) for n in ("a/b", "../x", "/x", "a/../../x")] == [True, False, False, False]


if __name__ == "__main__":
    test_blobtoken_roundtrip()
    test_blobtoken_tamper_rejected()
    test_blobtoken_expiry_rejected()
    test_zip_slip_rejected()
    test_tar_unsafe_members_skipped()
    test_archive_bomb_limits()
    test_safe_name()
    print("ok")
