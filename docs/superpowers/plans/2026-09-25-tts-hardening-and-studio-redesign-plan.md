# Unified TTS Hardening and Studio Redesign Implementation Plan

## Goal and success criteria

Implement the approved design in the existing FastAPI, Python engine layer, SQLite history layer, and vanilla HTML/CSS/JavaScript frontend. The result must be safer when exposed beyond localhost, deterministic under concurrent requests, installable from a built artifact, and visually redesigned into the focused audio studio described in the design specification.

Success criteria:

- Caller-supplied Piper voices cannot escape the Piper model directory.
- Uploads are bounded while being read; oversized content is rejected without unbounded buffering.
- Audio8 start/stop and engine load/unload transitions are concurrency-safe.
- Downloads are integrity-checked, cleaned on failure, and cannot race on one .part file.
- Batch artifacts participate in retention; history ordering is deterministic.
- Clean wheel/sdist installation serves / and /static/app.js.
- UI is a coherent, accessible audio-studio interface with working labels, tabs, dialogs, errors, progress/loading feedback, pagination, confirmations, and mobile touch targets.
- Tests never touch real user data, and the full suite plus browser/package smoke checks pass.

## Architecture and file map

The existing module boundaries remain:

- server.py owns FastAPI routes, request validation, orchestration, history, and output serving.
- tts_engine.py owns the common engine contract, synthesis pipeline, model state, locking, and engine implementations.
- audio8_manager.py owns the Audio8 subprocess lifecycle and internal HTTP client.
- model_downloader.py owns Hugging Face downloads and download state.
- templates/index.html contains semantic page structure.
- static/style.css contains the visual system and responsive layout.
- static/app.js contains client state and interaction behavior.
- tests/ contains unit/API/packaging regression tests.
- pyproject.toml is the canonical package/dependency definition.
- README.md, AGENTS.md, Dockerfile, and CI configuration are synchronized release/documentation surfaces.

No new framework or frontend build step will be introduced. Small reusable helpers will stay near the existing owner module unless a separate module clearly improves isolation.

## Task sequence

### Task 1: Establish isolated test fixtures and lifecycle coverage

Files: tests/conftest.py, tests/test_api.py, tests/test_audio8_manager.py

1. Add a test that a configured TTS_DATA_DIR/TTS_OUTPUT_DIR is still overridden by the test fixture, and run it to expose the current setdefault behavior.
2. Change fixtures to assign isolated temporary paths unconditionally and clean them after the session.
3. Inject pid_path and log_path into Audio8ServiceManager and update manager tests to use temporary paths.
4. Add a lifespan test using with TestClient(app) while mocking daemon shutdown and monitor behavior.
5. Add tests for deterministic history ordering with equal timestamps.
6. Run python3 -m pytest -q tests/conftest.py tests/test_audio8_manager.py tests/test_api.py.

Done when: tests never reference real user directories or the real /tmp/audio8_unified_tts.pid, and startup/shutdown code is covered.

### Task 2: Close the Piper voice and upload resource paths

Files: tts_engine.py, server.py, tests/test_validation.py, tests/test_api.py

1. Add a failing test proving a traversal voice is rejected by the API with 400 and does not reach PiperVoice.load.
2. Add a shared validate_voice/containment check in the server orchestration path and a defensive check in PiperEngine._load_voice.
3. Apply the validation to single generation, regeneration, preview, and batch paths.
4. Add a failing test with a fake UploadFile whose read raises if called for an oversized declared Content-Length.
5. Implement bounded chunked upload reading with a 30 MB maximum and reject before concatenation.
6. Log unexpected synthesis/upload errors with logger.exception, then return sanitized client errors.
7. Add a bounded GenerateRequest.text limit aligned with the UI and a local synthesis semaphore.
8. Run focused API and validation tests, then the full suite.

Done when: unsafe voices return 400, oversized uploads are rejected during streaming, ordinary API errors remain sanitized, and long single-generation requests are bounded.

### Task 3: Make Audio8 and engine lifecycle concurrency-safe

Files: audio8_manager.py, tts_engine.py, server.py, tests/test_audio8_manager.py, tests/test_engine_base.py

1. Add a test that simultaneous start() calls invoke only one subprocess and retain the correct process handle.
2. Add a manager lock around start()/stop(), with health re-check inside the lock and safe temporary paths.
3. Add a test that an unload cannot run while a request is active.
4. Update TTSEngine.unload() implementations or a base coordination helper to acquire the serialization lock and recheck busy state.
5. Add tests proving concurrent _ensure_loaded() calls load a model once for Kokoro and Kitten.
6. Replace monitor sleep() with an interruptible event wait.
7. Add tests for clean shutdown and no duplicate Audio8 process behavior.
8. Run focused manager/base tests and then the full suite.

Done when: concurrent lifecycle operations cannot clear in-use models or create duplicate daemons.

### Task 4: Harden model downloads and artifact paths

Files: model_downloader.py, tests/test_packaging.py, new downloader tests under tests/

1. Add tests for rejected Hugging Face paths, incomplete content, .part cleanup, and concurrent start_download().
2. Add a safe destination resolver that rejects absolute paths, . or .., and unresolved containment failures.
3. Set download state to downloading before releasing the task lock and reject duplicate starts.
4. Compare received size with expected metadata when available, flush/fsync, atomically rename, and remove partial files on failure.
5. Add pinned checksums where the project can maintain them; otherwise fail closed or clearly mark hash verification unavailable rather than silently accepting corruption.
6. Add tests for state transitions and all model download helpers using mocked HTTP and filesystem boundaries.
7. Run focused downloader tests and the full suite.

Done when: truncated or unsafe downloads cannot be reported installed and retries do not leave unbounded partial files or race on one path.

### Task 5: Add artifact retention, safe output access, and API error clarity

Files: server.py, tts_engine.py, tests/test_api.py, new retention tests

1. Add a database migration/index for artifact retention and deterministic created_at, id ordering.
2. Track batch ZIP artifacts with their timestamps so cleanup removes them.
3. Replace per-request threading.Timer instances with a single bounded cleanup scheduler.
4. Add an authenticated output endpoint or a safe signed/checked serving strategy without breaking local browser playback.
5. Add tests for ZIP cleanup, deterministic pagination, output access with/without API key, and failed batch cleanup.
6. Add user-facing error mapping for missing models, invalid voices, unavailable daemon, and unsupported formats.
7. Run retention/API tests and the full suite.

Done when: output access follows the chosen threat model, batch artifacts are reclaimable, and history pages cannot skip or duplicate tied records.

### Task 6: Correct packaging, dependencies, Docker, and CI

Files: pyproject.toml, requirements.txt, check_deps.py, Dockerfile, .github/workflows/ci.yml, README.md, AGENTS.md, tests/test_packaging.py

1. Add tests for documented extras and clean artifact contents.
2. Define a valid canonical install strategy and update check_deps.py groups to match it.
3. Include static/ and templates/ in wheel/sdist package data.
4. Build wheel and sdist, install into a clean temporary environment, and smoke-test import, /, and /static/app.js.
5. Add non-root Docker execution, healthcheck, and Audio8 voice/model volumes.
6. Add Python matrix, coverage, and clean install/run.sh/Docker checks to CI.
7. Synchronize README and AGENTS install commands, engine lists, sizes, CI behavior, and architecture diagrams.
8. Run packaging tests and the full suite.

Done when: a clean artifact install serves the UI and the documented minimal install command works.

### Task 7: Redesign the UI structure and visual system

Files: templates/index.html, static/style.css, static/app.js

1. Replace the current dense page structure with the approved focused audio-studio hierarchy.
2. Add a real page heading, semantic landmarks, visible labels, descriptions, and tab semantics.
3. Rework CSS tokens, spacing, hierarchy, responsive breakpoints, contrast, focus states, and 44px touch targets while retaining light/dark themes.
4. Add accessible model status, empty state, loading state, and inline error regions.
5. Update dynamic engine/parameter rendering to create labelled controls and selected states.
6. Use safe DOM construction for untrusted values where practical.
7. Verify the rendered page at 375, 768, 1024, and 1440px.

Done when: the UI has the intended visual hierarchy, no source-level accessibility gaps in the audited controls, and no horizontal overflow at the target viewports.

### Task 8: Finish UI interaction correctness

Files: static/app.js, static/style.css, templates/index.html, browser verification

1. Add keyboard tab behavior and selected-state announcements.
2. Implement native/fallback dialog focus management, Escape, focus restoration, and inline errors.
3. Replace the nonfunctional batch progress bar with real polling or honest loading feedback.
4. Reset Regenerate on Clear.
5. Implement history pagination, delete confirmation, cleanup confirmation, pending state, and inline errors.
6. Remove API keys from URLs immediately and avoid unsafe HTML interpolation.
7. Run browser smoke tests, keyboard checks, console/network checks, and screenshot evidence at all target viewports.

Done when: all audited UI state bugs are fixed and browser verification reports no console errors or failed requests.

### Task 9: Final regression and delivery verification

Files: all changed files

1. Run python3 -m pytest -q.
2. Run ruff check . and mypy --ignore-missing-imports . when the tools are available.
3. Build wheel/sdist and run the clean artifact smoke test.
4. Run API, Docker/run.sh, and browser verification.
5. Confirm no real data/model/output files were changed and git status contains only intended changes.
6. Review the final diff for scope, security, accessibility, and documentation consistency.

Done when: all verification evidence is recorded and the final report distinguishes passing checks from environment-limited checks.

## Edge cases and failure modes

- Piper voice values containing /, backslash, dot, or .. are rejected.
- An upload with a missing or false Content-Length is still bounded by chunk reading.
- A download failure, short response, unsafe remote path, duplicate request, or rename failure leaves no installed artifact and no unbounded partial file.
- Concurrent Audio8 startup uses one process; reload windows do not spawn a second daemon.
- Unload never clears a model while synthesis is active.
- Batch and preview cleanup uses bounded shared scheduling rather than one timer per request.
- History rows with equal timestamps remain deterministic across pages.
- A packaged wheel contains the UI assets and can serve them without the source checkout.
- Dialogs restore focus, expose errors inside the active overlay, and remain keyboard usable.
- Mobile controls meet the minimum touch-target requirement.

## Assumptions

- Local-only use remains supported; public exposure will require the documented API key/TLS/reverse-proxy setup.
- The UI remains a thin client; synthesis continues to run server-side.
- Existing model licenses and the vendored Audio8 boundary remain unchanged except for security/reliability fixes.
- A pinned hash is introduced only where a trusted hash is available; otherwise size and completion checks are the minimum required behavior.
- Generated audio remains available to the local browser; protected output serving must preserve that flow.
- The first implementation keeps the existing engine IDs and avoids adding a new engine.
