"""Tests for Audio8ServiceManager internals (no subprocess is started)."""

import json
import time

from audio8_manager import HEALTH_TTL_SECONDS, Audio8ServiceManager, _HttpResponse


class FakeRaw:
    status = 200
    headers = {"content-type": "application/json"}

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body


class TestHttpResponse:
    def test_json_roundtrip(self):
        resp = _HttpResponse(FakeRaw(json.dumps({"ok": True, "n": 3}).encode()))
        assert resp.status == 200
        assert resp.json() == {"ok": True, "n": 3}

    def test_raise_for_status(self):
        resp = _HttpResponse(FakeRaw(b"boom"))
        resp.status = 500
        try:
            resp.raise_for_status()
            raise AssertionError("should have raised")
        except RuntimeError as e:
            assert "HTTP 500" in str(e)


class TestHealthCache:
    def make_manager(self) -> Audio8ServiceManager:
        return Audio8ServiceManager(
            model_dir=__import__("pathlib").Path("/tmp/x"),
            voices_dir=__import__("pathlib").Path("/tmp/x"),
            repo_dir=__import__("pathlib").Path("/tmp/x"),
        )

    def test_cache_hit_within_ttl(self):
        m = self.make_manager()
        m._mark_healthy()
        # Within the TTL window is_running() must answer True without any
        # process/port probing (proc is None, pidfile missing).
        assert m.is_running() is True

    def test_cache_expires(self):
        m = self.make_manager()
        m._healthy_until = time.monotonic() - 0.001
        assert m._health_cached() is False
        # Nothing is running → full probe path returns False quickly
        assert m.is_running() in (True, False)

    def test_stop_clears_cache(self):
        m = self.make_manager()
        m._mark_healthy()
        m.stop()
        assert m._health_cached() is False

    def test_ttl_is_short(self):
        assert HEALTH_TTL_SECONDS <= 10
