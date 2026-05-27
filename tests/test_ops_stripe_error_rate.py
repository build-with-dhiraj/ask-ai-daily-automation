"""Ops stripe error rate uses observations as denominator, never traces.

Numerator is a count of error *observation spans* (Langfuse /api/public/
observations?level=ERROR). A single trace fans out into many observations,
so dividing by traces produces rates that exceed 100% on a normal day. The
first dogfood run rendered "errors 561%" because of this bug.

These tests assert the rate is bounded to [0, 100] for realistic fixtures
and that the fallback degrades gracefully when the observations denominator
is missing.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load_digest():
    spec = importlib.util.spec_from_file_location(
        "daily_digest", _ROOT / "daily_digest.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daily_digest"] = mod
    spec.loader.exec_module(mod)
    return mod


def _extract_rate_pct(stripe: str):
    m = re.search(r"errors\s+(\d+(?:\.\d+)?)\s*%", stripe)
    if m is None:
        return None
    return float(m.group(1))


class TestOpsStripeErrorRate(unittest.TestCase):
    def setUp(self):
        self.mod = _load_digest()

    def test_rate_bounded_zero_to_hundred(self):
        # Realistic: 1k traces, ~7k observations, 42 error observations.
        today_summary = {
            "langfuse_errors_total": 42,
            "total_traces_24h": 1000,
            "total_observations_24h": 7000,
            "cost_latency_answer_by_model": {},
        }
        stripe = self.mod._build_digest_ops_stripe_text(today_summary, None, None)
        rate = _extract_rate_pct(stripe)
        self.assertIsNotNone(rate, f"expected percent in stripe, got: {stripe!r}")
        self.assertGreaterEqual(rate, 0.0)
        self.assertLessEqual(rate, 100.0)

    def test_rate_never_exceeds_100_for_busy_day(self):
        # Day similar to the broken dogfood: 50 traces, 300 obs, 50 error obs.
        # Old math: 50 / 50 * 100 = 100% (already maxed); push higher with
        # 60 error obs / 50 traces = 120% (would render as 120.0%).
        today_summary = {
            "langfuse_errors_total": 60,
            "total_traces_24h": 50,
            "total_observations_24h": 300,
            "cost_latency_answer_by_model": {},
        }
        stripe = self.mod._build_digest_ops_stripe_text(today_summary, None, None)
        rate = _extract_rate_pct(stripe)
        self.assertIsNotNone(rate)
        self.assertLessEqual(rate, 100.0)
        self.assertAlmostEqual(rate, 20.0, places=1)

    def test_fallback_to_raw_count_when_observations_missing(self):
        # Older snapshot without total_observations_24h. Should NOT render a
        # percentage with traces as denominator; render a raw count instead.
        today_summary = {
            "langfuse_errors_total": 17,
            "total_traces_24h": 100,
            "cost_latency_answer_by_model": {},
        }
        stripe = self.mod._build_digest_ops_stripe_text(today_summary, None, None)
        self.assertNotIn("%", stripe.split("errors")[-1])
        self.assertIn("errors 17", stripe)

    def test_zero_errors_renders_zero_pct(self):
        today_summary = {
            "langfuse_errors_total": 0,
            "total_traces_24h": 1000,
            "total_observations_24h": 7000,
            "cost_latency_answer_by_model": {},
        }
        stripe = self.mod._build_digest_ops_stripe_text(today_summary, None, None)
        rate = _extract_rate_pct(stripe)
        self.assertEqual(rate, 0.0)


if __name__ == "__main__":
    unittest.main()
