"""Pytest configuration.

Must run before any project import: redirects the SQLite history DB and the
output directory into a temp folder so tests never touch the developer's
real data/, and imports the app once for the whole session.
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="unified-tts-test-")
os.environ.setdefault("TTS_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("TTS_OUTPUT_DIR", os.path.join(_TMP, "output"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    """TestClient for the FastAPI app (session-scoped: import side effects
    like init_db() and the monitor thread only run once)."""
    import server

    # Tests may mutate this; reset between sessions is unnecessary because
    # each test that cares sets it explicitly.
    yield TestClient(server.app)


@pytest.fixture()
def db():
    """Direct access to the history-DB helper module."""
    import tts_engine

    return tts_engine
