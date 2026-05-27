"""CI lint: no em-dashes or en-dashes in Jinja templates.

Dogfood QA #5: em-dashes from the poster glyph dicts ('flat', 'neutral')
and inline banner copy were rendering as 'AI hallmark' dashes in the
production poster output. Per user instruction (CLAUDE.md, em-dash sweep
follow-ups), the rendered surfaces must contain ASCII-safe punctuation:
middle-dot (·), hyphen, or rewritten sentences.

This test is a regression bumper — fail loudly the next time an em-dash
slips into a template, so the QA cycle doesn't have to catch it visually
in Slack.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES_DIR = _ROOT / "templates"


class TestNoEmDashesInTemplates(unittest.TestCase):
    def test_no_em_or_en_dashes_in_jinja_templates(self):
        offenders = []
        for p in sorted(_TEMPLATES_DIR.glob("*.j2")):
            text = p.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "—" in line:  # em-dash
                    offenders.append(f"em-dash in {p.name}:{lineno}: {line!r}")
                if "–" in line:  # en-dash
                    offenders.append(f"en-dash in {p.name}:{lineno}: {line!r}")
        self.assertEqual(
            offenders,
            [],
            "em/en-dashes found in templates (use '·' or hyphen instead):\n"
            + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
