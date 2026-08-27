# AGENTS.md — Unified TTS

## What this is

A Python FastAPI web UI for multi-engine text-to-speech. Synthesis runs server-side; the client only sends text and plays back audio.

## Running

```bash
bash run.sh              # launches FastAPI on 127.0.0.1:8000 (TTS_HOST/TTS_PORT override)
python3 check_deps.py    # pre-flight dependency check
pytest                   # test suite (core deps only; no models needed)
```

`run.sh` activates `.venv` if present, else reuses an already-active
`$VIRTUAL_ENV`, else falls back to the system python3. It sets `PYTHONPATH`
to the repo root, suppresses UserWarning noise, and runs a dependency
check before starting the server.

The server binds loopback by default (security); set `TTS_HOST=0.0.0.0` to
expose it, and optionally `TTS_API_KEY` to require an `X-API-Key` header on
`/api/*` calls.

Audio8 daemon (separate FastAPI service) runs on port **8024** as a subprocess — never start it directly.

## Installing

The project uses dependency extras so you can install only what you need:

```bash
pip install -r requirements.txt[core]    # Web UI + online engines (minimal)
pip install -r requirements.txt[local]   # + local ONNX engines (Piper/Kokoro/Kitten TTS/Audio8)
pip install -r requirements.txt[online]  # + edge-tts, gTTS
pip install -r requirements.txt[audio]   # MP3/FLAC export (needs system ffmpeg)
pip install -r requirements.txt          # everything
```

**System packages** (only for local engines):
- `espeak-ng` — required by Kokoro
- `ffmpeg` — required for MP3 export

Run `python3 check_deps.py` to see exactly what's missing on your machine.

## Architecture

| File | Role |
|---|---|
| `server.py` | Main FastAPI app — routes, UI, model download API, hardware detection |
| `tts_engine.py` | Engine abstraction layer + 6 engine implementations |
| `audio8_manager.py` | Subprocess lifecycle manager for the Audio8 daemon |
| `model_downloader.py` | Background HuggingFace model downloader (no external deps) |
| `audio8_repo/` | Audio8 ONNX runtime (imported as `audio8_repo.service`) |
| `static/` + `templates/` | Thin-client web UI (vanilla JS, no build step) |

**Engine registry** — every engine subclasses `TTSEngine` and is decorated with `@register_engine("id")`.
Adding a new engine = subclass + one decorator line; the UI discovers engines from `/api/engines`.

### Engines

| ID | Type | In-process? | Requires model? |
|---|---|---|---|
| `kitten-tts` | Local ONNX | yes | yes (~80 MB) |
| `piper` | Local ONNX | yes | yes (~300 MB RAM) |
| `kokoro` | Local ONNX | yes | yes (~900 MB RAM) |
| `audio8` | Daemon-backed ONNX INT4 | no (subprocess on :8024) | yes (~1.6 GB RAM) |
| `edge-tts` | Online (Microsoft) | yes | no |
| `gtts` | Online (Google) | yes | no |

### Key directories

| Path | Contents |
|---|---|
| `models/piper/` | Piper `.onnx` + `.onnx.json` voice files |
| `models/kokoro/` | `kokoro-v1.0.onnx` + `voices-v1.0.bin` |
| `models/kitten-tts/` | `kitten_tts_mini_v0_8.onnx` + `voices.npz` |
| `audio8_models/` | Audio8 ONNX INT4 weights + tokenizer |
| `audio8_voices/` | Registered voice profiles for Audio8 |
| `audio8_repo/` | Audio8 ONNX runtime source (`service.py`, `runtime.py`, etc.) |
| `output/` | Generated audio files + batch zips |
| `data/generations.db` | SQLite history (WAL mode) |

## Adding a new engine

1. Subclass `TTSEngine` in `tts_engine.py`.
2. Decorate with `@register_engine("your_id")`.
3. Implement `synthesize(text, voice, **params)` → `(np.ndarray, sr)`.
4. Implement `list_voices()` → `list[str]`.
5. Set class-level flags: `is_online`, `is_downloadable`, `native_speed`, `engine_params`.
6. The UI picks it up automatically — no frontend change needed.

## Kokoro gotcha

Kokoro requires `phonemizer` which needs espeak-ng:
- Data: `/usr/share/espeak-ng-data` (standard across distros)
- Library: auto-detected via `_find_espeak_lib()` across common distro paths
  (Fedora `/usr/lib64`, Debian/Ubuntu `/usr/lib/x86_64-linux-gnu`, arm64),
  falling back to the system library search (ldconfig).

Phonemizer 3.2.1+ removed `EspeakWrapper.set_data_path`; patched at load time.

## Audio8 daemon lifecycle

The daemon is a separate FastAPI app started via `uvicorn audio8_repo.service:app` from
`audio8_manager.py`. Environment variables control it:

```
ARKTTS_MODEL_DIR      → audio8_models/
ARKTTS_VOICES_DIR     → audio8_voices/
ARKTTS_REGISTRATION_DIR → audio8_models/registration/
ARKTTS_PRECISION      = int4
ARKTTS_CODEC_PRECISION = fp16
ARKTTS_THREADS        = 5
```

Auto-unload: engines idle > `TTS_UNLOAD_IDLE_SECONDS` (default 300) are unloaded.
The Audio8 daemon is exempt (`auto_unload = False`) — it is a separate process and
restarting it costs 60–90s of model reload; it persists across server restarts.

## Model downloads

All from HuggingFace, no CLI dependency:
- Piper: `rhasspy/piper-voices` (individual voice .onnx + .json)
- Kokoro: `hexgrad/kokoro` (kokoro-v1.0.onnx + voices-v1.0.bin)
- Kitten TTS: `KittenML/kitten-tts-mini-0.8` (mini model + voices.npz)
- Audio8: `Audio8/Audio8-TTS-Preview-0.6B-ONNX-INT4` (full snapshot via tree API)

Downloads run in background threads with progress tracked per-engine.

## Type checking

```bash
mypy --ignore-missing-imports .
```

`mypy.ini` sets `ignore_missing_imports = True` and `warn_unused_ignores = False`.

## Testing & linting

```bash
pip install -e ".[dev]"   # pytest, httpx, ruff, mypy
pytest                    # tests/ — pure logic + API (TestClient); no models needed
ruff check .              # config in pyproject.toml ([tool.ruff])
```

Tests redirect the SQLite DB and output dir via `TTS_DATA_DIR`/`TTS_OUTPUT_DIR`
(set in `tests/conftest.py`), so they never touch real user data. CI
(`.github/workflows/ci.yml`) runs ruff + mypy + pytest.

## Constraints to remember

- Audio8 single-request text limit: 900 chars (split into chunks automatically).
- Piper pitch/speed handled by librosa in the base class; Kokoro speed is native.
- Audio output is always WAV internally; optional conversion to MP3/FLAC via pydub.
- `torch` is CPU-only (`2.12.0+cpu`) — no CUDA/MPS needed for any engine.
- Python 3.14.
