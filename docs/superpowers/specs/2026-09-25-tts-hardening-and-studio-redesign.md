# Unified TTS Hardening and Studio Redesign

**Date:** 2026-09-25  
**Status:** Approved design for implementation

## Goal

Make Unified TTS safer, more reliable, easier to install, and visually polished without replacing the existing FastAPI + vanilla JavaScript architecture or engine registry.

## Product direction

Retain the warm amber/rose identity, but simplify the interface into a focused audio studio:

- compact header with clear engine/device status;
- engine tabs with a restrained model status strip;
- large text composition area;
- grouped voice, timing, format, and advanced controls;
- one dominant Generate action with quieter secondary actions;
- clear playback/output empty state;
- recent generations below or beside the main workspace;
- honest loading, error, progress, confirmation, and empty states.

The interface must remain usable with keyboard, screen reader, mouse, and touch, and must retain the current light/dark themes.

## Scope

### 1. Backend security and lifecycle

- Validate every caller-supplied voice against the engine’s available voices before synthesis.
- Reject unsafe paths inside Piper and Audio8 download handling.
- Read uploaded reference audio in bounded chunks, enforcing the 30 MB limit before buffering excessive data.
- Serialize Audio8 manager start/stop operations and recheck health under the lock.
- Make engine model load/unload transitions safe with the engine serialization lock and active-request checks.
- Add bounded single-generation text and local synthesis concurrency limits.
- Make regeneration state client/session-scoped rather than a single process-global value.
- Preserve tracebacks in server logs while returning sanitized API errors.
- Use constant-time API-key comparison.
- Add deterministic history ordering using created_at DESC, id DESC.

### 2. Downloads, artifacts, and retention

- Make download state transitions atomic under the downloader lock.
- Validate content length, expected artifact sizes, and pinned hashes where available.
- Flush and fsync completed files before atomic rename.
- Remove partial files after failures.
- Reject unsafe Hugging Face tree paths.
- Track batch ZIP artifacts so history cleanup can reclaim them.
- Replace per-request long-lived cleanup timers with one shared cleanup scheduler.
- Add safe output serving/retention behavior without breaking the current browser playback flow.

### 3. API and operational hardening

- Map common upload, model, daemon, and validation failures to appropriate 4xx responses.
- Add health/readiness distinctions where useful.
- Ensure shutdown waits on the monitor event rather than sleeping through it.
- Keep runtime directories configurable and create nested directories safely.

### 4. Packaging, dependencies, CI, and Docker

- Make pyproject.toml the canonical dependency definition.
- Define a real core or online install path and remove invalid requirements.txt[extra] guidance.
- Include static and templates in built wheels and sdists.
- Add clean-artifact install and UI asset smoke tests.
- Add Python-version matrix coverage, coverage reporting, and Docker/run.sh smoke checks.
- Persist Audio8 model and voice directories in Docker.
- Run the container as a non-root user and add a healthcheck.
- Update README, AGENTS, and demo documentation to match the actual engine registry and install commands.

### 5. UI redesign and accessibility

- Add a clear page heading and semantic regions.
- Associate all form controls with visible labels and descriptions.
- Implement accessible engine and mode tabs with selected state and keyboard behavior.
- Use native dialogs where practical; otherwise implement focus trapping, Escape, focus restoration, and inert backgrounds.
- Put errors inside the active dialog or drawer.
- Implement real batch progress or an honest indeterminate loading state.
- Reset Regenerate when the workspace is cleared.
- Implement history pagination, deletion confirmation, cleanup confirmation, and inline error handling.
- Raise small text contrast to WCAG AA targets.
- Increase mobile touch targets to at least 44px.
- Avoid URL/localStorage API-key exposure where possible and remove query-string keys immediately.
- Keep DOM APIs for untrusted values where practical and avoid unsafe HTML interpolation.
- Preserve reduced-motion behavior and responsive desktop/tablet/mobile layouts.

## File responsibilities

- server.py: API validation, request lifecycle, artifact serving, response/error mapping, and app configuration.
- tts_engine.py: engine interface, model load/unload state, synthesis locking, and safe voice handling.
- audio8_manager.py: daemon synchronization, secure temporary paths, health state, and HTTP error translation.
- model_downloader.py: download task state, integrity checks, path validation, and cleanup.
- templates/index.html: semantic page structure and accessible control markup.
- static/style.css: tokens, visual system, layout, responsive behavior, focus states, and touch targets.
- static/app.js: state management, accessible interactions, progress, dialogs, pagination, and safe rendering.
- tests/: regression tests for changed behaviors, isolated fixtures, lifecycle tests, and packaging smoke tests.
- pyproject.toml: canonical dependencies/extras and packaged web assets.
- README.md, AGENTS.md, Docker/CI configuration: synchronized user and release documentation.

## Testing strategy

Each behavior change follows test-first development:

1. Add a focused regression test.
2. Run it and confirm the expected failure.
3. Implement the smallest fix.
4. Run the focused test and then the full suite.
5. Refactor only while tests remain green.

Required verification includes pytest, Ruff and mypy when available, clean wheel/sdist build and install, API smoke tests, browser verification at 375px, 768px, 1024px, and 1440px, keyboard checks, browser console checks, and confirmation that tests do not modify real user data.

## Non-goals

- Replacing FastAPI, SQLite, or the vanilla JavaScript frontend.
- Replacing the engine registry with a plugin marketplace.
- Adding a new TTS engine in this work.
- Building a general-purpose multi-user cloud service.
- Redesigning the vendored Audio8 runtime beyond security and reliability fixes required by the parent application.

## Success criteria

- Piper cannot load a caller-supplied path outside its model directory.
- A large upload cannot be fully buffered before the size limit is enforced.
- Concurrent startup, load, and unload paths cannot create duplicate daemons or clear an in-use model.
- Failed or truncated downloads do not report success and do not leave unbounded partial files.
- History ordering is deterministic and batch artifacts participate in retention.
- The documented minimal install path works from a clean environment.
- A built wheel serves the UI and static assets.
- The redesigned UI has clear hierarchy, working focus and keyboard behavior, visible errors, working progress/loading feedback, and mobile-safe controls.
- The full test suite and clean-install/browser checks pass without modifying real user data.
