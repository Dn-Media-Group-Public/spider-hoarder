"""Client of the OCR sidecar. The wrapper puts OCR_URL and OCR_TOKEN in the environment only when the job asked for remote OCR
(`features.ocr`); without them there is no remote code path. Tesseract and rasterising are docs/plans/ocr-hybrid.md and call this for
the remote step; a failure (sidecar down, timeout, token or endpoint error) must never fail the parse: the caller keeps its text and notes it."""

import json
import os
import urllib.request

TIMEOUT = int(os.environ.get("OCR_TIMEOUT") or 120)


def enabled() -> bool:
    return bool(os.environ.get("OCR_URL") and os.environ.get("OCR_TOKEN"))


def remote_ocr(image: bytes, content_type: str = "image/png") -> str:
    """Transcript of a PNG/JPEG (already downscaled) from the profile this job was given. Raises on any failure."""
    req = urllib.request.Request(os.environ["OCR_URL"].rstrip("/") + "/ocr", data=image, method="POST",
                                 headers={"Authorization": f"Bearer {os.environ['OCR_TOKEN']}", "Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # a proxy variable in the environment would not be honoured here: there is none
        return json.loads(r.read())["text"]
