"""CI lint: follow_up_generator opens with the locked verdict shape.

The first ~60 chars of the follow-up text companion become the mobile
lock-screen preview in Slack. Per the locked Phase 4 plan this single
line carries the verdict, and it must follow one of exactly two shapes:

    "Top risk: ..."
    "No urgent risks today, ..."

Both shapes are checked against the public regex
`follow_up_generator.VERDICT_OPENING_RE` so we cannot drift the contract
without breaking this test.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

# Make the repo root importable so `scripts.follow_up_generator` resolves.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.follow_up_generator import (  # noqa: E402
    VERDICT_OPENING_RE,
    _build_deterministic_follow_up,
    _deterministic_digest,
    _deterministic_scoreboard,
)


class TestVerdictRegexAcceptsLockedShapes(unittest.TestCase):
    """The locked-shape opener for breach and quiet days both pass."""

    def test_breach_day_opener_matches(self) -> None:
        text = "Top risk: Academic FAIL hit 23.1 percent yesterday."
        self.assertIsNotNone(VERDICT_OPENING_RE.match(text))

    def test_quiet_day_opener_matches(self) -> None:
        text = (
            "No urgent risks today, Academic FAIL 4.3 percent stays "
            "inside the 6 percent floor."
        )
        self.assertIsNotNone(VERDICT_OPENING_RE.match(text))


class TestVerdictRegexRejectsDriftedShapes(unittest.TestCase):
    """Any other opener fails so the LLM cannot drift the contract."""

    def test_yesterday_opener_rejected(self) -> None:
        self.assertIsNone(
            VERDICT_OPENING_RE.match("Yesterday was normal."),
        )

    def test_tldr_opener_rejected(self) -> None:
        self.assertIsNone(VERDICT_OPENING_RE.match("TLDR: all green."))

    def test_lowercased_top_risk_rejected(self) -> None:
        self.assertIsNone(VERDICT_OPENING_RE.match("top risk: foo"))

    def test_missing_colon_rejected(self) -> None:
        self.assertIsNone(VERDICT_OPENING_RE.match("Top risk Academic FAIL"))

    def test_empty_string_rejected(self) -> None:
        self.assertIsNone(VERDICT_OPENING_RE.match(""))


class TestDeterministicFallbackOpensWithVerdict(unittest.TestCase):
    """Even when the LLM dies and we fall back, the verdict regex passes."""

    def _make_snapshot(self) -> dict:
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

    def test_breach_scoreboard_fallback_passes_regex(self) -> None:
        text = _deterministic_scoreboard(self._make_snapshot(), breach=True)
        self.assertIsNotNone(VERDICT_OPENING_RE.match(text))

    def test_quiet_scoreboard_fallback_passes_regex(self) -> None:
        text = _deterministic_scoreboard(self._make_snapshot(), breach=False)
        self.assertIsNotNone(VERDICT_OPENING_RE.match(text))

    def test_breach_digest_fallback_passes_regex(self) -> None:
        text = _deterministic_digest(self._make_snapshot(), breach=True)
        self.assertIsNotNone(VERDICT_OPENING_RE.match(text))

    def test_quiet_digest_fallback_passes_regex(self) -> None:
        text = _deterministic_digest(self._make_snapshot(), breach=False)
        self.assertIsNotNone(VERDICT_OPENING_RE.match(text))


class TestFallbackHasNoLongDashes(unittest.TestCase):
    """Deterministic fallback prose contains no em-dashes or en-dashes."""

    # chr() at runtime keeps this test file ASCII-only so the
    # no_em_dashes_anywhere lint does not flag it.
    _EM = chr(0x2014)
    _EN = chr(0x2013)

    def test_scoreboard_fallback_clean(self) -> None:
        snap = {"standings": []}
        text = _build_deterministic_follow_up(
            surface="scoreboard", snapshot=snap, breach=False,
        )
        self.assertNotIn(self._EM, text)
        self.assertNotIn(self._EN, text)

    def test_digest_fallback_clean(self) -> None:
        snap = {"standings": []}
        text = _build_deterministic_follow_up(
            surface="digest", snapshot=snap, breach=True,
        )
        self.assertNotIn(self._EM, text)
        self.assertNotIn(self._EN, text)


if __name__ == "__main__":
    unittest.main()
