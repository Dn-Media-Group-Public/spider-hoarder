"""Client of the parser service (docs/plans/parser-wrapper.md): "parse job X", then poll. The gateway sends no file and reads the result
(`index.json` and the artefacts) from the blob store itself. Parsing never fails a job: every problem becomes a note."""

import json
import logging
import os
import time

import httpx

from spider_hoarder_sdk.gateway import creds as scoped

log = logging.getLogger("parse_client")
POLL_TIMEOUT = 12 * 60  # the wrapper's 10 min job limit plus queueing; the MCP tool call stays synchronous for the client


def _wire(options: dict) -> dict:
    """Scoped fsspec options as JSON: a GCS downscoped token is a Credentials object, sent as its access token."""
    tok = options.get("token")
    if tok is not None and not isinstance(tok, str):
        import google.auth.transport.requests
        tok.refresh(google.auth.transport.requests.Request())
        return {**{k: v for k, v in options.items() if k != "token"}, "gcs_access_token": tok.token}
    return options


def parse(key: str, base_url: str = "", features: dict | None = None) -> list[str]:
    """Parse the job at storage `key` (`projects/<proj>/data/<session>/<job>`) once all its originals are stored; returns notes."""
    url, token = os.environ.get("PARSE_URL", "").rstrip("/"), os.environ.get("PARSE_TOKEN", "")
    if not url:
        return ["not parsed (no parser service configured)"]
    if features is None and os.environ.get("PARSE_OCR"):  # per-backend: which OCR profile may see this backend's files
        features = {"ocr": os.environ["PARSE_OCR"]}
    parts = key.split("/")
    ref = f"{parts[1]}/{parts[3]}/{parts[4]}"
    head = {"Authorization": f"Bearer {token}"}
    try:
        cred = scoped.mint(parts[1], parts[3], parts[4])
    except Exception as e:  # noqa: BLE001
        log.exception("no scoped credential")
        return [f"not parsed (no storage credential: {type(e).__name__})"]
    try:
        wire = _wire(cred.options)
        body = {"credentials": wire, "base_url": base_url, **({"features": features} if features else {})}
        with httpx.Client(timeout=30) as c:
            for attempt in (0, 1):  # one retry on 503 or a refused connection
                try:
                    r = c.post(f"{url}/jobs/{ref}", json=body, headers=head)
                except httpx.TransportError as e:
                    if attempt:
                        return [f"not parsed (parser unavailable: {type(e).__name__})"]
                    time.sleep(1)
                    continue
                if r.status_code == 503 and not attempt:
                    time.sleep(1)
                    continue
                break
            if r.status_code != 202:
                return [f"not parsed (parser answered {r.status_code}: {r.text[:200]})"]
            delay, deadline = 0.05, time.monotonic() + POLL_TIMEOUT
            while time.monotonic() < deadline:
                time.sleep(delay)
                delay = min(delay * 1.5, 1.0)
                try:
                    s = c.get(f"{url}/jobs/{ref}", headers={**head, "X-Credentials": json.dumps(wire)}).json()
                except (httpx.TransportError, ValueError):
                    continue
                if s.get("status") == "done":
                    return []
                if s.get("status") == "failed":
                    return [f"not parsed ({s.get('error', 'failed')})"]
            return ["not parsed (timed out waiting for the parser)"]
    finally:
        cred.release()
