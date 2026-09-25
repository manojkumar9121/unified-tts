"""Pytest configuration.

Must run before any project import: redirects the SQLite history DB and the
output directory into a temp folder so tests never touch the developer's
real data/, and imports the app once for the whole session.
"""

import os
import tempfile

_TMP = tempfile.TemporaryDirectory(prefix="unified-tts-test-")
os.environ["TTS_DATA_DIR"] = os.path.join(_TMP.name, "data")
os.environ["TTS_OUTPUT_DIR"] = os.path.join(_TMP.name, "output")

import pytest
from fastapi.testclient import TestClient


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


def pytest_sessionfinish(session, exitstatus):
    """Remove the session's isolated runtime tree after all tests finish."""
    _TMP.cleanup()
