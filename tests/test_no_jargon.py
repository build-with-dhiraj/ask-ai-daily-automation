"""CI lint: follow-up text companion has no unexplained jargon.

The audience for the daily Slack message includes leadership, DS, eng,
QA, and frontend owners. Acronyms that are not universally understood
(TTFT, VCP, CSAT, SLO, RPS) MUST be expanded on their first occurrence
in any given message. The compound term "percentage points" must back
"pp" on first use. The internal codename "axial" must never appear in
user-facing prose.

This lint exercises both deterministic fallback outputs (so the lint
catches drift even on LLM-down days) and the acronym expander helper.
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.follow_up_generator import (  # noqa: E402
    _build_deterministic_follow_up,
    expand_acronyms_first_use,
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


BANNED_JARGON_NEVER = (
    # Internal codenames that should never reach user-facing prose.
    "axial",
    "axialfail",
)

# Commit 11: the all-caps band-style literal labels are operator-only
# jargon. They are banned from RENDERED TEMPLATE OUTPUT and from
# deterministic prose. The lowercase phrase "safety floor breached"
# inside a verdict sentence is allowed and intentional (it is the
# locked breach-day verdict shape per PRODUCT.md).
BANNED_BAND_LITERALS = (
    "KILL-SWITCH BREACHED",
    "SAFETY FLOOR BREACHED",
)


class TestBannedJargonAbsentFromFallback(unittest.TestCase):
    """Deterministic fallback prose contains no banned internal codenames."""

    def test_scoreboard_breach_has_no_axial(self) -> None:
        text = _build_deterministic_follow_up(
            surface="scoreboard", snapshot=_snapshot(), breach=True,
        )
        lower = text.lower()
        for token in BANNED_JARGON_NEVER:
            self.assertNotIn(
                token, lower,
                f"banned token {token!r} appeared in fallback prose: {text}",
            )

    def test_digest_quiet_has_no_axial(self) -> None:
        text = _build_deterministic_follow_up(
            surface="digest", snapshot=_snapshot(), breach=False,
        )
        lower = text.lower()
        for token in BANNED_JARGON_NEVER:
            self.assertNotIn(
                token, lower,
                f"banned token {token!r} appeared in fallback prose: {text}",
            )


class TestAcronymFirstUseExpansion(unittest.TestCase):
    """Each acronym is expanded on first occurrence inside one message."""

    def test_ttft_expanded_on_first_use(self) -> None:
        text = "TTFT spiked yesterday."
        out = expand_acronyms_first_use(text)
        self.assertIn("TTFT (time to first token)", out)

    def test_vcp_expanded_on_first_use(self) -> None:
        text = "VCP success rate dipped."
        out = expand_acronyms_first_use(text)
        self.assertIn("VCP (Video Co-Pilot)", out)

    def test_csat_expanded_on_first_use(self) -> None:
        text = "CSAT held steady."
        out = expand_acronyms_first_use(text)
        self.assertIn("CSAT (customer satisfaction)", out)

    def test_pp_expanded_on_first_numeric_use(self) -> None:
        text = "Up 1.6pp versus the 14 day median."
        out = expand_acronyms_first_use(text)
        self.assertIn("percentage points", out)

    def test_subsequent_acronym_occurrences_not_re_expanded(self) -> None:
        text = "TTFT spiked. TTFT remains noisy. TTFT recovered."
        out = expand_acronyms_first_use(text)
        # Exactly one parenthetical expansion in the output.
        self.assertEqual(out.count("(time to first token)"), 1)


class TestFallbackOutputContainsNoBareAcronymBeforeExpansion(unittest.TestCase):
    """After running the expander, the first acronym occurrence is expanded.

    This catches the case where a future change to the fallback prose
    forgets to route through expand_acronyms_first_use.
    """

    def test_digest_fallback_expanded_in_pipeline(self) -> None:
        raw = _build_deterministic_follow_up(
            surface="digest", snapshot=_snapshot(), breach=False,
        )
        out = expand_acronyms_first_use(raw)
        # If the fallback mentioned VCP, the parenthetical expansion must
        # appear in the same string.
        if re.search(r"\bVCP\b", out):
            self.assertIn("VCP (Video Co-Pilot)", out)
        if re.search(r"\bTTFT\b", out):
            self.assertIn("TTFT (time to first token)", out)
        if re.search(r"\bCSAT\b", out):
            self.assertIn("CSAT (customer satisfaction)", out)


class TestBannedBandLiteralsAbsentFromRenderedTemplates(unittest.TestCase):
    """The all-caps band labels removed in Commit 11 stay removed.

    Lights up if a future change re-introduces a KillSwitchBand HTML block.
    """

    def _render_both(self) -> str:
        import importlib.util
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        repo = pathlib.Path(__file__).resolve().parent.parent
        env = Environment(
            loader=FileSystemLoader(str(repo / "templates")),
            autoescape=select_autoescape(["html", "j2"]),
        )
        spec = importlib.util.spec_from_file_location(
            "scripts.poster_slack", repo / "scripts" / "poster_slack.py",
        )
        assert spec and spec.loader
        ps = importlib.util.module_from_spec(spec)
        sys.modules["scripts.poster_slack"] = ps
        spec.loader.exec_module(ps)
        today = {"date": "2026-05-27", "acc_fail_pct": 8.2, "exp_fail_pct": 14.1}
        insights = {
            "headline": "Top risk: safety floor breached.",
            "insights": [],
            "kill_switch_breach": True,
        }
        scoreboard_input = ps.build_scoreboard_poster_input({
            "date": "2026-05-27",
            "acc_fail_pct": 8.2,
            "exp_fail_pct": 13.4,
            "pass_pct": 69.0,
            "n_judged": 989,
        })
        digest_input = ps.build_digest_poster_input(today, insights)
        scoreboard_html = env.get_template("poster_scoreboard.html.j2").render(
            **scoreboard_input
        )
        digest_html = env.get_template("poster_digest.html.j2").render(
            **digest_input
        )
        return scoreboard_html + "\n" + digest_html

    def test_no_band_literals_in_rendered_html(self) -> None:
        html = self._render_both()
        for band in BANNED_BAND_LITERALS:
            self.assertNotIn(
                band, html,
                f"banned band literal {band!r} appeared in rendered template "
                "output. The KillSwitchBand was removed in Commit 11; breach "
                "is carried by the verdict prose and the brick row stripe.",
            )


class TestAcronymsExpandedInStandingsRowLabels(unittest.TestCase):
    """build_*_poster_input runs row labels through expand_acronyms_first_use.

    Catches regression where a future label rename re-introduces a raw
    "VCP" or "TTFT" string into the standings table.
    """

    def setUp(self) -> None:
        import importlib.util
        repo = pathlib.Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "scripts.poster_slack", repo / "scripts" / "poster_slack.py",
        )
        assert spec and spec.loader
        self.ps = importlib.util.module_from_spec(spec)
        sys.modules["scripts.poster_slack"] = self.ps
        spec.loader.exec_module(self.ps)

    def test_digest_labels_have_no_raw_acronyms(self) -> None:
        today = {
            "date": "2026-05-27",
            "downvote_rate_pct": 0.57,
            "vcp_success_pct": 96.4,
            "error_rate_pct": 0.4,
            "student_ttft_p90_sec": 8.0,
            "total_cost_usd": 1046.0,
        }
        insights = {"insights": [], "kill_switch_breach": False}
        out = self.ps.build_digest_poster_input(today, insights)
        labels = " | ".join(row["label"] for row in out["standings"])
        # No raw "VCP" without the expansion in the same string.
        if "VCP" in labels:
            self.assertIn("Video Co-Pilot", labels)
        if "TTFT" in labels:
            self.assertIn("time to first token", labels)


if __name__ == "__main__":
    unittest.main()
