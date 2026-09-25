# Unified TTS container image.
#
# Build:  docker build -t unified-tts .
# Run:    docker run -p 8000:8000 -v tts-data:/app/data unified-tts
#
# Model files are NOT baked in — download them via the UI (Models → Download)
# and persist them with volumes:
#   -v tts-models:/app/models        (Piper/Kokoro/Kitten weights)
#   -v tts-audio8:/app/audio8_models (Audio8 snapshot)

FROM python:3.12-slim

# ffmpeg  → MP3/FLAC export
# espeak-ng + data → Kokoro phonemizer
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng espeak-ng-data \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE.md ./
COPY server.py tts_engine.py audio8_manager.py model_downloader.py check_deps.py ./
COPY audio8_repo ./audio8_repo
COPY static ./static
COPY templates ./templates

RUN pip install --no-cache-dir ".[all]" \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/output /app/data /app/models /app/audio8_models /app/audio8_voices \
    && chown -R appuser:appuser /app

# In a container the server must listen on all interfaces; the source-level
# default is loopback-only (a safety choice for bare-metal runs).
ENV TTS_HOST=0.0.0.0 \
    TTS_PORT=8000 \
    ARKTTS_MODEL_DIR=/app/audio8_models \
    ARKTTS_VOICES_DIR=/app/audio8_voices

EXPOSE 8000

VOLUME ["/app/output", "/app/data", "/app/models", "/app/audio8_models", "/app/audio8_voices"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

USER appuser

CMD ["python", "server.py"]
