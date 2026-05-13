"""Phase 2 digest cost+latency unit tests.

Covers the new Cost & Latency section added on top of the Langfuse Metrics API:
  • fetch_langfuse_metrics — URL-encodes the JSON query parameter correctly
  • fmt_cost_and_latency — per-model rendering with day-on-day deltas
  • fmt_cost_and_latency — flags 🔴 above the regression / spike thresholds
  • fmt_cost_and_latency — "(no TTFT data)" fallback for un-instrumented models
  • fmt_cost_and_latency — total row + per-model cost deltas
  • fmt_cost_and_latency — cost spike threshold flagging
  • _safe_pct_delta — zero-baseline / None / NaN guards
  • Section graceful-omit when the Metrics API fails entirely
  • Snapshot includes per-model latency + cost for Top 3 Insights consumption

All tests are pure (no real HTTP, no Slack contact). `urllib.request.urlopen`
is patched whenever a fetch is exercised — no live Langfuse calls.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
import urllib.error
import urllib.parse
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


def _mock_urlopen_returning(body: dict):
    """Build a context-manager mock for urllib.request.urlopen returning `body`."""
    payload = json.dumps(body).encode()
    fake_resp = mock.MagicMock()
    fake_resp.read.return_value = payload
    fake_resp.__enter__ = lambda self: fake_resp
    fake_resp.__exit__ = lambda self, *a: False
    return fake_resp


# ---------------------------------------------------------------------------
# 1. fetch_langfuse_metrics URL-encoding
# ---------------------------------------------------------------------------


class TestFetchLangfuseMetricsUrlEncoding(unittest.TestCase):
    def test_query_json_url_encoded_correctly(self):
        """The `query` param must be a URL-encoded JSON string with the exact
        shape the Metrics API expects (view, metrics, dimensions, timestamps,
        timeDimension). Round-trip via urlparse → parse_qs → json.loads must
        match what we passed."""
        digest = _load_digest()
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.headers)
            return _mock_urlopen_returning({"data": []})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rows = digest.fetch_langfuse_metrics(
                measures=["timeToFirstToken", "timeToFirstToken"],
                aggregations=["p50", "p90"],
                dimensions=["providedModelName"],
                from_ts="2026-05-12T00:00:00Z",
                to_ts="2026-05-13T00:00:00Z",
                granularity="day",
            )
        self.assertEqual(rows, [])

        # Parse the URL and pull out the `query` param.
        parsed = urllib.parse.urlparse(captured["url"])
        self.assertTrue(parsed.path.endswith("/api/public/metrics"))
        qs = urllib.parse.parse_qs(parsed.query)
        self.assertIn("query", qs)
        round_tripped = json.loads(qs["query"][0])
        self.assertEqual(round_tripped["view"], "observations")
        self.assertEqual(
            round_tripped["metrics"],
            [
                {"measure": "timeToFirstToken", "aggregation": "p50"},
                {"measure": "timeToFirstToken", "aggregation": "p90"},
            ],
        )
        self.assertEqual(
            round_tripped["dimensions"], [{"field": "providedModelName"}]
        )
        self.assertEqual(round_tripped["fromTimestamp"], "2026-05-12T00:00:00Z")
        self.assertEqual(round_tripped["toTimestamp"], "2026-05-13T00:00:00Z")
        self.assertEqual(round_tripped["timeDimension"], {"granularity": "day"})
        # Auth header must be HTTP Basic, base64-encoded.
        auth = captured["headers"].get("Authorization") or captured["headers"].get("authorization")
        self.assertTrue(auth and auth.startswith("Basic "))

    def test_mismatched_lengths_raises(self):
        """Defensive: measures and aggregations must zip 1:1."""
        digest = _load_digest()
        with self.assertRaises(ValueError):
            digest.fetch_langfuse_metrics(
                measures=["timeToFirstToken"],
                aggregations=["p50", "p90"],
                dimensions=["providedModelName"],
                from_ts="2026-05-12T00:00:00Z",
                to_ts="2026-05-13T00:00:00Z",
            )


# ---------------------------------------------------------------------------
# 2. fmt_cost_and_latency — per-model rendering with deltas
# ---------------------------------------------------------------------------


class TestFmtModelLatencyRendersPerModelWithDeltas(unittest.TestCase):
    def test_three_models_render_with_pct_arrows(self):
        digest = _load_digest()
        latency = {
            "ok": True,
            "yesterday": "2026-05-12",
            "day_before": "2026-05-11",
            "by_model": {
                "gpt-4.1": {
                    "yesterday": {"p50": 420, "p90": 1200, "p95": 1850},
                    "day_before": {"p50": 400, "p90": 1165, "p95": 1814},
                },
                "gpt-5.2": {
                    "yesterday": {"p50": 480, "p90": 1350, "p95": 2100},
                    "day_before": {"p50": 490, "p90": 1364, "p95": 2100},
                },
            },
        }
        cost = {"ok": True, "by_model": {}}
        body = digest.fmt_cost_and_latency(latency, cost)
        # Both models appear, each with three percentiles formatted in seconds.
        self.assertIn("`gpt-4.1`", body)
        self.assertIn("`gpt-5.2`", body)
        self.assertIn("p50: 0.42s", body)
        self.assertIn("p90: 1.20s", body)
        self.assertIn("p95: 1.85s", body)
        # gpt-5.2 p50 went 490 → 480 = -2%, should render as ↓2%.
        self.assertIn("↓2%", body)
        # gpt-4.1 p50 went 400 → 420 = +5%, should render as ↑5%.
        self.assertIn("↑5%", body)


# ---------------------------------------------------------------------------
# 3. fmt_cost_and_latency — flags 🔴 above regression threshold
# ---------------------------------------------------------------------------


class TestFmtModelLatencyFlagsRegressionOnThreshold(unittest.TestCase):
    def test_red_flag_emitted_at_or_above_threshold(self):
        digest = _load_digest()
        # 1000 → 1280 ms = +28%, exceeds 20% default → 🔴.
        # 800 → 880 = +10% → no flag.
        latency = {
            "ok": True,
            "by_model": {
                "gemini-flash-3": {
                    "yesterday": {"p50": 310, "p90": 950, "p95": 1280},
                    "day_before": {"p50": 242, "p90": 779, "p95": 1000},
                },
                "gpt-4.1": {
                    "yesterday": {"p50": 420, "p90": 1200, "p95": 880},
                    "day_before": {"p50": 400, "p90": 1165, "p95": 800},
                },
            },
        }
        body = digest.fmt_cost_and_latency(latency, {"ok": True, "by_model": {}})
        # gemini-flash-3 row should contain at least one 🔴 (p50 +28%, p95 +28%).
        gemini_line = next(
            line for line in body.splitlines() if "gemini-flash-3" in line
        )
        self.assertIn("🔴", gemini_line)
        # gpt-4.1 row should have NO 🔴 (max delta is 10%).
        gpt_line = next(line for line in body.splitlines() if "gpt-4.1" in line)
        self.assertNotIn("🔴", gpt_line)

    def test_red_flag_only_on_per_percentile_not_whole_row(self):
        digest = _load_digest()
        # p50 +5%, p90 +6%, p95 +30% — only p95 should carry 🔴.
        latency = {
            "ok": True,
            "by_model": {
                "gpt-4.1": {
                    "yesterday": {"p50": 420, "p90": 1230, "p95": 2600},
                    "day_before": {"p50": 400, "p90": 1160, "p95": 2000},
                }
            },
        }
        body = digest.fmt_cost_and_latency(latency, {"ok": True, "by_model": {}})
        # Slice the line and verify 🔴 follows the p95 number, not p50/p90.
        line = next(l for l in body.splitlines() if "gpt-4.1" in l)
        # The string should contain "p95: ..." followed by a "(...🔴)" group.
        # The actual format is "p95: 2.60s (↑30% 🔴)" — space between % and 🔴.
        self.assertRegex(line, r"p95:[^|]*🔴")
        # And NEITHER p50 nor p90 should carry 🔴.
        # Split by "|" to isolate each percentile's segment.
        segments = [s.strip() for s in line.split("|")]
        p50_seg = next(s for s in segments if "p50" in s)
        p90_seg = next(s for s in segments if "p90" in s)
        self.assertNotIn("🔴", p50_seg)
        self.assertNotIn("🔴", p90_seg)


# ---------------------------------------------------------------------------
# 4. fmt_cost_and_latency — (no TTFT data) fallback
# ---------------------------------------------------------------------------


class TestFmtModelLatencyHandlesMissingTtftForAModel(unittest.TestCase):
    def test_all_none_percentiles_render_no_ttft_data_placeholder(self):
        digest = _load_digest()
        latency = {
            "ok": True,
            "by_model": {
                # Real TTFT data.
                "gpt-4.1": {
                    "yesterday": {"p50": 420, "p90": 1200, "p95": 1850},
                    "day_before": {"p50": 400, "p90": 1165, "p95": 1814},
                },
                # Model that exists in cost data but has no TTFT instrumentation.
                # The fetcher's filter wouldn't normally let this row through,
                # but the renderer must still handle it defensively in case a
                # future Langfuse change emits all-None percentiles.
                "gpt-4.1-2025-04-14": {
                    "yesterday": {"p50": None, "p90": None, "p95": None},
                    "day_before": {"p50": None, "p90": None, "p95": None},
                },
            },
        }
        body = digest.fmt_cost_and_latency(latency, {"ok": True, "by_model": {}})
        self.assertIn("(no TTFT data)", body)
        # The placeholder must be on the gpt-4.1-2025-04-14 line, not gpt-4.1.
        line = next(
            l for l in body.splitlines() if "gpt-4.1-2025-04-14" in l
        )
        self.assertIn("(no TTFT data)", line)


# ---------------------------------------------------------------------------
# 5. fmt_cost_and_latency — total row + per-model cost deltas
# ---------------------------------------------------------------------------


class TestFmtModelCostRendersTotalAndDeltas(unittest.TestCase):
    def test_per_model_cost_with_total_row(self):
        digest = _load_digest()
        cost = {
            "ok": True,
            "by_model": {
                "gpt-4.1": {
                    "yesterday": {"total": 24.18},
                    "day_before": {"total": 22.40},
                },
                "gpt-5.2": {
                    "yesterday": {"total": 18.42},
                    "day_before": {"total": 17.88},
                },
                "gemini-flash-3": {
                    "yesterday": {"total": 6.05},
                    "day_before": {"total": 6.85},
                },
            },
        }
        body = digest.fmt_cost_and_latency({"ok": True, "by_model": {}}, cost)
        self.assertIn("$24.18", body)
        self.assertIn("$18.42", body)
        self.assertIn("$6.05", body)
        # Total = 24.18 + 18.42 + 6.05 = 48.65
        self.assertIn("$48.65", body)
        # Sort order: heaviest first (gpt-4.1 > gpt-5.2 > gemini-flash-3).
        i_gpt41 = body.index("gpt-4.1")
        i_gpt52 = body.index("gpt-5.2")
        i_gemini = body.index("gemini-flash-3")
        self.assertLess(i_gpt41, i_gpt52)
        self.assertLess(i_gpt52, i_gemini)

    def test_zero_cost_rows_suppressed(self):
        digest = _load_digest()
        cost = {
            "ok": True,
            "by_model": {
                "gpt-4.1": {
                    "yesterday": {"total": 10.00},
                    "day_before": {"total": 9.50},
                },
                "deprecated-model": {
                    "yesterday": {"total": 0.0},
                    "day_before": {"total": 0.0},
                },
            },
        }
        body = digest.fmt_cost_and_latency({"ok": True, "by_model": {}}, cost)
        self.assertIn("gpt-4.1", body)
        self.assertNotIn("deprecated-model", body)


# ---------------------------------------------------------------------------
# 6. fmt_cost_and_latency — cost spike threshold
# ---------------------------------------------------------------------------


class TestFmtModelCostFlagsSpikeAboveThreshold(unittest.TestCase):
    def test_red_flag_emitted_strictly_above_threshold(self):
        digest = _load_digest()
        # Default cost spike threshold = 30% (renderer uses `> spike_pct`,
        # not `>=`, per the plan's ">30%" wording).
        # 100 → 135 = +35% → 🔴
        # 100 → 130 = +30% → no flag (boundary)
        # 100 → 120 = +20% → no flag
        cost = {
            "ok": True,
            "by_model": {
                "spike-model": {
                    "yesterday": {"total": 135.0},
                    "day_before": {"total": 100.0},
                },
                "boundary-model": {
                    "yesterday": {"total": 130.0},
                    "day_before": {"total": 100.0},
                },
                "calm-model": {
                    "yesterday": {"total": 120.0},
                    "day_before": {"total": 100.0},
                },
            },
        }
        body = digest.fmt_cost_and_latency({"ok": True, "by_model": {}}, cost)
        # spike-model line carries 🔴; the other two don't.
        spike_line = next(l for l in body.splitlines() if "spike-model" in l)
        boundary_line = next(l for l in body.splitlines() if "boundary-model" in l)
        calm_line = next(l for l in body.splitlines() if "calm-model" in l)
        self.assertIn("🔴", spike_line)
        self.assertNotIn("🔴", boundary_line)
        self.assertNotIn("🔴", calm_line)


# ---------------------------------------------------------------------------
# 7. _safe_pct_delta — zero-baseline / None / NaN guards
# ---------------------------------------------------------------------------


class TestSafePctDeltaHandlesZeroBaseline(unittest.TestCase):
    def test_zero_baseline_returns_none_not_raises(self):
        digest = _load_digest()
        self.assertIsNone(digest._safe_pct_delta(50, 0))

    def test_none_inputs_return_none(self):
        digest = _load_digest()
        self.assertIsNone(digest._safe_pct_delta(None, 100))
        self.assertIsNone(digest._safe_pct_delta(100, None))
        self.assertIsNone(digest._safe_pct_delta(None, None))

    def test_non_numeric_returns_none(self):
        digest = _load_digest()
        self.assertIsNone(digest._safe_pct_delta("nan", 100))
        self.assertIsNone(digest._safe_pct_delta(100, "oops"))

    def test_happy_path(self):
        digest = _load_digest()
        self.assertAlmostEqual(digest._safe_pct_delta(110, 100), 10.0)
        self.assertAlmostEqual(digest._safe_pct_delta(80, 100), -20.0)


# ---------------------------------------------------------------------------
# 8. Section omitted gracefully when Metrics API fails entirely
# ---------------------------------------------------------------------------


class TestMetricsFetchFailureOmitsSections(unittest.TestCase):
    def test_renderer_with_failed_fetches_shows_placeholders_only(self):
        """When both fetches return ok=False, the section body still renders
        (one section, two sub-blocks) but each sub-block shows a 'fetch
        failed' placeholder. The digest can still be assembled and posted."""
        digest = _load_digest()
        body = digest.fmt_cost_and_latency(
            {"ok": False, "by_model": {}},
            {"ok": False, "by_model": {}},
        )
        self.assertIn("latency unavailable", body)
        self.assertIn("cost unavailable", body)
        # No model rows present.
        self.assertNotIn("`gpt-4.1`", body)
        self.assertNotIn("`gpt-5.2`", body)

    def test_fetch_langfuse_metrics_returns_empty_on_http_error(self):
        """A 500 from Langfuse must short-circuit to [] (not raise)."""
        digest = _load_digest()
        err = urllib.error.HTTPError(
            url="https://cloud.langfuse.com/api/public/metrics",
            code=500,
            msg="Internal Server Error",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"oops"),
        )
        with mock.patch("urllib.request.urlopen", side_effect=err):
            rows = digest.fetch_langfuse_metrics(
                measures=["timeToFirstToken"],
                aggregations=["p50"],
                dimensions=["providedModelName"],
                from_ts="2026-05-12T00:00:00Z",
                to_ts="2026-05-13T00:00:00Z",
            )
        self.assertEqual(rows, [])

    def test_build_blocks_still_assembles_when_latency_data_none(self):
        """Defence-in-depth: even if main() passes latency_data=None to
        build_blocks (e.g. an unexpected branch), the function must still
        produce a valid blocks list with a Cost & Latency section showing
        placeholders. The digest must never blow up because of this section."""
        digest = _load_digest()
        blocks = digest.build_blocks(
            academic_rows=None,
            nonacademic_rows=None,
            dump_rows=None,
            score_items=[],
            dv_in_sample=0,
            error_obs=[],
            total_errors=0,
            total_traces=0,
            latency_data=None,
            cost_data=None,
        )
        # The Cost & Latency section must be present and contain placeholders.
        all_text = json.dumps(blocks)
        self.assertIn("Cost & Latency", all_text)
        self.assertIn("latency unavailable", all_text)
        self.assertIn("cost unavailable", all_text)


# ---------------------------------------------------------------------------
# 9. Snapshot includes per-model latency + cost
# ---------------------------------------------------------------------------


class TestSnapshotIncludesLatencyAndCost(unittest.TestCase):
    def test_summarise_emits_model_latency_and_cost_keys(self):
        digest = _load_digest()
        latency_data = {
            "ok": True,
            "yesterday": "2026-05-12",
            "day_before": "2026-05-11",
            "by_model": {
                "gpt-4.1": {
                    "yesterday": {"avg": 2510.5, "p50": 2495.0, "p90": 3567.9, "p95": 4143.45},
                    "day_before": {"avg": 2410.0, "p50": 2400.0, "p90": 3500.0, "p95": 4100.0},
                },
            },
        }
        cost_data = {
            "ok": True,
            "yesterday": "2026-05-12",
            "day_before": "2026-05-11",
            "by_model": {
                "gpt-4.1": {
                    "yesterday": {"total": 218.3257},
                    "day_before": {"total": 198.10},
                },
                "zero-traffic-model": {
                    "yesterday": {"total": 0.0},
                    "day_before": {"total": 0.0},
                },
            },
        }
        summary = digest._summarise_today_for_snapshot(
            error_obs=[],
            total_errors=0,
            score_items=[],
            dv_in_sample=0,
            total_traces=0,
            dump_rows=None,
            academic_rows=None,
            non_academic_rows=None,
            behavior_follow_rows=None,
            behavior_rephrase_rows=None,
            classifier_snapshot=None,
            latency_data=latency_data,
            cost_data=cost_data,
        )
        # Latency: yesterday-only flat shape, ms preserved.
        self.assertIn("model_latency_yesterday", summary)
        self.assertEqual(
            summary["model_latency_yesterday"]["gpt-4.1"],
            {"avg_ms": 2510.5, "p50_ms": 2495.0, "p90_ms": 3567.9, "p95_ms": 4143.45},
        )
        # Cost: yesterday-only, $0 traffic suppressed, value rounded to 4dp.
        self.assertIn("model_cost_yesterday", summary)
        self.assertEqual(summary["model_cost_yesterday"], {"gpt-4.1": 218.3257})
        self.assertNotIn("zero-traffic-model", summary["model_cost_yesterday"])

    def test_summarise_omits_keys_gracefully_when_no_data_passed(self):
        """Backwards-compat: callers that don't pass latency_data/cost_data
        (e.g. legacy tests) must still get a valid snapshot dict — the new
        keys are present but empty rather than missing entirely."""
        digest = _load_digest()
        summary = digest._summarise_today_for_snapshot(
            error_obs=[],
            total_errors=0,
            score_items=[],
            dv_in_sample=0,
            total_traces=0,
            dump_rows=None,
            academic_rows=None,
            non_academic_rows=None,
            behavior_follow_rows=None,
            behavior_rephrase_rows=None,
            classifier_snapshot=None,
        )
        self.assertEqual(summary.get("model_latency_yesterday"), {})
        self.assertEqual(summary.get("model_cost_yesterday"), {})


# ---------------------------------------------------------------------------
# 10. Classifier vs Answer TTFT sub-block split
# ---------------------------------------------------------------------------


class TestLatencySubBlockSplit(unittest.TestCase):
    def _latency(self, by_model):
        return {"ok": True, "yesterday": "y", "day_before": "d", "by_model": by_model}

    def test_classifier_only_renders_classifier_header_not_answer(self):
        digest = _load_digest()
        body = digest.fmt_cost_and_latency(
            self._latency({
                "gpt-5-nano": {
                    "yesterday": {"avg": 310, "p50": 280, "p90": 420, "p95": 540},
                    "day_before": {"avg": 300, "p50": 275, "p90": 410, "p95": 530},
                },
            }),
            {"ok": True, "by_model": {}},
        )
        self.assertIn("Classifier Latency", body)
        self.assertNotIn("Answer TTFT", body)
        self.assertIn("avg: 0.31s", body)
        self.assertIn("p50: 0.28s", body)

    def test_answer_only_renders_answer_header_not_classifier(self):
        digest = _load_digest()
        body = digest.fmt_cost_and_latency(
            self._latency({
                "gpt-4.1": {
                    "yesterday": {"avg": 500, "p50": 420, "p90": 1200, "p95": 1850},
                    "day_before": {"avg": 480, "p50": 400, "p90": 1165, "p95": 1814},
                },
            }),
            {"ok": True, "by_model": {}},
        )
        self.assertIn("Answer TTFT", body)
        self.assertNotIn("Classifier Latency", body)
        # Answer block must NOT show avg.
        self.assertNotIn("avg:", body)

    def test_both_present_renders_both_headers(self):
        digest = _load_digest()
        body = digest.fmt_cost_and_latency(
            self._latency({
                "gpt-5-nano": {
                    "yesterday": {"avg": 310, "p50": 280, "p90": 420, "p95": 540},
                    "day_before": {"avg": 300, "p50": 275, "p90": 410, "p95": 530},
                },
                "gpt-4.1": {
                    "yesterday": {"avg": 500, "p50": 420, "p90": 1200, "p95": 1850},
                    "day_before": {"avg": 480, "p50": 400, "p90": 1165, "p95": 1814},
                },
            }),
            {"ok": True, "by_model": {}},
        )
        self.assertIn("Classifier Latency", body)
        self.assertIn("Answer TTFT", body)
        # avg appears (classifier row) but only once — answer row drops it.
        self.assertEqual(body.count("avg:"), 1)

    def test_neither_present_renders_no_traffic_placeholder(self):
        digest = _load_digest()
        body = digest.fmt_cost_and_latency(
            self._latency({}),
            {"ok": True, "by_model": {}},
        )
        self.assertIn("no TTFT-instrumented model traffic", body)
        self.assertNotIn("Classifier Latency", body)
        self.assertNotIn("Answer TTFT", body)

    def test_all_none_row_renders_no_ttft_data(self):
        digest = _load_digest()
        body = digest.fmt_cost_and_latency(
            self._latency({
                "gpt-5-nano": {
                    "yesterday": {"avg": None, "p50": None, "p90": None, "p95": None},
                    "day_before": {"avg": None, "p50": None, "p90": None, "p95": None},
                },
            }),
            {"ok": True, "by_model": {}},
        )
        line = next(l for l in body.splitlines() if "gpt-5-nano" in l)
        self.assertIn("(no TTFT data)", line)

    def test_avg_present_percentiles_none_still_renders_classifier_row(self):
        digest = _load_digest()
        body = digest.fmt_cost_and_latency(
            self._latency({
                "gpt-5-nano": {
                    "yesterday": {"avg": 305, "p50": None, "p90": None, "p95": None},
                    "day_before": {"avg": 300, "p50": None, "p90": None, "p95": None},
                },
            }),
            {"ok": True, "by_model": {}},
        )
        self.assertIn("Classifier Latency", body)
        line = next(l for l in body.splitlines() if "gpt-5-nano" in l)
        # avg renders, percentiles render as em-dashes (None → `—`).
        self.assertIn("avg: 0.30s", line)  # 305ms → 0.30s (2dp)
        self.assertNotIn("(no TTFT data)", line)


# ---------------------------------------------------------------------------
# 11. Snapshot round-trip includes avg
# ---------------------------------------------------------------------------


class TestSnapshotRoundTripWithAvg(unittest.TestCase):
    def test_avg_ms_preserved_through_summarise(self):
        digest = _load_digest()
        latency_data = {
            "ok": True,
            "yesterday": "2026-05-12",
            "day_before": "2026-05-11",
            "by_model": {
                "gpt-5-nano": {
                    "yesterday": {"avg": 310.7, "p50": 280.0, "p90": 420.0, "p95": 540.0},
                    "day_before": {"avg": 300.0, "p50": 275.0, "p90": 410.0, "p95": 530.0},
                },
            },
        }
        summary = digest._summarise_today_for_snapshot(
            error_obs=[],
            total_errors=0,
            score_items=[],
            dv_in_sample=0,
            total_traces=0,
            dump_rows=None,
            academic_rows=None,
            non_academic_rows=None,
            behavior_follow_rows=None,
            behavior_rephrase_rows=None,
            classifier_snapshot=None,
            latency_data=latency_data,
            cost_data={"ok": True, "by_model": {}},
        )
        self.assertEqual(
            summary["model_latency_yesterday"]["gpt-5-nano"],
            {"avg_ms": 310.7, "p50_ms": 280.0, "p90_ms": 420.0, "p95_ms": 540.0},
        )
        # Round-trip via json — values must survive.
        round_tripped = json.loads(json.dumps(summary))
        self.assertEqual(
            round_tripped["model_latency_yesterday"]["gpt-5-nano"]["avg_ms"], 310.7
        )


if __name__ == "__main__":
    unittest.main()
