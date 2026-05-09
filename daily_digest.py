#!/usr/bin/env python3
"""
Ask AI Daily Digest — replaces n8n Cloud workflow.
Fetches Metabase + Langfuse data and posts a formatted summary to Slack.

Required env vars:
  METABASE_URL         e.g. https://metabase-prod.penpencil.co
  METABASE_API_KEY
  LANGFUSE_PUBLIC_KEY
  LANGFUSE_SECRET_KEY
  LANGFUSE_HOST        (default: https://cloud.langfuse.com)
  SLACK_WEBHOOK_URL
  METABASE_STREAM_LOGS_CARD_ID  optional — Metabase question for
      sql/vcp_stream_logs_digest_summary.sql (E2E API health vs Langfuse 24h block)

Metabase /api/card/.../query/json calls use no HTTP timeout (wait until the server
returns). Langfuse and Slack keep short timeouts. The GitHub Actions job still has
workflow `timeout-minutes` in `.github/workflows/daily-digest.yml` as the outer cap.

Usage:
  python3 daily_digest.py           # fetch + post to Slack
  python3 daily_digest.py --dry-run # fetch + print only, skip Slack
"""

import json
import os
import sys
import base64
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

METABASE_URL     = os.environ.get("METABASE_URL", "https://metabase-prod.penpencil.co")
METABASE_API_KEY = os.environ.get("METABASE_API_KEY", "")
LANGFUSE_HOST    = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
LANGFUSE_PK      = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SK      = os.environ.get("LANGFUSE_SECRET_KEY", "")
SLACK_WEBHOOK    = os.environ.get("SLACK_WEBHOOK_URL", "")

DRY_RUN = "--dry-run" in sys.argv

# C11/C12 — optional Metabase cards + yesterday's eval snapshot (same path as daily_eval.py)
EVAL_SUMMARY_PATH = os.environ.get("EVAL_SUMMARY_PATH", "/tmp/daily_eval_yesterday_summary.json")
BEHAVIOR_FOLLOWUP_CARD_ID = os.environ.get("METABASE_BEHAVIOR_FOLLOWUP_CARD_ID", "").strip()
# Accept typo alias from GitHub secret name (missing _ID suffix)
BEHAVIOR_REPHRASE_CARD_ID = (
    os.environ.get("METABASE_BEHAVIOR_REPHRASE_CARD_ID", "").strip()
    or os.environ.get("METABASE_BEHAVIOR_REPHRASE_CARD", "").strip()
)
STREAM_LOGS_CARD_ID = os.environ.get("METABASE_STREAM_LOGS_CARD_ID", "").strip()

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

now_utc   = datetime.now(timezone.utc)
yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
today_str = now_utc.strftime("%Y-%m-%d")
from_24h  = (now_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _http_post_json(
    url: str,
    headers: Dict,
    body: Optional[Dict] = None,
    timeout: Optional[float] = 90,
) -> Union[List, Dict]:
    """POST JSON body. timeout=None means no socket read limit (wait until Metabase finishes)."""
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _langfuse_auth_header() -> str:
    token = base64.b64encode(f"{LANGFUSE_PK}:{LANGFUSE_SK}".encode()).decode()
    return f"Basic {token}"

# ---------------------------------------------------------------------------
# Metabase fetchers
# ---------------------------------------------------------------------------

def fetch_metabase_card(card_id: int) -> Optional[List]:
    url = f"{METABASE_URL}/api/card/{card_id}/query/json"
    headers = {
        "X-Api-Key": METABASE_API_KEY,
        "Content-Type": "application/json",
    }
    try:
        # No timeout — Metabase /query/json duration is unbounded on heavy cards.
        result = _http_post_json(url, headers, timeout=None)
        return result if isinstance(result, list) else None
    except Exception as exc:
        print(f"[warn] Metabase card {card_id} failed: {exc}", file=sys.stderr)
        return None


def fmt_academic(rows: Optional[List]) -> str:
    if rows is None:
        return "  _(unavailable)_"
    lines = []
    for row in rows:
        text  = row.get("feedback_text", "Unknown")
        count = row.get("downvotes", 0)
        lines.append(f"  • {text}: {count:,}")
    return "\n".join(lines) if lines else "  _(no data)_"


def fmt_nonacademic(rows: Optional[List]) -> str:
    return fmt_academic(rows)


def fmt_downvote_dump(rows: Optional[List]) -> str:
    if rows is None:
        return "  _(unavailable)_"

    # Filter to yesterday — check every string field for a value starting with yesterday
    yesterday_rows = []
    for row in rows:
        for v in row.values():
            if isinstance(v, str) and v.startswith(yesterday):
                yesterday_rows.append(row)
                break

    n = len(yesterday_rows)
    if n == 0:
        return f"  0 downvoted queries logged for {yesterday}."

    # Category split — try common field names
    cat_counts: Dict[str, int] = {}
    for row in yesterday_rows:
        cat = (
            row.get("category")
            or row.get("Category")
            or row.get("type")
            or "unknown"
        )
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    cat_lines = "\n".join(
        f"  {cat}: {cnt:,}" for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1])
    )

    # Top tagged reasons — Q23036 uses "user_feedback" field
    reason_counts: Dict[str, int] = {}
    for row in yesterday_rows:
        reason = (
            row.get("user_feedback")
            or row.get("reason")
            or row.get("Reason")
            or row.get("feedback_text")
            or row.get("tag")
            or None
        )
        if reason:
            reason = reason.strip().rstrip(",").strip()  # clean "Incorrect answer, " → "Incorrect answer"
            if reason:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

    reason_lines = "\n".join(
        f"  • {r}: {c:,}"
        for r, c in sorted(reason_counts.items(), key=lambda x: -x[1])[:10]
    )
    if not reason_lines:
        reason_lines = "  _(no tagged reasons)_"

    return (
        f"{n:,} downvoted queries logged. Category split:\n{cat_lines}\n\n"
        f"Top tagged reasons (yesterday only):\n{reason_lines}"
    )

# ---------------------------------------------------------------------------
# Langfuse fetchers
# ---------------------------------------------------------------------------

def fetch_langfuse_scores() -> Tuple[List, int]:
    """Returns (score_items_sample, csat_downvote_count_from_sample).
    Fetches up to 500 scores and counts value==0 (CSAT downvotes) from the sample.
    Langfuse API does not support value filtering, so count is from sample only.
    """
    auth = {"Authorization": _langfuse_auth_header()}
    url = (
        f"{LANGFUSE_HOST}/api/public/scores"
        f"?fromTimestamp={urllib.parse.quote(from_24h)}&limit=100"
    )
    try:
        data = _http_get(url, auth)
        score_items = data.get("data", [])
        dv_count = sum(1 for s in score_items if s.get("value") == 0)
        return score_items, dv_count
    except Exception as exc:
        print(f"[warn] Langfuse scores failed: {exc}", file=sys.stderr)
        return [], 0


def fetch_langfuse_errors() -> Tuple[List, int]:
    url = (
        f"{LANGFUSE_HOST}/api/public/observations"
        f"?fromTimestamp={urllib.parse.quote(from_24h)}&limit=100&level=ERROR"
    )
    try:
        data = _http_get(url, {"Authorization": _langfuse_auth_header()})
        return data.get("data", []), data.get("meta", {}).get("totalItems", 0)
    except Exception as exc:
        print(f"[warn] Langfuse errors failed: {exc}", file=sys.stderr)
        return [], 0


def fetch_langfuse_traces_total() -> int:
    url = (
        f"{LANGFUSE_HOST}/api/public/traces"
        f"?fromTimestamp={urllib.parse.quote(from_24h)}&limit=1"
    )
    try:
        data = _http_get(url, {"Authorization": _langfuse_auth_header()})
        return data.get("meta", {}).get("totalItems", 0)
    except Exception as exc:
        print(f"[warn] Langfuse traces failed: {exc}", file=sys.stderr)
        return 0

# ---------------------------------------------------------------------------
# Error categorisation
# ---------------------------------------------------------------------------

_ERROR_RULES: List[Tuple[str, List[str]]] = [
    ("ClientError 404 Not Found (image/asset fetch)",  ["404", "Not Found", "ClientError"]),
    ("Cancelled by cancel scope",                       ["cancel scope", "CancelledError", "cancelled"]),
    ("AzureChatOpenAI error",                           ["AzureChatOpenAI"]),
    ("RunnableSequence error",                          ["RunnableSequence"]),
    # 400 + Invalid/Bad Request — handled specially below
    ("Failed to download image",                        ["download image", "Failed to download"]),
    ("429 RESOURCE_EXHAUSTED",                          ["429", "RESOURCE_EXHAUSTED"]),
    # 400 + text + image — handled specially below
    ("500 server error",                                ["500"]),
]

def _categorise_error(msg: str) -> str:
    if not msg:
        return "Other"
    m = msg.lower()

    # Priority 1 — 404
    if "404" in m or "not found" in m or "clienterror" in m:
        return "ClientError 404 Not Found (image/asset fetch)"
    # Priority 2 — cancel
    if "cancel scope" in m or "cancellederr" in m or "cancelled" in m:
        return "Cancelled by cancel scope"
    # Priority 3 — AzureChatOpenAI
    if "azurechatopenai" in m:
        return "AzureChatOpenAI error"
    # Priority 4 — RunnableSequence
    if "runnablesequence" in m:
        return "RunnableSequence error"
    # Priority 5 — 400 + Invalid/Bad Request
    if "400" in m and ("invalid" in m or "bad request" in m):
        return "400: Invalid request"
    # Priority 6 — Failed to download image
    if "download image" in m or "failed to download" in m:
        return "Failed to download image"
    # Priority 7 — 429 / RESOURCE_EXHAUSTED
    if "429" in m or "resource_exhausted" in m:
        return "429 RESOURCE_EXHAUSTED"
    # Priority 8 — 400 + text + image
    if "400" in m and "text" in m and "image" in m:
        return "400: missing text/image/audio"
    # Priority 9 — 500
    if "500" in m:
        return "500 server error"
    return "Other"


def fmt_errors(error_obs: List, total_errors: int) -> str:
    if total_errors == 0 and not error_obs:
        return "  No error observations in the last 24h."

    unique_traces = len({o.get("traceId") for o in error_obs if o.get("traceId")})
    n_sample = len(error_obs)

    cat_counts: Dict[str, int] = {}
    for obs in error_obs:
        msg = obs.get("statusMessage") or obs.get("name") or ""
        cat = _categorise_error(msg)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    sorted_cats = sorted(cat_counts.items(), key=lambda x: -x[1])
    total_sample = sum(c for _, c in sorted_cats) or 1

    cat_lines = []
    for cat, cnt in sorted_cats:
        pct = cnt / total_sample * 100
        cat_lines.append(f"  • {cat}: {cnt:,} (~{pct:.0f}%)")
    cat_block = "\n".join(cat_lines) if cat_lines else "  _(no categorised errors)_"

    # Dominant error note
    top_cat, top_cnt = sorted_cats[0] if sorted_cats else ("Other", 0)
    top_pct = top_cnt / total_sample * 100
    if "404" in top_cat and top_pct > 50:
        dominant = f":warning: Dominant error: {top_cat} — 404 Not Found on image/asset retrieval — worth an upstream check."
    elif "cancel" in top_cat.lower() and top_pct > 30:
        dominant = f":warning: Dominant error: {top_cat} — Client disconnects (499s) — check TTFT latency."
    else:
        dominant = f":warning: Dominant error: {top_cat}"

    return (
        f"Error observations: {total_errors:,} (sampled {n_sample:,} across {unique_traces:,} unique traces)\n\n"
        f"Error type breakdown (from sample, sorted by count):\n{cat_block}\n\n"
        f"{dominant}"
    )


def _stream_logs_get(row: dict, col: str, default: float = 0.0) -> float:
    """Case-insensitive column lookup (Metabase JSON keys may vary)."""
    want = col.lower()
    for k, v in row.items():
        if k is not None and str(k).lower() == want:
            if v is None:
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return default
    return default


def fmt_stream_logs_summary(
    rows: Optional[List],
    *,
    card_configured: bool,
    day_label: str,
) -> str:
    """Format single-row summary from vcp_stream_logs_digest_summary.sql."""
    if not card_configured:
        return (
            "  _(Not configured — save `sql/vcp_stream_logs_digest_summary.sql` as a Metabase "
            "question and set `METABASE_STREAM_LOGS_CARD_ID` in GitHub secrets.)_"
        )
    if rows is None:
        return "  _(unavailable — Metabase fetch failed.)_"
    if not rows:
        return "  _(no summary row — check the Metabase question.)_"
    r = rows[0]
    n_req = int(_stream_logs_get(r, "n_requests", 0))
    n_fail = int(_stream_logs_get(r, "n_failure", 0))
    n_ok = int(_stream_logs_get(r, "n_success", 0))
    fail_pct = _stream_logs_get(r, "failure_pct", 0.0)
    n_400 = int(_stream_logs_get(r, "n_http_400", 0))
    n_499 = int(_stream_logs_get(r, "n_http_499", 0))
    n_500 = int(_stream_logs_get(r, "n_http_500", 0))
    n_fail_200 = int(_stream_logs_get(r, "n_failure_http_200", 0))
    n_sff = int(_stream_logs_get(r, "n_stream_flow_failed", 0))
    n_she = int(_stream_logs_get(r, "n_success_with_handled_errors", 0))
    n_can = int(_stream_logs_get(r, "n_cancelled_error", 0))

    lines = [
        f"  • *Requests (yesterday {day_label}):* {n_req:,}",
        f"  • *Failures:* {n_fail:,} ({fail_pct:.2f}% of requests)  |  *Successes:* {n_ok:,}",
        f"  • *HTTP:* 400 → {n_400:,}  |  499 (disconnect) → {n_499:,}  |  500 → {n_500:,}",
        f"  • *Mid-stream failure signal:* FAILURE with HTTP 200 → {n_fail_200:,}  |  "
        f"`stream_flow_failed` in steps → {n_sff:,}",
        f"  • *SUCCESS with handled_errors (degraded but answered):* {n_she:,}  |  "
        f"*CancelledError-type:* {n_can:,}",
        "  _SUCCESS can still include handled_errors (infra recovered). "
        "This block is *calendar yesterday* (Trino); Langfuse errors above are *rolling 24h*._",
    ]
    return "\n".join(lines)


def fmt_scores(score_items: list, dv_total: int, total_traces: int) -> str:
    """dv_total = accurate downvote count from Langfuse meta.totalItems (value=0 filter)."""
    rate_str = "n/a"
    if total_traces > 0:
        rate = dv_total / total_traces * 100
        rate_str = f"{rate:.2f}"

    # Pull free-text comments from the sample (value==0 scores with a comment)
    downvote_sample = [s for s in score_items if s.get("value") == 0]
    comments = [
        s.get("comment") for s in downvote_sample
        if s.get("comment") and s.get("comment").strip()
    ][:10]

    comment_lines = "\n".join(f'  "{c}"' for c in comments) if comments else "  _(no free-text comments)_"

    return (
        f"Total downvotes (csat=0): {dv_total:,}  |  Total traces: {total_traces:,}  |  Rate: {rate_str}%\n\n"
        f"Sample free-text comments (verbatim):\n{comment_lines}"
    )


def load_eval_summary(summary_path: str) -> Optional[dict]:
    """Load JSON written by daily_eval.py (formatting_hotspot_chapters, axial_fail_pct, etc.)."""
    if not summary_path:
        return None
    try:
        with open(summary_path) as _sf:
            return json.load(_sf)
    except Exception:
        return None


def _row_chapter(row: dict) -> Optional[str]:
    for k in ("chapter", "Chapter", "standardchaptername", "standardChapterName"):
        v = row.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return None


def _row_pct(row: dict) -> float:
    for k in (
        "triple_followup_60s_pct",
        "rephrase_keyword_pct",
        "rate_pct",
        "pct",
        "value",
    ):
        v = row.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def fmt_behavior_proxy(rows: Optional[List]) -> str:
    if rows is None:
        return (
            "  _(not configured — add Metabase card + set "
            "`METABASE_BEHAVIOR_FOLLOWUP_CARD_ID` / `METABASE_BEHAVIOR_REPHRASE_CARD_ID`)_"
        )
    if not rows:
        return "  _(no rows)_"
    lines = []
    for row in rows[:12]:
        ch = _row_chapter(row)
        if not ch:
            continue
        pct = _row_pct(row)
        nq = row.get("n_queries")
        nq_s = f" _(n={nq})_" if nq is not None else ""
        lines.append(f"  • *{ch}*: {pct:.2f}%{nq_s}")
    if not lines:
        return "  _(no chapter column in result — check SQL aliases)_"
    return "\n".join(lines)


def fmt_confirmed_regressions(
    follow_rows: Optional[List],
    rephrase_rows: Optional[List],
    eval_summary: Optional[dict],
    rephrase_threshold: float = 3.0,
    follow_threshold: float = 5.0,
) -> str:
    """C12 — chapters that are both formatting hotspots (judge) and behavioral spikes."""
    fmt_hot = set(eval_summary.get("formatting_hotspot_chapters") or []) if eval_summary else set()
    if not fmt_hot:
        return (
            "  _(No `formatting_hotspot_chapters` in eval snapshot — run daily eval on this host first, "
            "or snapshot path differs from `EVAL_SUMMARY_PATH`.)_"
        )
    behavioral: set[str] = set()
    for row in rephrase_rows or []:
        ch = _row_chapter(row)
        if ch and _row_pct(row) >= rephrase_threshold:
            behavioral.add(ch)
    for row in follow_rows or []:
        ch = _row_chapter(row)
        if ch and _row_pct(row) >= follow_threshold:
            behavioral.add(ch)
    both = sorted(fmt_hot & behavioral)
    if not both:
        return (
            "  _No overlap today between judge *formatting* hotspots and behavioral "
            "elevated chapters (thresholds: rephrase ≥ {:.1f}%, follow-up burst ≥ {:.1f}%)._"
        ).format(rephrase_threshold, follow_threshold)
    lines = "\n".join(
        f"  • *{c}* — formatting FAIL hotspot ∩ behavioral spike"
        for c in both
    )
    return f":rotating_light: *Confirmed regression signal* (judge × behavior)\n{lines}"


def _digest_footer_links(stream_logs_card_id: str) -> str:
    parts = [
        f":link: <{METABASE_URL}/question/24973|Academic Reasons>",
        f"<{METABASE_URL}/question/24974|Non-Academic Reasons>",
        f"<{METABASE_URL}/question/23036|Downvote Dump>",
        f"<{LANGFUSE_HOST}|Langfuse>",
    ]
    sid = (stream_logs_card_id or "").strip()
    if sid.isdigit():
        parts.append(f"<{METABASE_URL}/question/{sid}|Stream logs (VCP)>")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_blocks(
    academic_rows,
    nonacademic_rows,
    dump_rows,
    score_items,
    total_scores,
    error_obs,
    total_errors,
    total_traces,
    behavior_follow_rows: Optional[List] = None,
    behavior_rephrase_rows: Optional[List] = None,
    eval_summary: Optional[dict] = None,
    stream_logs_rows: Optional[List] = None,
    stream_logs_card_id: str = "",
) -> list:
    academic_block    = fmt_academic(academic_rows)
    nonacademic_block = fmt_nonacademic(nonacademic_rows)
    dump_block        = fmt_downvote_dump(dump_rows)
    scores_block      = fmt_scores(score_items, total_scores, total_traces)
    errors_block      = fmt_errors(error_obs, total_errors)
    sl_cfg = bool(stream_logs_card_id and stream_logs_card_id.isdigit())
    stream_logs_block = fmt_stream_logs_summary(
        stream_logs_rows,
        card_configured=sl_cfg,
        day_label=yesterday,
    )

    follow_txt = fmt_behavior_proxy(behavior_follow_rows)
    rephrase_txt = fmt_behavior_proxy(behavior_rephrase_rows)
    reg_txt = fmt_confirmed_regressions(
        behavior_follow_rows, behavior_rephrase_rows, eval_summary
    )

    def section(text: str) -> dict:
        return {"type": "section", "text": {"type": "mrkdwn", "text": text}}

    divider: dict = {"type": "divider"}

    # Order: system health + student voice first (stakeholder onboarding); then context blocks.
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"\U0001f4ca Ask AI Daily Digest — {today_str}", "emoji": True},
        },
        divider,
        section(f":rotating_light: *Langfuse Errors (last 24h)*\n{errors_block}"),
        divider,
        section(
            f":gear: *Video co-pilot API health (stream_logs, yesterday)*\n{stream_logs_block}"
        ),
        divider,
        section(f":speech_balloon: *User Comments on Downvotes (Langfuse, last 24h)*\n{scores_block}"),
        divider,
        section(f":thumbsdown: *Downvote Reasons — Academic (rolling 21d)*\n{academic_block}"),
        divider,
        section(f":thumbsdown: *Downvote Reasons — Non-Academic (rolling 21d)*\n{nonacademic_block}"),
        divider,
        section(f":bar_chart: *Yesterday's Downvoted Queries Snapshot ({yesterday})*\n{dump_block}"),
        divider,
        section(
            f":brain: *Silent-failure proxies (yesterday, academic VCP)*\n"
            f"*Multi-turn burst (≥3 queries in 60s / user):*\n{follow_txt}\n\n"
            f"*Rephrase / shorter / language-switch keyword rate:*\n{rephrase_txt}\n\n"
            f"{reg_txt}"
        ),
        divider,
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": _digest_footer_links(stream_logs_card_id),
                }
            ],
        },
    ]


def post_to_slack(blocks: list, fallback_text: str) -> None:
    if not SLACK_WEBHOOK:
        print("[warn] SLACK_WEBHOOK_URL not set — skipping Slack post.", file=sys.stderr)
        return
    payload = json.dumps({"text": fallback_text, "blocks": blocks}).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        status = resp.getcode()
        if status != 200:
            print(f"[warn] Slack returned HTTP {status}", file=sys.stderr)


def main() -> None:
    print(f"[info] Fetching data for digest ({today_str}, yesterday={yesterday}) …", file=sys.stderr)

    # Metabase
    academic_rows    = fetch_metabase_card(24973)
    nonacademic_rows = fetch_metabase_card(24974)
    dump_rows        = fetch_metabase_card(23036)

    # Langfuse
    score_items, total_scores = fetch_langfuse_scores()
    error_obs, total_errors   = fetch_langfuse_errors()
    total_traces               = fetch_langfuse_traces_total()

    # Optional C11 — production SQL must be saved as Metabase questions first
    follow_rows = (
        fetch_metabase_card(int(BEHAVIOR_FOLLOWUP_CARD_ID))
        if BEHAVIOR_FOLLOWUP_CARD_ID.isdigit() else None
    )
    rephrase_rows = (
        fetch_metabase_card(int(BEHAVIOR_REPHRASE_CARD_ID))
        if BEHAVIOR_REPHRASE_CARD_ID.isdigit() else None
    )
    eval_summary = load_eval_summary(EVAL_SUMMARY_PATH)

    stream_logs_rows = (
        fetch_metabase_card(int(STREAM_LOGS_CARD_ID))
        if STREAM_LOGS_CARD_ID.isdigit()
        else None
    )

    blocks = build_blocks(
        academic_rows,
        nonacademic_rows,
        dump_rows,
        score_items,
        total_scores,
        error_obs,
        total_errors,
        total_traces,
        behavior_follow_rows=follow_rows,
        behavior_rephrase_rows=rephrase_rows,
        eval_summary=eval_summary,
        stream_logs_rows=stream_logs_rows,
        stream_logs_card_id=STREAM_LOGS_CARD_ID,
    )
    fallback_text = f"Ask AI Daily Digest — {today_str}"
    print(f"{fallback_text}\n[{len(blocks)} blocks]")

    if DRY_RUN:
        import pprint
        pprint.pprint(blocks)
        print("\n[info] --dry-run: Slack post skipped.", file=sys.stderr)
    else:
        post_to_slack(blocks, fallback_text)
        print("[info] Message posted to Slack.", file=sys.stderr)


if __name__ == "__main__":
    main()
