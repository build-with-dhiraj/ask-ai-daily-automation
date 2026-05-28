"""Unit tests for scripts.follow_up_generator.

Covers the public surface defined in `__all__`:
    - generate_follow_up        (returns InsightPayload)
    - expand_acronyms_first_use
    - breach_mention_prefix
    - VERDICT_OPENING_RE
    - InsightPayload + Callout + InsightCard structured shapes
    - FollowUp (backward-compat alias for InsightPayload)

LLM is always mocked; this test must run offline in CI.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.follow_up_generator import (  # noqa: E402
    VERDICT_OPENING_RE,
    Callout,
    FollowUp,
    InsightCard,
    InsightPayload,
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
# generate_follow_up: happy path with mocked LLM (JSON mode)
# ---------------------------------------------------------------------------

def _mock_scoreboard_llm_payload() -> str:
    return json.dumps({
        "verdict": (
            "Top risk: Academic FAIL hit 23.1 percent yesterday, "
            "well above the 6 percent floor."
        ),
        "text_companion": "ignored, caller rebuilds the slim text",
        "scoreboard_callouts": [
            {
                "label": "WHY IT MATTERS",
                "body": (
                    "Academic FAIL closed at 23.1 percent, the worst single "
                    "day reading since calibration in March."
                ),
            },
            {
                "label": "WORTH WATCHING",
                "body": (
                    "A5 answer-incomplete fired 137 times yesterday, "
                    "well above the 14-day median."
                ),
            },
        ],
        "digest_cards": [],
    })


def _mock_digest_llm_payload(*, n_cards: int = 4) -> str:
    cards = [
        {
            "topic_label": "ACCURACY",
            "icon": "",
            "claim": f"Card {i+1} claim with 1.{i}% number.",
            "evidence": f"Card {i+1} evidence sentence.",
            "context": None,
        }
        for i in range(n_cards)
    ]
    return json.dumps({
        "verdict": "Top risk: safety floor breached.",
        "text_companion": "ignored",
        "scoreboard_callouts": [],
        "digest_cards": cards,
    })


class TestGenerateFollowUpHappyPath(unittest.TestCase):
    def test_scoreboard_llm_success_returns_non_degraded(self) -> None:
        with mock.patch(
            "scripts.follow_up_generator._call_llm",
            return_value=_mock_scoreboard_llm_payload(),
        ):
            result = generate_follow_up(
                "scoreboard", _snapshot(),
                breach=True, breach_signal="academic",
                deep_dive_url="https://example.com/deep",
                retries=1, retry_gap_sec=0,
            )
        self.assertIsInstance(result, InsightPayload)
        self.assertFalse(result.degraded)
        # Verdict regex passes.
        self.assertIsNotNone(VERDICT_OPENING_RE.match(result.verdict))
        # Exactly 2 callouts for scoreboard, no digest cards.
        self.assertEqual(len(result.scoreboard_callouts), 2)
        self.assertEqual(len(result.digest_cards), 0)
        for callout in result.scoreboard_callouts:
            self.assertIsInstance(callout, Callout)
            self.assertTrue(callout.label)
            self.assertTrue(callout.body)
        # text_companion is 3 lines: verdict, @-mention, deep-dive link.
        # F4 (PR #30): the deep-dive line uses Slack mrkdwn `<url|label>`
        # syntax now so readers see a clickable "Deep dive" label, not the
        # raw URL.
        lines = result.text_companion.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("Top risk:"))
        self.assertIn("<@U03P01CHELQ>", lines[1])  # Naresh (academic owner)
        self.assertEqual(lines[2], "<https://example.com/deep|Deep dive>")

    def test_digest_llm_success_returns_four_cards(self) -> None:
        with mock.patch(
            "scripts.follow_up_generator._call_llm",
            return_value=_mock_digest_llm_payload(n_cards=4),
        ):
            result = generate_follow_up(
                "digest", _snapshot(),
                breach=True, breach_signal="academic",
                deep_dive_url="https://example.com/digest",
                retries=1, retry_gap_sec=0,
            )
        self.assertFalse(result.degraded)
        self.assertEqual(len(result.digest_cards), 4)
        self.assertEqual(len(result.scoreboard_callouts), 0)
        for card in result.digest_cards:
            self.assertIsInstance(card, InsightCard)
            self.assertTrue(card.topic_label)
            self.assertTrue(card.claim)


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
                deep_dive_url="https://example.com/deep",
                retries=3, retry_gap_sec=0,
            )
        self.assertTrue(result.degraded)
        self.assertIn("Azure 502", result.reason or "")
        # Deterministic fallback still passes the verdict regex.
        self.assertIsNotNone(VERDICT_OPENING_RE.match(result.verdict))
        # Structured payload still populated on the fallback path.
        self.assertEqual(len(result.scoreboard_callouts), 2)
        # text_companion still slim 3-line shape.
        self.assertEqual(len(result.text_companion.split("\n")), 3)

    def test_llm_returns_drifted_opener_falls_back(self) -> None:
        bad_payload = json.dumps({
            "verdict": "Yesterday was a normal day with nothing to report.",
            "scoreboard_callouts": [],
            "digest_cards": [],
        })
        with mock.patch(
            "scripts.follow_up_generator._call_llm",
            return_value=bad_payload,
        ):
            result = generate_follow_up(
                "digest", _snapshot(), breach=False,
                deep_dive_url="https://example.com/deep",
                retries=1, retry_gap_sec=0,
            )
        self.assertTrue(result.degraded)
        # Verdict regex still passes the fallback text.
        self.assertIsNotNone(VERDICT_OPENING_RE.match(result.verdict))

    def test_llm_returns_empty_string_falls_back(self) -> None:
        with mock.patch(
            "scripts.follow_up_generator._call_llm",
            return_value="",
        ):
            result = generate_follow_up(
                "digest", _snapshot(), breach=False,
                deep_dive_url="https://example.com/deep",
                retries=1, retry_gap_sec=0,
            )
        self.assertTrue(result.degraded)

    def test_llm_returns_invalid_json_falls_back(self) -> None:
        with mock.patch(
            "scripts.follow_up_generator._call_llm",
            return_value="this is not JSON",
        ):
            result = generate_follow_up(
                "scoreboard", _snapshot(), breach=False,
                deep_dive_url="https://example.com/deep",
                retries=1, retry_gap_sec=0,
            )
        self.assertTrue(result.degraded)


# ---------------------------------------------------------------------------
# Slim text companion shape (3 lines)
# ---------------------------------------------------------------------------

class TestSlimTextCompanionShape(unittest.TestCase):
    def test_breach_scoreboard_text_companion_is_three_lines(self) -> None:
        with mock.patch(
            "scripts.follow_up_generator._call_llm",
            side_effect=RuntimeError("offline"),
        ):
            result = generate_follow_up(
                "scoreboard", _snapshot(),
                breach=True, breach_signal="academic",
                deep_dive_url="https://example.com/deep",
                retries=1, retry_gap_sec=0,
            )
        lines = result.text_companion.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertIsNotNone(VERDICT_OPENING_RE.match(lines[0]))
        self.assertIn("<@", lines[1])
        # F4 (PR #30): Slack mrkdwn link syntax `<url|Deep dive>`.
        self.assertEqual(lines[2], "<https://example.com/deep|Deep dive>")

    def test_quiet_digest_text_companion_has_blank_mention_line(self) -> None:
        with mock.patch(
            "scripts.follow_up_generator._call_llm",
            side_effect=RuntimeError("offline"),
        ):
            result = generate_follow_up(
                "digest", _snapshot(), breach=False,
                deep_dive_url="https://example.com/q",
                retries=1, retry_gap_sec=0,
            )
        lines = result.text_companion.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1], "")  # no mention on quiet day


# ---------------------------------------------------------------------------
# InsightPayload backward-compat shape
# ---------------------------------------------------------------------------

class TestFollowUpShape(unittest.TestCase):
    def test_as_block_kit_section_shape(self) -> None:
        # FollowUp is now an alias for InsightPayload. Build via the new
        # InsightPayload(verdict=, text_companion=) signature.
        payload = InsightPayload(
            verdict="Top risk: foo.",
            text_companion="Top risk: foo.",
        )
        block = payload.as_block_kit_section()
        self.assertEqual(block["type"], "section")
        self.assertEqual(block["text"]["type"], "mrkdwn")
        self.assertEqual(block["text"]["text"], "Top risk: foo.")

    def test_text_property_aliases_text_companion(self) -> None:
        payload = InsightPayload(
            verdict="Top risk: foo.",
            text_companion="Top risk: foo.\n\nDeep dive: bar",
        )
        self.assertEqual(payload.text, payload.text_companion)

    def test_followup_alias_resolves_to_insight_payload(self) -> None:
        self.assertIs(FollowUp, InsightPayload)


# ---------------------------------------------------------------------------
# F6 (PR #30): _AXIS_OWNERS loaded from config/axis_owners.json with schema
# validation at import time. Misconfiguration must raise loud, not silently
# page the wrong human on first breach.
# ---------------------------------------------------------------------------

class TestAxisOwnersConfigSchema(unittest.TestCase):
    def setUp(self) -> None:
        from scripts.follow_up_generator import _load_axis_owners
        self.load = _load_axis_owners

    def _write(self, tmpdir: str, payload) -> str:
        path = os.path.join(tmpdir, "axis_owners.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def test_valid_config_loads(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, {"academic": ["U03P01CHELQ"], "wildcard": []})
            out = self.load(path)
        self.assertEqual(out["academic"], ["U03P01CHELQ"])
        self.assertEqual(out["wildcard"], [])

    def test_top_level_not_dict_raises(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, ["U03P01CHELQ"])
            with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                self.load(path)

    def test_value_not_list_raises(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, {"academic": "U03P01CHELQ"})
            with self.assertRaisesRegex(ValueError, "must be a list"):
                self.load(path)

    def test_entry_not_string_raises(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, {"academic": [123]})
            with self.assertRaisesRegex(ValueError, "must be str"):
                self.load(path)

    def test_entry_missing_u_prefix_raises(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, {"academic": ["03P01CHELQ"]})
            with self.assertRaisesRegex(ValueError, "Slack user ID"):
                self.load(path)

    def test_entry_lowercase_raises(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, {"academic": ["u03p01chelq"]})
            with self.assertRaisesRegex(ValueError, "Slack user ID"):
                self.load(path)

    def test_shipped_config_is_valid(self) -> None:
        # The actual config/axis_owners.json shipped with the repo must
        # load cleanly. Any drift in that file fails CI loud.
        out = self.load()
        self.assertIn("academic", out)
        self.assertIn("wildcard", out)
        for axis, ids in out.items():
            for uid in ids:
                self.assertRegex(uid, r"^U[A-Z0-9]{8,}$")


if __name__ == "__main__":
    unittest.main()
