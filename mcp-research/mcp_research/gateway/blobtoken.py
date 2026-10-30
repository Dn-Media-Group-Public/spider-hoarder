"""HMAC-signed opaque tokens for GET /blob/<token>: encode {job_id, path, exp}."""

import base64
import hmac
import json
import os
import time
from hashlib import sha256

SECRET = os.environ.get("BLOB_TOKEN_SECRET", "")
if not SECRET:
    raise RuntimeError(
        "BLOB_TOKEN_SECRET must be set to a non-empty random value "
        "(e.g. `openssl rand -hex 32`) — an empty secret makes /blob tokens forgeable."
    )

TTL_SECONDS = 15 * 60


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint(job_id: str, path: str, ttl: int = TTL_SECONDS) -> str:
    payload = json.dumps({"job_id": job_id, "path": path, "exp": int(time.time()) + ttl}).encode()
    sig = hmac.new(SECRET.encode(), payload, sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def verify(token: str) -> dict:
    """Returns {"job_id", "path"} or raises ValueError."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        sig = _unb64(sig_b64)
    except Exception:
        raise ValueError("malformed token")

    expected_sig = hmac.new(SECRET.encode(), payload, sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("bad signature")

    data = json.loads(payload)
    if data["exp"] < time.time():
        raise ValueError("expired token")
    return {"job_id": data["job_id"], "path": data["path"]}
