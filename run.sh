#!/bin/bash
# Unified TTS launcher
cd "$(dirname "$0")"

# Activate a virtual environment if one is present: a local .venv takes
# priority, otherwise reuse an already-activated $VIRTUAL_ENV. If neither
# exists, fall back to the system python3.
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -n "$VIRTUAL_ENV" ] && [ -f "$VIRTUAL_ENV/bin/activate" ]; then
    source "$VIRTUAL_ENV/bin/activate"
fi

export PYTHONWARNINGS="ignore::UserWarning"
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$(pwd)"

# ─── Pre-flight dependency check ─────────────────────────────────────────────
# Fail fast when core deps are missing. Local engines (Piper/Kokoro/Audio8)
# and system packages (espeak-ng, ffmpeg) are optional: the web UI + online
# engines work without them. Run `python3 check_deps.py` for the full report.
if ! python3 check_deps.py --core; then
    echo ""
    echo "Install the missing dependencies:"
    echo "  pip install -e \".[core]\"    # web UI + online engines"
    echo "  pip install -e \".[local]\"   # + Piper, Kokoro, Audio8"
    echo "  pip install -e \".[online]\"  # + edge-tts, gTTS"
    echo "  pip install -e \".\"          # everything"
    echo ""
    echo "System packages needed for local engines:"
    echo "  sudo apt install espeak-ng ffmpeg   # Debian/Ubuntu"
    echo "  sudo dnf install espeak-ng ffmpeg   # Fedora"
    exit 1
fi

exec python3 server.py "$@"
