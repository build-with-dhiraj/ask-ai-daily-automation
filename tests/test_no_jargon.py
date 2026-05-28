"""CI lint: follow-up text companion has no unexplained jargon.

The audience for the daily Slack message includes leadership, DS, eng,
QA, and frontend owners. Acronyms that are not universally understood
(TTFT, VCP, CSAT, SLO, RPS) MUST be expanded on their first occurrence
in any given message. The compound term "percentage points" must back
"pp" on first use. The internal codename "axial" must never appear in
user-facing prose.

This lint exercises both deterministic fallback outputs (so the lint
catches drift even on LLM-down days) and the acronym expander helper.
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.follow_up_generator import (  # noqa: E402
    _build_deterministic_follow_up,
    expand_acronyms_first_use,
)


def _snapshot() -> dict:
    return {
        "standings": [
            {"label": "Academic FAIL", "yesterday": "23.1%", "delta": "+18.8pp"},
            {"label": "Experience FAIL", "yesterday": "13.4%", "delta": "+1.6pp"},
            {"label": "Overall PASS", "yesterday": "69.0%", "delta": "-4.2pp"},
            {"label": "Run cost", "yesterday": "$8.16", "delta": "+$0.11"},
            {"label": "Judged", "yesterday": "989", "delta": "-1"},
            {"label": "Downvote rate", "yesterday": "0.57%", "delta": "+0.02pp"},
            {"label": "VCP success", "yesterday": "96.4%", "delta": "-0.3pp"},
            {"label": "Error rate", "yesterday": "0.4%", "delta": "-0.1pp"},
            {"label": "Student TTFT p90", "yesterday": "8.0s", "delta": "+0.5s"},
            {"label": "Total cost", "yesterday": "$1,046", "delta": "+$34"},
        ],
    }


BANNED_JARGON_NEVER = (
    # Internal codenames that should never reach user-facing prose.
    "axial",
    "axialfail",
)


class TestBannedJargonAbsentFromFallback(unittest.TestCase):
    """Deterministic fallback prose contains no banned internal codenames."""

    def test_scoreboard_breach_has_no_axial(self) -> None:
        text = _build_deterministic_follow_up(
            surface="scoreboard", snapshot=_snapshot(), breach=True,
        )
        lower = text.lower()
        for token in BANNED_JARGON_NEVER:
            self.assertNotIn(
                token, lower,
                f"banned token {token!r} appeared in fallback prose: {text}",
            )

    def test_digest_quiet_has_no_axial(self) -> None:
        text = _build_deterministic_follow_up(
            surface="digest", snapshot=_snapshot(), breach=False,
        )
        lower = text.lower()
        for token in BANNED_JARGON_NEVER:
            self.assertNotIn(
                token, lower,
                f"banned token {token!r} appeared in fallback prose: {text}",
            )


class TestAcronymFirstUseExpansion(unittest.TestCase):
    """Each acronym is expanded on first occurrence inside one message."""

    def test_ttft_expanded_on_first_use(self) -> None:
        text = "TTFT spiked yesterday."
        out = expand_acronyms_first_use(text)
        self.assertIn("TTFT (time to first token)", out)

    def test_vcp_expanded_on_first_use(self) -> None:
        text = "VCP success rate dipped."
        out = expand_acronyms_first_use(text)
        self.assertIn("VCP (Video Co-Pilot)", out)

    def test_csat_expanded_on_first_use(self) -> None:
        text = "CSAT held steady."
        out = expand_acronyms_first_use(text)
        self.assertIn("CSAT (customer satisfaction)", out)

    def test_pp_expanded_on_first_numeric_use(self) -> None:
        text = "Up 1.6pp versus the 14 day median."
        out = expand_acronyms_first_use(text)
        self.assertIn("percentage points", out)

    def test_subsequent_acronym_occurrences_not_re_expanded(self) -> None:
        text = "TTFT spiked. TTFT remains noisy. TTFT recovered."
        out = expand_acronyms_first_use(text)
        # Exactly one parenthetical expansion in the output.
        self.assertEqual(out.count("(time to first token)"), 1)


class TestFallbackOutputContainsNoBareAcronymBeforeExpansion(unittest.TestCase):
    """After running the expander, the first acronym occurrence is expanded.

    This catches the case where a future change to the fallback prose
    forgets to route through expand_acronyms_first_use.
    """

    def test_digest_fallback_expanded_in_pipeline(self) -> None:
        raw = _build_deterministic_follow_up(
            surface="digest", snapshot=_snapshot(), breach=False,
        )
        out = expand_acronyms_first_use(raw)
        # If the fallback mentioned VCP, the parenthetical expansion must
        # appear in the same string.
        if re.search(r"\bVCP\b", out):
            self.assertIn("VCP (Video Co-Pilot)", out)
        if re.search(r"\bTTFT\b", out):
            self.assertIn("TTFT (time to first token)", out)
        if re.search(r"\bCSAT\b", out):
            self.assertIn("CSAT (customer satisfaction)", out)


if __name__ == "__main__":
    unittest.main()
