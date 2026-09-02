"""The bundled UI has to reach an installed environment, not only a checkout.

ADR-007 accepts a build-free UI on the grounds that ``pip install gdap && gdap system serve``
gives a working one. That was not true: ``web/`` lived at the repository root, hatchling ships
``src/gdap`` and nothing else, and the path was resolved four parents up from ``app.py`` — which
lands on the repo root in a checkout and on ``site-packages/../..`` in an install. A wheel
therefore answered ``/`` with the JSON fallback and ``/assets/app.js`` with 404, and the Docker
image did the same.

These tests pin the two halves of the fix: the assets live inside the package, and the app
actually serves them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import gdap
from gdap.api.app import WEB_DIR

pytestmark = pytest.mark.integration


def test_the_ui_lives_inside_the_installed_package() -> None:
    """The packaging invariant. Anything outside ``src/gdap`` is not in the wheel."""
    package_root = Path(gdap.__file__).resolve().parent

    assert WEB_DIR.resolve().is_relative_to(package_root), (
        f"{WEB_DIR} is outside {package_root}, so it will not ship — hatchling packages "
        "only src/gdap"
    )


def test_the_bundled_files_are_present() -> None:
    assert (WEB_DIR / "index.html").is_file()
    assert (WEB_DIR / "assets" / "app.js").is_file()
    assert (WEB_DIR / "assets" / "style.css").is_file()


def test_the_app_serves_the_ui_and_its_assets(api_client: Any) -> None:
    """End to end: the routes an operator's browser actually asks for."""
    index = api_client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert "<title>GDAP" in index.text

    script = api_client.get("/assets/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]

    style = api_client.get("/assets/style.css")
    assert style.status_code == 200
    assert "css" in style.headers["content-type"]
