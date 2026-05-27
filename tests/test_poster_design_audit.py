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


# ---------------------------------------------------------------------------
# D4 — row labels mono (unified register with eyebrows)
# ---------------------------------------------------------------------------

class TestD4ScoreboardRowLabelsMono(unittest.TestCase):
    def test_row_label_uses_mono_class(self) -> None:
        html = _render("poster_scoreboard.html.j2", _scoreboard_ctx_breach())
        # The scoreboard row label that previously used `font-semibold
        # tracking-wide uppercase` (Inter) must now use the `mono` class
        # so its register matches the eyebrows.
        m = re.search(
            r'<div[^>]*class="[^"]*mono[^"]*tracking-wide[^"]*uppercase[^"]*"[^>]*>'
            r'\s*Academic FAIL\s*</div>',
            html,
        )
        if m is None:
            # alternative class ordering
            m = re.search(
                r'<div[^>]*class="[^"]*uppercase[^"]*mono[^"]*"[^>]*>'
                r'\s*Academic FAIL\s*</div>',
                html,
            )
        self.assertIsNotNone(
            m,
            "scoreboard row label should use the 'mono' class so its register "
            "matches the eyebrows above it",
        )


# ---------------------------------------------------------------------------
# D5 — bar chart shows CODES (novel info), not axes (restated headline)
# ---------------------------------------------------------------------------

class TestD5TopDriversAreCodesNotAxes(unittest.TestCase):
    def setUp(self) -> None:
        self.ps = _import("scripts.poster_slack", "scripts/poster_slack.py")

    def test_top_drivers_are_codes_not_axes(self) -> None:
        """The bar chart now sources from open_codes_fired_count and emits
        entries whose `code` field matches /^[A-E][0-9]$/. Previously the
        builder shipped axis names like 'academic' / 'tone' in `label` and
        empty codes."""
        snap = {
            "date": "2026-05-27", "n_judged": 989,
            "acc_fail_pct": 8.2, "exp_fail_pct": 14.1, "pass_pct": 71.3,
            "axial_fail_pct": {"academic": 13.8, "tone": 9.6, "intent": 3.1},
            "open_codes_fired_count": {
                "A5": 152, "A1": 107, "A2": 96, "E2": 41, "C1": 12,
            },
        }
        out = self.ps.build_scoreboard_poster_input(snap)
        drivers = out["top_drivers"]
        self.assertEqual(
            len(drivers), 3,
            "expected exactly 3 top driver codes",
        )
        for d in drivers:
            self.assertRegex(
                d["code"], r"^[A-E][0-9]$",
                f"driver entry code should be a single open-code id "
                f"(A1..E3), got {d['code']!r} in {d!r}",
            )
        # Top driver count should be the max (152), and bar_pct=100.
        self.assertEqual(drivers[0]["code"], "A5")
        self.assertEqual(drivers[0]["count"], 152)
        self.assertEqual(drivers[0]["bar_pct"], 100)

    def test_scoreboard_renders_code_bars_with_per_code_heading(self) -> None:
        snap = {
            "date": "2026-05-27", "n_judged": 989,
            "acc_fail_pct": 8.2, "exp_fail_pct": 14.1, "pass_pct": 71.3,
            "axial_fail_pct": {"academic": 13.8},
            "open_codes_fired_count": {"A5": 152, "A1": 107, "A2": 96},
        }
        out = self.ps.build_scoreboard_poster_input(snap)
        html = _render("poster_scoreboard.html.j2", out)
        # Heading now reflects codes, not axes.
        self.assertNotIn("Top axis by fail rate", html,
            "heading should no longer say 'Top axis by fail rate'")
        # A5 / A1 / A2 codes appear in the rendered bars.
        self.assertIn("A5", html)
        self.assertIn("A1", html)


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

    def test_scoreboard_note_text_does_not_emit_axial(self) -> None:
        """Builder defaults must not put 'axial' or 'within band' into row
        notes (e.g. via 'per-axial detail in thread' on the Experience row)."""
        snap = {
            "date": "2026-05-27", "n_judged": 989,
            "acc_fail_pct": 5.0, "exp_fail_pct": 14.0, "pass_pct": 81.0,
            "axial_fail_pct": {},
        }
        out = self.ps.build_scoreboard_poster_input(snap)
        for row in out["scoreboard"]:
            note = (row.get("note") or "").lower()
            self.assertNotIn("axial", note)
            self.assertNotIn("within band", note)


if __name__ == "__main__":
    unittest.main()
