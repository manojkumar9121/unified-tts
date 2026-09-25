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
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, ClassVar, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import model_downloader as md
from audio8_manager import Audio8ServiceManager
from tts_engine import (
    _ENGINE_REGISTRY,
    TTSEngine,
    add_generation,
    create_engine,
    delete_generation,
    delete_generations_older_than,
    engine_ids,
    get_all_engines,
    get_generations,
    init_db,
    set_audio8_manager,
)

logger = logging.getLogger("unified_tts.server")

init_db()


def _require_engine(engine_id: str) -> TTSEngine:
    """Validate engine_id and return the singleton instance.

    Raises HTTPException(400) for unknown engines with a generic message
    that does not leak the full registry.
    """
    if engine_id not in _ENGINE_REGISTRY:
        raise HTTPException(400, f"Unknown engine: {engine_id}")
    return get_engine(engine_id)

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
LOCAL_SYNTHESIS_CONCURRENCY = max(1, int(os.environ.get("TTS_LOCAL_CONCURRENCY", "2")))
_local_synthesis_slots = threading.BoundedSemaphore(LOCAL_SYNTHESIS_CONCURRENCY)


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


@contextmanager
def generation_use(eng: TTSEngine) -> Iterator[TTSEngine]:
    """Bound concurrent in-process local synthesis and mark the engine busy."""
    if getattr(type(eng), "is_online", False):
        with engine_use(eng) as active:
            yield active
    else:
        with _local_synthesis_slots, engine_use(eng) as active:
            yield active


def _resolve_voice(eng: TTSEngine, requested_voice: str = "") -> str:
    """Resolve a default voice and reject caller-supplied unavailable names."""
    with engine_use(eng):
        voices = eng.list_voices()
    if requested_voice:
        if requested_voice not in voices:
            raise HTTPException(400, f"Unknown voice: {requested_voice}")
        return requested_voice
    return voices[0] if voices else ""


def _monitor_loop(stop_event: threading.Event) -> None:
    """Background thread: unload engines that have been idle too long."""
    while not stop_event.is_set():
        if stop_event.wait(timeout=15):
            break
        now = time.time()
        with _engine_cache_lock:
            cached = list(_engine_cache.values())
        for eng in cached:
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


_monitor_stop = threading.Event()
_monitor_thread: threading.Thread | None = None


def _shutdown() -> None:
    """Gracefully unload all engines and stop the Audio8 daemon."""
    logger.info("shutting down — unloading engines …")
    with _engine_cache_lock:
        cached = list(_engine_cache.values())
    for eng in cached:
        try:
            eng.unload()
        except Exception:
            logger.exception("unload failed during shutdown for %s", eng.get_engine_id())
    try:
        audio8_manager.stop()
    except Exception:
        logger.exception("error stopping Audio8 daemon during shutdown")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _monitor_thread
    # Start the auto-unload monitor thread on startup
    _monitor_stop.clear()
    _monitor_thread = threading.Thread(
        target=_monitor_loop, args=(_monitor_stop,), name="engine-auto-unload", daemon=True
    )
    _monitor_thread.start()
    try:
        yield
    finally:
        # Stop the monitor thread
        _monitor_stop.set()
        if _monitor_thread is not None:
            _monitor_thread.join(timeout=5)
        # Graceful cleanup
        _shutdown()


app = FastAPI(title="Unified TTS", lifespan=lifespan)


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    """Require ``X-API-Key`` on /api/* when TTS_API_KEY is configured.

    The UI page, static assets and /output audio stay open so browser
    <audio> playback keeps working; only the JSON API is protected.
    Note: /output URLs are bearer tokens when exposed — anyone with a
    filename URL can fetch the audio without a key.
    """
    if API_KEY and request.url.path.startswith("/api/") and request.headers.get("x-api-key") != API_KEY:
        return JSONResponse({"detail": "Invalid or missing X-API-Key"}, status_code=401)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")


# ─── Health endpoint ────────────────────────────────────────────────────────


@app.get("/health")
def health():
    """Lightweight liveness probe for container orchestrators."""
    import psutil

    vm = psutil.virtual_memory()
    return {
        "status": "ok",
        "cpu_count": psutil.cpu_count(logical=True),
        "ram_total_mb": vm.total // (1024 * 1024),
        "ram_available_mb": vm.available // (1024 * 1024),
    }


# ─── Request models ───────────────────────────────────────────────────────────

# Shared bounds for the global speed/pitch controls. The UI sliders stay
# inside these, but the API clamps them too: an unvalidated speed=1e9 would
# otherwise reach librosa time-stretch and blow up memory/CPU.
SpeedField = Field(default=1.0, ge=0.25, le=4.0, description="Speed multiplier")
PitchField = Field(default=0.0, ge=-24.0, le=24.0, description="Pitch shift (semitones)")
Format = Literal["wav", "mp3", "flac"]


class GenerateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    engine_id: str = "piper"
    voice: str = ""
    speed: float = SpeedField
    pitch: float = PitchField
    fmt: Format = "wav"
    params: dict | None = None

    model_config = {"extra": "forbid"}


class RegenerateRequest(BaseModel):
    voice: str | None = None
    speed: float | None = Field(default=None, ge=0.25, le=4.0)
    pitch: float | None = Field(default=None, ge=-24.0, le=24.0)
    fmt: Format | None = None
    params: dict | None = None

    model_config = {"extra": "forbid"}


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
    MAX_CHARS_PER_ITEM: ClassVar[int] = 5000

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
    if ptype == "str":
        return str(value) if value is not None else None
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
        try:
            value = _coerce_param(raw_params[name], spec)
        except (ValueError, TypeError):
            raise HTTPException(400, f"Invalid value for param: {name}")
        # Enforce allowlist for string selects (e.g. Kokoro lang)
        options = spec.get("options")
        if options is not None and value is not None and value not in options:
            raise HTTPException(400, f"Invalid value for param: {name}")
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
        with engine_use(_require_engine(engine_id)) as eng:
            voices = eng.list_voices()
        return {"voices": voices, "is_online": eng.is_online}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Failed to list voices")


@app.post("/api/voices")
def list_voices_post(req: ListVoicesRequest):
    try:
        with engine_use(_require_engine(req.engine_id)) as eng:
            voices = eng.list_voices()
        return {"voices": voices, "is_online": eng.is_online}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Failed to list voices")


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
    eng = _require_engine(engine_id)
    voice = _resolve_voice(eng, voice)
    validated = _validate_params(engine_id, params)
    with generation_use(eng):
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
            "params": dict(validated),
        }
    return filepath, duration, filename, voice, speed, pitch, fmt


@app.post("/api/generate")
def generate(req: GenerateRequest):
    try:
        _filepath, duration, filename, _voice, _, _, _ = run_generation(
            req.engine_id, req.text, req.voice, req.speed, req.pitch, req.fmt, req.params
        )
        return {"filename": filename, "duration": round(duration, 2), "url": f"/output/{filename}"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("unexpected generation failure")
        raise HTTPException(500, "An internal error occurred")


@app.post("/api/regenerate")
def regenerate(req: RegenerateRequest):
    """Re-run the last single generation, optionally with new voice/params."""
    with _last_generation_lock:
        base = dict(last_generation) if last_generation else None
    if base is None:
        raise HTTPException(400, "Nothing to regenerate yet — generate something first")
    try:
        _filepath, duration, filename = run_generation(
            engine_id=base["engine_id"],
            text=base["text"],
            voice=req.voice if req.voice is not None else base["voice"],
            speed=req.speed if req.speed is not None else base["speed"],
            pitch=req.pitch if req.pitch is not None else base["pitch"],
            fmt=req.fmt if req.fmt is not None else base["fmt"],
            params=req.params if req.params is not None else base.get("params"),
        )[:3]
        return {"filename": filename, "duration": round(duration, 2), "url": f"/output/{filename}"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("unexpected regeneration failure")
        raise HTTPException(500, "An internal error occurred")


def _run_batch_tasks(
    req: BatchGenerateRequest,
    eng: TTSEngine,
    default_voice: str,
    validated: dict,
) -> tuple[str, list[tuple[int, tuple[str, float] | BaseException]], int]:
    """Run batch generation tasks with progress tracking.

    Returns (batch_id, results, total) where results is a list of
    (index, (filepath, duration) | exception) tuples.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    texts_to_process = req.safe_texts
    # Only truly remote stateless engines parallelize. Audio8Engine.is_online
    # is dynamic (True when its local daemon runs) — fanning out 5 concurrent
    # POSTs into a 1.6 GB local daemon contends rather than helps.
    concurrent = eng.get_engine_id() in ("edge-tts", "gtts")
    batch_id = str(uuid.uuid4())
    total = len(texts_to_process)

    # Seed progress tracking so callers can poll /api/batch-progress/{batch_id}
    _batch_progress[batch_id] = {"done": 0, "total": total, "failed": [], "running": True}

    results: list[tuple[int, tuple[str, float] | BaseException]] = []
    max_workers = 5 if concurrent else 1

    def _generate_one(idx: int) -> tuple[int, tuple[str, float] | BaseException]:
        try:
            with generation_use(eng):
                filepath, duration = eng.generate(
                    texts_to_process[idx],
                    voice=default_voice,
                    speed=req.speed,
                    pitch=req.pitch,
                    fmt=req.fmt,
                    params=validated,
                )
            return idx, (filepath, duration)
        except Exception as exc:
            return idx, exc

    def _update_progress(result: tuple[int, tuple[str, float] | BaseException]) -> None:
        state = _batch_progress.get(batch_id)
        if state:
            if isinstance(result[1], BaseException):
                state["failed"].append({"index": result[0] + 1, "error": str(result[1]) or result[1].__class__.__name__})
            state["done"] += 1

    if concurrent:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_generate_one, i) for i in range(len(texts_to_process))]
            for future in as_completed(futures):
                result: tuple[int, tuple[str, float] | BaseException] = future.result()
                results.append(result)
                _update_progress(result)
        # Re-sort to preserve input order for the zip manifest
        results.sort(key=lambda x: x[0])
    else:
        for i in range(len(texts_to_process)):
            result = _generate_one(i)
            results.append(result)
            _update_progress(result)
    state = _batch_progress.get(batch_id)
    if state:
        state["running"] = False

    return batch_id, results, total


@app.post("/api/generate-batch")
def generate_batch(req: BatchGenerateRequest):
    import zipfile

    texts_to_process = req.safe_texts
    if not texts_to_process:
        raise HTTPException(400, "No non-empty texts provided")
    if any(len(t) > req.MAX_CHARS_PER_ITEM for t in texts_to_process):
        raise HTTPException(400, f"Each batch item must be {req.MAX_CHARS_PER_ITEM} characters or fewer")

    validated = _validate_params(req.engine_id, req.params)

    eng = _require_engine(req.engine_id)
    default_voice = ""
    default_voice = _resolve_voice(eng, req.voice)

    batch_id, results, _total = _run_batch_tasks(req, eng, default_voice, validated)
    engine_id = req.engine_id

    # Write the ZIP directly to disk (streaming entries in) instead of
    # buffering every WAV in RAM: 100 items easily exceed available memory.
    batch_name = f"batch_{uuid.uuid4().hex[:12]}.zip"
    batch_path = OUTPUT_DIR / batch_name
    tmp_path = batch_path.with_name(batch_name + ".part")
    failures: list[dict] = []
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, item in results:
                if isinstance(item, BaseException):
                    logger.warning("batch item %d failed: %s", idx + 1, item)
                    failures.append({"index": idx + 1, "error": str(item) or item.__class__.__name__})
                    continue
                filepath, duration = item
                filename = Path(filepath).name
                # Stream from disk into the archive without reading whole file.
                zf.write(filepath, f"text_{idx+1}_{filename}")
                add_generation(filename, texts_to_process[idx], engine_id, default_voice, req.speed, req.pitch, duration, req.fmt)

            dropped = req.dropped_count
            if failures or dropped:
                manifest = {
                    "total_requested": len(req.texts),
                    "processed": len(texts_to_process),
                    "dropped_over_limit": dropped,
                    "failed": failures,
                }
                zf.writestr("_manifest.json", json.dumps(manifest, indent=2))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    tmp_path.rename(batch_path)
    _schedule_batch_progress_cleanup(batch_id)

    return FileResponse(
        path=str(batch_path),
        media_type="application/zip",
        filename=batch_name,
        headers={
            "X-Batch-Failed": str(len(failures)),
            "X-Batch-Dropped": str(req.dropped_count),
            "X-Batch-Id": batch_id,
        },
    )


# ─── Batch progress tracking ──────────────────────────────────────────────────

_batch_progress: dict[str, dict] = {}


@app.get("/api/batch-progress/{batch_id}")
def batch_progress_route(batch_id: str):
    """Progress for a batch generation.

    Note: the batch endpoints are synchronous, so the ID is only known from
    the response headers after completion. Completed state is retained
    briefly (see _schedule_batch_progress_cleanup); unknown/expired IDs 404.
    """
    state = _batch_progress.get(batch_id)
    if state is None:
        raise HTTPException(404, "batch not found")
    return state


@app.post("/api/generate-batch-stream")
async def generate_batch_stream(req: BatchGenerateRequest):
    """Batch generation returning a ZIP archive.

    Progress is tracked separately at ``/api/batch-progress/{batch_id}``.
    The ZIP contains generated audio files and an optional ``_manifest.json``
    listing any failures or items dropped due to the MAX_TEXTS limit.
    """
    import asyncio
    import zipfile

    texts_to_process = req.safe_texts
    if not texts_to_process:
        raise HTTPException(400, "No non-empty texts provided")
    if any(len(t) > req.MAX_CHARS_PER_ITEM for t in texts_to_process):
        raise HTTPException(400, f"Each batch item must be {req.MAX_CHARS_PER_ITEM} characters or fewer")

    validated = _validate_params(req.engine_id, req.params)

    eng = _require_engine(req.engine_id)
    default_voice = ""
    default_voice = _resolve_voice(eng, req.voice)

    loop = asyncio.get_running_loop()
    batch_id, results, _total = await loop.run_in_executor(
        None, _run_batch_tasks, req, eng, default_voice, validated
    )

    failures = [r for r in results if isinstance(r[1], BaseException)]

    # Write directly to disk to avoid buffering every WAV in RAM.
    batch_name = f"batch_{uuid.uuid4().hex[:12]}.zip"
    batch_path = OUTPUT_DIR / batch_name
    tmp_path = batch_path.with_name(batch_name + ".part")
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, result in results:
                if isinstance(result, BaseException):
                    continue
                filepath, duration = result
                filename = Path(filepath).name
                zf.write(filepath, f"text_{idx+1}_{filename}")
                add_generation(filename, texts_to_process[idx], req.engine_id, default_voice, req.speed, req.pitch, duration, req.fmt)

            dropped = req.dropped_count
            if failures or dropped:
                manifest = {
                    "total_requested": len(req.texts),
                    "processed": len(texts_to_process),
                    "dropped_over_limit": dropped,
                    "failed": [{"index": r[0] + 1, "error": str(r[1]) or r[1].__class__.__name__} for r in failures],
                }
                zf.writestr("_manifest.json", json.dumps(manifest, indent=2))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    tmp_path.rename(batch_path)
    _schedule_batch_progress_cleanup(batch_id)

    return FileResponse(
        path=str(batch_path),
        media_type="application/zip",
        filename=batch_name,
        headers={
            "X-Batch-Failed": str(len(failures)),
            "X-Batch-Dropped": str(req.dropped_count),
            "X-Batch-Id": batch_id,
        },
    )


@app.get("/api/audio-preview")
def audio_preview(voice: str = "", engine_id: str = "piper", text: str = "Hello, this is a voice preview."):
    if len(text) > 500:
        raise HTTPException(400, "Preview text must be 500 characters or fewer")
    eng = _require_engine(engine_id)
    voice = _resolve_voice(eng, voice)
    if not voice:
        raise HTTPException(400, "No voices available")
    try:
        with generation_use(eng):
            audio, sr = eng.preview_voice(voice, text)
        import soundfile as sf
        # uuid suffix: second-granularity timestamps collided when two
        # previews landed in the same second, silently overwriting one.
        filename = f"preview_{uuid.uuid4().hex[:12]}.wav"
        filepath = OUTPUT_DIR / filename
        sf.write(str(filepath), audio, sr)

        # Delete the preview after a grace period so the client can fetch
        # and play it first. Immediate deletion (BackgroundTask) raced the
        # subsequent GET /output/preview_*.wav and always 404'd.
        _schedule_preview_cleanup(filepath, delay_seconds=600)

        return JSONResponse(
            {"url": f"/output/{filename}", "filename": filename},
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("unexpected voice preview failure")
        raise HTTPException(500, "An internal error occurred")


# ─── History endpoints ────────────────────────────────────────────────────────


def _schedule_preview_cleanup(filepath: Path, delay_seconds: float = 600) -> None:
    """Delete a preview file after a grace period (daemon timer)."""
    def _remove() -> None:
        try:
            os.remove(filepath)
        except OSError:
            pass

    timer = threading.Timer(delay_seconds, _remove)
    timer.daemon = True
    timer.start()


def _schedule_batch_progress_cleanup(batch_id: str, delay_seconds: float = 60) -> None:
    """Forget completed batch progress after a grace period.

    The batch endpoints are synchronous, so a client cannot poll mid-flight
    (it only learns the ID from the response headers). Retaining the final
    state briefly at least makes the progress endpoint return the completed
    counts instead of always 404ing.
    """
    def _forget() -> None:
        _batch_progress.pop(batch_id, None)

    timer = threading.Timer(delay_seconds, _forget)
    timer.daemon = True
    timer.start()


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
        generations = get_generations(limit=limit + 1, offset=offset)
        has_more = len(generations) > limit
        return {"generations": generations[:limit], "has_more": has_more}
    except Exception:
        raise HTTPException(500, "An internal error occurred")


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
    except Exception:
        raise HTTPException(500, "An internal error occurred")


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
    except Exception:
        raise HTTPException(500, "An internal error occurred")


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
    except ValueError:
        raise HTTPException(400, "Invalid download request")
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
    eng = _require_engine(req.engine_id)
    eng.unload()
    return {"engine_id": req.engine_id, "loaded": eng.loaded}


# ─── Audio8 endpoints ─────────────────────────────────────────────────────────

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _read_upload_bounded(audio: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read an upload without buffering beyond the configured limit."""
    declared = audio.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise HTTPException(400, "Audio sample too large (max 30 MB)")
        except ValueError:
            pass

    data = bytearray()
    while len(data) < max_bytes:
        chunk = audio.file.read(min(_UPLOAD_CHUNK_BYTES, max_bytes - len(data)))
        if not chunk:
            break
        if len(chunk) > max_bytes - len(data):
            raise HTTPException(400, "Audio sample too large (max 30 MB)")
        data.extend(chunk)
    if len(data) == max_bytes:
        extra = audio.file.read(1)
        if extra:
            raise HTTPException(400, "Audio sample too large (max 30 MB)")
    return bytes(data)



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

    try:
        audio_bytes = _read_upload_bounded(audio)
    except HTTPException:
        raise
    except Exception:
        logger.exception("failed to read Audio8 voice upload")
        raise HTTPException(400, "Failed to read audio file")
    if not audio_bytes:
        raise HTTPException(400, "Empty audio file")
    if not audio8_manager.is_running() and not audio8_manager.start():
        raise HTTPException(503, "Audio8 service unavailable")

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
    except Exception:
        logger.exception("Audio8 voice registration request failed")
        raise HTTPException(502, "Audio8 registration failed")


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