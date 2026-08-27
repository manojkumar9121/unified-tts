"""Tests for server-side engine-param validation helpers."""

import pytest

from tts_engine import _ENGINE_REGISTRY
from server import _coerce_param, _validate_params


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
