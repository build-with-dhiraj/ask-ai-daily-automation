"""Tests for loud, actionable logging when digest sections come back empty.

Covers:
  • fmt_downvote_dump emits a [warn] line when card returned rows but the
    yesterday filter dropped all of them (Python-side filter mismatch case).
  • fmt_downvote_dump emits a [warn] line when card returned 0 rows
    (upstream lag case).
  • _log_section_emptiness categorises None vs [] vs populated.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

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


class TestFmtDownvoteDumpLoudWarningOnEmpty(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()

    def test_logs_loud_warning_when_yesterday_rows_empty_due_to_upstream_lag(
        self,
    ) -> None:
        """Card returned 0 total rows → log says 'upstream silver table data lag'."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            out = self.mod.fmt_downvote_dump([])
        log = buf.getvalue()
        self.assertIn("[warn]", log)
        self.assertIn("downvote dump", log)
        self.assertIn("23036", log)
        self.assertIn("0 yesterday rows", log)
        self.assertIn("card returned 0 total rows", log)
        self.assertIn("upstream silver table data lag", log)
        self.assertIn(self.mod.yesterday, out)

    def test_logs_loud_warning_when_card_had_rows_but_yesterday_filter_dropped_all(
        self,
    ) -> None:
        """Card returned rows but none match yesterday's date prefix."""
        # Use a date that definitely is NOT yesterday.
        rows = [
            {"createdat": "2020-01-01T00:00:00Z", "user_feedback": "Wrong"},
            {"createdat": "2019-12-31T00:00:00Z", "user_feedback": "Bad"},
        ]
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.mod.fmt_downvote_dump(rows)
        log = buf.getvalue()
        self.assertIn("[warn]", log)
        self.assertIn("card returned 2 total rows", log)
        # Distinguishes from upstream-lag case.
        self.assertIn("none matched yesterday", log)

    def test_logs_loud_warning_when_card_fetch_returned_none(self) -> None:
        """Card fetch failed (None) → log distinguishes 'fetch returned None' case."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.mod.fmt_downvote_dump(None)
        log = buf.getvalue()
        self.assertIn("[warn]", log)
        self.assertIn("card fetch returned None", log)


class TestLogSectionEmptiness(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()

    def test_logs_warn_when_rows_none(self) -> None:
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.mod._log_section_emptiness(
                "behavior_followup",
                "33282",
                None,
                configured=True,
            )
        log = buf.getvalue()
        self.assertIn("[warn]", log)
        self.assertIn("fetch returned None", log)
        self.assertIn("33282", log)

    def test_logs_warn_when_rows_empty_list(self) -> None:
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.mod._log_section_emptiness(
                "behavior_followup",
                "33282",
                [],
                configured=True,
            )
        log = buf.getvalue()
        self.assertIn("[warn]", log)
        self.assertIn("0 rows", log)
        self.assertIn("upstream silver table data lag", log)

    def test_logs_info_when_rows_populated(self) -> None:
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.mod._log_section_emptiness(
                "behavior_followup",
                "33282",
                [{"chapter": "Algebra"}, {"chapter": "Physics"}],
                configured=True,
            )
        log = buf.getvalue()
        self.assertIn("[info]", log)
        self.assertIn("2 rows", log)

    def test_silent_when_not_configured(self) -> None:
        """Don't double-warn: _warn_metabase_card_env_var already covers unconfigured."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.mod._log_section_emptiness(
                "behavior_followup",
                "",
                None,
                configured=False,
            )
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
