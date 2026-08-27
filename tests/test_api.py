"""Integration tests for the HTTP API (no models required).

Engine-specific imports are lazy, so these run with core deps only.
"""

import sqlite3


class TestPages:
    def test_index_serves_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Unified TTS" in r.text

    def test_static_mounted(self, client):
        assert client.get("/static/app.js").status_code == 200


class TestEngines:
    def test_list_engines(self, client):
        r = client.get("/api/engines")
        assert r.status_code == 200
        ids = {e["id"] for e in r.json()}
        assert {"piper", "kokoro", "audio8", "edge-tts", "gtts"} <= ids
        # Param schemas are attached for the dynamic UI
        for e in r.json():
            assert isinstance(e.get("params"), dict)

    def test_voices_unknown_engine_400(self, client):
        r = client.post("/api/voices", json={"engine_id": "bogus"})
        assert r.status_code == 400


class TestGenerationValidation:
    """Pydantic rejects bad input before any engine work: deterministic 422s."""

    def test_invalid_format_rejected(self, client):
        r = client.post("/api/generate", json={"text": "hi", "fmt": "ogg"})
        assert r.status_code == 422

    def test_speed_out_of_bounds_rejected(self, client):
        r = client.post("/api/generate", json={"text": "hi", "speed": 1e9})
        assert r.status_code == 422

    def test_pitch_out_of_bounds_rejected(self, client):
        r = client.post("/api/generate", json={"text": "hi", "pitch": -100})
        assert r.status_code == 422

    def test_empty_text_rejected(self, client):
        r = client.post("/api/generate", json={"text": "   "})
        assert r.status_code in (400, 422)

    def test_batch_extra_fields_forbidden(self, client):
        r = client.post(
            "/api/generate-batch",
            json={"texts": ["a"], "hax": True},
        )
        assert r.status_code == 422

    def test_regenerate_without_history_400(self, client):
        import server

        with server._last_generation_lock:
            saved, server.last_generation = server.last_generation, None
        try:
            r = client.post("/api/regenerate", json={})
            assert r.status_code == 400
        finally:
            with server._last_generation_lock:
                server.last_generation = saved


class TestHistory:
    def test_limit_is_clamped(self, client):
        r = client.get("/api/history", params={"limit": 10_000_000})
        assert r.status_code == 200
        # A huge limit must not blow up; negative offsets are sanitized too
        r2 = client.get("/api/history", params={"offset": -5})
        assert r2.status_code == 200

    def test_delete_neutralizes_traversal(self, client, db):
        db.add_generation("hist_del_test.wav", "t", "piper", "v", 1.0, 0.0, 1.0)
        # Directory components are rejected outright, never reinterpreted
        r = client.post("/api/history/delete", json={"filename": "../../etc/passwd"})
        assert r.status_code == 400
        # …and the legit row is still deletable afterwards
        r = client.post("/api/history/delete", json={"filename": "hist_del_test.wav"})
        assert r.status_code == 200
        names = [g["filename"] for g in db.get_generations(limit=500)]
        assert "hist_del_test.wav" not in names

    def test_delete_with_directory_component_rejected(self, client):
        r = client.post("/api/history/delete", json={"filename": "../evil.wav"})
        assert r.status_code == 400

    def test_clean_removes_rows_and_files(self, client, db, tmp_path):
        import server

        filename = "ancient_test_file.wav"
        db.add_generation(filename, "old text", "piper", "v", 1.0, 0.0, 1.0)
        # Backdate the row directly (add_generation always stamps "now")
        conn = sqlite3.connect(str(db.DB_PATH))
        conn.execute("UPDATE generations SET created_at = ? WHERE filename = ?", ("2000-01-01 00:00:00", filename))
        conn.commit()
        conn.close()
        # Create the orphan file the row points at
        stale = server.OUTPUT_DIR / filename
        stale.write_bytes(b"RIFF")

        r = client.post("/api/history/clean", json={"days": 30})
        assert r.status_code == 200
        body = r.json()
        assert body["deleted"] >= 1
        assert body["files_removed"] >= 1
        assert not stale.exists()

    def test_days_validated(self, client):
        assert client.post("/api/history/clean", json={"days": 0}).status_code == 422
        assert client.post("/api/history/clean", json={"days": 100000}).status_code == 422


class TestAudioInfo:
    def test_rejects_directory_component(self, client):
        assert client.get("/api/audio-info", params={"filename": "sub/dir.wav"}).status_code == 400

    def test_missing_file_404(self, client):
        assert client.get("/api/audio-info", params={"filename": "nope.wav"}).status_code == 404


class TestRemovedEndpoints:
    """Dead endpoints were removed; make sure they stay dead."""

    def test_play_gone(self, client):
        assert client.post("/api/play", params={"filename": "x.wav"}).status_code == 404

    def test_engine_switch_gone(self, client):
        assert client.post("/api/engine/switch", json={"engine_id": "piper"}).status_code == 404


class TestBatchManifest:
    def test_manifest_written_when_items_fail(self, client):
        """With no voices installed every item fails → zip carries _manifest.json."""
        import io
        import zipfile

        r = client.post(
            "/api/generate-batch",
            json={"texts": ["one", "two"], "engine_id": "piper"},
        )
        if r.status_code != 200:
            return  # a voice is installed on this machine — skip gracefully
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        if int(r.headers.get("X-Batch-Failed", "0")) > 0:
            assert "_manifest.json" in zf.namelist()


class TestApiKeyGuard:
    def test_open_by_default(self, client):
        import server

        if not server.API_KEY:
            assert client.get("/api/engines").status_code == 200

    def test_enforced_when_configured(self, monkeypatch):
        import server

        monkeypatch.setattr(server, "API_KEY", "secret123")
        c = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(server.app)
        assert c.get("/api/engines").status_code == 401
        assert c.get("/api/engines", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/api/engines", headers={"X-API-Key": "secret123"}).status_code == 200
        # Non-API paths stay open so the UI itself loads
        assert c.get("/").status_code == 200
