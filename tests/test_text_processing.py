"""Tests for pure text-processing helpers in tts_engine."""

import numpy as np
import pytest

from tts_engine import chunk_text, split_sentences, _hard_split, _time_stretch


class TestSplitSentences:
    def test_empty(self):
        assert split_sentences("") == []
        assert split_sentences("   ") == []

    def test_ascii(self):
        assert split_sentences("Hello there. How are you? Great!") == [
            "Hello there.",
            "How are you?",
            "Great!",
        ]

    def test_cjk_punctuation(self):
        parts = split_sentences("你好。世界！")
        assert parts == ["你好。", "世界！"]

    def test_no_terminal_punctuation(self):
        assert split_sentences("one sentence without end") == ["one sentence without end"]


class TestHardSplit:
    def test_respects_max_chars(self):
        words = " ".join(["word"] * 50)
        chunks = _hard_split(words, 20)
        assert all(len(c) <= 20 for c in chunks)
        assert " ".join(chunks) == words

    def test_single_long_word(self):
        assert _hard_split("a" * 100, 10) == ["a" * 100]


class TestChunkText:
    def test_short_text_unchanged(self):
        assert chunk_text("short", 100) == ["short"]

    def test_empty(self):
        assert chunk_text("", 10) == []
        assert chunk_text("   ", 10) == []

    def test_splits_on_sentence_boundaries(self):
        text = "First sentence here. Second sentence here. Third one too!"
        chunks = chunk_text(text, 25)
        assert len(chunks) >= 2
        assert all(len(c) <= 25 for c in chunks)
        # No text lost
        assert sum(len(c) for c in chunks) + len(chunks) - 1 == len(text)

    def test_hard_split_for_overlong_sentence(self):
        text = "word " * 40  # single "sentence" of 200 chars
        chunks = chunk_text(text, 50)
        assert all(len(c) <= 50 for c in chunks)
        assert len(chunks) >= 4


class TestTimeStretchFallback:
    def test_stretch_changes_length_without_librosa_assumptions(self):
        audio = np.ones(1000, dtype=np.float32)
        out = _time_stretch(audio, 2.0)
        assert isinstance(out, np.ndarray)
        # Faster playback (rate=2) must yield roughly half the samples,
        # whichever code path (librosa or naive fallback) ran.
        assert abs(len(out) - 500) < 60

    def test_invalid_rate_raises_valueerror(self):
        with pytest.raises(Exception):
            _time_stretch(np.ones(10), 0.0)
