"""Tests for the engine registry, base-class pipeline, and param handling."""

import threading

import numpy as np
import pytest

from tts_engine import (
    _ENGINE_REGISTRY,
    TTSEngine,
    create_engine,
    engine_ids,
)


def _lock_is_held(lock: threading.RLock) -> bool:
    """Whether ``lock`` is currently held by another thread.

    ``RLock.locked()`` only exists on Python 3.14+, but CI still runs the
    3.10/3.12 matrix, so fall back to a non-blocking acquire probe. The probe
    is exact unless the calling thread already holds the lock, which is fine
    for the in-line generate() calls that use this.
    """
    locked = getattr(lock, "locked", None)
    if locked is not None:
        return locked()
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


class TestRegistry:
    def test_all_engines_registered(self):
        assert set(engine_ids()) == {"piper", "kokoro", "kitten-tts", "audio8", "edge-tts", "gtts"}

    def test_create_unknown_engine_raises(self):
        with pytest.raises(ValueError, match="Unknown engine"):
            create_engine("definitely-not-an-engine")

    def test_online_flags(self):
        assert _ENGINE_REGISTRY["edge-tts"].is_online
        assert _ENGINE_REGISTRY["gtts"].is_online
        assert not _ENGINE_REGISTRY["piper"].is_online

    def test_audio8_char_limit(self):
        assert _ENGINE_REGISTRY["audio8"].max_text_chars == 900

    def test_local_engines_serialize_generation(self):
        for eid in ("piper", "kokoro", "kitten-tts"):
            assert _ENGINE_REGISTRY[eid].serialize_generation, eid


class TestParamSchema:
    """Engine params must carry the metadata the UI and Audio8 rely on."""

    def test_every_param_has_type_and_label(self):
        for eid in engine_ids():
            for name, spec in _ENGINE_REGISTRY[eid].engine_params.items():
                assert "type" in spec, (eid, name)
                assert "label" in spec, (eid, name)

    def test_audio8_defaults_are_coercible(self):
        cls = _ENGINE_REGISTRY["audio8"]
        eng = cls("output")
        body = {
            "temperature": eng._param(None, "temperature"),
            "top_p": eng._param(None, "top_p"),
            "top_k": eng._param(None, "top_k"),
            "seed": eng._param(None, "seed"),
            "max_new_tokens": eng._param(None, "max_new_tokens"),
        }
        assert body == {
            "temperature": 0.3,
            "top_p": 0.9,
            "top_k": 50,
            "seed": 42,
            "max_new_tokens": 1024,
        }
        # Explicit values win over schema defaults; types are coerced.
        assert eng._param({"seed": "7"}, "seed") == 7

    def test_param_falls_back_to_default_on_garbage(self):
        eng = _ENGINE_REGISTRY["audio8"]("output")
        assert eng._param({"max_new_tokens": "not-a-number"}, "max_new_tokens") == 1024


class TestBasePipeline:
    """Exercise generate() end-to-end with a fake in-memory engine."""

    class FakeEngine(TTSEngine):
        name = "fake"
        sr = 16000

        def synthesize(self, text, voice="", **params):
            n = int(0.1 * self.sr) * max(1, len(text))
            return np.zeros(n, dtype=np.float32), self.sr

        def list_voices(self):
            return ["v1"]

    def test_generate_writes_wav_and_duration(self, tmp_path):
        eng = self.FakeEngine(str(tmp_path))
        filepath, duration = eng.generate("hello world")
        assert filepath.endswith(".wav")
        assert duration > 0

    def test_generate_empty_text_raises(self, tmp_path):
        with pytest.raises(ValueError, match="empty"):
            self.FakeEngine(str(tmp_path)).generate("   ")

    def test_generate_chunking_over_max_chars(self, tmp_path):
        eng = self.FakeEngine(str(tmp_path))
        eng.max_text_chars = 10
        _filepath, duration = eng.generate("one two three four five six.")  # >10 chars → chunks
        assert duration >= 0.3  # several chunks + gaps

    def test_serialization_lock_is_honored(self, tmp_path):
        eng = self.FakeEngine(str(tmp_path))
        eng.serialize_generation = True
        assert not _lock_is_held(eng._engine_lock), "lock should be free before use"
        eng.generate("hi")
        assert not _lock_is_held(eng._engine_lock), "lock must be released after generate"

    def test_pitch_shift_fallback_without_librosa(self, tmp_path, monkeypatch):
        # Simulate a core-only install: librosa import fails inside the
        # base helper, which must degrade gracefully instead of raising.
        import builtins

        real_import = builtins.__import__

        def no_librosa(name, *args, **kwargs):
            if name == "librosa":
                raise ImportError("librosa disabled for test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_librosa)
        eng = self.FakeEngine(str(tmp_path))
        audio = eng._apply_pitch_shift(np.zeros(1000, dtype=np.float32), 16000, 2.0)
        assert len(audio) == 1000  # returned unchanged


class TestLifecycleSafety:
    class ModelEngine(TTSEngine):
        name = "model-engine"
        serialize_generation = True

        def __init__(self, output_dir):
            super().__init__(output_dir)
            self.model = object()

        def synthesize(self, text, voice="", **params):
            return np.zeros(1, dtype=np.float32), 16000

        def list_voices(self):
            return ["voice"]

        def _unload_model(self):
            self.model = None

    def test_unload_does_not_clear_model_while_request_is_active(self, tmp_path):
        engine = self.ModelEngine(str(tmp_path))
        engine.acquire()
        try:
            engine.unload()
            assert engine.model is not None
        finally:
            engine.release()
        engine.unload()
        assert engine.model is None

    @pytest.mark.parametrize("engine_id", ["kokoro", "kitten-tts"])
    def test_concurrent_model_ensure_waits_for_serialization_lock(self, engine_id, tmp_path, monkeypatch):
        import tts_engine

        engine = tts_engine.create_engine(engine_id, str(tmp_path))
        monkeypatch.setattr(engine, "_model_dir", lambda: tmp_path / "missing")
        started = threading.Event()
        finished = threading.Event()

        def ensure_loaded():
            started.set()
            try:
                engine._ensure_loaded()
            except FileNotFoundError:
                pass
            finally:
                finished.set()

        with engine._engine_lock:
            worker = threading.Thread(target=ensure_loaded)
            worker.start()
            assert started.wait(timeout=1)
            assert finished.wait(timeout=0.05) is False
        worker.join(timeout=2)
        assert finished.is_set()
