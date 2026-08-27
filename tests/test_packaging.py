"""Guards against packaging regressions.

* requirements.txt must stay in sync with pyproject.toml (the canonical spec).
* The console-script entry point (``server:main``) must actually exist.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _requirements_lines() -> list[str]:
    out = []
    for raw in (ROOT / "requirements.txt").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _pyproject_requirements() -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    deps = list(data["project"]["dependencies"])
    for extra in ("local", "online", "audio"):
        deps.extend(data["project"]["optional-dependencies"][extra])
    return deps


def _norm(req: str) -> str:
    """Canonicalize a requirement string for comparison."""
    req = req.strip().replace(" ", "")
    name = re.split(r"[\[<>=!~;]", req, maxsplit=1)[0]
    canonical_name = re.sub(r"[-_.]+", "-", name).lower()
    return canonical_name + req[len(name):]


def test_requirements_match_pyproject():
    assert sorted(map(_norm, _requirements_lines())) == sorted(map(_norm, _pyproject_requirements()))


def test_console_script_target_exists():
    import server

    assert callable(server.main)


def test_entry_point_declared():
    with (ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    scripts = data["project"].get("scripts", {})
    assert scripts.get("unified-tts") == "server:main"
