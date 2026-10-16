"""Shared fixtures: the backend importable from tests, auth off, and a fake
HuRI server speaking huri-core's protocol on a real local port."""

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# main.py reads these at import time.
os.environ["REQUIRE_AUTH"] = "0"
os.environ.pop("OIDC_ISSUER", None)
os.environ.pop("MAGIC_LINK_SECRET", None)

from fake_huri import FakeHuri  # noqa: E402


@pytest.fixture
def fake_huri():
    server = FakeHuri()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def huri_env(fake_huri, monkeypatch):
    """main.py pointed at the fake server."""
    monkeypatch.setenv("HURI_URL", fake_huri.ws_url)
    return fake_huri
