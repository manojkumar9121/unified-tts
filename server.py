"""Unified TTS Web UI server — FastAPI backend.

Serves a thin-client web UI: all synthesis happens server-side (either
in-process for Piper/Kokoro or in the managed Audio8 background daemon),
the client only sends text and plays back audio.
"""

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tts_engine import (
    TTSEngine,
    create_engine, init_db, add_generation, get_generations,
    delete_generation, delete_generations_older_than, get_all_engines,
    engine_ids, set_audio8_manager, _ENGINE_REGISTRY,
)
from audio8_manager import Audio8ServiceManager
import model_downloader as md

init_db()

BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

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

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")


# ─── Engine lifecycle (singleton instances + idle auto-unload) ───────────────

_engine_cache: dict[str, TTSEngine] = {}


def get_engine(engine_id: str) -> TTSEngine:
    """Return the singleton engine instance, creating it on first use."""
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
                    print(f"[auto-unload] unloading idle engine: {eng.get_engine_id()}")
                    eng.unload()
            except Exception:
                pass


threading.Thread(target=_monitor_loop, name="engine-auto-unload", daemon=True).start()


# ─── Request models ───────────────────────────────────────────────────────────


class GenerateRequest(BaseModel):
    text: str
    engine_id: str = "piper"
    voice: str = ""
    speed: float = 1.0
    pitch: float = 0.0
    fmt: str = "wav"
    params: dict | None = None


class RegenerateRequest(BaseModel):
    voice: str | None = None
    speed: float | None = None
    pitch: float | None = None
    fmt: str | None = None
    params: dict | None = None


class BatchGenerateRequest(BaseModel):
    texts: list[str]
    engine_id: str = "piper"
    voice: str = ""
    speed: float = 1.0
    pitch: float = 0.0
    fmt: str = "wav"
    params: dict | None = None

    model_config = {"extra": "forbid"}

    @property
    def safe_texts(self) -> list[str]:
        """Return stripped, non-empty texts (up to 100)."""
        return [t.strip() for t in self.texts if t.strip()][:100]


class SwitchEngineRequest(BaseModel):
    engine_id: str


class DeleteRequest(BaseModel):
    filename: str

    model_config = {"extra": "forbid"}

    @property
    def safe_filename(self) -> str:
        """Strip directory components to prevent path traversal."""
        return Path(self.filename).name


class CleanRequest(BaseModel):
    days: int = 30


class ListVoicesRequest(BaseModel):
    engine_id: str = "piper"


class DownloadRequest(BaseModel):
    engine_id: str
    voice: str = ""


class UnloadRequest(BaseModel):
    engine_id: str


class DownloadHFRequest(BaseModel):
    repo: str
    revision: str = "main"
    file_pattern: str = ""
    dest_subdir: str = ""
    hf_token: str = ""

    model_config = {"extra": "forbid"}

    @property
    def repo_id(self) -> str:
        """Normalize and validate the repo identifier."""
        repo = self.repo.strip()
        if not repo or "/" not in repo:
            raise ValueError("repo must be in 'org/repo' form")
        return repo

    @property
    def safe_dest_subdir(self) -> str:
        """Prevent path traversal in dest_subdir."""
        return Path(self.dest_subdir or "").name


# Last successful single generation — powers in-line regeneration.
last_generation: dict | None = None


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


@app.post("/api/engine/switch")
def switch_engine(req: SwitchEngineRequest):
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
    if last_generation is None:
        raise HTTPException(400, "Nothing to regenerate yet — generate something first")
    base = last_generation
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
            except Exception:
                continue

    zip_buffer.seek(0)
    batch_name = f"batch_{int(time.time())}.zip"
    batch_path = OUTPUT_DIR / batch_name
    with open(batch_path, "wb") as f:
        f.write(zip_buffer.getvalue())
    return FileResponse(path=str(batch_path), media_type="application/zip", filename=batch_name)


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
        timestamp = int(time.time())
        filepath = OUTPUT_DIR / f"preview_{timestamp}.wav"
        sf.write(str(filepath), audio, sr)
        return {"url": f"/output/preview_{timestamp}.wav", "filename": f"preview_{timestamp}.wav"}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── History endpoints ────────────────────────────────────────────────────────


@app.get("/api/history")
def history(limit: int = 100, offset: int = 0):
    try:
        generations = get_generations(limit=limit, offset=offset)
        return {"generations": generations, "has_more": len(generations) == limit}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/history/delete")
def delete_history(req: DeleteRequest):
    try:
        safe_name = req.safe_filename
        if not safe_name:
            raise HTTPException(400, "Invalid filename")
        filepath = OUTPUT_DIR / safe_name
        resolved = filepath.resolve()
        if not str(resolved).startswith(str(OUTPUT_DIR.resolve())):
            raise HTTPException(400, "Invalid filename")
        if filepath.exists():
            os.remove(filepath)
        delete_generation(safe_name)
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/history/clean")
def clean_history(req: CleanRequest):
    try:
        count = delete_generations_older_than(req.days)
        return {"deleted": count}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── Audio info ───────────────────────────────────────────────────────────────


@app.get("/api/audio-info")
def audio_info(filename: str):
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "Invalid filename")
    path = OUTPUT_DIR / safe_name
    if not path.exists():
        raise HTTPException(404, "File not found")
    import soundfile as sf
    info = sf.info(str(path))
    return {"duration": round(info.duration, 2), "samplerate": info.samplerate, "channels": info.channels, "format": info.format}


@app.post("/api/play")
def play(filename: str = Query(...)):
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "Invalid filename")
    path = OUTPUT_DIR / safe_name
    if not path.exists():
        raise HTTPException(404, "File not found")
    return {"status": "playing"}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")