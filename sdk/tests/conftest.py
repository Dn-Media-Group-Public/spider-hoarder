"""The gateway picks its driver at import: register a fake backend (real entry-point discovery) before any test imports it."""

import os
import tempfile

from spider_hoarder_sdk import testing

_DIR = tempfile.mkdtemp(prefix="fake-backend-")
testing.install_fake_backend(_DIR)
testing.gateway_env(BACKEND="fake_backend", BACKENDS="fake_backend")


import pytest


@pytest.fixture(autouse=True)
def _no_metadata(request, monkeypatch):
    """Metadata extraction needs exiftool/ffprobe and adds files and notes: only test_metadata.py (marker `metadata`) runs it."""
    if "metadata" not in request.keywords:
        from spider_hoarder_sdk.content import metadata
        monkeypatch.setattr(metadata, "extract_metadata", lambda *a, **k: {})
