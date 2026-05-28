"""Unit tests for scripts.follow_up_generator.

Covers the public surface defined in `__all__`:
    - generate_follow_up
    - expand_acronyms_first_use
    - breach_mention_prefix
    - VERDICT_OPENING_RE
    - FollowUp

LLM is always mocked; this test must run offline in CI.
"""
from __future__ import annotations

import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.follow_up_generator import (  # noqa: E402
    VERDICT_OPENING_RE,
    FollowUp,
    breach_mention_prefix,
    expand_acronyms_first_use,
    generate_follow_up,
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


# ---------------------------------------------------------------------------
# expand_acronyms_first_use
# ---------------------------------------------------------------------------

class TestExpandAcronymsFirstUse(unittest.TestCase):
    def test_ttft_expanded_on_first_occurrence_only(self) -> None:
        text = "TTFT spiked. TTFT remains noisy."
        out = expand_acronyms_first_use(text)
        self.assertIn("TTFT (time to first token)", out)
        # The expansion happens exactly once.
        self.assertEqual(out.count("(time to first token)"), 1)

    def test_vcp_expanded(self) -> None:
        out = expand_acronyms_first_use("VCP success dropped.")
        self.assertIn("VCP (Video Co-Pilot)", out)

    def test_csat_expanded(self) -> None:
        out = expand_acronyms_first_use("CSAT held flat.")
        self.assertIn("CSAT (customer satisfaction)", out)

    def test_pp_suffix_expanded(self) -> None:
        out = expand_acronyms_first_use("Academic FAIL was +18.8pp vs median.")
        self.assertIn("percentage points", out)

    def test_idempotent_if_already_expanded(self) -> None:
        text = "TTFT (time to first token) spiked. TTFT remains noisy."
        out = expand_acronyms_first_use(text)
        self.assertEqual(out, text)  # already expanded; unchanged

    def test_empty_string_unchanged(self) -> None:
        self.assertEqual(expand_acronyms_first_use(""), "")

    def test_no_acronym_no_change(self) -> None:
        text = "All metrics stayed inside their bands."
        self.assertEqual(expand_acronyms_first_use(text), text)


# ---------------------------------------------------------------------------
# breach_mention_prefix
# ---------------------------------------------------------------------------

class TestBreachMentionPrefix(unittest.TestCase):
    def test_academic_pings_naresh_and_deepesh(self) -> None:
        out = breach_mention_prefix("academic")
        self.assertIn("<@U03P01CHELQ>", out)  # Naresh
        self.assertIn("<@U091F0LPG7Q>", out)  # Deepesh

    def test_vcp_pings_ankita(self) -> None:
        out = breach_mention_prefix("vcp")
        self.assertIn("<@U05D4FS3HB2>", out)  # Ankita

    def test_ui_bug_pings_three_frontend_owners(self) -> None:
        out = breach_mention_prefix("ui_bug")
        self.assertIn("<@U085FBH4Q8Y>", out)  # Pankaj
        self.assertIn("<@U03NCBHSUAZ>", out)  # Tarun
        self.assertIn("<@U039CQ75QGY>", out)  # Vishal

    def test_test_signal_pings_prince(self) -> None:
        out = breach_mention_prefix("test")
        self.assertIn("<@U05G8P8CGTH>", out)  # Prince

    def test_empty_signal_returns_empty_string(self) -> None:
        self.assertEqual(breach_mention_prefix(None), "")
        self.assertEqual(breach_mention_prefix(""), "")

    def test_unknown_signal_with_wildcard_env(self) -> None:
        with mock.patch.dict(os.environ, {"BREACH_WILDCARD_OWNER": "UXXXWILD"}):
            out = breach_mention_prefix("nonsense")
            self.assertIn("<@UXXXWILD>", out)

    def test_unknown_signal_without_wildcard_returns_empty(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "BREACH_WILDCARD_OWNER"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(breach_mention_prefix("nonsense"), "")


# ---------------------------------------------------------------------------
# generate_follow_up: happy path with mocked LLM
# ---------------------------------------------------------------------------

class TestGenerateFollowUpHappyPath(unittest.TestCase):
    def test_llm_success_returns_non_degraded(self) -> None:
        mocked = (
            "Top risk: Academic FAIL hit 23.1 percent yesterday, "
            "well above the 6 percent floor. "
            "Experience FAIL was 13.4 percent, holding inside the band. "
            "Overall PASS dipped to 69 percent, 4 points below the 14 day median. "
            "Run cost was $8.16, in line with last week. "
            "Judged 989 traces, one fewer than yesterday."
        )
        with mock.patch(
            "scripts.follow_up_generator._call_llm",
            return_value=mocked,
        ):
            result = generate_follow_up(
                "scoreboard", _snapshot(),
                breach=True, breach_signal="academic",
                retries=1, retry_gap_sec=0,
            )
        self.assertIsInstance(result, FollowUp)
        self.assertFalse(result.degraded)
        # @-mention prefix prepended for breach.
        self.assertTrue(result.text.startswith("<@U03P01CHELQ>"))
        # Verdict regex passes on the text after the prefix is stripped.
        after_prefix = result.text.split("> ", 2)[-1]
        self.assertIsNotNone(VERDICT_OPENING_RE.match(after_prefix))


# ---------------------------------------------------------------------------
# generate_follow_up: degraded fallbacks
# ---------------------------------------------------------------------------

class TestGenerateFollowUpFallbacks(unittest.TestCase):
    def test_llm_raises_three_times_returns_degraded_deterministic(self) -> None:
        with mock.patch(
            "scripts.follow_up_generator._call_llm",
            side_effect=RuntimeError("Azure 502"),
        ):
            result = generate_follow_up(
                "scoreboard", _snapshot(),
                breach=True, breach_signal="academic",
                retries=3, retry_gap_sec=0,
            )
        self.assertTrue(result.degraded)
        self.assertIn("Azure 502", result.reason or "")
        # Deterministic fallback still passes the verdict regex.
        text_no_prefix = result.text.split("> ", 2)[-1]
        self.assertIsNotNone(VERDICT_OPENING_RE.match(text_no_prefix))

    def test_llm_returns_drifted_opener_falls_back(self) -> None:
        with mock.patch(
            "scripts.follow_up_generator._call_llm",
            return_value="Yesterday was a normal day with nothing to report.",
        ):
            result = generate_follow_up(
                "digest", _snapshot(), breach=False,
                retries=1, retry_gap_sec=0,
            )
        self.assertTrue(result.degraded)
        # Verdict regex still passes the fallback text.
        self.assertIsNotNone(VERDICT_OPENING_RE.match(result.text))

    def test_llm_returns_empty_string_falls_back(self) -> None:
        with mock.patch(
            "scripts.follow_up_generator._call_llm",
            return_value="",
        ):
            result = generate_follow_up(
                "digest", _snapshot(), breach=False,
                retries=1, retry_gap_sec=0,
            )
        self.assertTrue(result.degraded)


# ---------------------------------------------------------------------------
# FollowUp dataclass shape
# ---------------------------------------------------------------------------

class TestFollowUpShape(unittest.TestCase):
    def test_as_block_kit_section_shape(self) -> None:
        fu = FollowUp(text="Top risk: foo.")
        block = fu.as_block_kit_section()
        self.assertEqual(block["type"], "section")
        self.assertEqual(block["text"]["type"], "mrkdwn")
        self.assertEqual(block["text"]["text"], "Top risk: foo.")


if __name__ == "__main__":
    unittest.main()
