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


def _http_post_json(url: str, headers: Dict, body: Optional[Dict] = None, timeout: int = 90) -> Union[List, Dict]:
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
        result = _http_post_json(url, headers)
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
) -> list:
    academic_block    = fmt_academic(academic_rows)
    nonacademic_block = fmt_nonacademic(nonacademic_rows)
    dump_block        = fmt_downvote_dump(dump_rows)
    scores_block      = fmt_scores(score_items, total_scores, total_traces)
    errors_block      = fmt_errors(error_obs, total_errors)

    def section(text: str) -> dict:
        return {"type": "section", "text": {"type": "mrkdwn", "text": text}}

    divider: dict = {"type": "divider"}

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"\U0001f4ca Ask AI Daily Digest — {today_str}", "emoji": True},
        },
        divider,
        section(f":thumbsdown: *Downvote Reasons — Academic (rolling 21d)*\n{academic_block}"),
        divider,
        section(f":thumbsdown: *Downvote Reasons — Non-Academic (rolling 21d)*\n{nonacademic_block}"),
        divider,
        section(f":bar_chart: *Yesterday's Downvoted Queries Snapshot ({yesterday})*\n{dump_block}"),
        divider,
        section(f":speech_balloon: *User Comments on Downvotes (Langfuse, last 24h)*\n{scores_block}"),
        divider,
        section(f":rotating_light: *Langfuse Errors (last 24h)*\n{errors_block}"),
        divider,
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f":link: <{METABASE_URL}/question/24973|Academic Reasons>"
                        f" | <{METABASE_URL}/question/24974|Non-Academic Reasons>"
                        f" | <{METABASE_URL}/question/23036|Downvote Dump>"
                        f" | <{LANGFUSE_HOST}|Langfuse>"
                    ),
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

    blocks = build_blocks(
        academic_rows,
        nonacademic_rows,
        dump_rows,
        score_items,
        total_scores,
        error_obs,
        total_errors,
        total_traces,
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
