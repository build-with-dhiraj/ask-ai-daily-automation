"""Tests for the pre-flight upstream data-freshness probe.

Covers:
  • _wait_for_upstream_yesterday_data — happy path, polling-then-success,
    budget exhaustion, probe exception handling.
  • fmt_downvote_dump — loud warning when yesterday rows == 0 but card
    returned data (tests in test_loud_empty_section_logging.py).

All tests are pure (no real HTTP, no Slack contact, no real sleep).
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load_digest():
    spec = importlib.util.spec_from_file_location("daily_digest", _ROOT / "daily_digest.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daily_digest"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestWaitForUpstreamYesterdayData(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()
        self.sleep_calls: list = []

        def fake_sleep(secs):
            self.sleep_calls.append(secs)

        self._fake_sleep = fake_sleep

    def test_returns_true_when_data_present_on_first_attempt(self) -> None:
        """Canary card returns rows immediately → True, no sleeps."""
        with mock.patch.object(
            self.mod, "fetch_metabase_card", return_value=[{"chapter": "Algebra"}]
        ) as mfetch:
            result = self.mod._wait_for_upstream_yesterday_data(
                timeout_min=30,
                poll_interval_min=5,
                probe_card_id=33282,
                sleep_fn=self._fake_sleep,
            )
        self.assertTrue(result)
        self.assertEqual(mfetch.call_count, 1)
        self.assertEqual(self.sleep_calls, [])

    def test_polls_and_succeeds_after_initial_empty(self) -> None:
        """First two attempts return [] (still ingesting), third returns rows → True."""
        responses = [[], [], [{"chapter": "Physics"}]]
        with mock.patch.object(
            self.mod, "fetch_metabase_card", side_effect=responses
        ) as mfetch:
            result = self.mod._wait_for_upstream_yesterday_data(
                timeout_min=30,
                poll_interval_min=5,
                probe_card_id=33282,
                sleep_fn=self._fake_sleep,
            )
        self.assertTrue(result)
        self.assertEqual(mfetch.call_count, 3)
        # Slept twice (between attempts 1→2 and 2→3), 5 min each.
        self.assertEqual(self.sleep_calls, [300, 300])

    def test_returns_false_after_timeout_budget_exhausted(self) -> None:
        """All 6 attempts return [] → False, slept 5 times (between attempts)."""
        with mock.patch.object(
            self.mod, "fetch_metabase_card", return_value=[]
        ) as mfetch:
            result = self.mod._wait_for_upstream_yesterday_data(
                timeout_min=30,
                poll_interval_min=5,
                probe_card_id=33282,
                sleep_fn=self._fake_sleep,
            )
        self.assertFalse(result)
        # 30 / 5 = 6 attempts. 5 sleeps between them.
        self.assertEqual(mfetch.call_count, 6)
        self.assertEqual(len(self.sleep_calls), 5)
        self.assertTrue(all(s == 300 for s in self.sleep_calls))

    def test_handles_probe_exception_returns_false_without_crash(self) -> None:
        """If fetch_metabase_card raises, probe returns False, never propagates."""
        with mock.patch.object(
            self.mod,
            "fetch_metabase_card",
            side_effect=RuntimeError("metabase boom"),
        ) as mfetch:
            result = self.mod._wait_for_upstream_yesterday_data(
                timeout_min=30,
                poll_interval_min=5,
                probe_card_id=33282,
                sleep_fn=self._fake_sleep,
            )
        self.assertFalse(result)
        # On exception → bail out without sleeping (probe broken, not just slow).
        self.assertEqual(mfetch.call_count, 1)
        self.assertEqual(self.sleep_calls, [])

    def test_fetch_returns_none_treated_as_probe_failure(self) -> None:
        """fetch returning None (HTTP error) → False immediately, no further polling."""
        with mock.patch.object(
            self.mod, "fetch_metabase_card", return_value=None
        ) as mfetch:
            result = self.mod._wait_for_upstream_yesterday_data(
                timeout_min=30,
                poll_interval_min=5,
                probe_card_id=33282,
                sleep_fn=self._fake_sleep,
            )
        self.assertFalse(result)
        self.assertEqual(mfetch.call_count, 1)
        self.assertEqual(self.sleep_calls, [])


if __name__ == "__main__":
    unittest.main()
