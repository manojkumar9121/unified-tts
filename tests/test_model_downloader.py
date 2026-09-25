"""Reliability tests for the stdlib Hugging Face downloader."""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import model_downloader as md


def _response(body: bytes, content_length: int | None = None) -> io.BytesIO:
    response = io.BytesIO(body)
    response.headers = {} if content_length is None else {"Content-Length": str(content_length)}
    return response


@pytest.mark.parametrize(
    "remote_path",
    ["/absolute.bin", "../escape.bin", "nested/../escape.bin", "./same.bin", r"..\escape.bin"],
)
def test_safe_destination_rejects_unsafe_remote_paths(tmp_path: Path, remote_path: str):
    with pytest.raises(ValueError):
        md._safe_destination(tmp_path, remote_path)


def test_safe_destination_rejects_resolved_symlink_escape(tmp_path: Path):
    outside = tmp_path.parent / "outside-download-root"
    outside.mkdir(exist_ok=True)
    base = tmp_path / "models"
    base.mkdir()
    (base / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        md._safe_destination(base, "linked/model.bin")


def test_download_rejects_incomplete_content_and_removes_partial(tmp_path: Path, monkeypatch):
    dest = tmp_path / "model.bin"
    monkeypatch.setattr(md.urllib.request, "urlopen", lambda *_args, **_kwargs: _response(b"abc", 4))

    with pytest.raises(ValueError, match="(?i:incomplete)"):
        md._download_to_file("https://example.test/model.bin", dest, lambda _inc, _total: None)

    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_rejects_metadata_size_mismatch_and_removes_partial(tmp_path: Path, monkeypatch):
    dest = tmp_path / "model.bin"
    monkeypatch.setattr(md.urllib.request, "urlopen", lambda *_args, **_kwargs: _response(b"abc", 3))

    with pytest.raises(ValueError, match="size"):
        md._download_to_file(
            "https://example.test/model.bin",
            dest,
            lambda _inc, _total: None,
            expected_size=4,
        )

    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_fsyncs_and_atomically_replaces_destination(tmp_path: Path, monkeypatch):
    dest = tmp_path / "nested" / "model.bin"
    dest.parent.mkdir()
    dest.write_bytes(b"old")
    fsync_calls: list[int] = []
    real_fsync = __import__("os").fsync

    def tracking_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(md.urllib.request, "urlopen", lambda *_args, **_kwargs: _response(b"new", 3))
    monkeypatch.setattr(md.os, "fsync", tracking_fsync)

    md._download_to_file(
        "https://example.test/model.bin",
        dest,
        lambda _inc, _total: None,
        expected_size=3,
    )

    assert dest.read_bytes() == b"new"
    assert len(fsync_calls) >= 2
    assert not dest.with_name(dest.name + ".part").exists()


def test_audio8_tree_rejects_unsafe_remote_path(monkeypatch):
    monkeypatch.setattr(
        md,
        "_fetch_json",
        lambda _url: [{"type": "file", "path": "../outside.bin", "size": 3}],
    )

    with pytest.raises(ValueError):
        md._audio8_file_list()


def test_concurrent_start_creates_one_worker(monkeypatch):
    real_threading = threading
    created: list[object] = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            created.append(self)

        def start(self):
            return None

    with md._lock:
        md._tasks.pop("kokoro", None)
    monkeypatch.setattr(md, "threading", SimpleNamespace(Lock=real_threading.Lock, Thread=FakeThread))

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _index: md.start_download("kokoro"), range(32)))

    assert len(created) == 1
    assert all(status["state"] == "downloading" for status in statuses)
    assert all(status is not statuses[0] for status in statuses[1:])
