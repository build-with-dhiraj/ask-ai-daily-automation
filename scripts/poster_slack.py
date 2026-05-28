"""C1.3: Poster Slack render orchestration.

Bridges the data layers (eval snapshot, digest summary) to the poster
renderer (HTML+Jinja+Playwright PNG) + publisher (gh-pages) + Slack
incoming webhook (image_url block + text companion + thread reply).

Public surface:
    build_scoreboard_poster_input(snapshot)  -> dict
    build_digest_poster_input(today_data, insights_payload) -> dict
    publish_and_assemble(surface, poster_input, *, date_str,
                        ops_text, safety_text, footer_text,
                        fallback_text_block) -> tuple[list, str]
    post_blocks_to_slack(webhook_url, blocks, fallback_text) -> bool

Design contract:
    • Every publish step is wrapped so a failure NEVER crashes the
      daily pipeline; on any PosterRenderError / publish failure /
      URL-not-reachable, the assembler returns the fallback text-only
      Block Kit (caller passes it in) and logs a warning.
    • POSTER_DRY_RUN=1 short-circuits publish_poster (render still
      runs, validating the template + data shape).
    • Thread reply orchestration is delegated to the caller; we
      provide `post_thread_reply(...)` as a convenience helper.

This module does not perform any I/O at import time.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional


# Make repo root importable when this module is loaded under pytest's
# tests/conftest.py paths.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Slack webhook post (blocks payload)
# ---------------------------------------------------------------------------

def post_blocks_to_slack(
    webhook_url: str,
    blocks: list,
    fallback_text: str,
    timeout: float = 120.0,
) -> bool:
    """POST a Block Kit payload to a Slack incoming webhook.

    Parallel to daily_eval.post_to_slack(webhook, text) and
    daily_digest.post_to_slack(blocks, fallback_text), kept as a NEW
    function so existing text-only and digest-blocks paths and their
    tests are unaffected. Returns True on Slack `ok` body, else False.
    """
    # Tightened guard: require BOTH that we're in GitHub Actions AND that a
    # Slack webhook env var is actually configured. The earlier guard let
    # `act` and any local re-runner that exports GITHUB_ACTIONS=true write to
    # Slack as long as a webhook string was passed in. With this check, a
    # local runner with no webhook secret configured cannot accidentally
    # post even if it forges GITHUB_ACTIONS=true.
    in_actions = os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"
    has_webhook = bool(
        os.environ.get("SLACK_WEBHOOK_URL")
        or os.environ.get("SLACK_WEBHOOK_URL_TEST")
        # forward-compat: SLACK_WEBHOOK_URL_PROD is not currently set anywhere
        # in this repo (we use SLACK_WEBHOOK_URL for prod), but kept here so a
        # future _PROD secret naming pivot does not break this guard silently.
        or os.environ.get("SLACK_WEBHOOK_URL_PROD")
    )
    if not (in_actions and has_webhook):
        print(
            "[info] Not running in GitHub Actions with a webhook env var set; "
            "skipping Slack post.",
            file=sys.stderr,
        )
        return False

    payload = {"blocks": blocks, "text": fallback_text}
    body = json.dumps(payload).encode("utf-8")
    retryable_codes = (429, 502, 503, 504)
    for attempt in range(2):
        req = urllib.request.Request(
            webhook_url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body_text = resp.read().decode("utf-8")
            if body_text.strip() == "ok":
                return True
            print(
                f"[warn] Slack webhook returned non-ok: {body_text}",
                file=sys.stderr,
            )
            return False
        except urllib.error.HTTPError as exc:
            if exc.code in retryable_codes and attempt == 0:
                time.sleep(5)
                continue
            print(f"[warn] Slack HTTP {exc.code}: {exc!r}", file=sys.stderr)
            return False
        except urllib.error.URLError as exc:
            if attempt == 0:
                time.sleep(5)
                continue
            print(f"[warn] Slack URLError: {exc!r}", file=sys.stderr)
            return False
    return False


# ---------------------------------------------------------------------------
# Poster input builders: adapt snapshot/summary dicts to the template schema
# ---------------------------------------------------------------------------

def _fmt_pct(value: Optional[float], decimals: int = 1) -> str:
    """Format a percentage value, returns 'n/a' for None."""
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{decimals}f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_usd(value: Optional[float]) -> str:
    """Format a USD amount, returns 'n/a' for None.

    Whole-dollar grain for amounts >= $100 (so the standings cell stays
    legible at ~14px in IBM Plex Mono), two-decimal grain below.
    """
    if value is None:
        return "n/a"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if abs(v) >= 100:
        return f"${v:,.0f}"
    return f"${v:,.2f}"


def _fmt_int(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{int(round(float(value)))}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_seconds(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}s"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_delta_pp(today: Optional[float], median: Optional[float]) -> str:
    """Format a percentage-point delta between today and the 14d median.

    Returns 'n/a' when either input is None. Otherwise a signed string
    like '+1.6pp', '-0.3pp', or '+0.0pp'.
    """
    if today is None or median is None:
        return "n/a"
    try:
        delta = float(today) - float(median)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.1f}pp"


def _fmt_delta_usd(today: Optional[float], median: Optional[float]) -> str:
    if today is None or median is None:
        return "n/a"
    try:
        delta = float(today) - float(median)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if delta >= 0 else ""
    return f"{sign}${abs(delta):,.2f}" if delta < 0 else f"{sign}${delta:,.2f}"


def _fmt_delta_int(today: Optional[float], median: Optional[float]) -> str:
    if today is None or median is None:
        return "n/a"
    try:
        delta = float(today) - float(median)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if delta >= 0 else ""
    return f"{sign}{int(round(delta))}"


def _fmt_delta_seconds(today: Optional[float], median: Optional[float]) -> str:
    if today is None or median is None:
        return "n/a"
    try:
        delta = float(today) - float(median)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.1f}s"


def _build_scoreboard_verdict(
    acc_fail: float,
    breach: bool,
    breach_top_code_label: Optional[str] = None,
) -> str:
    """Deterministic verdict-sentence builder for the scoreboard.

    Locked verdict shapes:
    - Breach:  "Top risk: Academic FAIL crossed the 6% floor at {pct}% today{, driven by ...}."
    - Quiet:   "No urgent risks today, Academic FAIL {pct}% holding inside the 6% floor."

    A deterministic builder is used because: (a) the poster image cannot
    afford an async LLM call in the render path, (b) the prompt-driven
    follow-up text below the image carries the human-readable nuance. The
    poster verdict is structural; the follow-up text is editorial.
    """
    if breach:
        suffix = ""
        if breach_top_code_label:
            suffix = f", driven by {breach_top_code_label}"
        return (
            f"Top risk: Academic FAIL crossed the 6% floor at {acc_fail:.1f}% "
            f"today{suffix}."
        )
    return (
        f"No urgent risks today, Academic FAIL {acc_fail:.1f}% holding "
        f"inside the 6% floor."
    )


# Code -> human label mapping mirrors judge_runner.CODE_LABELS so the
# poster does not import a circular dep at module-load time.
_CODE_LABELS = {
    "A1": "conceptual error",   "A2": "misunderstood doubt",
    "A3": "wrong OCR",          "A4": "calculation error",
    "A5": "answer-incomplete codes",  "A6": "incorrect validation",
    "B1": "ambiguous, badly handled",
    "C1": "equation unreadable","C2": "steps not structured",
    "C3": "symbols corrupted",  "C4": "chem notation broken",
    "D1": "too advanced",       "D2": "too basic",
    "D3": "no direct answer",   "D4": "no clarification asked",
    "E1": "too long",           "E2": "minor details missing",
    "E3": "tone / naturalness",
}


def build_scoreboard_poster_input(snapshot: dict) -> dict:
    """Map an eval snapshot dict to the Variant D template schema.

    Schema emitted (consumed by templates/poster_scoreboard.html.j2):
        date_human, date_iso, n_judged, kill_switch_breach,
        verdict,                # one English sentence
        standings,              # list of 5 row dicts
        spark_series_by_metric, # wired but not rendered in Variant D
        brand_mark              # legacy, retained for alt_text consumers

    Defensive: any missing key defaults to a neutral 'n/a' rendering. The
    template tolerates 'n/a' in any standings cell.
    """
    # Import inline so module-load works in environments where the history
    # file is absent.
    try:
        from scripts.snapshot_history import (
            eval_median, eval_series,
        )
    except ImportError:
        def eval_median(*args, **kwargs):
            return None

        def eval_series(*args, **kwargs):
            return []

    snap = snapshot or {}
    date_iso = snap.get("date") or date.today().isoformat()
    try:
        dt = datetime.fromisoformat(date_iso)
        date_human = dt.strftime("%a %d %b")
    except Exception:
        date_human = date_iso

    acc_fail = float(snap.get("acc_fail_pct") or 0.0)
    exp_fail = float(snap.get("exp_fail_pct") or 0.0)
    pass_pct = float(snap.get("pass_pct") or 0.0)
    n_judged = int(snap.get("n_judged") or 0)
    run_cost = snap.get("run_cost_usd")
    run_cost_f = float(run_cost) if run_cost is not None else None

    kill_switch_breach = acc_fail > 6.0

    # Determine the driving code (label) for the verdict suffix on breach.
    breach_top_code_label = None
    code_counts_raw = snap.get("open_codes_fired_count") or {}
    if code_counts_raw:
        try:
            top_code = max(
                ((k, int(v or 0)) for k, v in code_counts_raw.items()),
                key=lambda kv: kv[1],
            )[0]
            breach_top_code_label = _CODE_LABELS.get(top_code, top_code.lower())
        except (TypeError, ValueError):
            breach_top_code_label = None

    verdict = _build_scoreboard_verdict(
        acc_fail=acc_fail,
        breach=kill_switch_breach,
        breach_top_code_label=breach_top_code_label,
    )

    # 14-day medians from history.
    med_acc = eval_median("acc_fail_pct")
    med_exp = eval_median("exp_fail_pct")
    med_pass = eval_median("pass_pct")
    med_cost = eval_median("run_cost_usd")
    med_n_judged = eval_median("n_judged")

    # Row labels: Commit 11 jargon sweep. RUN COST and JUDGED were insider
    # terms; the new labels read as plain English so a leadership reader can
    # parse the standings without a glossary. Acronyms are expanded via
    # expand_acronyms_first_use after the standings list is built, so the
    # poster image carries the same disambiguation as the text companion.
    standings = [
        {
            "label": "Academic FAIL",
            "yesterday": _fmt_pct(acc_fail),
            "median_14d": _fmt_pct(med_acc),
            "delta": _fmt_delta_pp(acc_fail, med_acc),
            "breach": kill_switch_breach,
        },
        {
            "label": "Experience FAIL",
            "yesterday": _fmt_pct(exp_fail),
            "median_14d": _fmt_pct(med_exp),
            "delta": _fmt_delta_pp(exp_fail, med_exp),
            "breach": False,
        },
        {
            "label": "Overall PASS",
            "yesterday": _fmt_pct(pass_pct),
            "median_14d": _fmt_pct(med_pass),
            "delta": _fmt_delta_pp(pass_pct, med_pass),
            "breach": False,
        },
        {
            "label": "Yesterday's run cost",
            "yesterday": _fmt_usd(run_cost_f),
            "median_14d": _fmt_usd(med_cost),
            "delta": _fmt_delta_usd(run_cost_f, med_cost),
            "breach": False,
        },
        {
            "label": "Traces graded",
            "yesterday": _fmt_int(n_judged),
            "median_14d": _fmt_int(med_n_judged),
            "delta": _fmt_delta_int(n_judged, med_n_judged),
            "breach": False,
        },
    ]
    # Apply acronym expansion to row labels so VCP / TTFT etc. are not
    # surfaced raw inside the standings table itself. Idempotent: row
    # labels that contain no acronym stay unchanged.
    from scripts.follow_up_generator import expand_acronyms_first_use
    for row in standings:
        row["label"] = expand_acronyms_first_use(row["label"])
    verdict = expand_acronyms_first_use(verdict)

    # spark_series_by_metric: wired but unused by Variant D. A future
    # variant can render any of these without a data migration.
    spark_series_by_metric = {
        "acc_fail_pct": eval_series("acc_fail_pct"),
        "exp_fail_pct": eval_series("exp_fail_pct"),
        "pass_pct": eval_series("pass_pct"),
        "run_cost_usd": eval_series("run_cost_usd"),
    }

    # Top driver codes (Commit 11): top-3 axial codes by fire count. The
    # mapping CODE_LABELS translates each raw code into a Hanken-readable
    # label. Empty list on calm days; up to 3 entries on breach days.
    top_driver_codes: list[dict] = []
    if code_counts_raw:
        try:
            ranked = sorted(
                ((str(k), int(v or 0)) for k, v in code_counts_raw.items()),
                key=lambda kv: kv[1],
                reverse=True,
            )
            for code, count in ranked[:3]:
                if count <= 0:
                    continue
                top_driver_codes.append({
                    "code": code,
                    "label": _CODE_LABELS.get(code, code.lower()),
                    "count": count,
                })
        except (TypeError, ValueError):
            top_driver_codes = []

    return {
        "date_human": date_human,
        "date_iso": date_iso,
        "n_judged": n_judged,
        "kill_switch_breach": kill_switch_breach,
        "verdict": verdict,
        "eyebrow_separator": " " + chr(0x00B7) + " ",
        "standings": standings,
        "top_driver_codes": top_driver_codes,
        # scoreboard_callouts: populated by the caller after generate_follow_up
        # returns an InsightPayload. The builder seeds an empty list so the
        # template renders without callouts when the LLM step is skipped.
        "scoreboard_callouts": [],
        "spark_series_by_metric": spark_series_by_metric,
        # Legacy keys retained so alt_text + a few consumer paths keep working
        # while callers migrate. New code should use `verdict`.
        "headline": verdict,
        "brand_mark": "Ask AI, daily eval",
    }


def _synthesize_breach_insight(today: dict) -> dict:
    """Manufacture a single insight describing why the safety floor breached.

    Design audit D2 (carry-forward): when kill_switch_breach=True but the
    LLM insight list is empty, callers should NEVER render a "no anomalies
    today" panel alongside a red breach band. The two contradict each
    other.

    Variant D removes the dedicated insights panel from the poster, but the
    same contradiction can still appear in the Slack text companion below
    the image (LLM follow-up generator may return empty insights on breach
    days due to retry exhaustion). The follow_up_generator deterministic
    fallback uses THIS helper to build a single insight from today_summary,
    so the contradiction cannot manifest.

    Returns a dict in the legacy "insights v2" shape so the follow-up
    fallback path and the few remaining consumers stay compatible.
    """
    acc_fail = float(today.get("acc_fail_pct") or 0.0)
    exp_fail = float(today.get("exp_fail_pct") or 0.0)
    if acc_fail > 6.0:
        return {
            "topic_label": "ACADEMIC",
            "icon": "ALERT",
            "claim": f"Academic FAIL {acc_fail:.1f}% above the 6% floor.",
            "evidence": "Kill switch tripped; see deep-dive for the per-code breakdown.",
            "context": None,
            "spark_series": None,
        }
    # Fallback: a generic safety-floor insight when the acc_fail signal is
    # not above the floor but some other gate flipped breach=True. Keep the
    # surface honest about what we know.
    return {
        "topic_label": "FEEDBACK",
        "icon": "ALERT",
        "claim": "Safety floor breached.",
        "evidence": (
            f"Academic FAIL {acc_fail:.1f}% and Experience FAIL {exp_fail:.1f}%. "
            "Details in deep-dive."
        ),
        "context": None,
        "spark_series": None,
    }


def _build_digest_verdict(today: dict, breach: bool) -> str:
    """Deterministic verdict-sentence builder for the digest.

    Picks the metric most clearly off-trend as the top risk, falling back
    to a calm "no urgent risks today" sentence on quiet days.

    Locked verdict shapes:
    - Breach:  "Top risk: safety floor breached; downvote rate {pct}% above the watch line."
    - Latency drift: "Top risk: student TTFT drifted up {pct}% to {sec} at the 90th percentile."
    - Quiet:   "No urgent risks today, all four watch metrics holding inside their bands."
    """
    if breach:
        dv = today.get("downvote_rate_pct")
        if dv is not None:
            try:
                return (
                    f"Top risk: safety floor breached; downvote rate "
                    f"{float(dv):.2f}% above the watch line."
                )
            except (TypeError, ValueError):
                pass
        return "Top risk: safety floor breached, see deep-dive for the driving metric."
    # Non-breach: a calm verdict.
    return (
        "No urgent risks today, all four watch metrics holding inside their bands."
    )


def _digest_today_value(today: dict, key: str) -> Optional[float]:
    v = (today or {}).get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_digest_poster_input(
    today_data: dict, insights_payload: dict
) -> dict:
    """Map digest today_summary + insights payload to Variant D schema.

    Schema emitted (consumed by templates/poster_digest.html.j2):
        date_human, date_iso, kill_switch_breach,
        verdict,                # one English sentence
        standings,              # list of 5 row dicts
        spark_series_by_metric, # wired but not rendered in Variant D
        brand_mark              # legacy
        insights                # legacy, retained for follow-up text generator
    """
    try:
        from scripts.snapshot_history import (
            digest_median, digest_series,
        )
    except ImportError:
        def digest_median(*args, **kwargs):
            return None

        def digest_series(*args, **kwargs):
            return []

    today = today_data or {}
    insights = insights_payload or {}
    date_iso = today.get("date") or date.today().isoformat()
    try:
        dt = datetime.fromisoformat(date_iso)
        date_human = dt.strftime("%a %d %b")
    except Exception:
        date_human = date_iso

    # F7: derive breach from the snapshot, NOT from the LLM-returned payload.
    # Reading `insights["kill_switch_breach"]` made breach detection silently
    # depend on a downstream consumer's payload shape; when that consumer
    # changed (or when we dropped the upstream fmt_top_insights call), the
    # poster image went green on a real-breach day. Mirror the academic-floor
    # pattern used by build_scoreboard_poster_input (acc_fail > 6.0).
    # Thresholds match daily_digest._KILL_SWITCH_* constants.
    _ACADEMIC_FLOOR = 6.0
    _DOWNVOTE_FLOOR = 1.0

    def _snap_breach(snap: dict) -> bool:
        # Accept either `academic_fail_pct` (digest snapshot convention) or
        # `acc_fail_pct` (eval snapshot convention) for the academic floor;
        # both flow through this builder during cross-surface dogfooding.
        for key, floor in (
            ("academic_fail_pct", _ACADEMIC_FLOOR),
            ("acc_fail_pct", _ACADEMIC_FLOOR),
            ("downvote_rate_pct", _DOWNVOTE_FLOOR),
        ):
            v = snap.get(key)
            if v is None or isinstance(v, bool):
                continue
            try:
                if float(v) > floor:
                    return True
            except (TypeError, ValueError):
                continue
        # Defensive fallthrough: if a future caller pre-computed the breach
        # signal and dropped it on the snapshot directly, honor that too.
        return bool(snap.get("kill_switch_breach"))

    breach = _snap_breach(today)
    insight_list = list(insights.get("insights") or [])
    if breach and not insight_list:
        # D2: never let downstream consumers see a breach + empty insights
        # combo. The follow-up text generator uses this synthetic insight
        # when the LLM has nothing to say but the kill switch fired.
        insight_list = [_synthesize_breach_insight(today)]

    verdict = insights.get("verdict") or _build_digest_verdict(today, breach)

    # Today values (defensive: any may be missing or non-numeric).
    dv_rate = _digest_today_value(today, "downvote_rate_pct")
    vcp_succ = _digest_today_value(today, "vcp_success_pct")
    err_rate = _digest_today_value(today, "error_rate_pct")
    ttft_p90 = _digest_today_value(today, "student_ttft_p90_sec")
    total_cost = _digest_today_value(today, "total_cost_usd")

    # 14-day medians.
    med_dv = digest_median("downvote_rate_pct")
    med_vcp = digest_median("vcp_success_pct")
    med_err = digest_median("error_rate_pct")
    med_ttft = digest_median("student_ttft_p90_sec")
    med_cost = digest_median("total_cost_usd")

    # Row labels: Commit 11 jargon sweep. "VCP" and "TTFT" become full
    # English; the digest stops looking like a private dashboard. The
    # standings list is retained here for the few consumers (alt_text,
    # legacy follow-up) that still read it; the digest TEMPLATE no longer
    # renders this list, so the data is informational not visual.
    standings = [
        {
            "label": "Downvote rate",
            "yesterday": _fmt_pct(dv_rate, decimals=2),
            "median_14d": _fmt_pct(med_dv, decimals=2),
            "delta": _fmt_delta_pp(dv_rate, med_dv),
            "breach": breach,
        },
        {
            "label": "Video Co-Pilot OK %",
            "yesterday": _fmt_pct(vcp_succ),
            "median_14d": _fmt_pct(med_vcp),
            "delta": _fmt_delta_pp(vcp_succ, med_vcp),
            "breach": False,
        },
        {
            "label": "Error rate",
            "yesterday": _fmt_pct(err_rate),
            "median_14d": _fmt_pct(med_err),
            "delta": _fmt_delta_pp(err_rate, med_err),
            "breach": False,
        },
        {
            "label": "Student wait, 90th pct",
            "yesterday": _fmt_seconds(ttft_p90),
            "median_14d": _fmt_seconds(med_ttft),
            "delta": _fmt_delta_seconds(ttft_p90, med_ttft),
            "breach": False,
        },
        {
            "label": "Total cost",
            "yesterday": _fmt_usd(total_cost),
            "median_14d": _fmt_usd(med_cost),
            "delta": _fmt_delta_usd(total_cost, med_cost),
            "breach": False,
        },
    ]
    # Apply acronym expansion to row labels and verdict so the poster image
    # mirrors the slim text companion's plain-English voice.
    from scripts.follow_up_generator import expand_acronyms_first_use
    for row in standings:
        row["label"] = expand_acronyms_first_use(row["label"])
    verdict = expand_acronyms_first_use(verdict)

    spark_series_by_metric = {
        "downvote_rate_pct": digest_series("downvote_rate_pct"),
        "vcp_success_pct": digest_series("vcp_success_pct"),
        "error_rate_pct": digest_series("error_rate_pct"),
        "student_ttft_p90_sec": digest_series("student_ttft_p90_sec"),
        "total_cost_usd": digest_series("total_cost_usd"),
    }

    # F9: the builder no longer seeds `digest_eyebrow_right`. The caller is
    # the single source of truth: it sets the eyebrow AFTER the InsightPayload
    # exists so the rendered card count matches the LLM result. Builder-side
    # seeding caused the count chip to drift from the actual card count when
    # the deterministic fallback and the LLM disagreed on insight count.

    return {
        "date_human": date_human,
        "date_iso": date_iso,
        "kill_switch_breach": breach,
        "verdict": verdict,
        "eyebrow_separator": " " + chr(0x00B7) + " ",
        "standings": standings,
        # digest_cards: populated by the caller after the InsightPayload is
        # generated. Empty list on import-time render so the template falls
        # back to the quiet-day path.
        "digest_cards": [],
        "spark_series_by_metric": spark_series_by_metric,
        # Legacy retained for follow-up text generator + alt_text consumers.
        "headline": verdict,
        "subhead": "",
        "insights": insight_list,
        "brand_mark": "Ask AI, daily digest",
    }


# ---------------------------------------------------------------------------
# Slack block helpers
# ---------------------------------------------------------------------------

def _alt_text_for(poster_input: dict, surface: str) -> str:
    """Compose alt_text combining verdict + key numbers (search-friendly).

    Slack VoiceOver / TalkBack reads this in under 10 seconds. The verdict
    sentence is the first thing read; the standings numbers follow as
    comma-separated values. Surface-specific: scoreboard reads the 5
    standings rows; digest reads the same 5 (the standings cells contain
    everything important in the image).
    """
    verdict = (
        poster_input.get("verdict")
        or poster_input.get("headline")
        or ""
    ).strip()
    standings = poster_input.get("standings") or []
    if standings:
        nums = ", ".join(
            f"{row.get('label', '')} {row.get('yesterday', '')}".strip()
            for row in standings
            if row.get("label") and row.get("yesterday")
        )
        return f"{verdict} | {nums}".strip(" |")
    # Legacy paths (old tests, old fixture files): fall through to the old
    # scoreboard / insights fields if present so we don't strand callers
    # that still pass legacy schemas.
    if surface == "scoreboard":
        scoreboard = poster_input.get("scoreboard") or []
        nums = ", ".join(
            f"{row.get('label', '')} {row.get('value_text', '')}".strip()
            for row in scoreboard
        )
        return f"{verdict} | {nums}".strip(" |")
    insights = poster_input.get("insights") or []
    nums = ", ".join(
        (ins.get("claim") or "").strip()
        for ins in insights if ins.get("claim")
    )
    return f"{verdict} | {nums}".strip(" |")


def make_image_block(image_url: str, alt_text: str) -> dict:
    return {"type": "image", "image_url": image_url, "alt_text": alt_text[:1900]}


def make_section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def make_divider() -> dict:
    return {"type": "divider"}


def make_context(text: str) -> dict:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text}],
    }


# ---------------------------------------------------------------------------
# Orchestration: render + publish (with full fallback)
# ---------------------------------------------------------------------------

def render_and_publish(
    surface: str,
    poster_input: dict,
    date_str: str,
) -> Optional[str]:
    """Try to render + publish the poster. Return public URL on success.

    POSTER_DRY_RUN=1 skips publish but still renders (validates the template).

    On failure, this function PROPAGATES the underlying exception so the
    caller can distinguish render vs publish vs publish_unreachable in its
    operator-facing log line. The caller's _POSTER_RECOVERABLE tuple already
    catches PosterRenderError, PosterPublishError, and
    PosterPublishUnreachableError.

    Returns None only for the small set of pre-flight conditions where there
    is no exception to surface (renderer import failed, render produced
    non-PNG bytes). In those cases the caller logs cause=render with
    reason=render_and_publish returned None, which is accurate.

    Previously this function caught bare `Exception` around both the render
    and publish steps and returned None, causing the caller to log
    `cause=render reason=render_and_publish returned None` even when the
    actual failure was a PosterPublishError 403 from gh-pages. That misled
    the operator on dogfood run #26532281104. Narrowing the excepts so the
    typed exceptions propagate fixes the cause string.
    """
    try:
        from scripts.poster_renderer import (  # type: ignore
            PosterRenderError, render_poster,
        )
    except ImportError as exc:
        # Module-load failure is recoverable (missing deps in this env) but
        # is not a PosterRenderError; surface it as a render failure via the
        # None path, the caller's "returned None" wording is correct here.
        print(f"[warn] poster_renderer import failed: {exc!r}", file=sys.stderr)
        return None

    # Narrowed: PosterRenderError propagates so caller logs cause=render with
    # the actual exception message. The previous bare `except Exception:`
    # swallowed AttributeError/TypeError/etc. silently. Now those programming
    # errors surface in CI, exactly the contract _POSTER_RECOVERABLE assumes.
    png = render_poster(surface, poster_input)  # type: ignore[arg-type]

    if not png or not png.startswith(b"\x89PNG"):
        print("[warn] render returned non-PNG bytes", file=sys.stderr)
        return None

    if os.environ.get("POSTER_DRY_RUN", "").strip() == "1":
        print(
            f"[info] POSTER_DRY_RUN=1, skipping publish for {surface} {date_str}",
            file=sys.stderr,
        )
        # Synthetic local URL so the caller can still see the assembled blocks
        # without hitting the network. Tests can pivot on this prefix.
        return f"file:///tmp/POSTER_DRY_RUN/{surface}/{date_str}.png"

    from scripts.poster_publisher import (  # type: ignore
        PosterPublishUnreachableError,
        _verify_url_reachable,
        publish_deep_dive,
        publish_poster,
    )
    # Narrowed: PosterPublishError (and PosterPublishUnreachableError below)
    # propagate so the caller's _POSTER_RECOVERABLE catch can read the type
    # and log cause=publish (not cause=render). Previously bare-excepted.
    url = publish_poster(png, surface, date_str)  # type: ignore[arg-type]

    # Publish the deep-dive HTML page alongside the PNG. The footer link
    # in the Slack message points here. Best-effort: a deep-dive publish
    # failure does NOT roll back the main poster publish; we log and
    # continue so the Slack message still has the image.
    try:
        publish_deep_dive(
            surface,  # type: ignore[arg-type]
            date_str,
            poster_input=poster_input,
        )
    except Exception as exc:  # noqa: BLE001 - deep dive is best-effort
        print(
            f"[poster] [warn] deep-dive publish failed for "
            f"{surface}/{date_str}: {exc!r}",
            file=sys.stderr,
        )

    # gh-pages takes 5-30s+ to propagate a freshly-pushed file to the CDN.
    # If Slack server-side fetches the image_url before propagation it caches
    # a 404 forever and the message renders broken. So: probe the URL with a
    # bounded retry loop and raise PosterPublishUnreachableError on timeout
    # so the caller degrades to the legacy text post with the right cause.
    #
    # Skip the probe when POSTER_AUTO_PUSH=0: in that mode the publisher only
    # commits locally and the public URL is not expected to be reachable.
    auto_push = os.environ.get("POSTER_AUTO_PUSH", "").strip() == "1"
    if auto_push:
        if not _verify_url_reachable(url, timeout=120):
            raise PosterPublishUnreachableError(
                f"gh-pages URL not reachable within 120s: {url}"
            )
    return url


# ---------------------------------------------------------------------------
# Footer link helpers (constants documented in plan §"Footer links")
# ---------------------------------------------------------------------------

def _deep_dive_url(surface: str, date_iso: str) -> str:
    """Compose the GitHub Pages deep-dive URL for a given surface + date.

    Mirrors the path layout in scripts/poster_publisher.py. Overridable
    via POSTER_PAGES_BASE_URL for staging or alternate-org forks.
    """
    base = os.environ.get(
        "POSTER_PAGES_BASE_URL",
        "https://build-with-dhiraj.github.io/ask-ai-daily-automation",
    )
    base = base.rstrip("/")
    return f"{base}/posters/{surface}/{date_iso}/"


def _langfuse_link_url() -> Optional[str]:
    """Resolve the Langfuse link URL or return None.

    Order:
      1. LANGFUSE_URL env (explicit override).
      2. LANGFUSE_PROJECT_ID env -> hosted Langfuse project URL.
      3. None (the caller must omit the link rather than ship a dead one).
    """
    explicit = os.environ.get("LANGFUSE_URL", "").strip()
    if explicit:
        return explicit
    project_id = os.environ.get("LANGFUSE_PROJECT_ID", "").strip()
    if project_id:
        return f"https://cloud.langfuse.com/project/{project_id}"
    return None


def _stream_logs_link_url() -> Optional[str]:
    """Resolve the Stream logs link URL or return None.

    Order:
      1. STREAM_LOGS_URL env (explicit override).
      2. METABASE_URL + METABASE_STREAM_LOGS_CARD_ID -> question card URL.
      3. None (caller must omit the link).
    """
    explicit = os.environ.get("STREAM_LOGS_URL", "").strip()
    if explicit:
        return explicit
    base = os.environ.get("METABASE_URL", "").strip().rstrip("/")
    card_id = (
        os.environ.get("METABASE_STREAM_LOGS_CARD_ID", "").strip()
        or os.environ.get("METABASE_QUESTION_ID", "").strip()
    )
    if base and card_id:
        return f"{base}/question/{card_id}"
    return None


def _footer_links_common(date_iso: Optional[str], surface: str) -> str:
    """Compose the footer links string for a surface.

    Always emits the Deep dive link (it's surfaced in the slim text companion
    too, but the footer is the durable reference). Langfuse + Stream logs
    are appended only when their env-driven URLs resolve, so a misconfigured
    deployment NEVER ships a dead `https://langfuse` link.
    """
    iso = date_iso or date.today().isoformat()
    parts = []
    langfuse = _langfuse_link_url()
    if langfuse:
        parts.append(f"<{langfuse}|Langfuse>")
    stream = _stream_logs_link_url()
    if stream:
        parts.append(f"<{stream}|Stream logs>")
    sep = "  " + chr(0x00B7) + "  "
    return sep.join(parts)


def scoreboard_footer_links(date_iso: Optional[str] = None) -> str:
    """Scoreboard footer: Langfuse + Stream logs (when configured).

    Deep dive moved to the slim text companion (F4). Langfuse + Stream logs
    are omitted when their env URLs are unset, never shipped as placeholders.
    """
    return _footer_links_common(date_iso, "scoreboard")


def digest_footer_links(date_iso: Optional[str] = None) -> str:
    """Digest footer: Langfuse + Stream logs (when configured).

    Deep dive moved to the slim text companion (F4). Confluence was dropped
    earlier. Langfuse + Stream logs omitted when env URLs are unset.
    """
    return _footer_links_common(date_iso, "digest")
