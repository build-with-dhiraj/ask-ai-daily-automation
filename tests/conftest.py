"""Shared pytest helpers.

`_chromium_installed()` is consumed by the Playwright-backed renderer tests
(see `tests/test_poster_renderer.py`) so they can skip cleanly on a dev
machine that hasn't run `python -m playwright install chromium` yet.
"""
from __future__ import annotations

import os
from pathlib import Path


def _chromium_installed() -> bool:
    """Return True if a Playwright Chromium build is present on this machine.

    Detection strategy (cheap, no Playwright import required):
      1. Honour `PLAYWRIGHT_BROWSERS_PATH` if set.
      2. Otherwise probe the per-OS default cache dir for any `chromium-*` build.
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    else:
        home = Path.home()
        # macOS
        candidates.append(home / "Library" / "Caches" / "ms-playwright")
        # Linux
        candidates.append(home / ".cache" / "ms-playwright")
        # Windows
        candidates.append(home / "AppData" / "Local" / "ms-playwright")

    for root in candidates:
        if not root.exists():
            continue
        for child in root.iterdir():
            name = child.name.lower()
            if name.startswith("chromium-") or name.startswith("chromium_headless_shell-"):
                return True
    return False
