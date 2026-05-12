"""Phase 1 digest restructure unit tests.

Covers:
  • fmt_top_insights (LLM call mocked) — happy path, Azure URLError fallback,
    no-digit validation fallback, first-run (no snapshot) path
  • _write_digest_snapshot / _load_yesterday_snapshot — round-trip + staleness
  • fmt_downvote_reasons_table — junk-tag filter, count floor, top-6 cap
  • fmt_multi_turn_burst / fmt_rephrase_rate — top-5 cap, context block, empty
  • fmt_broken_chapter — plain English body (no `∩`); no-overlap message
  • fmt_scores — anti-regression: no verbatim sample lines

All tests are pure (no real HTTP, no Slack contact).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
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


def _make_mock_openai_client(content: str):
    """Mirror the mock client shape from test_feedback_classifier."""
    client = mock.MagicMock()
    msg = mock.MagicMock()
    msg.content = content
    choice = mock.MagicMock()
    choice.message = msg
    resp = mock.MagicMock()
    resp.choices = [choice]
    client.chat.completions.create.return_value = resp
    return client


# ---------------------------------------------------------------------------
# fmt_top_insights — Azure call mocked
# ---------------------------------------------------------------------------


class TestFmtTopInsights(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()
        # Provide a deployment name so the call path doesn't bail early
        os.environ["DEPLOYMENT_NAME"] = "gpt-4.1-test"
        # Patch sleep so retries are instant
        self._sleep_patch = mock.patch("time.sleep")
        self._sleep_patch.start()

    def tearDown(self) -> None:
        self._sleep_patch.stop()
        os.environ.pop("DEPLOYMENT_NAME", None)

    def test_first_run_no_snapshot_skips_llm(self) -> None:
        """yesterday_snapshot=None → placeholder, NO Azure call."""
        with mock.patch.object(self.mod, "_call_top_insights_llm") as called:
            out = self.mod.fmt_top_insights({"x": 1}, None)
        self.assertEqual(called.call_count, 0)
        self.assertIn("insights begin tomorrow", out)

    def test_happy_path_returns_text(self) -> None:
        valid = (
            "1. Errors up 30% from 100 to 130 in last 24h.\n"
            "2. Multi-turn burst on Chapter Foo at 7.5%, up from 5.2%.\n"
            "3. Rephrase rate down to 3.1% from 4.8% on Chapter Bar."
        )
        client = _make_mock_openai_client(valid)
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=valid
        ) as patched:
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        # Output must NOT contain reader-facing "LLM" wording (we don't surface it)
        self.assertNotIn("LLM", out)
        self.assertNotIn("gpt-", out.lower())
        # Three numbered bullets present
        self.assertIn("1.", out)
        self.assertIn("2.", out)
        self.assertIn("3.", out)
        self.assertEqual(patched.call_count, 1)

    def test_url_error_returns_unavailable_placeholder(self) -> None:
        with mock.patch.object(
            self.mod,
            "_call_top_insights_llm",
            side_effect=urllib.error.URLError("net"),
        ), mock.patch.object(sys, "stderr", io.StringIO()):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertIn("insights unavailable today", out)

    def test_generic_exception_returns_unavailable_placeholder(self) -> None:
        with mock.patch.object(
            self.mod,
            "_call_top_insights_llm",
            side_effect=RuntimeError("Azure 503 boom"),
        ), mock.patch.object(sys, "stderr", io.StringIO()):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertIn("insights unavailable today", out)

    def test_no_digits_in_output_fails_validation(self) -> None:
        bad = "Things look fine this week, no major changes worth noting."
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=bad
        ), mock.patch.object(sys, "stderr", io.StringIO()):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertIn("insights unavailable today", out)

    def test_no_significant_change_sentence_passes_without_digits(self) -> None:
        """The fixed sentinel sentence is allowed even though it has no digits."""
        sentence = (
            "No significant day-on-day changes today; baseline behavior."
        )
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=sentence
        ):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertEqual(out, sentence)

    def test_empty_llm_response_returns_unavailable(self) -> None:
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value="   "
        ), mock.patch.object(sys, "stderr", io.StringIO()):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertIn("insights unavailable today", out)


# ---------------------------------------------------------------------------
# Snapshot read/write
# ---------------------------------------------------------------------------


class TestDigestSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()
        self.tmp = Path("/tmp/_test_digest_snapshot.json")
        try:
            self.tmp.unlink()
        except FileNotFoundError:
            pass

    def tearDown(self) -> None:
        try:
            self.tmp.unlink()
        except FileNotFoundError:
            pass

    def test_round_trip(self) -> None:
        today = {"errors_total": 100, "downvotes": 42}
        self.mod._write_digest_snapshot(today, path=str(self.tmp))
        loaded = self.mod._load_yesterday_snapshot(path=str(self.tmp))
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["errors_total"], 100)
        self.assertEqual(loaded["downvotes"], 42)
        self.assertIn("date", loaded)

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(self.mod._load_yesterday_snapshot(path=str(self.tmp)))

    def test_malformed_returns_none(self) -> None:
        self.tmp.write_text("not json", encoding="utf-8")
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertIsNone(self.mod._load_yesterday_snapshot(path=str(self.tmp)))

    def test_stale_returns_none(self) -> None:
        old_date = (
            datetime.now(timezone.utc) - timedelta(days=10)
        ).strftime("%Y-%m-%d")
        self.tmp.write_text(
            json.dumps({"date": old_date, "errors_total": 5}), encoding="utf-8"
        )
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertIsNone(self.mod._load_yesterday_snapshot(path=str(self.tmp)))

    def test_recent_within_two_days_loads(self) -> None:
        recent = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        self.tmp.write_text(
            json.dumps({"date": recent, "errors_total": 7}), encoding="utf-8"
        )
        loaded = self.mod._load_yesterday_snapshot(path=str(self.tmp))
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["errors_total"], 7)


# ---------------------------------------------------------------------------
# fmt_downvote_reasons_table
# ---------------------------------------------------------------------------


class TestFmtDownvoteReasonsTable(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()

    def test_junk_tags_filtered(self) -> None:
        rows = [
            {"feedback_text": ".", "downvotes": 9999},
            {"feedback_text": "nhi", "downvotes": 9999},
            {"feedback_text": "BAD", "downvotes": 9999},  # case-insensitive
            {"feedback_text": "no", "downvotes": 9999},
            {"feedback_text": "Too long", "downvotes": 9999},
            {"feedback_text": "...", "downvotes": 9999},
            {"feedback_text": "Real reason", "downvotes": 100},
        ]
        block = self.mod.fmt_downvote_reasons_table(rows, [])
        academic_text = block["fields"][0]["text"]
        for junk in (".", "nhi", "BAD", "Too long", "..."):
            self.assertNotIn(junk, academic_text)
        self.assertIn("Real reason", academic_text)

    def test_count_floor_drops_low_rows(self) -> None:
        rows = [
            {"feedback_text": "Above floor", "downvotes": 100},
            {"feedback_text": "Below floor", "downvotes": 49},
        ]
        block = self.mod.fmt_downvote_reasons_table(rows, [])
        academic_text = block["fields"][0]["text"]
        self.assertIn("Above floor", academic_text)
        self.assertNotIn("Below floor", academic_text)

    def test_top_six_cap_per_side(self) -> None:
        rows = [
            {"feedback_text": f"Reason {i}", "downvotes": 1000 - i}
            for i in range(20)
        ]
        block = self.mod.fmt_downvote_reasons_table(rows, rows)
        academic_text = block["fields"][0]["text"]
        # First 6 reasons should be present, 7th should not be
        for i in range(6):
            self.assertIn(f"Reason {i}", academic_text)
        self.assertNotIn("Reason 6", academic_text)
        self.assertNotIn("Reason 19", academic_text)

    def test_two_columns_emitted(self) -> None:
        block = self.mod.fmt_downvote_reasons_table(
            [{"feedback_text": "Acad", "downvotes": 100}],
            [{"feedback_text": "Non-acad", "downvotes": 100}],
        )
        self.assertEqual(block["type"], "section")
        self.assertEqual(len(block["fields"]), 2)
        self.assertIn("Academic", block["fields"][0]["text"])
        self.assertIn("Non-Academic", block["fields"][1]["text"])

    def test_empty_inputs_render_placeholder(self) -> None:
        block = self.mod.fmt_downvote_reasons_table([], [])
        self.assertIn("no rows", block["fields"][0]["text"])
        self.assertIn("no rows", block["fields"][1]["text"])

    def test_none_inputs_render_placeholder(self) -> None:
        block = self.mod.fmt_downvote_reasons_table(None, None)
        self.assertIn("no rows", block["fields"][0]["text"])


# ---------------------------------------------------------------------------
# fmt_multi_turn_burst + fmt_rephrase_rate
# ---------------------------------------------------------------------------


class TestFmtMultiTurnBurst(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()

    def test_top_five_cap(self) -> None:
        rows = [
            {"chapter": f"Ch{i}", "triple_followup_60s_pct": 10 - i, "n_queries": 100}
            for i in range(10)
        ]
        blocks = self.mod.fmt_multi_turn_burst(rows)
        # Body section + context block
        self.assertEqual(len(blocks), 2)
        body = blocks[0]["text"]["text"]
        self.assertIn("Ch0", body)
        self.assertIn("Ch4", body)
        self.assertNotIn("Ch5", body)

    def test_context_block_explainer_present(self) -> None:
        blocks = self.mod.fmt_multi_turn_burst(
            [{"chapter": "X", "triple_followup_60s_pct": 5.0}]
        )
        self.assertEqual(blocks[1]["type"], "context")
        explainer = blocks[1]["elements"][0]["text"]
        self.assertIn("first-answer failure", explainer)

    def test_empty_rows_renders_no_rows(self) -> None:
        blocks = self.mod.fmt_multi_turn_burst([])
        self.assertEqual(len(blocks), 2)
        self.assertIn("no rows", blocks[0]["text"]["text"])

    def test_card_not_configured_renders_setup_hint(self) -> None:
        blocks = self.mod.fmt_multi_turn_burst(None, card_configured=False)
        self.assertIn("not configured", blocks[0]["text"]["text"])

    def test_header_uses_multi_turn_label(self) -> None:
        blocks = self.mod.fmt_multi_turn_burst([{"chapter": "X", "triple_followup_60s_pct": 5.0}])
        self.assertIn("Multi-turn burst", blocks[0]["text"]["text"])


class TestFmtRephraseRate(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()

    def test_top_five_cap(self) -> None:
        rows = [
            {"chapter": f"Ch{i}", "rephrase_keyword_pct": 10 - i, "n_queries": 100}
            for i in range(10)
        ]
        blocks = self.mod.fmt_rephrase_rate(rows)
        self.assertEqual(len(blocks), 2)
        body = blocks[0]["text"]["text"]
        self.assertIn("Ch4", body)
        self.assertNotIn("Ch5", body)

    def test_context_explainer_present(self) -> None:
        blocks = self.mod.fmt_rephrase_rate(
            [{"chapter": "X", "rephrase_keyword_pct": 3.0}]
        )
        self.assertEqual(blocks[1]["type"], "context")
        explainer = blocks[1]["elements"][0]["text"]
        self.assertIn("clarity failure", explainer)

    def test_empty_data_no_rows(self) -> None:
        blocks = self.mod.fmt_rephrase_rate([])
        self.assertIn("no rows", blocks[0]["text"]["text"])

    def test_header_uses_rephrase_label(self) -> None:
        blocks = self.mod.fmt_rephrase_rate([{"chapter": "X", "rephrase_keyword_pct": 3.0}])
        self.assertIn("Rephrase", blocks[0]["text"]["text"])


# ---------------------------------------------------------------------------
# fmt_broken_chapter (renamed + plain English rewrite)
# ---------------------------------------------------------------------------


class TestFmtBrokenChapter(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()

    def test_overlap_uses_plain_english_no_intersection_symbol(self) -> None:
        eval_summary = {"formatting_hotspot_chapters": ["Detailed 11th Revision"]}
        follow_rows = [
            {"chapter": "Detailed 11th Revision", "triple_followup_60s_pct": 10.0}
        ]
        rephrase_rows = [
            {"chapter": "Detailed 11th Revision", "rephrase_keyword_pct": 10.0}
        ]
        out = self.mod.fmt_broken_chapter(follow_rows, rephrase_rows, eval_summary)
        # No set-theory notation
        self.assertNotIn("∩", out)
        # Plain English signal phrasing
        self.assertIn("AI output quality flagged", out)
        self.assertIn("users keep retrying", out)
        self.assertIn("likely fix candidate", out)
        self.assertIn("Detailed 11th Revision", out)

    def test_no_overlap_uses_plain_english_no_chapter_message(self) -> None:
        eval_summary = {"formatting_hotspot_chapters": ["Some Chapter"]}
        out = self.mod.fmt_broken_chapter([], [], eval_summary)
        self.assertIn("no chapter shows both", out)
        self.assertNotIn("∩", out)

    def test_alias_function_still_exists(self) -> None:
        # Backwards-compat alias for the existing test that imports the old name
        self.assertTrue(hasattr(self.mod, "fmt_confirmed_regressions"))


# ---------------------------------------------------------------------------
# fmt_scores anti-regression — no verbatim sample lines
# ---------------------------------------------------------------------------


class TestFmtScoresNoVerbatim(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()

    def test_no_verbatim_in_scores_output(self) -> None:
        scores = [
            {"value": 0, "comment": "this answer is hallucinated nonsense"},
            {"value": 0, "comment": "speaker is too fast"},
        ] * 600  # large sample to trigger rate display path
        out = self.mod.fmt_scores(scores, dv_in_sample=1200, total_traces=10_000)
        # Headline stats line is present
        self.assertIn("Downvotes (csat=0)", out)
        # Verbatim sample section text MUST NOT appear
        self.assertNotIn("Sample free-text comments", out)
        self.assertNotIn("verbatim", out.lower())
        self.assertNotIn("hallucinated nonsense", out)
        self.assertNotIn("speaker is too fast", out)


# ---------------------------------------------------------------------------
# build_blocks ordering — Phase 1 layout
# ---------------------------------------------------------------------------


class TestBuildBlocksPhase1Order(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()

    def test_top_insights_before_broken_chapter_before_errors(self) -> None:
        blocks = self.mod.build_blocks(
            academic_rows=[],
            nonacademic_rows=[],
            dump_rows=[],
            score_items=[],
            dv_in_sample=0,
            error_obs=[],
            total_errors=0,
            total_traces=0,
            top_insights_text="1. Test insight",
        )
        flat = json.dumps(blocks)
        idx_insights = flat.find("Top 3 Insights")
        idx_broken = flat.find("Today's broken chapter")
        idx_errors = flat.find("Langfuse Errors")
        self.assertGreater(idx_insights, 0, "Top 3 Insights must be present")
        self.assertGreater(idx_broken, 0, "Broken chapter must be present")
        self.assertGreater(idx_errors, 0, "Langfuse Errors must be present")
        self.assertLess(idx_insights, idx_broken)
        self.assertLess(idx_broken, idx_errors)

    def test_no_llm_label_in_user_facing_output(self) -> None:
        blocks = self.mod.build_blocks(
            academic_rows=[],
            nonacademic_rows=[],
            dump_rows=[],
            score_items=[],
            dv_in_sample=0,
            error_obs=[],
            total_errors=0,
            total_traces=0,
            top_insights_text="1. Test insight 100",
        )
        flat = json.dumps(blocks)
        self.assertNotIn("LLM", flat)
        self.assertNotIn("gpt-4", flat.lower())
        self.assertNotIn("azure openai", flat.lower())

    def test_merged_21d_table_uses_fields(self) -> None:
        blocks = self.mod.build_blocks(
            academic_rows=[{"feedback_text": "Reason A", "downvotes": 100}],
            nonacademic_rows=[{"feedback_text": "Reason B", "downvotes": 100}],
            dump_rows=[],
            score_items=[],
            dv_in_sample=0,
            error_obs=[],
            total_errors=0,
            total_traces=0,
        )
        # Locate the fields-block used for the 21d table
        has_fields_block = any(
            isinstance(b, dict) and "fields" in b for b in blocks
        )
        self.assertTrue(has_fields_block, "Merged 21d table must use fields block")


# ---------------------------------------------------------------------------
# Architect-review regression — snapshot must be written EVEN when the
# idempotency-marker check fires the early return. Otherwise any same-day
# rerun (staging test, cron retry, manual repost) silently leaves tomorrow's
# Top 3 Insights without a baseline.
# ---------------------------------------------------------------------------


class TestSnapshotWrittenEvenWhenMarkerSkip(unittest.TestCase):
    """End-to-end main() test with all upstream I/O mocked."""

    def setUp(self) -> None:
        self.mod = _load_digest()
        # Mock every upstream so main() runs through to the marker check
        # without hitting any real network or filesystem-state surface area.
        self._patches = [
            mock.patch.object(self.mod, "_preflight_langfuse_or_exit"),
            mock.patch.object(self.mod, "fetch_metabase_card", return_value=[]),
            mock.patch.object(
                self.mod, "fetch_metabase_card_detailed", return_value=([], None)
            ),
            mock.patch.object(
                self.mod,
                "fetch_langfuse_scores",
                return_value=([], 0, True, False),
            ),
            mock.patch.object(
                self.mod,
                "fetch_langfuse_errors",
                return_value=([], 0, True, False),
            ),
            mock.patch.object(
                self.mod, "fetch_langfuse_traces_total", return_value=(0, True)
            ),
            mock.patch.object(self.mod, "load_eval_summary", return_value=None),
            mock.patch.object(self.mod, "load_classifier_snapshot", return_value=None),
            # Force the LLM section to take the first-run path so no Azure
            # client is constructed.
            mock.patch.object(self.mod, "_load_yesterday_snapshot", return_value=None),
            mock.patch.object(sys, "stderr", io.StringIO()),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    def test_snapshot_written_even_when_marker_skip(self) -> None:
        """Architect-caught regression: snapshot write must happen BEFORE the
        marker-check early return, so a same-day rerun does not blank
        tomorrow's Top 3 Insights baseline."""
        with mock.patch.object(
            self.mod, "_already_posted_today", return_value=True
        ) as already, mock.patch.object(
            self.mod, "_write_digest_snapshot"
        ) as write_snap, mock.patch.object(
            self.mod, "post_to_slack"
        ) as post_slack:
            rc = self.mod.main()
        # main() returned 0 cleanly via the marker-skip early return
        self.assertEqual(rc, 0)
        # Marker check fired, post_to_slack was NOT called (early return)
        self.assertEqual(already.call_count, 1)
        self.assertEqual(post_slack.call_count, 0)
        # CRITICAL: snapshot was still written exactly once
        self.assertEqual(
            write_snap.call_count,
            1,
            "Snapshot must be written even when same-day marker skips the post",
        )

    def test_snapshot_written_exactly_once_on_normal_post_path(self) -> None:
        """Sibling assertion: when the post DOES happen, the snapshot is still
        written exactly once (no duplicate write from the moved call site)."""
        with mock.patch.object(
            self.mod, "_already_posted_today", return_value=False
        ), mock.patch.object(
            self.mod, "_write_digest_snapshot"
        ) as write_snap, mock.patch.object(
            self.mod, "post_to_slack", return_value=True
        ), mock.patch.object(
            self.mod, "_write_posted_marker"
        ):
            self.mod.main()
        self.assertEqual(
            write_snap.call_count,
            1,
            "Snapshot must be written exactly once on the normal post path",
        )


if __name__ == "__main__":
    unittest.main()
