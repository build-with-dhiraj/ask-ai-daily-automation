"""CI lint: poster templates contain none of impeccable's banned patterns.

Scans templates/*.j2 for known AI-generated-UI tells per impeccable.style
(side-stripe borders, hero-metric Inter-everywhere, gradient text, etc.).
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"

POSTER_TEMPLATES = (
    "poster_scoreboard.html.j2",
    "poster_digest.html.j2",
    "_sparkline.html.j2",
)


def _load(name: str) -> str:
    path = TEMPLATES / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


class TestNoSideStripeBorders(unittest.TestCase):
    """3px+ colored left/right border is impeccable's #1 AI-UI tell."""

    PATTERN = re.compile(
        r"border-(left|right)\s*:\s*[3-9]\d*px\s+[a-z]+\s+#[0-9a-fA-F]",
        re.IGNORECASE,
    )

    def test_no_side_stripe_borders_in_templates(self) -> None:
        offenders = []
        for name in POSTER_TEMPLATES:
            text = _load(name)
            for match in self.PATTERN.finditer(text):
                offenders.append((name, match.group(0)))
        if offenders:
            self.fail(
                "Side-stripe borders found (banned per impeccable/slop):\n"
                + "\n".join(f"  {n}: {m}" for n, m in offenders)
            )


class TestInterIsNotPrimaryFace(unittest.TestCase):
    """Inter is banned as the primary body face."""

    def test_inter_not_in_font_family_declarations(self) -> None:
        offenders = []
        for name in POSTER_TEMPLATES:
            text = _load(name)
            # Look for font-family declarations that lead with Inter.
            for match in re.finditer(
                r"font-family\s*:\s*['\"]?Inter['\"]?",
                text,
                re.IGNORECASE,
            ):
                offenders.append((name, match.group(0)))
        if offenders:
            self.fail(
                "Inter found as primary font face (banned per "
                "impeccable/slop overused-fonts rule):\n"
                + "\n".join(f"  {n}: {m}" for n, m in offenders)
            )


class TestGeistIsNotPrimaryFace(unittest.TestCase):
    """Geist is banned as primary face (AI-default-adjacent)."""

    def test_geist_not_in_font_family_declarations(self) -> None:
        offenders = []
        for name in POSTER_TEMPLATES:
            text = _load(name)
            for match in re.finditer(
                r"font-family\s*:\s*['\"]?Geist['\"]?",
                text,
                re.IGNORECASE,
            ):
                offenders.append((name, match.group(0)))
        if offenders:
            self.fail(
                "Geist found as primary font face (AI-default-adjacent):\n"
                + "\n".join(f"  {n}: {m}" for n, m in offenders)
            )


class TestNoGradientText(unittest.TestCase):
    """background-clip: text combined with gradient is banned."""

    def test_no_background_clip_text_with_gradient(self) -> None:
        offenders = []
        for name in POSTER_TEMPLATES:
            text = _load(name)
            if "background-clip" in text and "gradient" in text:
                # Heuristic: both present in the same file is enough to flag.
                # If false positives crop up, tighten to require proximity.
                offenders.append(name)
        if offenders:
            self.fail(
                "background-clip: text + gradient detected (banned):\n"
                + "\n".join(f"  {n}" for n in offenders)
            )


class TestNoRoundedCornersOnPosterCard(unittest.TestCase):
    """Poster register is squared corners. Hairlines do the work."""

    PATTERN = re.compile(
        r"border-radius\s*:\s*(?!0[;\s])[^;]+",
        re.IGNORECASE,
    )

    def test_no_nonzero_border_radius_in_posters(self) -> None:
        offenders = []
        for name in POSTER_TEMPLATES:
            text = _load(name)
            for match in self.PATTERN.finditer(text):
                offenders.append((name, match.group(0).strip()))
        if offenders:
            self.fail(
                "Nonzero border-radius found (poster register is "
                "squared corners):\n"
                + "\n".join(f"  {n}: {m}" for n, m in offenders)
            )


class TestNoDecorativeBoxShadow(unittest.TestCase):
    """Drop shadows on rounded rectangles is impeccable's lazy combination."""

    PATTERN = re.compile(
        r"box-shadow\s*:\s*(?!none[;\s])[^;]+",
        re.IGNORECASE,
    )

    def test_no_box_shadow_in_poster_templates(self) -> None:
        offenders = []
        for name in POSTER_TEMPLATES:
            text = _load(name)
            for match in self.PATTERN.finditer(text):
                offenders.append((name, match.group(0).strip()))
        if offenders:
            self.fail(
                "box-shadow found (poster is two-dimensional, no Z-axis):\n"
                + "\n".join(f"  {n}: {m}" for n, m in offenders)
            )


class TestNoGoogleFontsCdnAtRender(unittest.TestCase):
    """Poster templates self-host fonts; no Google Fonts CDN at render."""

    def test_no_fonts_googleapis_in_poster_templates(self) -> None:
        offenders = []
        for name in POSTER_TEMPLATES:
            text = _load(name)
            if "fonts.googleapis.com" in text:
                offenders.append(name)
        if offenders:
            self.fail(
                "fonts.googleapis.com reference found (templates must "
                "use self-hosted WOFF2 from static/fonts/):\n"
                + "\n".join(f"  {n}" for n in offenders)
            )


class TestNoTailwindCdnAtRender(unittest.TestCase):
    """Poster templates have inline <style>; no Tailwind CDN script tag."""

    def test_no_tailwindcss_cdn_script(self) -> None:
        offenders = []
        for name in POSTER_TEMPLATES:
            text = _load(name)
            if "cdn.tailwindcss.com" in text:
                offenders.append(name)
        if offenders:
            self.fail(
                "cdn.tailwindcss.com script tag found (poster templates "
                "use inline <style>, not CDN):\n"
                + "\n".join(f"  {n}" for n in offenders)
            )


if __name__ == "__main__":
    unittest.main()
