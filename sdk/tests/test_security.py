"""Self-check for the trust-boundary logic: blob tokens."""

import time

from spider_hoarder_sdk import testing

testing.gateway_env()

from spider_hoarder_sdk.gateway import blobtoken


def test_blobtoken_roundtrip():
    token = blobtoken.mint("job1", "parsed/a.md")
    data = blobtoken.verify(token)
    assert data == {"job_id": "job1", "path": "parsed/a.md", "policy": None}
    assert blobtoken.verify(blobtoken.mint("job1", "@upload", policy={"ocr": ["lan"]}))["policy"] == {"ocr": ["lan"]}


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
