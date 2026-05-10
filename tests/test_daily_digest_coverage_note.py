"""Unit tests for eval snapshot M/N lines in daily_digest (no network)."""

import importlib.util
import sys
import unittest
from pathlib import Path

# Load daily_digest from repo root without requiring package install
_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("daily_digest", _ROOT / "daily_digest.py")
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules["daily_digest"] = _mod
_spec.loader.exec_module(_mod)

_eval_sample_counts = _mod._eval_sample_counts
fmt_eval_coverage_note = _mod.fmt_eval_coverage_note
fmt_confirmed_regressions = _mod.fmt_confirmed_regressions


class TestEvalSampleCounts(unittest.TestCase):
    def test_n_sampled_preferred_over_metabase(self) -> None:
        m, n = _eval_sample_counts(
            {"n_sampled": 10, "n_metabase_rows": 99, "n_judged": 3}
        )
        self.assertEqual((m, n), (10, 3))

    def test_metabase_when_no_sampled(self) -> None:
        m, n = _eval_sample_counts({"n_metabase_rows": 100, "n_judged": 50})
        self.assertEqual((m, n), (100, 50))

    def test_coerce_integer_floats(self) -> None:
        m, n = _eval_sample_counts(
            {"n_metabase_rows": 1000.0, "n_judged": 400.0}
        )
        self.assertEqual((m, n), (1000, 400))


class TestFmtConfirmedRegressions(unittest.TestCase):
    def test_legacy_snapshot_without_hotspot_key(self) -> None:
        ev = {"n_judged": 10, "stopped_reason": "complete"}
        txt = fmt_confirmed_regressions(None, None, ev)
        self.assertNotIn("missing key", txt)
        self.assertIn("formatting hotspot", txt.lower())

    def test_empty_hotspot_list_same_as_legacy(self) -> None:
        ev = {"formatting_hotspot_chapters": [], "n_judged": 1}
        txt = fmt_confirmed_regressions(None, None, ev)
        self.assertNotIn("missing key", txt)
        self.assertIn("formatting hotspot chapters", txt.lower())


class TestFmtEvalCoverageNote(unittest.TestCase):
    def test_empty_summary(self) -> None:
        self.assertEqual(fmt_eval_coverage_note(None), "")
        self.assertEqual(fmt_eval_coverage_note({}), "")

    def test_complete_full_sample_silent(self) -> None:
        s = {
            "stopped_reason": "complete",
            "n_metabase_rows": 500,
            "n_judged": 500,
        }
        self.assertEqual(fmt_eval_coverage_note(s), "")

    def test_complete_partial_shows_neutral_only(self) -> None:
        s = {
            "stopped_reason": "complete",
            "n_metabase_rows": 1000,
            "n_judged": 400,
        }
        out = fmt_eval_coverage_note(s)
        self.assertIn("1000", out)
        self.assertIn("400", out)
        self.assertIn("C12 uses the judged set", out)
        self.assertNotIn("Stop:", out)

    def test_time_budget_partial_shows_stop(self) -> None:
        s = {
            "stopped_reason": "time_budget",
            "n_metabase_rows": 2000,
            "n_judged": 800,
        }
        out = fmt_eval_coverage_note(s)
        self.assertIn("2000", out)
        self.assertIn("800", out)
        self.assertIn("time_budget", out)

    def test_non_complete_full_sample_operator_line(self) -> None:
        s = {
            "stopped_reason": "sigterm",
            "n_metabase_rows": 100,
            "n_judged": 100,
        }
        out = fmt_eval_coverage_note(s)
        self.assertIn("sigterm", out)
        self.assertIn("eval Slack thread", out)

    def test_missing_counts_non_complete(self) -> None:
        s = {"stopped_reason": "time_budget"}
        out = fmt_eval_coverage_note(s)
        self.assertIn("time_budget", out)


if __name__ == "__main__":
    unittest.main()
