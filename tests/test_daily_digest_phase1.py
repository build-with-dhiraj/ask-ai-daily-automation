"""Phase 1 digest restructure unit tests.

PR #30 (F11) note: the fmt_top_insights LLM-call tests that used to live
here were dropped along with that function. The remaining coverage:
  * _write_digest_snapshot / _load_yesterday_snapshot: round-trip + staleness
  * fmt_downvote_reasons_table: junk-tag filter, count floor, top-6 cap
  * fmt_multi_turn_burst / fmt_rephrase_rate: top-5 cap, context block, empty
  * fmt_broken_chapter: plain English body, no-overlap message
  * fmt_scores: anti-regression, no verbatim sample lines

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
        # Write+read round-trip. Because the loader now requires snapshot date
        # to equal yesterday's UTC (#19), we simulate yesterday's run by
        # writing a payload with yesterday's date directly rather than calling
        # _write_digest_snapshot (which always stamps today). The serializer's
        # own date-stamping is exercised by test_serializer_writes_date_field.
        yesterday = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        self.tmp.write_text(
            json.dumps({"date": yesterday, "errors_total": 100, "downvotes": 42}),
            encoding="utf-8",
        )
        loaded = self.mod._load_yesterday_snapshot(path=str(self.tmp))
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["errors_total"], 100)
        self.assertEqual(loaded["downvotes"], 42)
        self.assertIn("date", loaded)

    def test_serializer_writes_date_field(self) -> None:
        # The reader's day-equality guard depends on the serializer stamping
        # the snapshot with the UTC date it was WRITTEN (which is "today's
        # data" at write time → "yesterday's baseline" at tomorrow's read).
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.mod._write_digest_snapshot(
            {"errors_total": 1}, path=str(self.tmp)
        )
        with open(self.tmp, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw.get("date"), today)

    def test_same_day_intra_day_snapshot_rejected(self) -> None:
        # The #19 corruption path: a same-day earlier run wrote a snapshot
        # to local /tmp; the cross-run artifact download failed; the loader
        # would previously fall back to this same-day file and feed Top 3
        # Insights a baseline that is hours-stale, not 24h-stale. The
        # day-equality guard must reject it as "not yesterday's".
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.tmp.write_text(
            json.dumps({"date": today, "errors_total": 999}),
            encoding="utf-8",
        )
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertIsNone(self.mod._load_yesterday_snapshot(path=str(self.tmp)))

    def test_missing_date_field_rejected(self) -> None:
        # Pre-#19 snapshots that lack the date field cannot be safely treated
        # as yesterday's; refuse rather than risk a stale-by-hours baseline.
        self.tmp.write_text(
            json.dumps({"errors_total": 5}), encoding="utf-8"
        )
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertIsNone(self.mod._load_yesterday_snapshot(path=str(self.tmp)))

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

    def test_no_overlap_renders_as_finding_with_cardinalities(self) -> None:
        # Empty state must read as a deliberate finding, not a missing field
        # (#20). Lead with a checkmark, restate intent, surface input counts
        # so the reader can see the cross-check actually ran.
        eval_summary = {
            "formatting_hotspot_chapters": ["Some Chapter", "Another Chapter"]
        }
        out = self.mod.fmt_broken_chapter([], [], eval_summary)
        self.assertIn(":white_check_mark:", out)
        self.assertIn("Today's broken chapter: none detected", out)
        # 2 judge hotspots, 0 behavioral (empty follow/rephrase rows) → 0 overlap.
        self.assertIn("2 judge hotspots", out)
        self.assertIn("0 behavioral chapters", out)
        self.assertIn("0 overlap", out)
        self.assertNotIn("∩", out)
        self.assertNotIn("no chapter shows both", out)

    def test_no_judge_hotspots_renders_as_finding_with_cardinalities(self) -> None:
        # When daily eval reported no formatting hotspots, the cross-check
        # still ran, just against an empty judge set. Empty state copy
        # surfaces that cardinality (#20).
        eval_summary = {"formatting_hotspot_chapters": []}
        follow_rows = [
            {"chapter": "Chapter A", "triple_followup_60s_pct": 10.0},
            {"chapter": "Chapter B", "triple_followup_60s_pct": 10.0},
        ]
        out = self.mod.fmt_broken_chapter(follow_rows, [], eval_summary)
        self.assertIn(":white_check_mark:", out)
        self.assertIn("Today's broken chapter: none detected", out)
        self.assertIn("0 judge hotspots", out)
        self.assertIn("2 behavioral chapters", out)
        self.assertIn("0 overlap", out)

    def test_alias_function_still_exists(self) -> None:
        # Backwards-compat alias for the existing test that imports the old name
        self.assertTrue(hasattr(self.mod, "fmt_confirmed_regressions"))


# ---------------------------------------------------------------------------
# fmt_scores anti-regression: no verbatim sample lines
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
# build_blocks ordering: Phase 1 layout
# ---------------------------------------------------------------------------


class TestBuildBlocksPhase1Order(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_digest()

    def test_broken_chapter_before_errors_no_top_insights_block(self) -> None:
        # F8 (PR #30): the legacy ":dart: *Top 3 Insights*" section is
        # gone, insights live inside the rendered poster PNG now. The
        # remaining ordering invariant is broken-chapter before
        # langfuse-errors so the degraded text fallback still leads with
        # the actionable signal.
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
        self.assertNotIn("Top 3 Insights", flat)
        idx_broken = flat.find("Today's broken chapter")
        idx_errors = flat.find("Langfuse Errors")
        self.assertGreater(idx_broken, 0, "Broken chapter must be present")
        self.assertGreater(idx_errors, 0, "Langfuse Errors must be present")
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
# Architect-review regression: snapshot must be written EVEN when the
# idempotency-marker check fires the early return. Otherwise any same-day
# rerun (staging test, cron retry, manual repost) silently leaves tomorrow's
# day-on-day delta computation without a baseline.
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
        tomorrow's day-on-day baseline."""
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
