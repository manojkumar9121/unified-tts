from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def test_studio_markup_has_semantic_regions_and_labelled_tabs():
    assert '<h1' in HTML
    assert 'role="tablist"' in HTML
    assert 'role="tab"' in HTML
    assert 'aria-selected="true"' in HTML
    assert 'for="text"' in HTML and 'for="batchText"' in HTML
    assert 'for="speed"' in HTML and 'for="pitch"' in HTML and 'for="format"' in HTML
    assert 'role="region"' in HTML


def test_studio_markup_exposes_dialog_and_state_regions():
    for element_id in ("modelsModal", "registerModal", "historyPanel", "modelsError", "registerError", "historyError"):
        assert f'id="{element_id}"' in HTML
    assert 'aria-live="polite"' in HTML
    assert 'aria-busy="false"' in HTML and 'aria-busy' in JS


def test_frontend_scrubs_key_and_handles_tabs_progress_history_and_clear():
    assert "replaceState" in JS
    assert "ArrowRight" in JS and "ArrowLeft" in JS
    assert "batch-progress" in JS
    assert "historyPage" in JS and "Previous" in JS and "Next" in JS
    assert "confirm" in JS
    assert "canRegenerate = false" in JS
