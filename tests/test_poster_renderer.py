"""Tests for `scripts.poster_renderer`.

Verifies that each of the four canonical sample inputs renders to a valid
PNG byte stream. We assert the PNG magic header rather than pixel content
— pixel-diffing is brittle across font versions and OS releases, and the
design review is owned by the Design Specialist with eyeballs on
`/tmp/poster_*.png`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.conftest import _chromium_installed

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = REPO_ROOT / "templates" / "sample_inputs"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_SKIP_REASON = (
    "Playwright Chromium not installed locally — run "
    "`python -m playwright install chromium` to enable, or set CI=true."
)

requires_chromium = pytest.mark.skipif(
    os.environ.get("CI") != "true" and not _chromium_installed(),
    reason=_SKIP_REASON,
)


def _load_sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text())


@requires_chromium
def test_render_scoreboard_normal_returns_png_bytes() -> None:
    from scripts.poster_renderer import render_poster

    png = render_poster("scoreboard", _load_sample("scoreboard_normal_day.json"))
    assert png.startswith(PNG_MAGIC), "output is not a PNG"
    assert len(png) > 1000, "PNG suspiciously small"


@requires_chromium
def test_render_scoreboard_breach() -> None:
    from scripts.poster_renderer import render_poster

    png = render_poster("scoreboard", _load_sample("scoreboard_breach_day.json"))
    assert png.startswith(PNG_MAGIC)
    assert len(png) > 1000


@requires_chromium
def test_render_digest_normal() -> None:
    from scripts.poster_renderer import render_poster

    png = render_poster("digest", _load_sample("digest_normal_day.json"))
    assert png.startswith(PNG_MAGIC)
    assert len(png) > 1000


@requires_chromium
def test_render_digest_quiet() -> None:
    from scripts.poster_renderer import render_poster

    png = render_poster("digest", _load_sample("digest_quiet_day.json"))
    assert png.startswith(PNG_MAGIC)
    assert len(png) > 1000
