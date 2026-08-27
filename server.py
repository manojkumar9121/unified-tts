"""Unified TTS Web UI server — FastAPI backend.

Serves a thin-client web UI: all synthesis happens server-side (either
in-process for Piper/Kokoro or in the managed Audio8 background daemon),
the client only sends text and plays back audio.
"""

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar, Iterator, Literal

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tts_engine import (
    TTSEngine,
    create_engine, init_db, add_generation, get_generations,
    delete_generation, delete_generations_older_than, get_all_engines,
    engine_ids, set_audio8_manager, _ENGINE_REGISTRY,
)
from audio8_manager import Audio8ServiceManager
import model_downloader as md

logger = logging.getLogger("unified_tts.server")

init_db()

BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = Path(os.environ.get("TTS_OUTPUT_DIR", BASE_DIR / "output")).resolve()
OUTPUT_DIR.mkdir(exist_ok=True)

HOST = os.environ.get("TTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("TTS_PORT", "8000"))
# Optional shared-secret for /api/* endpoints. When unset the API is open
# (fine for localhost use); set TTS_API_KEY to require the ``X-API-Key``
# header on every API call when exposing the server beyond localhost.
API_KEY = os.environ.get("TTS_API_KEY", "")

AUDIO8_MODEL_DIR = BASE_DIR / "audio8_models"
AUDIO8_VOICES_DIR = BASE_DIR / "audio8_voices"
AUDIO8_REPO_DIR = BASE_DIR / "audio8_repo"
AUDIO8_PORT = int(os.environ.get("AUDIO8_PORT", "8024"))

audio8_manager = Audio8ServiceManager(
    model_dir=AUDIO8_MODEL_DIR,
    voices_dir=AUDIO8_VOICES_DIR,
    repo_dir=AUDIO8_REPO_DIR,
    port=AUDIO8_PORT,
)
set_audio8_manager(audio8_manager)

# Seconds an engine may sit idle before auto-unloading its model from
# memory (RAM) / stopping the Audio8 daemon.
IDLE_UNLOAD_SECONDS = int(os.environ.get("TTS_UNLOAD_IDLE_SECONDS", "300"))

app = FastAPI(title="Unified TTS")


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    """Require ``X-API-Key`` on /api/* when TTS_API_KEY is configured.

    The UI page, static assets and /output audio stay open so browser
    <audio> playback keeps working; only the JSON API is protected.
    """
    if API_KEY and request.url.path.startswith("/api/"):
        if request.headers.get("x-api-key") != API_KEY:
            return JSONResponse({"detail": "Invalid or missing X-API-Key"}, status_code=401)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")


# ─── Engine lifecycle (singleton instances + idle auto-unload) ───────────────

_engine_cache: dict[str, TTSEngine] = {}
_engine_cache_lock = threading.Lock()


def get_engine(engine_id: str) -> TTSEngine:
    """Return the singleton engine instance, creating it on first use."""
    if engine_id not in _engine_cache:
        # Lock the check-then-create sequence so concurrent requests can't
        # construct duplicate engine instances.
        with _engine_cache_lock:
            if engine_id not in _engine_cache:
                _engine_cache[engine_id] = create_engine(engine_id, str(OUTPUT_DIR), audio8_manager)
    return _engine_cache[engine_id]


@contextmanager
def engine_use(eng: TTSEngine) -> Iterator[TTSEngine]:
    """Mark an engine as busy for the duration of a request."""
    eng.acquire()
    try:
        yield eng
    finally:
        eng.release()


def _monitor_loop() -> None:
    """Background thread: unload engines that have been idle too long."""
    while True:
        time.sleep(15)
        now = time.time()
        for eng in list(_engine_cache.values()):
            try:
                if (
                    getattr(eng, "auto_unload", True)
                    and eng.can_unload()
                    and not eng.busy
                    and now - eng.last_used > IDLE_UNLOAD_SECONDS
                ):
                    logger.info("auto-unload: unloading idle engine %s", eng.get_engine_id())
                    eng.unload()
            except Exception:
                logger.exception("auto-unload failed for %s", eng.get_engine_id())


threading.Thread(target=_monitor_loop, name="engine-auto-unload", daemon=True).start()


# ─── Request models ───────────────────────────────────────────────────────────

# Shared bounds for the global speed/pitch controls. The UI sliders stay
# inside these, but the API clamps them too: an unvalidated speed=1e9 would
# otherwise reach librosa time-stretch and blow up memory/CPU.
SpeedField = Field(default=1.0, ge=0.25, le=4.0, description="Speed multiplier")
PitchField = Field(default=0.0, ge=-24.0, le=24.0, description="Pitch shift (semitones)")
Format = Literal["wav", "mp3", "flac"]


class GenerateRequest(BaseModel):
    text: str
    engine_id: str = "piper"
    voice: str = ""
    speed: float = SpeedField
    pitch: float = PitchField
    fmt: Format = "wav"
    params: dict | None = None


class RegenerateRequest(BaseModel):
    voice: str | None = None
    speed: float | None = Field(default=None, ge=0.25, le=4.0)
    pitch: float | None = Field(default=None, ge=-24.0, le=24.0)
    fmt: Format | None = None
    params: dict | None = None


class BatchGenerateRequest(BaseModel):
    texts: list[str]
    engine_id: str = "piper"
    voice: str = ""
    speed: float = SpeedField
    pitch: float = PitchField
    fmt: Format = "wav"
    params: dict | None = None

    model_config = {"extra": "forbid"}

    MAX_TEXTS: ClassVar[int] = 100

    @property
    def safe_texts(self) -> list[str]:
        """Return stripped, non-empty texts (up to MAX_TEXTS)."""
        return [t.strip() for t in self.texts if t.strip()][: self.MAX_TEXTS]

    @property
    def dropped_count(self) -> int:
        """How many non-empty lines were cut by the MAX_TEXTS cap."""
        non_empty = sum(1 for t in self.texts if t.strip())
        return max(0, non_empty - self.MAX_TEXTS)


class DeleteRequest(BaseModel):
    filename: str

    model_config = {"extra": "forbid"}


class CleanRequest(BaseModel):
    days: int = Field(default=30, ge=1, le=3650)


class ListVoicesRequest(BaseModel):
    engine_id: str = "piper"


class DownloadRequest(BaseModel):
    engine_id: str
    voice: str = ""


class UnloadRequest(BaseModel):
    engine_id: str


# Last successful single generation — powers in-line regeneration.
# Guarded by a lock: concurrent requests may read/update it concurrently.
last_generation: dict | None = None
_last_generation_lock = threading.Lock()


# ─── Engine param validation ─────────────────────────────────────────────────


def _coerce_param(value: Any, spec: dict) -> Any:
    """Coerce a raw value to the type declared in the engine param spec."""
    ptype = spec.get("type", "float")
    if ptype == "int":
        return int(value) if value is not None else None
    if ptype == "float":
        return float(value) if value is not None else None
    if ptype == "bool":
        if value is None:
            return bool(spec.get("default", False))
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)
    return value


def _validate_params(engine_id: str, raw_params: dict | None) -> dict:
    """Validate and coerce engine params against the engine's schema.

    Returns a dict with only the schema-declared params and their coerced values.
    Raises HTTPException 400 on invalid input.
    """
    if not raw_params:
        return {}
    eng_cls = _ENGINE_REGISTRY.get(engine_id)
    if eng_cls is None:
        return {}
    schema = getattr(eng_cls, "engine_params", {}) or {}
    result: dict[str, Any] = {}
    for name, spec in schema.items():
        if name not in raw_params:
            continue
        value = _coerce_param(raw_params[name], spec)
        # Clamp numeric ranges
        min_val = spec.get("min")
        max_val = spec.get("max")
        if min_val is not None and value is not None and value < min_val:
            value = min_val
        if max_val is not None and value is not None and value > max_val:
            value = max_val
        result[name] = value
    return result


def _engine_param_schemas() -> dict[str, dict[str, dict]]:
    """Return per-engine param schemas for the API."""
    out: dict[str, dict[str, dict]] = {}
    for eid in engine_ids():
        eng_cls = _ENGINE_REGISTRY.get(eid)
        if eng_cls is not None:
            out[eid] = getattr(eng_cls, "engine_params", {}) or {}
    return out


# ─── Pages ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index():
    html = (BASE_DIR / "templates" / "index.html").read_text()
    return html


# ─── Engine & voice endpoints ────────────────────────────────────────────────


@app.get("/api/engines")
def list_engines():
    engines = get_all_engines()
    # Attach per-engine param schemas so the UI can render controls dynamically.
    schemas = _engine_param_schemas()
    for eng in engines:
        eng["params"] = schemas.get(eng["id"], {})
    return engines


@app.get("/api/voices")
def list_voices_get(engine_id: str = "piper"):
    try:
        with engine_use(get_engine(engine_id)) as eng:
            voices = eng.list_voices()
        return {"voices": voices, "is_online": eng.is_online}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/voices")
def list_voices_post(req: ListVoicesRequest):
    try:
        with engine_use(get_engine(req.engine_id)) as eng:
            voices = eng.list_voices()
        return {"voices": voices, "is_online": eng.is_online}
    except Exception as e:
        raise HTTPException(400, str(e))


# ─── Generation endpoints ─────────────────────────────────────────────────────


def run_generation(
    engine_id: str,
    text: str,
    voice: str,
    speed: float,
    pitch: float,
    fmt: str,
    params: dict | None = None,
) -> tuple[str, float, str, str, float, float, str]:
    """Generate audio with the singleton engine; returns results + request echo."""
    global last_generation
    if not text.strip():
        raise HTTPException(400, "Text is empty")
    eng = get_engine(engine_id)
    if not voice:
        voices = eng.list_voices()
        if voices:
            voice = voices[0]
    validated = _validate_params(engine_id, params)
    with engine_use(eng):
        filepath, duration = eng.generate(
            text, voice=voice, speed=speed, pitch=pitch, fmt=fmt, params=validated
        )
    filename = Path(filepath).name
    add_generation(filename, text, engine_id, voice, speed, pitch, duration, fmt)
    with _last_generation_lock:
        last_generation = {
            "text": text,
            "engine_id": engine_id,
            "voice": voice,
            "speed": speed,
            "pitch": pitch,
            "fmt": fmt,
        }
    return filepath, duration, filename, voice, speed, pitch, fmt


@app.post("/api/generate")
def generate(req: GenerateRequest):
    try:
        filepath, duration, filename, voice, _, _, _ = run_generation(
            req.engine_id, req.text, req.voice, req.speed, req.pitch, req.fmt, req.params
        )
        return {"filename": filename, "duration": round(duration, 2), "url": f"/output/{filename}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/regenerate")
def regenerate(req: RegenerateRequest):
    """Re-run the last single generation, optionally with new voice/params."""
    global last_generation
    with _last_generation_lock:
        base = dict(last_generation) if last_generation else None
    if base is None:
        raise HTTPException(400, "Nothing to regenerate yet — generate something first")
    try:
        filepath, duration, filename = run_generation(
            engine_id=base["engine_id"],
            text=base["text"],
            voice=req.voice if req.voice is not None else base["voice"],
            speed=req.speed if req.speed is not None else base["speed"],
            pitch=req.pitch if req.pitch is not None else base["pitch"],
            fmt=req.fmt if req.fmt is not None else base["fmt"],
            params=req.params,
        )[:3]
        return {"filename": filename, "duration": round(duration, 2), "url": f"/output/{filename}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/generate-batch")
def generate_batch(req: BatchGenerateRequest):
    import zipfile
    import io

    zip_buffer = io.BytesIO()
    texts_to_process = req.safe_texts
    if not texts_to_process:
        raise HTTPException(400, "No non-empty texts provided")

    validated = _validate_params(req.engine_id, req.params)

    eng = get_engine(req.engine_id)
    default_voice = ""
    with engine_use(eng):
        try:
            voices = eng.list_voices()
            if req.voice:
                default_voice = req.voice
            elif voices:
                default_voice = voices[0]
        except Exception:
            default_voice = req.voice or ""

    failures: list[dict] = []
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, text in enumerate(texts_to_process):
            try:
                with engine_use(eng):
                    filepath, duration = eng.generate(
                        text, voice=default_voice, speed=req.speed, pitch=req.pitch, fmt=req.fmt, params=validated
                    )
                filename = Path(filepath).name
                with open(filepath, "rb") as f:
                    zf.writestr(f"text_{i+1}_{filename}", f.read())
                add_generation(filename, text, req.engine_id, default_voice, req.speed, req.pitch, duration, req.fmt)
            except Exception as exc:
                # Don't kill the whole batch for one bad item; report it in
                # the manifest and the X-Batch-Failed header instead.
                logger.warning("batch item %d failed: %s", i + 1, exc)
                failures.append({"index": i + 1, "error": str(exc) or exc.__class__.__name__})

        dropped = req.dropped_count
        if failures or dropped:
            manifest = {
                "total_requested": len(req.texts),
                "processed": len(texts_to_process),
                "dropped_over_limit": dropped,
                "failed": failures,
            }
            zf.writestr("_manifest.json", json.dumps(manifest, indent=2))

    zip_buffer.seek(0)
    batch_name = f"batch_{uuid.uuid4().hex[:12]}.zip"
    batch_path = OUTPUT_DIR / batch_name
    with open(batch_path, "wb") as f:
        f.write(zip_buffer.getvalue())
    return FileResponse(
        path=str(batch_path),
        media_type="application/zip",
        filename=batch_name,
        headers={
            "X-Batch-Failed": str(len(failures)),
            "X-Batch-Dropped": str(dropped),
        },
    )


@app.get("/api/audio-preview")
def audio_preview(voice: str = "", engine_id: str = "piper", text: str = "Hello, this is a voice preview."):
    eng = get_engine(engine_id)
    if not voice:
        voices = eng.list_voices()
        if voices:
            voice = voices[0]
        else:
            raise HTTPException(400, "No voices available")
    try:
        with engine_use(eng):
            audio, sr = eng.preview_voice(voice, text)
        import soundfile as sf
        # uuid suffix: second-granularity timestamps collided when two
        # previews landed in the same second, silently overwriting one.
        filename = f"preview_{uuid.uuid4().hex[:12]}.wav"
        filepath = OUTPUT_DIR / filename
        sf.write(str(filepath), audio, sr)
        return {"url": f"/output/{filename}", "filename": filename}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── History endpoints ────────────────────────────────────────────────────────


def _safe_output_path(filename: str) -> Path | None:
    """Resolve ``filename`` inside OUTPUT_DIR; None if it escapes it."""
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        return None
    resolved = (OUTPUT_DIR / safe_name).resolve()
    if resolved.parent != OUTPUT_DIR.resolve():
        return None
    return resolved


@app.get("/api/history")
def history(limit: int = 100, offset: int = 0):
    try:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        generations = get_generations(limit=limit, offset=offset)
        return {"generations": generations, "has_more": len(generations) == limit}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/history/delete")
def delete_history(req: DeleteRequest):
    try:
        # _safe_output_path rejects directory components ("../x.wav") rather
        # than silently stripping them, and pins the result inside OUTPUT_DIR.
        path = _safe_output_path(req.filename)
        if path is None:
            raise HTTPException(400, "Invalid filename")
        if path.exists():
            os.remove(path)
        delete_generation(path.name)
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/history/clean")
def clean_history(req: CleanRequest):
    try:
        # Delete both the DB rows and the audio files they pointed at —
        # previously only rows went, leaving orphaned files on disk forever.
        filenames = delete_generations_older_than(req.days)
        removed = 0
        for name in filenames:
            path = _safe_output_path(name)
            if path and path.exists():
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    logger.warning("could not remove %s", path)
        return {"deleted": len(filenames), "files_removed": removed}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── Audio info ───────────────────────────────────────────────────────────────


@app.get("/api/audio-info")
def audio_info(filename: str):
    path = _safe_output_path(filename)
    if path is None:
        raise HTTPException(400, "Invalid filename")
    if not path.exists():
        raise HTTPException(404, "File not found")
    import soundfile as sf
    info = sf.info(str(path))
    return {"duration": round(info.duration, 2), "samplerate": info.samplerate, "channels": info.channels, "format": info.format}


# ─── Model download endpoints ─────────────────────────────────────────────────


@app.get("/api/models")
def models_list():
    out = []
    for eid in engine_ids():
        spec = md.download_spec(eid)
        if spec is not None:
            # Downloadable local model
            status = md.get_status(eid)
            out.append({
                "engine_id": eid,
                "label": spec["label"],
                "size_label": spec["size_label"],
                "voices": spec["voices"],
                "installed": md.is_installed(eid),
                "installed_voices": [v for v in spec["voices"] if md.is_piper_voice_installed(v)],
                "status": status,
                "is_online": False,
            })
        else:
            # Online-only engine — no model to download
            out.append({
                "engine_id": eid,
                "label": eid.capitalize().replace("Tts", " TTS").replace(" ", ""),
                "size_label": "No download needed",
                "voices": [],
                "installed": True,
                "installed_voices": [],
                "status": {"state": "done", "percent": 100.0, "message": "Online"},
                "is_online": True,
            })
    return {"models": out}


@app.post("/api/models/download")
def model_download(req: DownloadRequest):
    spec = md.download_spec(req.engine_id)
    if spec is None:
        raise HTTPException(404, f"No downloadable models for engine: {req.engine_id}")
    if req.engine_id == "piper" and req.voice not in md.PIPER_VOICES:
        raise HTTPException(400, f"Unknown Piper voice: {req.voice}")
    try:
        status = md.start_download(req.engine_id, req.voice)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return status


@app.get("/api/models/download/status")
def model_download_status(engine_id: str = "piper"):
    return md.get_status(engine_id)


# ─── Runtime (memory) management ──────────────────────────────────────────────


@app.get("/api/runtime/status")
def runtime_status():
    now = time.time()
    engines = []
    for eid in engine_ids():
        eng = get_engine(eid)
        engines.append({
            "engine_id": eid,
            "loaded": eng.loaded,
            "last_used": eng.last_used,
            "idle_seconds": round(max(0.0, now - eng.last_used), 1) if eng.last_used else None,
            "busy": eng.busy,
            "memory_mb": eng.memory_mb(),
        })
    return {
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "engines": engines,
    }


@app.post("/api/runtime/unload")
def runtime_unload(req: UnloadRequest):
    eng = get_engine(req.engine_id)
    eng.unload()
    return {"engine_id": req.engine_id, "loaded": eng.loaded}


# ─── Audio8 endpoints ─────────────────────────────────────────────────────────


@app.get("/api/audio8/status")
def audio8_status():
    is_running = audio8_manager.is_running()
    if is_running:
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{audio8_manager.port}/api/health", timeout=2) as r:
                return {"running": True, "healthy": r.status == 200}
        except Exception:
            return {"running": True, "healthy": False}
    return {"running": False, "healthy": False}


@app.post("/api/voices/register")
def register_custom_voice(
    audio: UploadFile = File(...),
    text: str = Form(...),
    name: str = Form(...),
    overwrite: bool = Form(False),
):
    """Proxy to the Audio8 service's custom-voice registration endpoint."""
    import json
    import urllib.error
    import urllib.request
    import uuid

    if not audio8_manager.is_running():
        if not audio8_manager.start():
            raise HTTPException(503, "Audio8 service unavailable")

    audio_bytes = audio.file.read()
    if not audio_bytes:
        raise HTTPException(400, "Empty audio file")
    max_bytes = 30 * 1024 * 1024
    if len(audio_bytes) > max_bytes:
        raise HTTPException(400, "Audio sample too large (max 30 MB)")

    boundary = uuid.uuid4().hex

    def part(field: str, value: bytes, filename: str | None = None, content_type: str | None = None) -> bytes:
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"'
        if filename:
            head += f'; filename="{filename}"'
        head += "\r\n"
        if content_type:
            head += f"Content-Type: {content_type}\r\n"
        return head.encode() + b"\r\n" + value + b"\r\n"

    body = b"".join([
        part("audio", audio_bytes, filename=audio.filename or "reference_audio.wav", content_type=audio.content_type or "application/octet-stream"),
        part("text", text.encode("utf-8")),
        part("name", name.encode("utf-8")),
        part("overwrite", ("true" if overwrite else "false").encode()),
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"http://127.0.0.1:{audio8_manager.port}/api/voices/register",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise HTTPException(e.code, detail)
    except Exception as e:
        raise HTTPException(502, f"Audio8 registration failed: {e}")


# ─── Hardware detection & model capability ───────────────────────────────────


@app.get("/api/device")
def device_info():
    import psutil

    vm = psutil.virtual_memory()
    ram_total_mb = vm.total // (1024 * 1024)
    ram_avail_mb = vm.available // (1024 * 1024)

    gpu_name: str | None = None
    vram_total_mb: int | None = None
    vram_avail_mb: int | None = None
    device_label = "CPU"
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            props = torch.cuda.get_device_properties(0)
            vram_total_mb = int(props.total_memory // (1024 * 1024))
            free, _ = torch.cuda.mem_get_info()
            vram_avail_mb = int(free // (1024 * 1024))
            device_label = f"GPU ({gpu_name[:40]})"
        else:
            device_label = "Apple Silicon" if hasattr(torch.backends, "mps") and torch.backends.mps.is_available() else "CPU"
    except Exception:
        pass

    # Per-engine capability check: can this machine run this model?
    engine_caps: dict[str, Any] = {}
    for eid in engine_ids():
        eng = get_engine(eid)
        req_mb = eng.requirement_mb()
        if eng.is_online:
            # Online engines always runnable: no local model needed.
            engine_caps[eid] = {
                "engine_id": eid,
                "requirement_mb": None,
                "installed": None,
                "runnable": True,
                "reason": None,
                "params": _engine_param_schemas().get(eid, {}),
            }
            continue
        installed = eng.is_installed()
        reason = None
        runnable = True
        if not installed:
            runnable = False
            reason = "not-installed"
        elif req_mb is not None and req_mb > ram_avail_mb:
            runnable = False
            reason = "insufficient-ram"
        engine_caps[eid] = {
            "engine_id": eid,
            "requirement_mb": req_mb,
            "installed": installed,
            "runnable": runnable,
            "reason": reason,
            "params": _engine_param_schemas().get(eid, {}),
        }

    return {
        "device": device_label,
        "cpu": {"name": _cpu_name(), "cores": psutil.cpu_count(logical=True)},
        "ram": {"total_mb": ram_total_mb, "available_mb": ram_avail_mb},
        "gpu": {"name": gpu_name, "vram_total_mb": vram_total_mb, "vram_available_mb": vram_avail_mb},
        "engines": engine_caps,
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
    }


def _cpu_name() -> str | None:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def main() -> None:
    """Console-script entry point (``unified-tts``) and ``python server.py``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()