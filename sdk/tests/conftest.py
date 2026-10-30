"""The gateway picks its driver at import: register a fake backend (real entry-point discovery) before any test imports it."""

import os
import tempfile

from spider_hoarder_sdk import testing

_DIR = tempfile.mkdtemp(prefix="fake-backend-")
testing.install_fake_backend(_DIR)
testing.gateway_env(BACKEND="fake_backend", BACKENDS="fake_backend")
