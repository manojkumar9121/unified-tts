#!/usr/bin/env python3
"""Pre-flight dependency check for Unified TTS.

Run this before starting the server to get a clear report of what's
installed and what's missing.  Exit code 0 = ready, 1 = missing deps.

Usage:
    python3 check_deps.py          # full check (all engines)
    python3 check_deps.py --core   # minimal: web UI + online engines only
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

# ─── Check groups ─────────────────────────────────────────────────────────────
# Each tuple: (group_name, display_name, [(import_name, pip_package, install_cmd)])

CORE = [
    ("fastapi",       "fastapi",        "pip install 'fastapi>=0.110'"),
    ("uvicorn",       "uvicorn",        "pip install 'uvicorn[standard]>=0.29'"),
    ("pydantic",      "pydantic",       "pip install 'pydantic>=2.0'"),
    ("numpy",         "numpy",          "pip install 'numpy>=1.24'"),
    ("soundfile",     "soundfile",      "pip install 'soundfile>=0.12'"),
    ("psutil",        "psutil",         "pip install 'psutil>=5.9'"),
]

LOCAL = [
    ("librosa",       "librosa",        "pip install 'librosa>=0.10'"),
    ("piper_tts",     "piper-tts",      "pip install 'piper-tts>=1.2.0'"),
    ("kokoro_onnx",   "kokoro-onnx",   "pip install 'kokoro-onnx>=0.4.0'"),
    ("kittentts",     "kittentts",     "pip install 'kittentts>=0.8.1'"),
    ("onnxruntime",   "onnxruntime",   "pip install 'onnxruntime>=1.17'"),
    ("tokenizers",    "tokenizers",    "pip install 'tokenizers>=0.15'"),
]

ONLINE = [
    ("edge_tts",      "edge-tts",       "pip install 'edge-tts>=6.1.0'"),
    ("gtts",          "gTTS",           "pip install 'gTTS>=2.5.0'"),
]

AUDIO = [
    ("pydub",         "pydub",          "pip install 'pydub>=0.25'"),
]

# System-level checks
SYSTEM = [
    ("espeak-ng",     None,             "sudo apt install espeak-ng  # Debian/Ubuntu\n  sudo dnf install espeak-ng  # Fedora"),
    ("ffmpeg",        None,             "sudo apt install ffmpeg  # Debian/Ubuntu\n  sudo dnf install ffmpeg  # Fedora"),
]


def _check_import(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def _check_binary(name: str) -> bool:
    return shutil.which(name) is not None


def _section(lines: list[str], title: str) -> None:
    if not lines:
        return
    print(f"\n  {title}")
    for line in lines:
        print(f"    {line}")


def check(groups: list[tuple], system_checks: list[tuple] | None = None) -> int:
    missing_core: list[str] = []
    missing_opt: list[str] = []
    missing_sys: list[str] = []
    warnings: list[str] = []

    for mod, pkg, cmd in groups:
        if _check_import(mod):
            continue
        # Categorise: core deps are fatal, everything else is a soft warning
        is_core_group = groups is CORE
        if is_core_group:
            missing_core.append(f"  ✗ {pkg}  —  run: {cmd}")
        else:
            missing_opt.append(f"  ⚠ {pkg}  —  run: {cmd}")

    if system_checks:
        for bin_name, pkg, cmd in system_checks:
            if not _check_binary(bin_name):
                missing_sys.append(f"  ✗ {bin_name}  —  run:\n      {cmd.strip().replace(chr(10), '  ')}")

    if missing_core:
        _section(missing_core, "❌ Missing (required)")
    if missing_opt:
        _section(missing_opt, "⚠  Missing (optional — UI will work without these)")
    if missing_sys:
        _section(missing_sys, "❌ Missing (system package required for local engines)")

    if warnings:
        print("\n  " + "\n  ".join(warnings))

    if missing_core or missing_sys:
        print("\n  Run `pip install -r requirements.txt` (or the specific extras below) to fix.\n")
        return 1

    print("\n  ✓ All required dependencies are installed.")
    if missing_opt:
        print("  (optional packages missing — web UI + online engines will work fine)")
    return 0


def main() -> int:
    mode = "full"
    if len(sys.argv) > 1:
        mode = sys.argv[1].lstrip("-")

    print("Unified TTS — Dependency Check")
    print("=" * 44)

    if mode == "core":
        # Minimal install: web UI + online engines only
        ok = check(CORE)
        check(ONLINE)  # informational only
        # espeak-ng is only needed for local engines, warn not fail
        check([], system_checks=[("espeak-ng", None, "  (required only for Kokoro engine)")])
        return ok

    # Full check
    ok_core = check(CORE)
    ok_local = check(LOCAL)
    ok_online = check(ONLINE)
    ok_audio = check(AUDIO)
    ok_sys = check([], system_checks=SYSTEM)

    return max(ok_core, ok_local, ok_online, ok_audio, ok_sys)


if __name__ == "__main__":
    sys.exit(main())
