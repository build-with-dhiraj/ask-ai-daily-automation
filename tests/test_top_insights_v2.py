"""Top Insights v2 structured-dict generator tests.

Covers the rewritten _call_top_insights_llm + fmt_top_insights pair:
  • Strict JSON schema (headline plus variable 0 to 5 insights)
  • Quality filter: drops insights missing delta verb / comparison anchor /
    overlength claims
  • Quiet-day path returns empty insights list (no padding)
  • Deterministic kill-switch detection on Academic FAIL > 6% OR Downvote
    rate > 1.0%, flagged into returned dict regardless of LLM output
  • LLM unavailable / malformed JSON → sentinel dict, never raises

All tests are pure (no real HTTP, Azure client mocked).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

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


def _make_mock_openai_client(content: str):
    client = mock.MagicMock()
    msg = mock.MagicMock()
    msg.content = content
    choice = mock.MagicMock()
    choice.message = msg
    resp = mock.MagicMock()
    resp.choices = [choice]
    client.chat.completions.create.return_value = resp
    return client


def _valid_insight(
    topic_label: str = "CLARITY",
    icon: str = "📈",
    claim: str = "Downvote rate spiking to 1.4% (was 0.8%).",
    evidence: str = "47 downvotes on 3,350 scored traces.",
    context=None,
    spark_series=None,
) -> dict:
    return {
        "topic_label": topic_label,
        "icon": icon,
        "claim": claim,
        "evidence": evidence,
        "context": context,
        "spark_series": spark_series,
    }


class _V2Base(unittest.TestCase):
    """Shared env setup so the SRE-fix pre-check passes."""

    def setUp(self) -> None:
        self.mod = _load_digest()
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "DEPLOYMENT_NAME",
                "AZURE_API_KEY",
                "AZURE_ENDPOINT",
                "AZURE_OPENAI_API_KEY",
                "AZURE_OPENAI_ENDPOINT",
                "AZURE_DEPLOYMENT_NAME",
            )
        }
        os.environ["DEPLOYMENT_NAME"] = "gpt-4.1-test"
        os.environ["AZURE_API_KEY"] = "test-key"
        os.environ["AZURE_ENDPOINT"] = "https://test.openai.azure.com"
        self._sleep_patch = mock.patch("time.sleep")
        self._sleep_patch.start()

    def tearDown(self) -> None:
        self._sleep_patch.stop()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestHappyPath(_V2Base):
    def test_three_well_formed_insights(self) -> None:
        payload = {
            "headline": "Clarity dipped while latency held flat.",
            "insights": [
                _valid_insight(),
                _valid_insight(
                    topic_label="LATENCY",
                    icon="⚠️",
                    claim="Student TTFT p95 degraded to 4.8s vs Friday 3.1s.",
                    evidence="Sample 12,400 answer requests.",
                ),
                _valid_insight(
                    topic_label="FEEDBACK",
                    icon="💬",
                    claim="Downvote 'too long' up 30% vs WoW.",
                    evidence="Chapter Foo 18/47 downvotes.",
                    context="Same chapter is #2 multi-turn source.",
                ),
            ],
        }
        with mock.patch.object(
            self.mod,
            "_call_top_insights_llm",
            return_value=payload,
        ):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertIsInstance(out, dict)
        self.assertEqual(len(out["insights"]), 3)
        self.assertFalse(out["kill_switch_breach"])
        self.assertFalse(out["_llm_unavailable"])
        self.assertEqual(
            out["headline"], "Clarity dipped while latency held flat."
        )


class TestQuietDay(_V2Base):
    def test_zero_insights_returns_empty_list(self) -> None:
        payload = {
            "headline": "All metrics within band today.",
            "insights": [],
        }
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=payload
        ):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertEqual(out["insights"], [])
        self.assertIsInstance(out["headline"], str)
        self.assertTrue(out["headline"])
        self.assertFalse(out["kill_switch_breach"])


class TestKillSwitch(_V2Base):
    def test_academic_fail_above_floor_flags_breach(self) -> None:
        today = {"academic_fail_pct": 8.0}
        payload = {"headline": "Routine.", "insights": [_valid_insight()]}
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=payload
        ):
            out = self.mod.fmt_top_insights(today, {"b": 2})
        self.assertTrue(out["kill_switch_breach"])
        # Insights still surface alongside the breach flag
        self.assertEqual(len(out["insights"]), 1)

    def test_downvote_rate_above_slo_flags_breach(self) -> None:
        today = {"downvote_rate_pct": 1.4}
        payload = {"headline": "Routine.", "insights": []}
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=payload
        ):
            out = self.mod.fmt_top_insights(today, {"b": 2})
        self.assertTrue(out["kill_switch_breach"])

    def test_under_floors_no_breach(self) -> None:
        today = {"academic_fail_pct": 4.0, "downvote_rate_pct": 0.6}
        payload = {"headline": "Routine.", "insights": []}
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=payload
        ):
            out = self.mod.fmt_top_insights(today, {"b": 2})
        self.assertFalse(out["kill_switch_breach"])


class TestLLMUnavailable(unittest.TestCase):
    """No env vars at all: sentinel dict, no Azure call attempt."""

    def setUp(self) -> None:
        self.mod = _load_digest()
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "DEPLOYMENT_NAME",
                "AZURE_API_KEY",
                "AZURE_ENDPOINT",
                "AZURE_OPENAI_API_KEY",
                "AZURE_OPENAI_ENDPOINT",
                "AZURE_DEPLOYMENT_NAME",
            )
        }
        for k in list(self._saved_env):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_env_returns_sentinel(self) -> None:
        with mock.patch.object(sys, "stderr", io.StringIO()):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertIsInstance(out, dict)
        self.assertTrue(out["_llm_unavailable"])
        self.assertEqual(out["insights"], [])


class TestMalformedJSON(_V2Base):
    def test_malformed_json_returns_sentinel(self) -> None:
        # _call_top_insights_llm raises ValueError on JSON parse fail; emulate.
        with mock.patch.object(
            self.mod,
            "_call_top_insights_llm",
            side_effect=ValueError("bad json"),
        ), mock.patch.object(sys, "stderr", io.StringIO()):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertTrue(out["_llm_unavailable"])
        self.assertEqual(out["insights"], [])


class TestQualityFilter(_V2Base):
    def test_overlength_claim_dropped_others_kept(self) -> None:
        too_long = "x" * 95  # > 90 chars, no delta verb anyway
        payload = {
            "headline": "Mixed signals.",
            "insights": [
                _valid_insight(claim=too_long),
                _valid_insight(),  # valid
            ],
        }
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=payload
        ), mock.patch.object(sys, "stderr", io.StringIO()):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertEqual(len(out["insights"]), 1)
        self.assertIn("spiking", out["insights"][0]["claim"])

    def test_claim_without_delta_verb_dropped(self) -> None:
        payload = {
            "headline": "Mixed.",
            "insights": [
                # No delta verb, no anchor
                _valid_insight(claim="Downvotes at 1.4% today."),
                _valid_insight(),  # valid
            ],
        }
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=payload
        ), mock.patch.object(sys, "stderr", io.StringIO()):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertEqual(len(out["insights"]), 1)

    def test_claim_without_comparison_anchor_dropped(self) -> None:
        payload = {
            "headline": "Mixed.",
            "insights": [
                # Has delta verb (spiking) but no anchor (no "was"/"vs"/numeric ref)
                _valid_insight(claim="Latency spiking on Chapter Foo."),
                _valid_insight(),
            ],
        }
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=payload
        ), mock.patch.object(sys, "stderr", io.StringIO()):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertEqual(len(out["insights"]), 1)


class TestCapsAndShape(_V2Base):
    def test_caps_at_five_insights(self) -> None:
        payload = {
            "headline": "Lots going on.",
            "insights": [_valid_insight() for _ in range(8)],
        }
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=payload
        ):
            out = self.mod.fmt_top_insights({"a": 1}, {"b": 2})
        self.assertLessEqual(len(out["insights"]), 5)

    def test_returned_dict_has_required_keys(self) -> None:
        payload = {"headline": "x.", "insights": []}
        with mock.patch.object(
            self.mod, "_call_top_insights_llm", return_value=payload
        ):
            out = self.mod.fmt_top_insights({"a": 1}, None)
        # First-run path still returns the full dict shape
        for key in ("headline", "insights", "kill_switch_breach", "_llm_unavailable"):
            self.assertIn(key, out)


if __name__ == "__main__":
    unittest.main()
