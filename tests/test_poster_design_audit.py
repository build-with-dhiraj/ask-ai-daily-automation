"""Regression tests for the 6 design fixes (D1..D6) from the Design
Specialist audit on top of PR #28.

These tests render the Jinja templates with realistic snapshot inputs and
assert structural / copy properties on the rendered HTML, so the next time
a designer or refactor reintroduces one of these issues the CI bumper
catches it.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TEMPLATES = _ROOT / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(["html", "j2"]),
    )


def _render(template_name: str, ctx: dict) -> str:
    return _env().get_template(template_name).render(**ctx)


def _import(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _scoreboard_ctx_breach() -> dict:
    return {
        "date_human": "Tue · 27 May",
        "date_iso": "2026-05-27",
        "n_judged": 989,
        "kill_switch_breach": True,
        "headline": "Academic FAIL has crossed the 6% floor.",
        "scoreboard": [
            {
                "label": "Academic FAIL", "value_text": "8.2%",
                "delta_text": "n/a", "delta_dir": "flat",
                "state": "red", "note": "above 6% floor",
            },
            {
                "label": "Experience FAIL", "value_text": "14.1%",
                "delta_text": "n/a", "delta_dir": "flat",
                "state": "neutral", "note": "details in thread",
            },
            {
                "label": "Overall PASS", "value_text": "71.3%",
                "delta_text": "n/a", "delta_dir": "flat",
                "state": "neutral", "note": "n=989",
            },
        ],
        "top_drivers": [
            {"code": "A5", "label": "answer incomplete", "count": 137, "bar_pct": 100},
            {"code": "A2", "label": "misunderstood doubt", "count": 95, "bar_pct": 69},
            {"code": "A1", "label": "conceptual error", "count": 92, "bar_pct": 67},
        ],
        "trend": {
            "label": "14-day Academic FAIL trend",
            "spark_series": [3.1, 2.9, 3.4, 3.0, 3.2, 3.1, 2.8, 3.3,
                              3.0, 3.5, 3.7, 4.1, 5.6, 8.2],
        },
        "brand_mark": "Ask AI · daily eval",
    }


def _digest_ctx_quiet() -> dict:
    return {
        "date_human": "Tue · 27 May",
        "date_iso": "2026-05-27",
        "kill_switch_breach": False,
        "headline": "Quiet day, no anomalies.",
        "subhead": "",
        "insights": [],
        "brand_mark": "Ask AI · daily digest",
    }


# ---------------------------------------------------------------------------
# D1 — no state glyph dots in scoreboard rows
# ---------------------------------------------------------------------------

class TestD1NoStateGlyph(unittest.TestCase):
    def test_scoreboard_render_contains_no_colored_dot_glyphs(self) -> None:
        html = _render("poster_scoreboard.html.j2", _scoreboard_ctx_breach())
        for forbidden in ("🔴", "🟡", "🟢"):
            self.assertNotIn(
                forbidden, html,
                f"state_glyph leak: {forbidden!r} should no longer appear in "
                "scoreboard row markup (only the kill-switch band may use red).",
            )

    def test_no_dash_state_glyph_in_scoreboard_delta_cluster(self) -> None:
        """The neutral-state '·' glyph used to render as a misleading empty
        marker inside the delta cluster. It is now gone; only dir_glyph
        survives in that span."""
        html = _render("poster_scoreboard.html.j2", _scoreboard_ctx_breach())
        # The kill-switch and "Safety floor holding" bands still legitimately
        # use a '·' character. The delta-cluster glyph is gone. Easiest
        # invariant: scoreboard row no longer contains a SECOND aria-hidden
        # span after the delta text.
        self.assertNotIn("text-[11px] ml-1", html,
            "leftover state_glyph wrapper span class found in scoreboard")


# ---------------------------------------------------------------------------
# D2 — breach + quiet-day contradiction in digest
# ---------------------------------------------------------------------------

class TestD2BreachQuietContradiction(unittest.TestCase):
    def setUp(self) -> None:
        self.ps = _import("scripts.poster_slack", "scripts/poster_slack.py")

    def test_breach_with_empty_insights_does_not_emit_quiet_panel(self) -> None:
        """When kill_switch_breach=True and insights is empty, the builder
        must NOT produce a poster input that the template would render as
        'No anomalies today'. Builder synthesizes a breach insight."""
        today = {"date": "2026-05-27", "acc_fail_pct": 8.2, "exp_fail_pct": 14.1}
        insights = {
            "headline": "Academic FAIL above floor.",
            "insights": [],
            "kill_switch_breach": True,
        }
        out = self.ps.build_digest_poster_input(today, insights)
        # Post-condition 1: poster input is no longer in the
        # quiet-day shape (breach implies SOMETHING to show).
        self.assertTrue(
            out["kill_switch_breach"],
            "kill_switch_breach must propagate to poster input",
        )
        self.assertGreaterEqual(
            len(out["insights"]), 1,
            "breach + empty insights must NOT pass through as quiet-day: "
            "builder should synthesize a breach insight",
        )

    def test_breach_quiet_render_never_shows_no_anomalies_string(self) -> None:
        """End-to-end: render the digest template with a breach-state input
        produced by build_digest_poster_input and confirm the rendered HTML
        does not contain the contradictory 'No anomalies today' string."""
        today = {"date": "2026-05-27", "acc_fail_pct": 8.2, "exp_fail_pct": 14.1}
        insights = {
            "headline": "Academic FAIL above floor.",
            "insights": [],
            "kill_switch_breach": True,
        }
        out = self.ps.build_digest_poster_input(today, insights)
        html = _render("poster_digest.html.j2", out)
        self.assertNotIn(
            "No anomalies today", html,
            "digest must not show 'No anomalies today' when kill switch breached",
        )


# ---------------------------------------------------------------------------
# D3 — thinner kill-switch band (marker not megaphone)
# ---------------------------------------------------------------------------

class TestD3KillSwitchBandThinned(unittest.TestCase):
    def test_scoreboard_kill_switch_band_drops_red_emoji_and_border(self) -> None:
        html = _render("poster_scoreboard.html.j2", _scoreboard_ctx_breach())
        # The original band carried a literal 🔴 emoji before the label.
        # Now removed.
        self.assertNotIn(
            "🔴",
            html,
            "kill-switch band should no longer carry the 🔴 emoji",
        )
        # Border removed: the band markup should not include `border` on the
        # red-50 wrapper. (Other borders in the doc are fine.)
        m = re.search(r'role="alert"[^>]*>', html)
        if m:
            # Look back ~80 chars (the opening div) for `border` keyword.
            opening = html[max(0, m.start() - 200): m.end()]
            self.assertNotIn(
                "border-red-200", opening,
                "kill-switch band should drop the red border",
            )

    def test_digest_kill_switch_band_drops_red_emoji(self) -> None:
        ctx = _digest_ctx_quiet()
        ctx["kill_switch_breach"] = True
        ctx["headline"] = "Safety floor breached."
        ctx["insights"] = [{
            "topic_label": "ACADEMIC", "icon": "🚨",
            "claim": "Academic FAIL above floor", "evidence": "",
            "context": None, "spark_series": None,
        }]
        html = _render("poster_digest.html.j2", ctx)
        # Strip allowed icon (🚨 in the insight card). We assert specifically
        # that the BAND no longer has the 🔴 marker emoji.
        m = re.search(r'role="alert"[^<]*<span[^>]*>([^<]*)</span>', html)
        if m:
            band_emoji = m.group(1).strip()
            self.assertNotIn("🔴", band_emoji)


# Removed in Variant D: D4 mono-row-label check obsolete per Commit e81acbc
# (Variant D template uses sans-serif metric labels by locked design choice).

# Removed in Variant D: D5 top_drivers-as-codes checks obsolete per Commit
# b265419/e81acbc (scoreboard input no longer emits a top_drivers bar chart;
# replaced by a 5-row standings table).


# ---------------------------------------------------------------------------
# D6 — drop "axial" + "within band" jargon
# ---------------------------------------------------------------------------

class TestD6JargonRemoved(unittest.TestCase):
    def setUp(self) -> None:
        self.ps = _import("scripts.poster_slack", "scripts/poster_slack.py")

    def _user_facing_template_text(self) -> str:
        """Concatenate the user-facing string content of both templates.

        Strips Jinja control blocks ({# ... #} and {% ... %}) so internal
        comments and logic that legitimately mention 'axial' do not count.
        """
        chunks = []
        for name in ("poster_scoreboard.html.j2", "poster_digest.html.j2"):
            text = (_TEMPLATES / name).read_text(encoding="utf-8")
            text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
            text = re.sub(r"\{%.*?%\}", "", text, flags=re.DOTALL)
            chunks.append(text)
        return "\n".join(chunks)

    def test_no_axial_jargon_in_user_facing_template_text(self) -> None:
        body = self._user_facing_template_text().lower()
        self.assertNotIn(
            "axial", body,
            "'axial' is internal jargon and must not appear in user-facing "
            "template strings",
        )

    def test_no_within_band_jargon_in_user_facing_template_text(self) -> None:
        body = self._user_facing_template_text().lower()
        self.assertNotIn(
            "within band", body,
            "'within band' is internal jargon and must not appear in "
            "user-facing template strings",
        )

    # Removed in Variant D: row-note jargon check obsolete per Commit b265419
    # (scoreboard input emits a standings table; rows no longer have a `note`
    # field, so the template-level jargon check above is the surviving guard).


if __name__ == "__main__":
    unittest.main()
