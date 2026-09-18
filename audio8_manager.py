"""Subprocess lifecycle manager for the Audio8 ONNX TTS service."""

from __future__ import annotations

import json as _json
import logging
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("unified_tts.audio8")

# How long a successful health check may be cached before is_running()
# probes the daemon again. Every HTTP helper calls is_running(), so without
# this cache each request could pay up to a 3s network timeout.
HEALTH_TTL_SECONDS = 5.0


class _HttpResponse:
    """Minimal HTTP response wrapper (urllib-based) with a requests-like API."""

    def __init__(self, raw):
        self.status = raw.status
        self.headers = dict(raw.headers.items())
        self._body = raw.read()

    @property
    def content(self) -> bytes:
        return self._body

    def json(self):
        return _json.loads(self._body.decode("utf-8"))

    def raise_for_status(self):
        if not 200 <= self.status < 300:
            raise RuntimeError(f"HTTP {self.status}: {self._body[:300]!r}")


class Audio8ServiceManager:
    """Manages the lifecycle of the Audio8 ONNX TTS subprocess."""

    def __init__(self, model_dir: Path, voices_dir: Path, repo_dir: Path, port: int = 8024):
        self.model_dir = model_dir.resolve()
        self.voices_dir = voices_dir.resolve()
        self.repo_dir = repo_dir.resolve()
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self._proc: subprocess.Popen | None = None
        self._adopted_pid: int | None = None  # PID of a daemon we found already running
        self._pidfile = Path("/tmp/audio8_unified_tts.pid")
        self._healthy_until: float = 0.0  # monotonic deadline for cached health

    def _mark_healthy(self) -> None:
        self._healthy_until = time.monotonic() + HEALTH_TTL_SECONDS

    def _health_cached(self) -> bool:
        return time.monotonic() < self._healthy_until

    # ── daemon detection ─────────────────────────────────────────────────────

    def _cmdline_matches(self, pid: int) -> bool:
        """True if PID exists and looks like our Audio8 daemon."""
        try:
            raw = (Path("/proc") / str(pid) / "cmdline").read_bytes().decode("utf-8", errors="ignore")
        except Exception:
            return False
        cmdline = raw.replace("\x00", " ")
        return "audio8_repo.service" in cmdline and f"--port {self.port}" in cmdline

    def _find_daemon_pid(self) -> int | None:
        """Locate the PID of a healthy Audio8 daemon listening on our port.

        Scans /proc for a uvicorn process serving ``audio8_repo.service`` on
        ``self.port``. Used to adopt daemons started outside this manager
        (e.g. by a previous server process) so we can track and manage them.
        Returns None on non-Linux hosts without /proc.
        """
        proc = Path("/proc")
        if not proc.is_dir():
            return None
        try:
            entries = list(proc.iterdir())
        except OSError:
            return None
        port_flag = f"--port {self.port}"
        for pid_dir in entries:
            if not pid_dir.name.isdigit():
                continue
            try:
                cmdline = (pid_dir / "cmdline").read_bytes().decode("utf-8", errors="ignore")
                cmdline = cmdline.replace("\x00", " ")
            except Exception:
                continue
            if "audio8_repo.service" in cmdline and port_flag in cmdline:
                return int(pid_dir.name)
        return None

    def is_running(self) -> bool:
        # A live subprocess means "starting or running", not "healthy".
        # Require a successful health probe before reporting ready; otherwise
        # callers POST during the 60-90s boot and get connection-refused.
        if self._proc is not None and self._proc.poll() is None:
            if self._health_cached():
                return True
            try:
                with urllib.request.urlopen(f"{self.url}/api/health", timeout=1) as r:
                    if r.status == 200:
                        self._mark_healthy()
                        return True
            except Exception:
                pass
            return False
        if self._health_cached():
            return True
        if self._adopted_pid is not None:
            try:
                os.kill(self._adopted_pid, 0)
            except (ProcessLookupError, PermissionError):
                self._adopted_pid = None
            else:
                if self._cmdline_matches(self._adopted_pid):
                    self._mark_healthy()
                    return True
                self._adopted_pid = None
        # Check pidfile (verify identity: PID reuse must not adopt/kill strangers)
        if self._pidfile.exists():
            try:
                pid = int(self._pidfile.read_text().strip())
                os.kill(pid, 0)
                if self._cmdline_matches(pid):
                    self._adopted_pid = pid
                    self._mark_healthy()
                    return True
            except (ProcessLookupError, ValueError, PermissionError):
                pass
        # Fallback: check if the port is listening and healthy, and adopt it
        try:
            with urllib.request.urlopen(f"{self.url}/api/health", timeout=3) as r:
                if r.status == 200:
                    self._mark_healthy()
                    if self._adopted_pid is None:
                        self._adopted_pid = self._find_daemon_pid()
                        if self._adopted_pid is not None:
                            self._pidfile.write_text(str(self._adopted_pid))
                    return True
        except Exception:
            pass
        return False

    def start(self) -> bool:
        if self.is_running():
            return True
        # Reap a stale pidfile only after verifying it really is our daemon.
        # Unverified SIGTERM could kill an unrelated process on PID reuse.
        if self._pidfile.exists():
            try:
                pid = int(self._pidfile.read_text().strip())
                if pid != self._adopted_pid and self._cmdline_matches(pid):
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(1)
            except (ProcessLookupError, ValueError, PermissionError):
                pass
            except Exception:
                pass
            try:
                self._pidfile.unlink(missing_ok=True)
            except OSError:
                pass

        env = os.environ.copy()
        env["ARKTTS_MODEL_DIR"] = str(self.model_dir)
        env["ARKTTS_VOICES_DIR"] = str(self.voices_dir)
        env["ARKTTS_REGISTRATION_DIR"] = str(self.model_dir / "registration")
        env["ARKTTS_PRECISION"] = "int4"
        env["ARKTTS_CODEC_PRECISION"] = "fp16"
        env["ARKTTS_THREADS"] = "5"

        venv_python = self.repo_dir / ".venv" / "bin" / "python"
        if not venv_python.exists():
            venv_python = Path("/usr/bin/python3")

        log_path = Path("/tmp/audio8_unified_tts.log")
        try:
            # The repo is a flat package (service.py at its root), so it must be
            # imported as `audio8_repo.service` with the parent dir on sys.path.
            self._proc = subprocess.Popen(
                [str(venv_python), "-m", "uvicorn", "audio8_repo.service:app",
                 "--app-dir", str(self.repo_dir.parent), "--host", "127.0.0.1", "--port", str(self.port)],
                cwd=str(self.repo_dir),
                env=env,
                stdout=log_path.open("ab"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._pidfile.write_text(str(self._proc.pid))
        except Exception as e:
            logger.error("Failed to start Audio8 service: %s", e)
            return False

        return self._wait_for_health(timeout=90)

    def stop(self) -> None:
        """Stop the daemon. Kills the spawned subprocess or the adopted daemon."""
        if self._proc and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGTERM)
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        else:
            target = self._adopted_pid
            if target is None and self._pidfile.exists():
                try:
                    target = int(self._pidfile.read_text().strip())
                except ValueError:
                    target = None
            if target is not None and self._cmdline_matches(target):
                try:
                    os.kill(target, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        self._proc = None
        self._adopted_pid = None
        self._healthy_until = 0.0
        self._pidfile.unlink(missing_ok=True)

    def get_client(self):
        """Return an HTTP helper for the running service."""
        if not self.is_running():
            raise RuntimeError("Audio8 service is not running")
        return self

    def get(self, url: str, timeout: float = 10.0) -> _HttpResponse:
        """GET request against the Audio8 service."""
        if not self.is_running():
            raise RuntimeError("Audio8 service is not running")
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return _HttpResponse(r)

    def post(self, url: str, json: dict | None = None, data: bytes | None = None, timeout: float = 60.0) -> _HttpResponse:
        """POST request against the Audio8 service (JSON body by default)."""
        if not self.is_running():
            raise RuntimeError("Audio8 service is not running")
        body = _json.dumps(json).encode("utf-8") if json is not None else (data or b"")
        headers = {"Content-Type": "application/json"} if json is not None else {}
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _HttpResponse(r)

    def _wait_for_health(self, timeout: int = 60) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Fail fast if the subprocess died (e.g. bad command or missing deps)
            if self._proc is not None and self._proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(f"{self.url}/api/health", timeout=1) as r:
                    if r.status == 200:
                        self._mark_healthy()
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def __del__(self):
        # Intentionally a no-op: the daemon runs in its own session and should
        # persist across server restarts. A new server instance adopts the
        # running daemon on first use instead of paying a 60-90s model reload.
        pass
