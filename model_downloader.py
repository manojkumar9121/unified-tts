"""On-demand model downloader for Piper / Kokoro / Audio8.

Downloads model files from Hugging Face in a background thread with progress
tracking. Uses stdlib ``urllib`` only — no extra dependencies.

Engines:
* ``piper``: individual voice files from ``rhasspy/piper-voices``
* ``kokoro``: the kokoro-v1.0.onnx + voices-v1.0.bin pair from ``hexgrad/kokoro``
* ``audio8``: full model snapshot of ``Audio8/Audio8-TTS-Preview-0.6B-ONNX-INT4``
  (resolved via the Hugging Face tree API)
"""

from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Callable
from functools import partial
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
MODELS_DIR = BASE_DIR / "models"
AUDIO8_MODEL_DIR = BASE_DIR / "audio8_models"

HF_BASE = "https://huggingface.co"
PIPER_REPO = "rhasspy/piper-voices"
KOKORO_REPO = "hexgrad/kokoro"
AUDIO8_REPO = "Audio8/Audio8-TTS-Preview-0.6B-ONNX-INT4"
KITTEN_TTS_REPO = "KittenML/kitten-tts-mini-0.8"

_PIPER_VOICE_PATH = {
    "en_US-lessac-medium": ("en/en_US/lessac/medium", 63),
    "en_US-lessac-high": ("en/en_US/lessac/high", 121),
    "en_US-amy-medium": ("en/en_US/amy/medium", 63),
    "en_US-john-medium": ("en/en_US/john/medium", 63),
    "en_US-ryan-medium": ("en/en_US/ryan/medium", 63),
    "en_US-ryan-high": ("en/en_US/ryan/high", 121),
    "en_US-ljspeech-medium": ("en/en_US/ljspeech/medium", 63),
    "en_GB-alba-medium": ("en/en_GB/alba/medium", 71),
    # Telugu (te_IN) — local ONNX voices from rhasspy/piper-voices
    "te_IN-venkatesh-medium": ("te/te_IN/venkatesh/medium", 63),
    "te_IN-maya-medium": ("te/te_IN/maya/medium", 63),
    "te_IN-padmavathi-medium": ("te/te_IN/padmavathi/medium", 63),
}
PIPER_VOICES = sorted(_PIPER_VOICE_PATH.keys())

KOKORO_FILES = {
    "kokoro-v1.0.onnx": 337,
    "voices-v1.0.bin": 84,
}

KITTEN_TTS_FILES = {
    "kitten_tts_mini_v0_8.onnx": 78,
    "voices.npz": 3,
}


# ─── Installed checks ─────────────────────────────────────────────────────────


def is_piper_voice_installed(voice: str) -> bool:
    return (MODELS_DIR / "piper" / f"{voice}.onnx").exists()


def is_kokoro_installed() -> bool:
    return (
        (MODELS_DIR / "kokoro" / "kokoro-v1.0.onnx").exists()
        and (MODELS_DIR / "kokoro" / "voices-v1.0.bin").exists()
    )


def is_audio8_installed() -> bool:
    return (AUDIO8_MODEL_DIR / "runtime_manifest.json").exists()


def is_kitten_tts_installed() -> bool:
    kitten_dir = MODELS_DIR / "kitten-tts"
    return (kitten_dir / "kitten_tts_mini_v0_8.onnx").exists() and \
           (kitten_dir / "voices.npz").exists()


def is_installed(engine_id: str, voice: str = "") -> bool:
    if engine_id == "piper":
        return is_piper_voice_installed(voice or "en_US-lessac-medium")
    if engine_id == "kokoro":
        return is_kokoro_installed()
    if engine_id == "audio8":
        return is_audio8_installed()
    if engine_id == "kitten-tts":
        return is_kitten_tts_installed()
    return False


# ─── Download state ───────────────────────────────────────────────────────────

_lock = threading.Lock()
_tasks: dict[str, dict] = {}


def _new_task() -> dict:
    return {
        "state": "idle",  # idle | downloading | done | error
        "percent": 0.0,
        "message": "",
        "error": None,
        "voice": "",
    }


def get_status(engine_id: str) -> dict:
    with _lock:
        task = _tasks.get(engine_id)
        if task is None:
            return _new_task()
        return dict(task)


def _update(engine_id: str, **kwargs) -> None:
    with _lock:
        task = _tasks.setdefault(engine_id, _new_task())
        task.update(kwargs)


# ─── HTTP helpers ─────────────────────────────────────────────────────────────


def _fetch_json(url: str, timeout: float = 30.0) -> list[dict] | dict:
    req = urllib.request.Request(url, headers={"User-Agent": "unified-tts/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _audio8_file_list() -> list[dict]:
    """Resolve the recursive file list of the Audio8 ONNX repo."""
    url = f"{HF_BASE}/api/models/{AUDIO8_REPO}/tree/main?recursive=true"
    data = _fetch_json(url)
    files = [(item["path"], item.get("size", 0)) for item in data if item.get("type") == "file"]
    return [{"path": path, "size": size} for path, size in files]


def _download_to_file(url: str, dest: Path, progress: Callable[[int, int], None]) -> None:
    """Stream ``url`` to ``dest`` (atomically via a .part temp file)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "unified-tts/1.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        transferred = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            transferred += len(chunk)
            if total:
                progress(transferred, total)
    tmp.rename(dest)
    progress(1, 1)


# ─── Download jobs ────────────────────────────────────────────────────────────


def start_download(engine_id: str, voice: str = "") -> dict:
    """Start a background download; returns the initial status."""
    with _lock:
        existing = _tasks.get(engine_id)
        if existing and existing["state"] == "downloading":
            return dict(existing)

    if engine_id == "piper":
        target = partial(_download_piper, voice)
    elif engine_id == "kokoro":
        target = partial(_download_kokoro)
    elif engine_id == "audio8":
        target = partial(_download_audio8)
    elif engine_id == "kitten-tts":
        target = partial(_download_kitten_tts)
    else:
        raise ValueError(f"No downloadable models for engine: {engine_id}")

    thread = threading.Thread(target=target, name=f"model-download-{engine_id}", daemon=True)
    thread.start()
    return get_status(engine_id)


def _download_piper(voice: str = "") -> None:
    voice = voice or "en_US-lessac-medium"
    if voice not in _PIPER_VOICE_PATH:
        _update("piper", state="error", error=f"Unknown Piper voice: {voice}")
        return
    rel_path, est_mb = _PIPER_VOICE_PATH[voice]
    dest_dir = MODELS_DIR / "piper"
    files = [f"{voice}.onnx", f"{voice}.onnx.json"]
    total_bytes = est_mb * 1024 * 1024
    _update("piper", state="downloading", voice=voice, message=f"Preparing {voice}", percent=0.0, error=None)

    downloaded = 0

    def progress(inc: int, _total: int) -> None:
        nonlocal downloaded
        downloaded += inc
        pct = min(99.0, downloaded / total_bytes * 100)
        _update("piper", percent=pct, message=f"Downloading {voice} … {pct:.0f}%")

    try:
        for fname in files:
            url = f"{HF_BASE}/{PIPER_REPO}/resolve/main/{rel_path}/{fname}"
            _download_to_file(url, dest_dir / fname, progress)
            _update("piper", message=f"Saved {fname}")
        _update("piper", state="done", percent=100.0, message=f"Installed Piper voice {voice}")
    except Exception as exc:
        _update("piper", state="error", error=f"Piper download failed: {exc}")


def _download_kokoro() -> None:
    dest_dir = MODELS_DIR / "kokoro"
    total_bytes = sum(size * 1024 * 1024 for size in KOKORO_FILES.values())
    _update("kokoro", state="downloading", message="Preparing Kokoro model", percent=0.0, error=None)

    downloaded = 0

    def progress(inc: int, _total: int) -> None:
        nonlocal downloaded
        downloaded += inc
        pct = min(99.0, downloaded / total_bytes * 100)
        _update("kokoro", percent=pct, message=f"Downloading Kokoro … {pct:.0f}%")

    try:
        for fname in KOKORO_FILES:
            url = f"{HF_BASE}/{KOKORO_REPO}/resolve/main/{fname}"
            _download_to_file(url, dest_dir / fname, progress)
            _update("kokoro", message=f"Saved {fname}")
        _update("kokoro", state="done", percent=100.0, message="Installed Kokoro model")
    except Exception as exc:
        _update("kokoro", state="error", error=f"Kokoro download failed: {exc}")


def _download_kitten_tts() -> None:
    dest_dir = MODELS_DIR / "kitten-tts"
    total_bytes = sum(size * 1024 * 1024 for size in KITTEN_TTS_FILES.values())
    _update("kitten-tts", state="downloading", message="Preparing Kitten TTS model", percent=0.0, error=None)

    downloaded = 0

    def progress(inc: int, _total: int) -> None:
        nonlocal downloaded
        downloaded += inc
        pct = min(99.0, downloaded / total_bytes * 100)
        _update("kitten-tts", percent=pct, message=f"Downloading Kitten TTS … {pct:.0f}%")

    try:
        for fname in KITTEN_TTS_FILES:
            url = f"{HF_BASE}/{KITTEN_TTS_REPO}/resolve/main/{fname}"
            _download_to_file(url, dest_dir / fname, progress)
            _update("kitten-tts", message=f"Saved {fname}")
        _update("kitten-tts", state="done", percent=100.0, message="Installed Kitten TTS model")
    except Exception as exc:
        _update("kitten-tts", state="error", error=f"Kitten TTS download failed: {exc}")


def _download_audio8() -> None:
    dest_dir = AUDIO8_MODEL_DIR
    _update("audio8", state="downloading", message="Resolving model files…", percent=1.0, error=None)
    try:
        files = _audio8_file_list()
    except Exception as exc:
        _update("audio8", state="error", error=f"Failed to list Audio8 files: {exc}")
        return
    total_bytes = sum(item["size"] for item in files)
    downloaded = 0

    def progress(inc: int, _total: int) -> None:
        nonlocal downloaded
        downloaded += inc
        pct = min(99.0, downloaded / total_bytes * 100)
        _update("audio8", percent=pct, message=f"Downloading Audio8 model … {pct:.0f}%")

    try:
        for item in files:
            url = f"{HF_BASE}/{AUDIO8_REPO}/resolve/main/{item['path']}"
            _download_to_file(url, dest_dir / item["path"], progress)
        _update("audio8", state="done", percent=100.0, message="Installed Audio8 model (start the engine to load it)")
    except Exception as exc:
        _update("audio8", state="error", error=f"Audio8 download failed: {exc}")


def download_spec(engine_id: str) -> dict | None:
    """Catalog info for the downloader UI."""
    if engine_id == "piper":
        return {"engine_id": "piper", "label": "Piper voice (ONNX)", "voices": PIPER_VOICES, "size_label": "~60–120 MB"}
    if engine_id == "kokoro":
        return {"engine_id": "kokoro", "label": "Kokoro model (ONNX)", "voices": [], "size_label": "~420 MB"}
    if engine_id == "audio8":
        return {"engine_id": "audio8", "label": "Audio8 model snapshot (ONNX INT4)", "voices": [], "size_label": "~970 MB"}
    if engine_id == "kitten-tts":
        return {"engine_id": "kitten-tts", "label": "Kitten TTS model (ONNX)", "voices": [], "size_label": "~80 MB"}
    return None