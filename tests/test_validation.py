"""Tests for server-side engine-param validation helpers."""

import pytest
from fastapi import HTTPException

import server
from server import _coerce_param, _validate_params
from tts_engine import _ENGINE_REGISTRY


class TestCoerceParam:
    def test_int(self):
        assert _coerce_param("42", {"type": "int"}) == 42
        assert _coerce_param(None, {"type": "int", "default": 5}) is None

    def test_float(self):
        assert _coerce_param("1.5", {"type": "float"}) == 1.5

    def test_bool(self):
        assert _coerce_param("true", {"type": "bool"}) is True
        assert _coerce_param("off", {"type": "bool"}) is False
        assert _coerce_param(1, {"type": "bool"}) is True
        # None → schema default
        assert _coerce_param(None, {"type": "bool", "default": True}) is True

    def test_str_passthrough(self):
        assert _coerce_param("en-us", {"type": "str"}) == "en-us"

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            _coerce_param("abc", {"type": "int"})


class TestValidateParams:
    def test_empty_params(self):
        assert _validate_params("piper", None) == {}
        assert _validate_params("piper", {}) == {}

    def test_unknown_engine_returns_empty(self):
        assert _validate_params("nope", {"volume": 2.0}) == {}

    def test_undeclared_keys_dropped(self):
        out = _validate_params("piper", {"evil_key": "<script>", "volume": 1.5})
        assert "evil_key" not in out
        assert out["volume"] == 1.5

    def test_numeric_clamping(self):
        out = _validate_params("piper", {"volume": 99.0, "noise_scale": -5})
        assert out["volume"] == 2.0   # clamped to max
        assert out["noise_scale"] == 0.0  # clamped to min

    def test_kokoro_lang_select(self):
        out = _validate_params("kokoro", {"lang": "de"})
        assert out["lang"] == "de"


class TestSchemaIntegrity:
    """min <= default <= max for every numeric param in every engine."""

    def test_ranges_are_consistent(self):
        for eid, cls in _ENGINE_REGISTRY.items():
            for name, spec in cls.engine_params.items():
                lo, hi = spec.get("min"), spec.get("max")
                dflt = spec.get("default")
                if lo is not None and hi is not None:
                    assert lo <= hi, (eid, name)
                if dflt is not None and lo is not None:
                    assert dflt >= lo, (eid, name)
                if dflt is not None and hi is not None:
                    assert dflt <= hi, (eid, name)


class TestVoiceValidation:
    @pytest.mark.parametrize(
        ("is_online", "lazy_list_voices"),
        [(True, False), (False, True)],
        ids=["online", "daemon"],
    )
    def test_explicit_remote_voice_does_not_require_discovery(
        self, is_online, lazy_list_voices
    ):
        class RemoteEngine:
            def __init__(self):
                self.list_calls = 0

            def acquire(self):
                pass

            def release(self):
                pass

            def list_voices(self):
                self.list_calls += 1
                raise RuntimeError("voice discovery temporarily unavailable")

        engine = RemoteEngine()
        type(engine).is_online = is_online
        type(engine).lazy_list_voices = lazy_list_voices

        assert server._resolve_voice(engine, "caller-selected") == "caller-selected"
        assert engine.list_calls == 0

    def test_default_voice_still_uses_discovery(self):
        class LocalEngine:
            is_online = False
            lazy_list_voices = False

            def acquire(self):
                pass

            def release(self):
                pass

            def list_voices(self):
                return ["discovered-default"]

        assert server._resolve_voice(LocalEngine()) == "discovered-default"

    def test_traversal_voice_is_rejected_before_generation(self, client, monkeypatch):
        class FakePiper:
            is_online = False
            serialize_generation = True

            def get_engine_id(self):
                return "piper"

            def list_voices(self):
                return ["safe_voice"]

            def generate(self, *args, **kwargs):
                raise AssertionError("unsafe voice reached synthesis")

            def acquire(self):
                pass

            def release(self):
                pass

        monkeypatch.setattr(server, "get_engine", lambda engine_id: FakePiper())
        response = client.post(
            "/api/generate",
            json={"text": "hello", "engine_id": "piper", "voice": "../../secret"},
        )
        assert response.status_code == 400
        assert "voice" in response.json()["detail"].lower()

    def test_piper_loader_rejects_unsafe_name_before_import(self, tmp_path, monkeypatch):
        from tts_engine import PiperEngine

        engine = PiperEngine(str(tmp_path))
        monkeypatch.setattr(engine, "_voices_dir", lambda: tmp_path)
        with pytest.raises(ValueError, match="voice"):
            engine._load_voice("../secret")


class TestUploadBounds:
    def test_declared_oversize_is_rejected_without_reading(self, monkeypatch):
        class ExplodingFile:
            def read(self, size=-1):
                raise AssertionError("oversized upload was read")

        class FakeUpload:
            headers = {"content-length": str(30 * 1024 * 1024 + 1)}
            file = ExplodingFile()
            filename = "reference.wav"
            content_type = "audio/wav"

        monkeypatch.setattr(server.audio8_manager, "is_running", lambda: True)
        with pytest.raises(HTTPException) as error:
            server.register_custom_voice(FakeUpload(), "sample", "voice")
        assert error.value.status_code == 400
        assert "30 MB" in error.value.detail

    def test_undeclared_oversize_is_bounded_during_chunked_read(self, monkeypatch):
        class StreamedFile:
            def __init__(self):
                self.calls = []

            def read(self, size=-1):
                self.calls.append(size)
                return b"x" * size

        upload = StreamedFile()
        class FakeUpload:
            headers = {}
            file = upload
            filename = "reference.wav"
            content_type = "audio/wav"

        monkeypatch.setattr(server.audio8_manager, "is_running", lambda: True)
        with pytest.raises(HTTPException) as error:
            server.register_custom_voice(FakeUpload(), "sample", "voice")
        assert error.value.status_code == 400
        assert upload.calls and max(upload.calls) <= 1024 * 1024


class TestSanitizedErrors:
    def test_generation_failure_logs_traceback_but_returns_generic_detail(self, monkeypatch, caplog):
        class FailingEngine:
            is_online = False
            serialize_generation = True

            def get_engine_id(self):
                return "piper"

            def list_voices(self):
                return ["safe_voice"]

            def generate(self, *args, **kwargs):
                raise RuntimeError("private synthesis details")

            def acquire(self):
                pass

            def release(self):
                pass

        monkeypatch.setattr(server, "get_engine", lambda engine_id: FailingEngine())
        with caplog.at_level("ERROR", logger="unified_tts.server"):
            response = client_for_generation_error()
        assert response.status_code == 500
        assert response.json() == {"detail": "An internal error occurred"}
        assert "private synthesis details" in caplog.text
        assert "Traceback" in caplog.text


def client_for_generation_error():
    from fastapi.testclient import TestClient

    return TestClient(server.app).post(
        "/api/generate",
        json={"text": "hello", "engine_id": "piper", "voice": "safe_voice"},
    )
