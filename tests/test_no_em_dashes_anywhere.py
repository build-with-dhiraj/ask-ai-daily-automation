"""CI lint: no em-dashes or en-dashes in the redesign surface.

Scope: files the Phase 4 redesign owns or touches. Legacy files outside
this scope are not scanned. Expand SCOPED_FILES in a future PR if you
want to widen enforcement.

Rationale: per the locked Phase 4 plan and user instruction (universal
rule), em-dash (U+2014) and en-dash (U+2013) are banned in user-facing
content and code. Use commas, semicolons, periods, parentheses, or
sentence breaks instead.

This lint is intentionally narrow at first. The bigger pre-existing
docstring sweep in legacy modules is a separate follow-up; do not block
the redesign PR on that.
"""
from __future__ import annotations

import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# chr() at runtime (not literal chars in source) so this file is
# pure ASCII and does not self-fail the lint it implements.
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

# Files the Phase 4 redesign owns. New files added by the redesign cycle
# go here. Existing files we wrote or substantially rewrote during this
# cycle also go here.
SCOPED_FILES: tuple[str, ...] = (
    "PRODUCT.md",
    "DESIGN.md",
    "scripts/poster_render.py",
    "scripts/slack_publisher.py",
    "scripts/follow_up_generator.py",
    "scripts/poster_publisher.py",
    "scripts/poster_renderer.py",
    "templates/poster_scoreboard.html.j2",
    "templates/poster_digest.html.j2",
    "templates/_sparkline.html.j2",
    "templates/README.md",
    "tests/test_follow_up_generator.py",
    "tests/test_verdict_opening_line.py",
    "tests/test_no_em_dashes_anywhere.py",
    "tests/test_no_jargon.py",
    "tests/test_no_anti_patterns.py",
)


class TestNoLongDashesInRedesignSurface(unittest.TestCase):
    """Each redesign-surface file has zero em-dash and zero en-dash chars."""

    def test_redesign_files_have_no_em_or_en_dashes(self) -> None:
        offenders: list[tuple[str, int, int]] = []
        scanned = 0
        for rel in SCOPED_FILES:
            path = REPO_ROOT / rel
            if not path.exists():
                continue
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self.fail(f"could not read {rel}: {exc}")
            em = text.count(EM_DASH)
            en = text.count(EN_DASH)
            if em or en:
                offenders.append((rel, em, en))
        self.assertGreater(
            scanned,
            0,
            "lint scoped to redesign surface scanned zero files. Either the "
            "files were renamed or SCOPED_FILES is stale.",
        )
        if offenders:
            lines = [
                f"  {rel}: {em} em-dash, {en} en-dash"
                for rel, em, en in offenders
            ]
            self.fail(
                "Em-dash or en-dash characters found in the redesign "
                "surface. Use commas, semicolons, periods, or parens:\n"
                + "\n".join(lines)
            )


if __name__ == "__main__":
    unittest.main()
