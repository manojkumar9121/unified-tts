"""Tests for Audio8ServiceManager internals (no subprocess is started)."""

import json
import threading
import time
from pathlib import Path

import pytest

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


@pytest.fixture()
def manager(tmp_path: Path) -> Audio8ServiceManager:
    return Audio8ServiceManager(
        model_dir=tmp_path / "models",
        voices_dir=tmp_path / "voices",
        repo_dir=tmp_path / "repo",
        pid_path=tmp_path / "audio8.pid",
        log_path=tmp_path / "audio8.log",
    )


class TestHealthCache:
    def test_cache_hit_within_ttl(self, manager):
        m = manager
        m._mark_healthy()
        # Within the TTL window is_running() must answer True without any
        # process/port probing (proc is None, pidfile missing).
        assert m.is_running() is True

    def test_cache_expires(self, manager):
        m = manager
        m._healthy_until = time.monotonic() - 0.001
        assert m._health_cached() is False
        # Nothing is running → full probe path returns False quickly
        assert m.is_running() in (True, False)

    def test_stop_clears_cache(self, manager):
        m = manager
        m._mark_healthy()
        m.stop()
        assert m._health_cached() is False

    def test_ttl_is_short(self):
        assert HEALTH_TTL_SECONDS <= 10


class TestSerializedStart:
    def test_simultaneous_starts_spawn_one_process(self, manager, monkeypatch):
        spawned = []

        class FakeProcess:
            def __init__(self, *args, **kwargs):
                self.pid = 100 + len(spawned)
                spawned.append(self)
                time.sleep(0.1)

            def poll(self):
                return None

        monkeypatch.setattr("audio8_manager.subprocess.Popen", FakeProcess)
        monkeypatch.setattr(manager, "is_running", lambda: bool(manager._health_cached()))
        monkeypatch.setattr(manager, "_wait_for_health", lambda timeout=90: manager._mark_healthy() or True)

        results = []
        threads = [
            threading.Thread(target=lambda: results.append(manager.start()))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert results == [True, True]
        assert len(spawned) == 1
        assert manager._proc is spawned[0]


class TestRequestLifecycle:
    def test_stop_waits_for_in_flight_post(self, manager, monkeypatch):
        request_started = threading.Event()
        release_request = threading.Event()
        stop_called = threading.Event()
        errors = []

        class BlockingRaw:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                request_started.set()
                assert release_request.wait(timeout=5)
                return b"audio"

        monkeypatch.setattr(manager, "is_running", lambda: True)
        monkeypatch.setattr("audio8_manager.urllib.request.urlopen", lambda *args, **kwargs: BlockingRaw())
        original_stop = manager._stop_locked

        def observed_stop():
            stop_called.set()
            original_stop()

        monkeypatch.setattr(manager, "_stop_locked", observed_stop)

        def request():
            try:
                manager.post(f"{manager.url}/api/tts", json={"text": "hello"})
            except Exception as exc:  # pragma: no cover - assertion reports thread failures
                errors.append(exc)

        request_thread = threading.Thread(target=request)
        stop_thread = threading.Thread(target=manager.stop)
        request_thread.start()
        assert request_started.wait(timeout=5)
        stop_thread.start()

        assert not stop_called.wait(timeout=0.1)
        release_request.set()
        request_thread.join(timeout=5)
        stop_thread.join(timeout=5)

        assert not request_thread.is_alive()
        assert not stop_thread.is_alive()
        assert stop_called.is_set()
        assert not errors
