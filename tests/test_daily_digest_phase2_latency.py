"""Cost & Latency section unit tests — stream_logs (Metabase) source.

This file used to cover the Langfuse-Metrics-API-backed implementation. As of
the `feat/digest-cost-latency` refactor, the section's data source is
`cdp.central.silver_stream_logs` via Metabase `/api/dataset`, with a new
yesterday-only shape (no day-on-day deltas) and three TTFT views per answer
model (server / student / llm-only) plus an aggregate classifier row.

Covers:
  • metabase_client.run_native_query — body shape, header, retry on 5xx,
    no-retry on 4xx, row-zipping from cols+rows
  • fetch_yesterday_cost_and_latency_from_stream_logs — shape mapping
  • fmt_cost_and_latency — full-data render, single-model, zero-data,
    classifier-only-no-answers
  • _summarise_today_for_snapshot — new shape preserved through snapshot

All tests are pure: `urllib.request.urlopen` is patched whenever HTTP is
exercised — no live Metabase calls.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import sys
import unittest
import urllib.error
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
    payload = json.dumps(body).encode()
    fake_resp = mock.MagicMock()
    fake_resp.read.return_value = payload
    fake_resp.__enter__ = lambda self: fake_resp
    fake_resp.__exit__ = lambda self, *a: False
    return fake_resp


def _dataset_response(cols_rows):
    """Build a Metabase /api/dataset response body from [(col, val), ...] tuples per row.

    `cols_rows` is List[List[Tuple[str, value]]]. Columns are derived from the
    first row's keys; rows are emitted in the same column order.
    """
    if not cols_rows:
        return {"data": {"cols": [], "rows": []}}
    col_names = [c for c, _ in cols_rows[0]]
    cols = [{"name": n} for n in col_names]
    rows = [[v for _, v in row] for row in cols_rows]
    return {"data": {"cols": cols, "rows": rows}}


# ---------------------------------------------------------------------------
# 1. metabase_client.run_native_query
# ---------------------------------------------------------------------------


class TestMetabaseClientRunNativeQuery(unittest.TestCase):
    def setUp(self):
        # Reload to ensure env-driven module constants pick up our patches.
        import metabase_client
        importlib.reload(metabase_client)
        self.mc = metabase_client

    def test_posts_correct_body_and_header(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.headers)
            captured["body"] = json.loads(req.data.decode())
            return _mock_urlopen_returning(
                _dataset_response([[("foo", 1), ("bar", "x")]])
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rows = self.mc.run_native_query(
                "SELECT 1 WHERE created_at >= {{start_ts}}",
                {"start_ts": "2026-05-12T00:00:00Z", "end_ts": "2026-05-13T00:00:00Z"},
                database_id=895,
            )

        self.assertEqual(rows, [{"foo": 1, "bar": "x"}])
        self.assertTrue(captured["url"].endswith("/api/dataset"))
        # Header case-insensitive (urllib lowercases custom names).
        hdrs = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertIn("x-api-key", hdrs)
        self.assertEqual(hdrs.get("content-type"), "application/json")
        body = captured["body"]
        self.assertEqual(body["database"], 895)
        self.assertEqual(body["type"], "native")
        self.assertIn("query", body["native"])
        self.assertIn("start_ts", body["native"]["template-tags"])
        self.assertIn("end_ts", body["native"]["template-tags"])
        # parameters list contains both, with date/single type.
        param_names = [p["target"][1][1] for p in body["parameters"]]
        self.assertIn("start_ts", param_names)
        self.assertIn("end_ts", param_names)
        for p in body["parameters"]:
            self.assertEqual(p["type"], "date/single")

    def test_5xx_triggers_one_retry_then_succeeds(self):
        err = urllib.error.HTTPError(
            url="https://metabase/api/dataset",
            code=503,
            msg="Service Unavailable",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"down"),
        )
        ok_resp = _mock_urlopen_returning(_dataset_response([[("c", 7)]]))

        # First call raises 5xx; second call succeeds.
        side_effects = [err, ok_resp]

        def fake_urlopen(req, timeout=None):
            v = side_effects.pop(0)
            if isinstance(v, BaseException):
                raise v
            return v

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch("time.sleep"):  # don't actually sleep 10s
                rows = self.mc.run_native_query("SELECT 1", {})
        self.assertEqual(rows, [{"c": 7}])

    def test_4xx_raises_immediately_no_retry(self):
        err = urllib.error.HTTPError(
            url="https://metabase/api/dataset",
            code=400,
            msg="Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"bad sql"),
        )
        call_count = {"n": 0}

        def fake_urlopen(req, timeout=None):
            call_count["n"] += 1
            raise err

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(self.mc.MetabaseQueryError):
                self.mc.run_native_query("SELECT 1", {})
        self.assertEqual(call_count["n"], 1, "4xx must not retry")

    def test_5xx_retry_exhaustion_raises(self):
        err = urllib.error.HTTPError(
            url="https://metabase/api/dataset",
            code=502,
            msg="Bad Gateway",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"oops"),
        )
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with mock.patch("time.sleep"):
                with self.assertRaises(self.mc.MetabaseQueryError):
                    self.mc.run_native_query("SELECT 1", {})


# ---------------------------------------------------------------------------
# 2. fetch_yesterday_cost_and_latency_from_stream_logs — shape mapping
# ---------------------------------------------------------------------------


class TestFetchYesterdayCostAndLatencyShape(unittest.TestCase):
    def test_maps_answer_and_classifier_rows_into_target_shape(self):
        digest = _load_digest()
        # First call → answer rows. Second call → classifier rows.
        answer_payload = _dataset_response([
            [
                ("llm_model_name", "gpt-4.1"),
                ("request_count", 39576),
                ("ttft_ms_p50", 4881.0),
                ("ttft_ms_p90", 6877.0),
                ("ttft_ms_p95", 7442.0),
                ("student_ttft_ms_p50", 6049.0),
                ("student_ttft_ms_p90", 7715.0),
                ("student_ttft_ms_p95", 8388.0),
                ("llm_ttft_ms_p50", 2876.0),
                ("llm_ttft_ms_p90", 4187.0),
                ("llm_ttft_ms_p95", 4788.0),
                ("llm_cost_usd", 435.27),
                ("llm_input_tokens", 330_960_000),
                ("llm_output_tokens", 15_680_000),
                ("llm_cached_tokens", 234_760_000),
            ],
        ])
        classifier_payload = _dataset_response([
            [
                ("request_count", 116803),
                ("avg_ms", 3026.23),
                ("classification_ms_p50", 3019.71),
                ("classification_ms_p90", 3527.36),
                ("classification_ms_p95", 3820.77),
                ("classification_cost_usd", 37.80),
            ],
        ])
        responses = [answer_payload, classifier_payload]

        def fake_urlopen(req, timeout=None):
            return _mock_urlopen_returning(responses.pop(0))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            out = digest.fetch_yesterday_cost_and_latency_from_stream_logs()

        self.assertTrue(out["ok"])
        gpt = out["answer_by_model"]["gpt-4.1"]
        self.assertEqual(gpt["request_count"], 39576)
        self.assertEqual(gpt["ttft_ms"], {"p50": 4881.0, "p90": 6877.0, "p95": 7442.0})
        self.assertEqual(gpt["student_ttft"], {"p50": 6049.0, "p90": 7715.0, "p95": 8388.0})
        self.assertEqual(gpt["llm_ttft"], {"p50": 2876.0, "p90": 4187.0, "p95": 4788.0})
        self.assertEqual(gpt["cost_usd"], 435.27)
        self.assertEqual(gpt["tokens"]["input"], 330_960_000)
        c = out["classifier"]
        self.assertEqual(c["request_count"], 116803)
        self.assertAlmostEqual(c["avg_ms"], 3026.23)
        self.assertAlmostEqual(c["cost_usd"], 37.80)

    def test_returns_ok_false_when_both_queries_fail(self):
        digest = _load_digest()
        err = urllib.error.HTTPError(
            url="https://metabase/api/dataset",
            code=500, msg="x", hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"oops"),
        )
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with mock.patch("time.sleep"):
                out = digest.fetch_yesterday_cost_and_latency_from_stream_logs()
        self.assertFalse(out["ok"])
        self.assertEqual(out["answer_by_model"], {})
        self.assertIsNone(out["classifier"])


# ---------------------------------------------------------------------------
# 3. fmt_cost_and_latency — render shapes
# ---------------------------------------------------------------------------


_FULL_DATA = {
    "ok": True,
    "yesterday": "2026-05-12",
    "answer_by_model": {
        "gpt-4.1": {
            "request_count": 39576,
            "ttft_ms": {"p50": 4881, "p90": 6877, "p95": 7442},
            "student_ttft": {"p50": 6049, "p90": 7715, "p95": 8388},
            "llm_ttft": {"p50": 2876, "p90": 4187, "p95": 4788},
            "cost_usd": 435.27,
            "tokens": {"input": 330_960_000, "output": 15_680_000, "cached": 234_760_000},
        },
        "gemini-3-flash-preview": {
            "request_count": 20000,
            "ttft_ms": {"p50": 5103, "p90": 7256, "p95": 8212},
            "student_ttft": {"p50": 6199, "p90": 7810, "p95": 10073},
            "llm_ttft": {"p50": 2961, "p90": 4248, "p95": 6567},
            "cost_usd": 177.99,
            "tokens": {"input": 334_200_000, "output": 15_300_000, "cached": 77_600_000},
        },
        "gpt-5.2": {
            "request_count": 30000,
            "ttft_ms": {"p50": 5046, "p90": 7315, "p95": 7792},
            "student_ttft": {"p50": 7056, "p90": 8535, "p95": 9217},
            "llm_ttft": {"p50": 3906, "p90": 5063, "p95": 5631},
            "cost_usd": 428.26,
            "tokens": {"input": 316_400_000, "output": 14_500_000, "cached": 213_500_000},
        },
    },
    "classifier": {
        "request_count": 116803,
        "avg_ms": 3026.23,
        "p50": 3019.71, "p90": 3527.36, "p95": 3820.77,
        "cost_usd": 37.80,
    },
}


class TestFmtCostAndLatencyFullData(unittest.TestCase):
    def test_renders_all_sections_with_metrics(self):
        digest = _load_digest()
        body = digest.fmt_cost_and_latency(_FULL_DATA)
        # Provenance hint
        self.assertIn("silver_stream_logs", body)
        # Classifier
        self.assertIn("Classifier Latency", body)
        self.assertIn("116,803", body)
        self.assertIn("avg 3026ms", body)
        # Answer TTFT
        self.assertIn("Answer TTFT", body)
        self.assertIn("`gpt-4.1`", body)
        self.assertIn("`gemini-3-flash-preview`", body)
        self.assertIn("`gpt-5.2`", body)
        self.assertIn("server: 4881 / 6877 / 7442ms", body)
        self.assertIn("student: 6049 / 7715 / 8388ms", body)
        self.assertIn("llm-only: 2876 / 4187 / 4788ms", body)
        # Three explanation lines
        self.assertIn("server   =", body)
        self.assertIn("student  =", body)
        self.assertIn("llm-only =", body)
        # Cost
        self.assertIn("$435.27", body)
        self.assertIn("$177.99", body)
        self.assertIn("$428.26", body)
        self.assertIn("Classifier", body)
        self.assertIn("$37.80", body)
        # Total = 435.27 + 177.99 + 428.26 + 37.80 = 1079.32
        self.assertIn("$1,079.32", body)
        # Token formatting
        self.assertIn("331.0M in", body)

    def test_single_model_case(self):
        digest = _load_digest()
        data = {
            "ok": True,
            "answer_by_model": {
                "gpt-4.1": _FULL_DATA["answer_by_model"]["gpt-4.1"],
            },
            "classifier": _FULL_DATA["classifier"],
        }
        body = digest.fmt_cost_and_latency(data)
        self.assertIn("`gpt-4.1`", body)
        self.assertNotIn("`gpt-5.2`", body)
        # Total = answer cost + classifier cost
        self.assertIn("$473.07", body)  # 435.27 + 37.80

    def test_zero_data_case(self):
        digest = _load_digest()
        data = {"ok": True, "answer_by_model": {}, "classifier": None}
        body = digest.fmt_cost_and_latency(data)
        # All three sub-blocks render their headers + "(no data)" placeholder.
        self.assertIn("Classifier Latency", body)
        self.assertIn("Answer TTFT", body)
        self.assertIn("(no data)", body)
        self.assertNotIn("$0.00", body)  # total row suppressed

    def test_classifier_only_no_answers(self):
        digest = _load_digest()
        data = {
            "ok": True,
            "answer_by_model": {},
            "classifier": _FULL_DATA["classifier"],
        }
        body = digest.fmt_cost_and_latency(data)
        self.assertIn("116,803", body)
        # Answer section renders no-data, not a model row.
        ans_idx = body.index("Answer TTFT")
        cost_idx = body.index("Cost")
        between = body[ans_idx:cost_idx]
        self.assertIn("(no data)", between)
        # Total = classifier cost only.
        self.assertIn("$37.80", body)

    def test_fetch_failure_renders_placeholder(self):
        digest = _load_digest()
        body = digest.fmt_cost_and_latency(
            {"ok": False, "answer_by_model": {}, "classifier": None}
        )
        self.assertIn("Metabase fetch failed", body)
        self.assertNotIn("`gpt-4.1`", body)


# ---------------------------------------------------------------------------
# 4. _summarise_today_for_snapshot — new shape
# ---------------------------------------------------------------------------


class TestSnapshotShape(unittest.TestCase):
    def test_summarise_emits_new_cost_latency_keys(self):
        digest = _load_digest()
        summary = digest._summarise_today_for_snapshot(
            error_obs=[], total_errors=0, score_items=[],
            dv_in_sample=0, total_traces=0,
            dump_rows=None, academic_rows=None, non_academic_rows=None,
            behavior_follow_rows=None, behavior_rephrase_rows=None,
            classifier_snapshot=None,
            cost_latency_data=_FULL_DATA,
        )
        self.assertIn("cost_latency_answer_by_model", summary)
        self.assertIn("cost_latency_classifier", summary)
        gpt = summary["cost_latency_answer_by_model"]["gpt-4.1"]
        self.assertEqual(gpt["request_count"], 39576)
        self.assertEqual(gpt["ttft_ms"], {"p50": 4881, "p90": 6877, "p95": 7442})
        self.assertEqual(gpt["cost_usd"], 435.27)
        c = summary["cost_latency_classifier"]
        self.assertEqual(c["request_count"], 116803)
        self.assertAlmostEqual(c["cost_usd"], 37.80)
        # Snapshot must JSON-round-trip cleanly.
        round_tripped = json.loads(json.dumps(summary))
        self.assertEqual(
            round_tripped["cost_latency_answer_by_model"]["gpt-4.1"]["cost_usd"],
            435.27,
        )

    def test_summarise_handles_missing_cost_latency_data(self):
        digest = _load_digest()
        summary = digest._summarise_today_for_snapshot(
            error_obs=[], total_errors=0, score_items=[],
            dv_in_sample=0, total_traces=0,
            dump_rows=None, academic_rows=None, non_academic_rows=None,
            behavior_follow_rows=None, behavior_rephrase_rows=None,
            classifier_snapshot=None,
        )
        self.assertEqual(summary.get("cost_latency_answer_by_model"), {})
        self.assertIsNone(summary.get("cost_latency_classifier"))


# ---------------------------------------------------------------------------
# 5. build_blocks still assembles when cost_latency_data is None
# ---------------------------------------------------------------------------


class TestBuildBlocksDefensive(unittest.TestCase):
    def test_build_blocks_with_no_cost_latency_data(self):
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
            cost_latency_data=None,
        )
        all_text = json.dumps(blocks)
        self.assertIn("Cost & Latency", all_text)
        self.assertIn("Metabase fetch failed", all_text)


if __name__ == "__main__":
    unittest.main()
