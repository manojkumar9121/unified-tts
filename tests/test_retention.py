"""Artifact retention, cleanup scheduling, and API mapping tests."""

from __future__ import annotations

import sqlite3
import threading

import server
import tts_engine as db


def test_history_index_matches_deterministic_order():
    db.init_db()
    conn = sqlite3.connect(str(db.DB_PATH))
    try:
        indexes = {row[1]: row[2] for row in conn.execute("PRAGMA index_list(generations)")}
        assert "idx_generations_created_at_id" in indexes
        columns = [row[2] for row in conn.execute("PRAGMA index_info(idx_generations_created_at_id)")]
        assert columns == ["created_at", "id"]
    finally:
        conn.close()


def test_batch_zip_is_tracked_for_retention(client, monkeypatch):
    import server as server_module

    before = {row["path"] for row in server_module.get_artifacts()}
    audio = server_module.OUTPUT_DIR / "tracked_batch_item.wav"
    audio.write_bytes(b"RIFFfake")
    monkeypatch.setattr(
        server_module,
        "_run_batch_tasks",
        lambda *_args: ("batch-test", [(0, (str(audio), 1.0))], 1),
    )

    response = client.post(
        "/api/generate-batch",
        json={"texts": ["hello"], "engine_id": "piper"},
    )

    assert response.status_code == 200
    artifacts = server_module.get_artifacts()
    new_artifacts = [row for row in artifacts if row["path"] not in before]
    assert len(new_artifacts) == 1
    assert new_artifacts[0]["kind"] == "batch_zip"
    assert new_artifacts[0]["path"].startswith("batch_")


def test_clean_history_removes_expired_batch_zip(client):
    artifact = server.OUTPUT_DIR / "expired_batch.zip"
    artifact.write_bytes(b"PK")
    db.add_artifact(artifact.name, "batch_zip", created_at="2000-01-01 00:00:00")

    response = client.post("/api/history/clean", json={"days": 30})

    assert response.status_code == 200
    assert not artifact.exists()
    assert all(row["path"] != artifact.name for row in db.get_artifacts())


def test_cleanup_scheduler_is_shared_bounded_and_runs_overflow_immediately():
    ran_immediately = threading.Event()
    scheduler = server.CleanupScheduler(max_entries=2)
    try:
        assert scheduler.schedule(60, lambda: None) is True
        assert scheduler.schedule(60, lambda: None) is True
        assert scheduler.schedule(60, ran_immediately.set) is False
        assert ran_immediately.wait(timeout=1)
        assert scheduler.pending_count == 2
        assert scheduler.thread is not None
        assert scheduler.thread.name == "unified-tts-cleanup"
    finally:
        scheduler.close()


def test_missing_model_maps_to_sanitized_conflict(client, monkeypatch):
    def missing_model(*_args, **_kwargs):
        raise FileNotFoundError("/private/models/secret/model.onnx")

    monkeypatch.setattr(server, "run_generation", missing_model)
    response = client.post(
        "/api/generate",
        json={"engine_id": "piper", "text": "hello", "voice": "missing"},
    )

    assert response.status_code == 409
    assert "download" in response.json()["detail"].lower()
    assert "private" not in response.json()["detail"]


def test_audio8_unavailable_maps_to_service_unavailable(client, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("Audio8 service is not running")

    monkeypatch.setattr(server, "run_generation", unavailable)
    response = client.post(
        "/api/generate",
        json={"engine_id": "audio8", "text": "hello", "voice": "voice"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Audio8 service unavailable"
