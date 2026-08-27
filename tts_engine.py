"""Multi-engine TTS backend with a strict engine abstraction layer.

Architecture
------------
* Every engine subclasses :class:`TTSEngine` and implements the strict,
  synchronous interface: ``synthesize(text, voice)`` and ``list_voices()``.
  All shared behavior (speed/pitch DSP, format conversion, sentence-aware
  chunking, saving) lives in the base class, so engines stay thin and the
  rest of the application never needs to know which engine it is talking to.

* Engines register themselves with the @register_engine decorator. Adding a
  new engine = subclass + one decorator line; the web UI discovers engine
  ids from the backend (``/api/engines``), so no frontend change is needed.

* Resource management: engines expose ``loaded``, ``unload()``,
  ``memory_mb()`` and ``last_used`` so the server can auto-unload idle
  models (RAM) and stop the Audio8 background daemon when idle.
"""

import gc
import io
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

logger = logging.getLogger("unified_tts.engine")

BASE_DIR = Path(__file__).parent.resolve()
MODELS_DIR = BASE_DIR / "models"
# TTS_DATA_DIR lets tests (or unusual installs) redirect the SQLite history
# away from the repo's data/ directory.
DATA_DIR = Path(os.environ.get("TTS_DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "generations.db"

# Gap (seconds) inserted between sentence chunks when concatenating audio.
CHUNK_GAP_SECONDS = 0.25

# ─── Database helpers ─────────────────────────────────────────────────────────


def _connect() -> sqlite3.Connection:
    """Open a history-DB connection.

    ``busy_timeout`` makes concurrent writers wait for the lock instead of
    raising "database is locked" (WAL allows a single writer at a time).
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db():
    conn = _connect()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            text TEXT NOT NULL,
            engine TEXT NOT NULL,
            voice TEXT NOT NULL,
            speed REAL NOT NULL DEFAULT 1.0,
            pitch REAL NOT NULL DEFAULT 0.0,
            duration REAL NOT NULL,
            format TEXT NOT NULL DEFAULT 'wav',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_created_at ON generations (created_at DESC)
    """)
    conn.commit()
    conn.close()


def add_generation(filename: str, text: str, engine: str, voice: str, speed: float, pitch: float, duration: float, fmt: str = "wav"):
    conn = _connect()
    conn.execute(
        "INSERT INTO generations (filename, text, engine, voice, speed, pitch, duration, format, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (filename, text, engine, voice, speed, pitch, duration, fmt, time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def get_generations(limit: int = 100, offset: int = 0):
    conn = _connect()
    cursor = conn.execute("SELECT id, filename, text, engine, voice, speed, pitch, duration, format, created_at FROM generations ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()
    return [dict(zip(columns, row)) for row in rows]


def delete_generation(filename: str):
    conn = _connect()
    conn.execute("DELETE FROM generations WHERE filename = ?", (filename,))
    conn.commit()
    conn.close()


def delete_generations_older_than(days: int) -> list[str]:
    """Delete history rows older than ``days``; returns their filenames so
    the caller can also remove the audio files from disk."""
    conn = _connect()
    date_threshold = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    cursor = conn.execute(
        "SELECT filename FROM generations WHERE created_at < ?", (date_threshold,)
    )
    filenames = [row[0] for row in cursor.fetchall()]
    conn.execute("DELETE FROM generations WHERE created_at < ?", (date_threshold,))
    conn.commit()
    conn.close()
    return filenames


# ─── Audio DSP helpers ────────────────────────────────────────────────────────


def _time_stretch(audio: np.ndarray, rate: float) -> np.ndarray:
    try:
        import librosa
        return librosa.effects.time_stretch(y=audio, rate=rate)
    except ImportError:
        n = len(audio)
        indices = np.linspace(0, n - 1, int(n / rate))
        indices = np.clip(indices, 0, n - 1).astype(int)
        return audio[indices]


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…。！？；;])")


def split_sentences(text: str) -> list[str]:
    """Split text at sentence boundaries, keeping the punctuation delimiters."""
    if not text or not text.strip():
        return []
    raw = _SENTENCE_SPLIT_RE.split(text)
    return [part.strip() for part in raw if part.strip()]


def _hard_split(segment: str, max_chars: int) -> list[str]:
    """Split an over-long segment on word boundaries."""
    words = segment.split()
    chunks: list[str] = []
    cur = ""
    for word in words:
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= max_chars:
            cur = f"{cur} {word}"
        else:
            chunks.append(cur)
            cur = word
    if cur:
        chunks.append(cur)
    return chunks


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks of at most ``max_chars`` characters.

    Prefers sentence boundaries; falls back to word-boundary hard splits for
    sentences longer than the limit.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current = ""
    for sentence in split_sentences(text):
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(sentence, max_chars))
            continue
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


# ─── Engine registry ──────────────────────────────────────────────────────────

_ENGINE_REGISTRY: dict[str, type["TTSEngine"]] = {}
_GLOBAL_MANAGER: Any = None  # Audio8ServiceManager | None (set by server)


def register_engine(engine_id: str):
    """Class decorator registering a TTS engine in the global registry."""

    def decorator(cls):
        _ENGINE_REGISTRY[engine_id] = cls
        return cls

    return decorator


def set_audio8_manager(manager: Any) -> None:
    """Make an Audio8 service manager available to any Audio8Engine."""
    global _GLOBAL_MANAGER
    _GLOBAL_MANAGER = manager


def engine_ids() -> list[str]:
    return list(_ENGINE_REGISTRY.keys())


# ─── Base engine ──────────────────────────────────────────────────────────────


class TTSEngine:
    """Strict TTS engine interface.

    Subclasses implement :meth:`synthesize` (raw audio in, raw audio out)
    and :meth:`list_voices`. Everything else — chunking, speed/pitch DSP,
    format conversion, file saving, resource tracking — is handled by this
    base class, so every engine behaves identically from the caller side.
    """

    name = "base"
    is_online = False
    #: True when this engine can fetch its own model files on demand.
    is_downloadable = False
    #: If True, list_voices() is deferred to the UI (heavy daemon-backed).
    lazy_list_voices = False
    #: Longest text a single backend call can handle; longer input is
    #: split into sentence chunks automatically. Default: effectively
    #: unlimited (chunking disabled).
    max_text_chars = 100_000
    # If True, the engine applies speed natively and the base class should
    # skip librosa time-stretching to avoid double-processing.
    native_speed = False
    native_pitch = False
    # Schema of engine-specific parameters exposed via the API.
    # Each entry maps param name → dict with: type, default, min, max, step,
    # label, help text, group ("main" or "advanced"), and optionally options
    # for string selects.
    engine_params: dict[str, dict] = {}
    #: If True, generate()/preview_voice() hold a per-instance lock for their
    #: whole run. Required for engines that mutate shared load state in
    #: synthesize() (e.g. Piper swaps voice models); harmless to leave off
    #: for stateless/remote engines.
    serialize_generation = False

    def __init__(self, output_dir: str = "output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self._engine_lock = threading.Lock()
        # Engine is "born" now: the idle timer starts at creation, so a
        # freshly adopted daemon isn't instantly unloaded by the monitor.
        self.last_used = time.time()
        self.active_requests = 0

    def _serialization(self):
        """Context manager serializing generation when the engine needs it."""
        return self._engine_lock if self.serialize_generation else nullcontext()

    def _param(self, params: dict | None, name: str) -> Any:
        """Read an engine param, falling back to its ``engine_params`` default.

        Keeps the call-site defaults and the API-exposed schema from drifting
        apart: the schema is the single source of truth.
        """
        spec = self.engine_params.get(name, {})
        value = (params or {}).get(name)
        if value is None:
            value = spec.get("default")
        ptype = spec.get("type")
        try:
            if ptype == "int" and value is not None:
                return int(value)
            if ptype == "float" and value is not None:
                return float(value)
            if ptype == "bool":
                return bool(value)
        except (TypeError, ValueError):
            return spec.get("default")
        return value

    # ── resource tracking (used by the server's auto-unload monitor) ──

    def touch(self) -> None:
        self.last_used = time.time()

    def acquire(self) -> None:
        self.active_requests += 1
        self.touch()

    def release(self) -> None:
        self.active_requests = max(0, self.active_requests - 1)

    @property
    def busy(self) -> bool:
        return self.active_requests > 0

    @property
    def loaded(self) -> bool:
        return False

    def can_unload(self) -> bool:
        return self.loaded

    def unload(self) -> None:
        """Release model memory (RAM) held by this engine."""

    def memory_mb(self) -> float | None:
        """Live estimate of memory held by the model, in MB (or None)."""
        return None

    def is_installed(self) -> bool:
        return False

    def requirement_mb(self) -> int | None:
        """Minimum RAM (MB) needed to run this engine's default model."""
        return None

    # ── strict interface: subclasses implement these two ──

    def synthesize(self, text: str, voice: str = "", **params) -> tuple[np.ndarray, int]:
        """Synthesize ``text`` with ``voice``; return (audio, sample_rate).

        Subclasses may accept additional keyword arguments via **params.
        Must be synchronous. Raises FileNotFoundError/ValueError/RuntimeError
        for missing models or generation failures.
        """
        raise NotImplementedError

    def list_voices(self) -> list[str]:
        """Return available voice names for this engine."""
        raise NotImplementedError

    # ── shared pipeline ──

    def _synthesize_chunked(self, chunks: list[str], voice: str, **params) -> tuple[np.ndarray, int]:
        """Synthesize each chunk and concatenate with a short pause."""
        audio_parts: list[np.ndarray] = []
        sr = 0
        for idx, chunk in enumerate(chunks):
            part_audio, part_sr = self.synthesize(chunk, voice, **params)
            sr = part_sr
            audio_parts.append(part_audio)
            if idx < len(chunks) - 1:
                audio_parts.append(np.zeros(max(1, int(CHUNK_GAP_SECONDS * part_sr)), dtype=part_audio.dtype))
        return np.concatenate(audio_parts), sr

    def generate(
        self,
        text: str,
        voice: str = "",
        speed: float = 1.0,
        pitch: float = 0.0,
        fmt: str = "wav",
        params: dict | None = None,
    ) -> tuple[str, float]:
        """Generate audio. Extra engine-specific params can be passed via ``params``."""
        self.touch()
        text = text.strip()
        if not text:
            raise ValueError("Text is empty")
        with self._serialization():
            if len(text) > self.max_text_chars:
                chunks = chunk_text(text, self.max_text_chars)
                audio, sr = self._synthesize_chunked(chunks, voice, **(params or {}))
            else:
                audio, sr = self.synthesize(text, voice, **(params or {}))

            if not self.native_speed and speed != 1.0:
                audio = _time_stretch(audio, speed)
            if not self.native_pitch and abs(pitch) > 0.01:
                audio = self._apply_pitch_shift(audio, sr, pitch)

            filepath, duration = self._save_audio(audio, sr, "wav")
        if fmt != "wav":
            filepath = self._convert_format(filepath, sr, fmt)
        return filepath, duration

    def preview_voice(self, voice_name: str, text: str = "Hello, this is a voice preview.", **params) -> tuple[np.ndarray, int]:
        with self._serialization():
            return self.synthesize(text, voice_name, **params)

    def get_engine_id(self) -> str:
        return self.name

    # ── shared helpers ──

    def _save_audio(self, audio: np.ndarray, sr: int, fmt: str = "wav") -> tuple[str, float]:
        file_id = uuid.uuid4().hex[:12]
        ext = fmt if fmt in ("wav", "flac") else "wav"
        filepath = self.output_dir / f"tts_{file_id}.{ext}"
        sf.write(str(filepath), audio, sr)
        duration = len(audio) / sr
        return str(filepath), duration

    def _apply_pitch_shift(self, audio: np.ndarray, sr: int, n_steps: float) -> np.ndarray:
        if abs(n_steps) < 0.01:
            return audio
        try:
            import librosa
            return librosa.effects.pitch_shift(y=audio, sr=sr, n_steps=n_steps)
        except ImportError:
            # librosa missing (core-only install): return audio unchanged
            # rather than failing the whole generation.
            logger.warning("librosa not installed — pitch shift skipped")
            return audio

    def _convert_format(self, filepath: str, sr: int, fmt: str) -> str:
        if fmt == "wav":
            return filepath
        if fmt == "mp3":
            from pydub import AudioSegment
            new_path = filepath.rsplit(".", 1)[0] + ".mp3"
            AudioSegment.from_wav(filepath).export(new_path, format="mp3")
            os.remove(filepath)
            return new_path
        if fmt == "flac":
            audio, sr = sf.read(filepath)
            new_path = filepath.rsplit(".", 1)[0] + ".flac"
            sf.write(new_path, audio, sr)
            os.remove(filepath)
            return new_path
        return filepath

    def _file_mb(self, *paths: Path) -> float:
        total = 0.0
        for p in paths:
            if p.exists():
                total += p.stat().st_size
        return total / (1024 * 1024)


# ─── Piper ────────────────────────────────────────────────────────────────────


@register_engine("piper")
class PiperEngine(TTSEngine):
    # Piper supports native volume and noise control via SynthesisConfig.
    # Speed is handled by the base class (librosa) to remain consistent with
    # the global speed slider; advanced users may set ``length_scale`` via
    # params for native speed control instead.
    serialize_generation = True  # synthesize() swaps shared voice models
    engine_params = {
        "speaker_id": {
            "type": "int",
            "default": None,
            "min": 0,
            "max": 9999,
            "label": "Speaker ID",
            "help": "Index of the speaker to use (multi-speaker models only).",
            "group": "advanced",
        },
        "noise_scale": {
            "type": "float",
            "default": None,
            "min": 0.0,
            "max": 2.0,
            "step": 0.01,
            "label": "Noise scale",
            "help": "Controls the randomness of synthesis. Higher = more breathy.",
            "group": "advanced",
        },
        "noise_w_scale": {
            "type": "float",
            "default": None,
            "min": 0.0,
            "max": 2.0,
            "step": 0.01,
            "label": "Noise W scale",
            "help": "Controls phoneme duration variation.",
            "group": "advanced",
        },
        "volume": {
            "type": "float",
            "default": 1.0,
            "min": 0.0,
            "max": 2.0,
            "step": 0.05,
            "label": "Volume",
            "help": "Output gain multiplier.",
            "group": "main",
        },
        "normalize_audio": {
            "type": "bool",
            "default": True,
            "label": "Normalize audio",
            "help": "Apply loudness normalization to output.",
            "group": "advanced",
        },
    }

    def __init__(self, output_dir: str = "output"):
        super().__init__(output_dir)
        self.name = "piper"
        self.is_downloadable = True
        self._voice: Any = None  # PiperVoice | None
        self._current_model_path = ""
        self._sr = 22050

    def _voices_dir(self) -> Path:
        return MODELS_DIR / "piper"

    def list_voices(self) -> list[str]:
        models_dir = self._voices_dir()
        if not models_dir.exists():
            return []
        voices = []
        for f in models_dir.iterdir():
            if f.suffix == ".onnx":
                voices.append(f.stem)
        return sorted(voices)

    def is_installed(self) -> bool:
        return len(self.list_voices()) > 0

    def requirement_mb(self) -> int:
        # Medium-quality Piper voices are ~60–120 MB on disk; runtime adds
        # espeak-ng + onnx overhead.
        return 300

    def _load_voice(self, voice_name: str) -> None:
        import piper
        model_path = self._voices_dir() / f"{voice_name}.onnx"
        config_path = self._voices_dir() / f"{voice_name}.onnx.json"
        if not model_path.exists():
            raise FileNotFoundError(f"Piper model not found: {model_path}")
        if not config_path.exists():
            raise FileNotFoundError(f"Piper config not found: {config_path}")
        self._voice = piper.PiperVoice.load(model_path=str(model_path), config_path=str(config_path))
        self._current_model_path = voice_name
        self._sr = self._voice.config.sample_rate

    @property
    def loaded(self) -> bool:
        return self._voice is not None

    def can_unload(self) -> bool:
        return self._voice is not None

    def unload(self) -> None:
        self._voice = None
        self._current_model_path = ""
        gc.collect()

    def memory_mb(self) -> float | None:
        if not self._current_model_path:
            return None
        model_path = self._voices_dir() / f"{self._current_model_path}.onnx"
        return round(self._file_mb(model_path) * 1.2, 1)

    def synthesize(self, text: str, voice: str = "", **params) -> tuple[np.ndarray, int]:
        if not voice:
            voices = self.list_voices()
            if not voices:
                raise RuntimeError("No Piper voices found")
            voice = voices[0]
        if voice != self._current_model_path:
            self._load_voice(voice)

        from piper.config import SynthesisConfig
        # Build SynthesisConfig only for params that are explicitly set
        syn_config = SynthesisConfig(
            speaker_id=params.get("speaker_id"),
            noise_scale=params.get("noise_scale"),
            noise_w_scale=params.get("noise_w_scale"),
            volume=float(params.get("volume", 1.0)),
            normalize_audio=bool(params.get("normalize_audio", True)),
        )
        chunks = list(self._voice.synthesize(text, syn_config=syn_config))  # type: ignore[union-attr]
        audio = np.concatenate([c.audio_float_array for c in chunks])
        return audio, self._sr


# ─── Kokoro ───────────────────────────────────────────────────────────────────

_ESPEAK_LIB_CANDIDATES = [
    "/usr/lib64/libespeak-ng.so.1",                 # Fedora / RHEL / openSUSE
    "/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1",  # Debian / Ubuntu (x86_64)
    "/usr/lib/aarch64-linux-gnu/libespeak-ng.so.1",  # Debian / Ubuntu (arm64)
    "/usr/lib/libespeak-ng.so.1",                   # generic
]


def _find_espeak_lib() -> str | None:
    """Locate libespeak-ng.so.1 across common distro paths.

    Returns None when nothing is found so kokoro_onnx falls back to the
    system library search (ldconfig).
    """
    for path in _ESPEAK_LIB_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


_ESPEAK_DATA_CANDIDATES = [
    "/usr/share/espeak-ng-data",            # standard across distros
    "/usr/lib/espeak-ng-data",              # some Debian splits
    "/usr/local/share/espeak-ng-data",      # source installs
    "/opt/homebrew/share/espeak-ng-data",   # macOS (Homebrew)
]


def _find_espeak_data() -> str | None:
    """Locate the espeak-ng data directory across common install paths."""
    for path in _ESPEAK_DATA_CANDIDATES:
        if os.path.isdir(path):
            return path
    return None


@register_engine("kokoro")
class KokoroEngine(TTSEngine):
    # Kokoro accepts speed natively, so skip the base-class librosa stretch.
    native_speed = True
    serialize_generation = True  # lazy model load in synthesize()

    engine_params = {
        "speed": {
            "type": "float",
            "default": 1.0,
            "min": 0.25,
            "max": 4.0,
            "step": 0.05,
            "label": "Speed",
            "help": "Speech speed multiplier (native; supersedes the global speed slider).",
            "group": "main",
        },
        "lang": {
            "type": "str",
            "default": "en-us",
            "options": ["en-us", "en-gb", "de", "fr", "ja", "zh"],
            "label": "Language",
            "help": "Language code for the model.",
            "group": "main",
        },
        "is_phonemes": {
            "type": "bool",
            "default": False,
            "label": "Input is phonemes",
            "help": "If True, treat input text as phoneme sequence instead of raw text.",
            "group": "advanced",
        },
        "trim": {
            "type": "bool",
            "default": True,
            "label": "Trim silence",
            "help": "Trim leading/trailing silence from output.",
            "group": "main",
        },
    }

    def __init__(self, output_dir: str = "output"):
        super().__init__(output_dir)
        self.name = "kokoro"
        self.is_downloadable = True
        self._kokoro: Any = None  # Kokoro | None
        self._voices: list[str] = []

    def _model_dir(self) -> Path:
        return MODELS_DIR / "kokoro"

    def list_voices(self) -> list[str]:
        try:
            self._ensure_loaded()
        except FileNotFoundError:
            pass
        return self._voices

    def is_installed(self) -> bool:
        model_path = self._model_dir() / "kokoro-v1.0.onnx"
        voices_path = self._model_dir() / "voices-v1.0.bin"
        return model_path.exists() and voices_path.exists()

    def requirement_mb(self) -> int:
        return 900

    def _ensure_loaded(self) -> None:
        if self._kokoro is not None:
            return
        model_path = self._model_dir() / "kokoro-v1.0.onnx"
        voices_path = self._model_dir() / "voices-v1.0.bin"

        if not model_path.exists():
            raise FileNotFoundError(f"Kokoro model not found at {model_path}")
        if not voices_path.exists():
            raise FileNotFoundError(f"Kokoro voices file not found at {voices_path}")

        # Patch phonemizer 3.2.1+ compatibility (removed EspeakWrapper.set_data_path)
        from phonemizer.backend.espeak.wrapper import EspeakWrapper
        if not hasattr(EspeakWrapper, "set_data_path"):
            EspeakWrapper.set_data_path = classmethod(lambda cls, path: None)

        from kokoro_onnx import Kokoro
        from kokoro_onnx.tokenizer import EspeakConfig
        espeak_config = EspeakConfig(
            data_path=_find_espeak_data(),
            lib_path=_find_espeak_lib(),
        )
        self._kokoro = Kokoro(str(model_path), str(voices_path), espeak_config=espeak_config)
        raw = self._kokoro.get_voices()
        if isinstance(raw, dict):
            self._voices = list(raw.keys())
        else:
            self._voices = list(raw)

    @property
    def loaded(self) -> bool:
        return self._kokoro is not None

    def can_unload(self) -> bool:
        return self._kokoro is not None

    def unload(self) -> None:
        self._kokoro = None
        gc.collect()

    def memory_mb(self) -> float | None:
        if self._kokoro is None:
            return None
        model_path = self._model_dir() / "kokoro-v1.0.onnx"
        voices_path = self._model_dir() / "voices-v1.0.bin"
        return round(self._file_mb(model_path, voices_path) * 1.2, 1)

    def synthesize(self, text: str, voice: str = "", **params) -> tuple[np.ndarray, int]:
        self._ensure_loaded()
        if not voice:
            voice = self._voices[0] if self._voices else "af"
        samples, sr = self._kokoro.create(
            text,
            voice=voice,
            speed=float(params.get("speed", 1.0)),
            lang=str(params.get("lang", "en-us")),
            is_phonemes=bool(params.get("is_phonemes", False)),
            trim=bool(params.get("trim", True)),
        )  # type: ignore[union-attr]
        return samples, sr


# ─── Kitten TTS ─────────────────────────────────────────────────────────────


@register_engine("kitten-tts")
class KittenTTSEngine(TTSEngine):
    native_speed = True
    serialize_generation = True  # lazy model load in synthesize()

    engine_params = {
        "speed": {
            "type": "float",
            "default": 1.0,
            "min": 0.5,
            "max": 2.0,
            "step": 0.05,
            "label": "Speed",
            "help": "Speech speed multiplier.",
            "group": "main",
        },
        "clean_text": {
            "type": "bool",
            "default": True,
            "label": "Clean text",
            "help": "Preprocess text (expand numbers, currencies, etc.).",
            "group": "advanced",
        },
    }

    def __init__(self, output_dir: str = "output"):
        super().__init__(output_dir)
        self.name = "kitten-tts"
        self.is_downloadable = True
        self._model: Any = None
        self._voices: list[str] = [
            "Bella", "Jasper", "Luna", "Bruno",
            "Rosie", "Hugo", "Kiki", "Leo",
        ]

    def _model_dir(self) -> Path:
        return MODELS_DIR / "kitten-tts"

    def list_voices(self) -> list[str]:
        return list(self._voices)

    def is_installed(self) -> bool:
        onnx = self._model_dir() / "kitten_tts_mini_v0_8.onnx"
        npz = self._model_dir() / "voices.npz"
        return onnx.exists() and npz.exists()

    def requirement_mb(self) -> int:
        return 80

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        onnx = self._model_dir() / "kitten_tts_mini_v0_8.onnx"
        npz = self._model_dir() / "voices.npz"
        if not onnx.exists() or not npz.exists():
            raise FileNotFoundError(
                "Kitten TTS model not found. Download it via Models → Download."
            )
        import kittentts
        self._model = kittentts.KittenTTS(str(onnx), cache_dir=str(self._model_dir()))

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def can_unload(self) -> bool:
        return self._model is not None

    def unload(self) -> None:
        self._model = None
        gc.collect()

    def memory_mb(self) -> float | None:
        if self._model is None:
            return None
        onnx = self._model_dir() / "kitten_tts_mini_v0_8.onnx"
        npz = self._model_dir() / "voices.npz"
        return round(self._file_mb(onnx, npz) * 1.2, 1)

    def synthesize(self, text: str, voice: str = "", **params) -> tuple[np.ndarray, int]:
        self._ensure_loaded()
        if not voice:
            voice = "Jasper"
        audio = self._model.generate(
            text,
            voice=voice,
            speed=float(params.get("speed", 1.0)),
            clean_text=bool(params.get("clean_text", True)),
        )
        return audio, 24000


# ─── Audio8 (daemon-backed) ───────────────────────────────────────────────────


@register_engine("audio8")
class Audio8Engine(TTSEngine):
    # The daemon caps a single synthesis request at 1000 chars.
    max_text_chars = 900
    # The daemon is a persistent background service (1.6 GB model). Restarting
    # it costs 60-90s of model reload, so the idle auto-unload monitor must
    # leave it alone.
    auto_unload = False

    engine_params = {
        "seed": {
            "type": "int",
            "default": 42,
            "min": 0,
            "label": "Seed",
            "help": "Random seed for deterministic synthesis. Use the same seed to reproduce output.",
            "group": "advanced",
        },
        "temperature": {
            "type": "float",
            "default": 0.3,
            "min": 0.05,
            "max": 2.0,
            "step": 0.05,
            "label": "Temperature",
            "help": "Controls randomness: lower = more deterministic, higher = more variation.",
            "group": "main",
        },
        "top_p": {
            "type": "float",
            "default": 0.9,
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
            "label": "Top P",
            "help": "Nucleus sampling threshold: only tokens within cumulative prob top_p are considered.",
            "group": "advanced",
        },
        "top_k": {
            "type": "int",
            "default": 50,
            "min": 1,
            "max": 4096,
            "label": "Top K",
            "help": "Only sample from the top K most likely next tokens.",
            "group": "advanced",
        },
        "max_new_tokens": {
            "type": "int",
            "default": 1024,
            "min": 16,
            "max": 2048,
            "label": "Max new tokens",
            "help": "Maximum number of audio tokens to generate (longer = longer output).",
            "group": "main",
        },
    }

    def __init__(self, output_dir: str = "output", manager=None):
        super().__init__(output_dir)
        self.name = "audio8"
        self.is_downloadable = True
        self.lazy_list_voices = True
        self._manager = manager or _GLOBAL_MANAGER
        self._default_voice: str | None = None
        self._default_voice_at = 0.0

    @property
    def _url(self) -> str:
        return f"http://127.0.0.1:{self._manager.port}" if self._manager else ""

    def _default_voice_name(self) -> str:
        """Pick a voice registered with the daemon, cached for 5 minutes.

        Falls back to "Ryan gosling" only when the daemon is unreachable or
        no voices are registered yet (fresh install). The UI always sends an
        explicit voice, so this only affects bare API calls.
        """
        now = time.monotonic()
        if self._default_voice is None or now - self._default_voice_at > 300:
            try:
                voices = self.list_voices()
                self._default_voice = voices[0] if voices else "Ryan gosling"
            except Exception:
                self._default_voice = "Ryan gosling"
            self._default_voice_at = now
        return self._default_voice

    def _get_client(self):
        if not self._manager:
            raise RuntimeError("Audio8Engine requires an Audio8ServiceManager")
        if not self._manager.is_running() and not self._manager.start():
            raise RuntimeError("Audio8 service failed to start")
        return self._manager

    @property
    def is_online(self) -> bool:  # type: ignore[override]
        # Dynamic: reflects whether the daemon is actually running, unlike the
        # static class-level flag used by online engines (edge-tts, gtts).
        return bool(self._manager and self._manager.is_running())

    @property
    def loaded(self) -> bool:
        return self.is_online

    def can_unload(self) -> bool:
        return self.loaded

    def unload(self) -> None:
        if self._manager:
            self._manager.stop()

    def memory_mb(self) -> float | None:
        if not self._manager or not self._manager.is_running():
            return None
        try:
            resp = self._manager.get(f"{self._url}/api/system", timeout=5)
            stats = resp.json()
            return round(stats["memory"].get("current_mb") or 0.0, 1)  # type: ignore[arg-type]
        except Exception:
            return None

    def is_installed(self) -> bool:
        from model_downloader import is_audio8_installed
        return is_audio8_installed()

    def requirement_mb(self) -> int:
        return 1600

    def list_voices(self) -> list[str]:
        if not self._manager:
            return []
        if not self._manager.is_running() and not self._manager.start():
            return []
        client = self._manager
        try:
            resp = client.get(f"{self._url}/api/voices", timeout=5)
            data = resp.json()
            voices = data.get("voices", [])
            return [v["name"] if isinstance(v, dict) else v for v in voices]
        except Exception:
            return []

    def synthesize(self, text: str, voice: str = "", **params) -> tuple[np.ndarray, int]:
        client = self._get_client()
        voice_name = voice or self._default_voice_name()
        # Defaults come from the engine_params schema (single source of truth)
        body: dict = {
            "text": text,
            "voice_name": voice_name,
            "temperature": self._param(params, "temperature"),
            "top_p": self._param(params, "top_p"),
            "top_k": self._param(params, "top_k"),
            "seed": self._param(params, "seed"),
            "max_new_tokens": self._param(params, "max_new_tokens"),
        }
        resp = client.post(f"{self._url}/api/tts", json=body, timeout=120)
        resp.raise_for_status()
        audio, sr = sf.read(io.BytesIO(resp.content))
        return audio, sr


# ─── edge-tts (online) ────────────────────────────────────────────────────────


@register_engine("edge-tts")
class EdgeTTSClient(TTSEngine):
    name = "edge-tts"
    is_online = True
    max_text_chars = 2000  # service caps a single request

    def __init__(self, output_dir: str = "output"):
        super().__init__(output_dir)
        self._voices: list[str] | None = None

    def list_voices(self) -> list[str]:
        if self._voices is None:
            import asyncio

            import edge_tts

            async def _fetch():
                voices = await edge_tts.list_voices()
                return sorted([v["ShortName"] for v in voices])

            self._voices = asyncio.run(_fetch())
        return self._voices

    def synthesize(self, text: str, voice: str = "", **params) -> tuple[np.ndarray, int]:
        import asyncio
        import io

        import edge_tts

        async def _run():
            if not voice:
                voices = await edge_tts.list_voices()
                selected = voices[0]["ShortName"] if voices else "en-US-BrianMultilingualNeural"
            else:
                selected = voice
            communicate = edge_tts.Communicate(text, selected)
            buffer = io.BytesIO()
            await communicate.save(buffer)  # type: ignore[arg-type]
            return buffer.getvalue()

        data = asyncio.run(_run())
        audio, sr = sf.read(io.BytesIO(data))
        return audio, sr


# ─── gTTS (online) ────────────────────────────────────────────────────────────


@register_engine("gtts")
class GTTSClient(TTSEngine):
    name = "gtts"
    is_online = True
    max_text_chars = 3000

    def __init__(self, output_dir: str = "output"):
        super().__init__(output_dir)

    def list_voices(self) -> list[str]:
        return ["google-en"]

    def synthesize(self, text: str, voice: str = "", **params) -> tuple[np.ndarray, int]:
        import io

        from gtts import gTTS
        tts = gTTS(text=text.strip(), lang="en")
        buffer = io.BytesIO()
        tts.save(buffer)
        buffer.seek(0)
        audio, sr = sf.read(buffer)
        return audio, sr


# ─── Factory ──────────────────────────────────────────────────────────────────


def create_engine(engine_id: str, output_dir: str = "output", audio8_manager=None) -> TTSEngine:
    """Create an engine instance from the global registry."""
    cls = _ENGINE_REGISTRY.get(engine_id)
    if cls is None:
        raise ValueError(f"Unknown engine: {engine_id}. Choose from: {engine_ids()}")
    if issubclass(cls, Audio8Engine):
        return cls(output_dir, manager=audio8_manager)  # type: ignore[call-arg]
    return cls(output_dir)


def get_all_engines() -> list[dict]:
    """Return metadata for all registered engines."""
    engines = []
    for eid in engine_ids():
        try:
            eng = create_engine(eid, str(BASE_DIR / "output"))
            if getattr(eng, "lazy_list_voices", False):
                # Daemon-backed engines: don't boot the daemon while listing.
                voices: list[str] = []
            else:
                try:
                    voices = eng.list_voices()
                except Exception:
                    voices = []
            engines.append({
                "id": eid,
                "name": eid.capitalize().replace("Tts", " TTS").replace(" ", ""),
                "voices": voices,
                "is_online": eng.is_online,
                "is_downloadable": eng.is_downloadable,
                "installed": eng.is_installed(),
                "requirement_mb": eng.requirement_mb(),
                "voice_count": len(voices),
            })
        except Exception as exc:
            engines.append({
                "id": eid,
                "name": eid.capitalize().replace("Tts", " TTS").replace(" ", ""),
                "voices": [],
                "is_online": eid in ("edge-tts", "gtts"),
                "is_downloadable": eid in ("piper", "kokoro", "audio8"),
                "installed": False,
                "requirement_mb": None,
                "voice_count": 0,
                "error": str(exc) or "unavailable",
            })
    return engines