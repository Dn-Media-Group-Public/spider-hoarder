"""Client of the parser service (docs/implemented-plans/parse-sandbox-library.md): "parse job X", then wait. The gateway sends no file and reads the
result (`index.json` and the artefacts) from the blob store itself. Parsing never fails a job: every problem becomes a note. A thin layer over
the cadmus-parser client: this project's job layout (input = <job>/original, output = <job>), its scoped credentials (creds.py) and its options."""

import logging
import os

from cadmus_parser import Client, ParseError

from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway import creds

log = logging.getLogger("parse_client")
POLL_TIMEOUT = int(os.environ.get("PARSE_POLL_TIMEOUT") or 12 * 60)  # seconds; must exceed the largest deadline_s of PARSE_LIMITS (the default is 10 min); the MCP tool call stays synchronous
RENEW_EVERY = 300  # seconds: well under half the credential's lifetime


def parse(key: str, base_url: str = "", features: dict | None = None) -> list[str]:
    """Parse the job at storage `key` (`projects/<proj>/data/<session>/<job>`) once all its originals are stored; returns notes."""
    url, token = os.environ.get("PARSE_URL", "").rstrip("/"), os.environ.get("PARSE_TOKEN", "")
    if not url:
        return ["not parsed (no parser service configured)"]
    if features is None:  # per-backend: which profile of each model sidecar may see this backend's files
        features = {f: os.environ[v] for f, v in (("ocr", "PARSE_OCR"), ("asr", "PARSE_ASR"), ("asr-fallback", "PARSE_ASR_FALLBACK"), ("caption", "PARSE_CAPTION"), ("caption-fallback", "PARSE_CAPTION_FALLBACK")) if os.environ.get(v)} or None
    _, in_url, out_url = creds.urls(key)
    proj, handles = key.split("/")[1], []
    client = Client(url, token, storage.BLOB_OPTIONS)
    options = {"files": {"base_url": base_url, "ctypes": storage.get_meta(key).get("content_types", {})}}
    try:
        # project-wide read grant, job write grant (creds.grants): the whole job in one call; every credential is registered for revoke_project
        s, _ = client.run(in_url, out_url, creds.grants(key), mint_options=storage.BLOB_OPTIONS, options=options, features=features, renew_every=RENEW_EVERY,
                          poll_timeout=POLL_TIMEOUT, on_mint=lambda h: (creds.track(proj, h), handles.append(h)))
    except ParseError as e:
        return [f"not parsed (parser unavailable: {e.message})" if e.status == 0 else f"not parsed (parser answered {e.status}: {e.message[:200]})"]
    except Exception as e:  # noqa: BLE001  no credential could be minted
        log.exception("no scoped credential")
        return [f"not parsed (no storage credential: {type(e).__name__})"]
    finally:
        for h in handles:
            creds.release(h)  # revoked already; this takes them out of the registry
    return [] if s["status"] == "done" else [f"not parsed ({s.get('error') or 'failed'})"]
