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

def _fmt_delta_pp(delta: Optional[float]) -> tuple[str, str, str]:
    """Return (delta_text, delta_dir, state) for a percentage-point delta."""
    if delta is None:
        return ("n/a", "flat", "neutral")
    # `:.1f` already prints "-1.4" with a leading minus; we only prepend "+"
    # for strictly positive deltas (zero stays "0.0pp").
    sign = "+" if delta > 0 else ""
    direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    state = "neutral"
    return (f"{sign}{delta:.1f}pp", direction, state)


def build_scoreboard_poster_input(snapshot: dict) -> dict:
    """Map an eval snapshot dict (from finalize_eval_run) to the template schema.

    Defensive: any missing key defaults to a neutral / empty rendering.
    """
    snap = snapshot or {}
    date_iso = snap.get("date") or date.today().isoformat()
    try:
        dt = datetime.fromisoformat(date_iso)
        date_human = dt.strftime("%a · %d %b")
    except Exception:
        date_human = date_iso

    acc_fail = float(snap.get("acc_fail_pct") or 0.0)
    exp_fail = float(snap.get("exp_fail_pct") or 0.0)
    pass_pct = float(snap.get("pass_pct") or 0.0)
    n_judged = int(snap.get("n_judged") or 0)

    kill_switch_breach = acc_fail > 6.0

    if kill_switch_breach:
        headline = (
            f"Academic FAIL has crossed the 6% floor "
            f"(today {acc_fail:.1f}%)."
        )
    else:
        headline = (
            f"Eval stable, Academic FAIL {acc_fail:.1f}% within the 6% floor."
        )

    scoreboard = [
        {
            "label": "Academic FAIL",
            "value_text": f"{acc_fail:.1f}%",
            "delta_text": "n/a",
            "delta_dir": "flat",
            "state": "red" if kill_switch_breach else "green",
            "note": "above 6% floor" if kill_switch_breach else "within 6% floor",
        },
        {
            "label": "Experience FAIL",
            "value_text": f"{exp_fail:.1f}%",
            "delta_text": "n/a",
            "delta_dir": "flat",
            "state": "neutral",
            "note": "per-axial detail in thread",
        },
        {
            "label": "Overall PASS",
            "value_text": f"{pass_pct:.1f}%",
            "delta_text": "n/a",
            "delta_dir": "flat",
            "state": "neutral",
            "note": f"n={n_judged}",
        },
    ]

    # Top drivers: pull from axial_fail_pct if present.
    axial = snap.get("axial_fail_pct") or {}
    drivers_sorted = sorted(
        ((k, float(v or 0.0)) for k, v in axial.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )[:3]
    top_drivers = [
        {
            "code": k,
            "label": k.replace("_", " "),
            "count": int(round(v * n_judged / 100.0)) if n_judged else 0,
            "bar_pct": int(round(100.0 * v / drivers_sorted[0][1])) if drivers_sorted and drivers_sorted[0][1] else 0,
        }
        for k, v in drivers_sorted
    ]

    return {
        "date_human": date_human,
        "date_iso": date_iso,
        "n_judged": n_judged,
        "kill_switch_breach": kill_switch_breach,
        "headline": headline,
        "scoreboard": scoreboard,
        "top_drivers": top_drivers,
        "trend": {
            "label": "14-day Academic FAIL trend",
            "spark_series": snap.get("acc_fail_pct_14d") or [],
        },
        "brand_mark": "Ask AI · daily eval",
    }


def build_digest_poster_input(
    today_data: dict, insights_payload: dict
) -> dict:
    """Map digest today_summary + fmt_top_insights v2 payload to template schema."""
    today = today_data or {}
    insights = insights_payload or {}
    date_iso = today.get("date") or date.today().isoformat()
    try:
        dt = datetime.fromisoformat(date_iso)
        date_human = dt.strftime("%a · %d %b")
    except Exception:
        date_human = date_iso

    return {
        "date_human": date_human,
        "date_iso": date_iso,
        "kill_switch_breach": bool(insights.get("kill_switch_breach")),
        "headline": insights.get("headline") or "",
        "subhead": "",
        "insights": insights.get("insights") or [],
        "brand_mark": "Ask AI · daily digest",
    }


# ---------------------------------------------------------------------------
# Slack block helpers
# ---------------------------------------------------------------------------

def _alt_text_for(poster_input: dict, surface: str) -> str:
    """Compose alt_text combining headline + key numbers (search-friendly)."""
    headline = (poster_input.get("headline") or "").strip()
    if surface == "scoreboard":
        scoreboard = poster_input.get("scoreboard") or []
        nums = " · ".join(
            f"{row.get('label', '')} {row.get('value_text', '')}".strip()
            for row in scoreboard
        )
        return f"{headline} | {nums}".strip(" |")
    insights = poster_input.get("insights") or []
    nums = " · ".join(
        (ins.get("claim") or "").strip() for ins in insights if ins.get("claim")
    )
    return f"{headline} | {nums}".strip(" |")


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
        publish_poster,
    )
    # Narrowed: PosterPublishError (and PosterPublishUnreachableError below)
    # propagate so the caller's _POSTER_RECOVERABLE catch can read the type
    # and log cause=publish (not cause=render). Previously bare-excepted.
    url = publish_poster(png, surface, date_str)  # type: ignore[arg-type]

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

def scoreboard_footer_links() -> str:
    one_pager = os.environ.get(
        "EVAL_ONE_PAGER_URL",
        "https://github.com/build-with-dhiraj/ask-ai-daily-automation/blob/main/ONE_PAGER.md",
    )
    metabase_q = os.environ.get(
        "EVAL_METABASE_URL",
        "https://metabase/question/33193",
    )
    return f"📄 <{one_pager}|Eval one-pager>  ·  <{metabase_q}|Metabase Q33193>"


def digest_footer_links() -> str:
    confluence = os.environ.get(
        "CONFLUENCE_ARCHIVE_URL",
        "https://placeholder.confluence/ask-ai-evals-archive",
    )
    langfuse = os.environ.get("LANGFUSE_URL", "https://langfuse")
    stream = os.environ.get("STREAM_LOGS_URL", "https://metabase/stream-logs")
    return (
        f"📄 <{confluence}|Confluence archive>  ·  "
        f"<{langfuse}|Langfuse>  ·  <{stream}|Stream logs>"
    )
