#!/usr/bin/env python3
"""
Ask AI Daily Digest — replaces n8n Cloud workflow.
Fetches Metabase + Langfuse data and posts a formatted summary to Slack.

Optional:
  DIGEST_STRICT_STREAM_LOGS=1 — fail the job if METABASE_STREAM_LOGS_CARD_ID is set
      but the Metabase card still fails after retries.

Exit code: 0 on success; 1 if Slack post fails, all three core Metabase cards fail,
  or (when DIGEST_STRICT_STREAM_LOGS) stream_logs Metabase fails.

Required env vars:
  METABASE_URL         e.g. https://metabase-prod.penpencil.co
  METABASE_API_KEY
  LANGFUSE_PUBLIC_KEY
  LANGFUSE_SECRET_KEY
  LANGFUSE_HOST        (default: https://cloud.langfuse.com)
  SLACK_WEBHOOK_URL

Optional tuning:
  METABASE_CARD_RETRIES (default 3) — all Metabase card queries
  LANGFUSE_OBSERVATION_PAGE_SIZE (default 500)
  LANGFUSE_ERROR_MAX_ITEMS / LANGFUSE_SCORE_MAX_ITEMS (default 500000)
  LANGFUSE_ERROR_MAX_PAGES / LANGFUSE_SCORE_MAX_PAGES (default 60 pages; 0 = unlimited)
  DIGEST_MIN_SCORE_ROWS_FOR_RATE (default 500) — sparse Langfuse scores: hide misleading rate vs all traces
  DIGEST_FAIL_ON_LANGFUSE_ERROR — set to 0/false/no to allow posting when Langfuse fetches fail.
      In GitHub Actions the default is strict: bad Langfuse config fails the job before Slack.
  LANGFUSE_HOST — if unset or empty, defaults to https://cloud.langfuse.com (empty secret must not override).

Optional card id env vars (digits only):
  METABASE_STREAM_LOGS_CARD_ID — Metabase question for
      sql/vcp_stream_logs_digest_summary.sql (E2E API health vs Langfuse 24h block).
      Prod PW: question 33285
      https://metabase-prod.penpencil.co/question/33285-metabase-stream-logs-card
  METABASE_BEHAVIOR_FOLLOWUP_CARD_ID / METABASE_BEHAVIOR_REPHRASE_CARD_ID
      Prod PW: 33282 follow-up, 33283 rephrase
      https://metabase-prod.penpencil.co/question/33282-metabase-behavior-followup-card
      https://metabase-prod.penpencil.co/question/33283-metabase-behavior-rephrase-card

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
import time
import base64
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union


# ---------------------------------------------------------------------------
# Idempotency guard — prevents duplicate Slack posts on the same UTC day.
# The cron job is `0 3 * * *` UTC on a self-hosted runner; if the workflow is
# retried, re-triggered, or accidentally invoked twice for the same UTC date,
# the marker file makes the second invocation a no-op. Marker is written ONLY
# after a successful Slack post (HTTP 200 "ok"), so a failed post can be
# retried. Set FORCE_REPOST=1 to bypass (debugging only — do NOT set in cron).
# ---------------------------------------------------------------------------

def _idempotency_marker_path(prefix: str = "digest-posted") -> Path:
    base = (
        os.environ.get("DIGEST_STATE_DIR", "").strip()
        or str(Path.home() / ".ask-ai-daily-automation" / "state")
    )
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Path(base) / f"{prefix}-{today_utc}.marker"


def _already_posted_today(prefix: str = "digest-posted") -> bool:
    if os.environ.get("FORCE_REPOST", "").strip() == "1":
        return False
    return _idempotency_marker_path(prefix).exists()


def _write_posted_marker(prefix: str = "digest-posted") -> None:
    marker = _idempotency_marker_path(prefix)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            datetime.now(timezone.utc).isoformat() + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        # Non-fatal: the Slack post already succeeded. Just log so the next
        # invocation is not silently blocked by a stale/missing marker.
        print(
            f"[warn] Could not write idempotency marker {marker}: {repr(exc)}",
            file=sys.stderr,
        )

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

METABASE_URL     = os.environ.get("METABASE_URL", "https://metabase-prod.penpencil.co")
METABASE_API_KEY = os.environ.get("METABASE_API_KEY", "")
# GitHub Actions often sets `LANGFUSE_HOST: ${{ secrets.LANGFUSE_HOST }}` — if the secret is
# unset, the env value is empty and must not override the Langfuse cloud default (empty host
# breaks URLs and yields 401-shaped failures).
def _env_strip_or_default(key: str, default: str) -> str:
    v = (os.environ.get(key) or "").strip()
    return v if v else default


LANGFUSE_HOST = _env_strip_or_default("LANGFUSE_HOST", "https://cloud.langfuse.com")
LANGFUSE_PK = (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip()
LANGFUSE_SK = (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip()
SLACK_WEBHOOK = (os.environ.get("SLACK_WEBHOOK_URL") or "").strip()

DRY_RUN = "--dry-run" in sys.argv

# C11/C12 — optional Metabase cards + yesterday's eval snapshot (same path as daily_eval.py)
EVAL_SUMMARY_PATH = os.environ.get("EVAL_SUMMARY_PATH", "/tmp/daily_eval_yesterday_summary.json")
BEHAVIOR_FOLLOWUP_CARD_ID = os.environ.get("METABASE_BEHAVIOR_FOLLOWUP_CARD_ID", "").strip()
# Accept typo alias from GitHub secret name (missing _ID suffix)
BEHAVIOR_REPHRASE_CARD_ID = os.environ.get("METABASE_BEHAVIOR_REPHRASE_CARD_ID", "").strip()
STREAM_LOGS_CARD_ID = os.environ.get("METABASE_STREAM_LOGS_CARD_ID", "").strip()


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Metabase: retry all saved-question fetches (stream_logs uses the same count).
# Bumped 3 → 6 (clamped 1..10) so a string of upstream timeouts doesn't kill the
# digest. Combined with the 10/20/40/80/120/120s backoff below, six attempts give
# ~5 min of cumulative wait — well within the 240-min job cap and worth the
# tradeoff vs a silent 10:00 IST miss.
METABASE_CARD_RETRIES = max(1, min(10, _env_int("METABASE_CARD_RETRIES", 6)))

# Per-request socket timeout for Metabase digest card POSTs (seconds).
# Default 1800 (30 min) — Metabase prod questions occasionally take 10+ min when
# central.silver_stream_logs is slow; previous value of None (wait forever) meant
# a hung Metabase node would block the digest indefinitely. Clamped 60..3600.
METABASE_DIGEST_TIMEOUT_SEC = max(60, min(3600, _env_int("METABASE_DIGEST_TIMEOUT_SEC", 1800)))

# Langfuse public API — page until empty or caps. LANGFUSE_*_MAX_PAGES=0 means no page cap.
# Cloud Langfuse caps `limit` at 100 on /api/public/observations and /api/public/scores
# (Zod validator: "Too big: expected number to be <=100"); sending limit>100 → HTTP 400.
LANGFUSE_PAGE_SIZE = max(1, min(100, _env_int("LANGFUSE_OBSERVATION_PAGE_SIZE", 100)))
LANGFUSE_ERROR_MAX_ITEMS = max(1000, _env_int("LANGFUSE_ERROR_MAX_ITEMS", 500_000))
LANGFUSE_SCORE_MAX_ITEMS = max(1000, _env_int("LANGFUSE_SCORE_MAX_ITEMS", 500_000))
LANGFUSE_ERROR_MAX_PAGES = _env_int("LANGFUSE_ERROR_MAX_PAGES", 60)
LANGFUSE_SCORE_MAX_PAGES = _env_int("LANGFUSE_SCORE_MAX_PAGES", 60)

# Downvotes: hide misleading "rate vs all traces" when Langfuse score rows are sparse.
DIGEST_MIN_SCORE_ROWS_FOR_RATE = max(0, _env_int("DIGEST_MIN_SCORE_ROWS_FOR_RATE", 500))

# Per-request socket timeout for Langfuse public REST GETs (seconds).
# Bumped from a hard-coded 90s to env-controlled (default 300, clamp 30..900) —
# Langfuse Cloud /api/public/observations slows to 60–120s past page ~50 under
# heavy 24h pagination, and the hardcoded 90s sometimes tripped before the
# retry loop got a chance to back off.
LANGFUSE_GET_TIMEOUT_SEC = max(30, min(900, _env_int("LANGFUSE_GET_TIMEOUT_SEC", 300)))

# If true, workflow fails when METABASE_STREAM_LOGS_CARD_ID is set but Metabase fetch fails.
DIGEST_STRICT_STREAM_LOGS = os.environ.get("DIGEST_STRICT_STREAM_LOGS", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

SLACK_SECTION_MAX = 2800


def _warn_metabase_card_env_var(name: str, value: str) -> None:
    """Log if a secret was passed but is not a plain integer card id (no secret contents)."""
    if not value:
        return
    stripped = value.strip()
    if stripped.isdigit():
        return
    print(
        f"[warn] {name}: env is non-empty but not digits-only after strip "
        f"(raw_len={len(value)}, stripped_len={len(stripped)}) — "
        "re-save the GitHub secret as plain digits (no quotes/BOM/newlines).",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

now_utc   = datetime.now(timezone.utc)
yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
today_str = now_utc.strftime("%Y-%m-%d")
from_24h  = (now_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest_fail_on_langfuse_error() -> bool:
    """In CI, refuse to post a digest with broken Langfuse blocks (set DIGEST_FAIL_ON_LANGFUSE_ERROR=0 to override)."""
    if DRY_RUN:
        return False
    raw = (os.environ.get("DIGEST_FAIL_ON_LANGFUSE_ERROR") or "").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    if raw in ("1", "true", "yes"):
        return True
    return os.environ.get("GITHUB_ACTIONS") == "true"


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def _langfuse_auth_header() -> str:
    token = base64.b64encode(f"{LANGFUSE_PK}:{LANGFUSE_SK}".encode()).decode()
    return f"Basic {token}"


def _http_get_langfuse(url: str, headers: dict, timeout: Optional[int] = None) -> dict:
    """GET with backoff on rate-limit / transient server errors.

    Defaults to env-controlled LANGFUSE_GET_TIMEOUT_SEC (default 300s).
    8 total attempts with bounded exponential backoff (10/20/40/80/120/120/120/120s).
    """
    if timeout is None:
        timeout = LANGFUSE_GET_TIMEOUT_SEC
    last_err: Optional[BaseException] = None
    for attempt in range(8):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            last_err = exc
            # 408/504 added: Langfuse Cloud returns gateway timeouts past ~page 50
            # under heavy 24h pagination; without retry, one bad page kills the fetch.
            # Cumulative max sleep over 7 retries: 10+20+40+80+120+120+120 = 510s ~ 8.5 min.
            if exc.code in (408, 429, 502, 503, 504) and attempt < 7:
                time.sleep(min(120.0, 10.0 * 2.0**attempt))
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("Langfuse GET exhausted retries")  # pragma: no cover


def _preflight_langfuse_or_exit() -> None:
    """Validate credentials + reachability before Metabase work so misconfig fails fast."""
    if not _digest_fail_on_langfuse_error():
        return
    if not LANGFUSE_PK or not LANGFUSE_SK:
        print(
            "[error] Langfuse API keys missing — set repository secrets LANGFUSE_PUBLIC_KEY and "
            "LANGFUSE_SECRET_KEY. If LANGFUSE_HOST is unset, it defaults to https://cloud.langfuse.com.",
            file=sys.stderr,
        )
        sys.exit(1)
    url = (
        f"{LANGFUSE_HOST}/api/public/traces"
        f"?fromTimestamp={urllib.parse.quote(from_24h)}&limit=1"
    )
    try:
        # Preflight: bumped 45 → 120s. Single trace lookup should be fast,
        # but a slow first connect to Langfuse Cloud during a partial outage
        # used to fail this gate before any retry could fire.
        _http_get_langfuse(url, {"Authorization": _langfuse_auth_header()}, timeout=120)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")[:500]
        except Exception:
            pass
        print(
            f"[error] Langfuse preflight HTTP {exc.code} against {LANGFUSE_HOST} — "
            "verify keys match the project and LANGFUSE_HOST is the project base URL "
            f"(not empty). Response: {body!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"[error] Langfuse preflight failed: {exc!r}", file=sys.stderr)
        sys.exit(1)


def _assert_langfuse_or_exit(ok: bool, where: str, detail: str = "") -> None:
    """Fail-fast post-fetch gate.

    Under the strict gate (`DIGEST_FAIL_ON_LANGFUSE_ERROR=1`, default in CI),
    abort with exit code 1 the moment any individual Langfuse fetch fails, so
    we never assemble a degraded Slack post with `_fetch failed_` filler text.
    Emits one structured log line for one-shot diagnosis.
    """
    if not _digest_fail_on_langfuse_error():
        return
    if ok:
        return
    extra = f" {detail}" if detail else ""
    print(
        f"[error] langfuse_fetch_failed where={where}{extra} "
        "(set DIGEST_FAIL_ON_LANGFUSE_ERROR=0 to allow degraded post)",
        file=sys.stderr,
    )
    sys.exit(1)


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


def _slack_escape(text: str) -> str:
    """Escape &, <, > for text embedded in Slack mrkdwn (user/DB-sourced)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate_section(text: str, max_len: int = SLACK_SECTION_MAX) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 24].rstrip() + "\n_… (truncated)_"


# ---------------------------------------------------------------------------
# Metabase fetchers
# ---------------------------------------------------------------------------

def fetch_metabase_card_detailed(
    card_id: int, *, retries: int = 1
) -> Tuple[Optional[List], Optional[str]]:
    """Returns (rows, error_repr). error_repr set when all attempts fail."""
    url = f"{METABASE_URL}/api/card/{card_id}/query/json"
    headers = {
        "X-Api-Key": METABASE_API_KEY,
        "Content-Type": "application/json",
    }
    last_exc: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            # METABASE_DIGEST_TIMEOUT_SEC (default 1800s) caps any single POST so a
            # hung Metabase node can't deadlock the job; relies on the retry loop
            # to recover when an attempt hits the cap.
            result = _http_post_json(url, headers, timeout=METABASE_DIGEST_TIMEOUT_SEC)
            if isinstance(result, list):
                return result, None
            return None, "Metabase returned non-list JSON"
        except Exception as exc:
            last_exc = exc
            print(
                f"[warn] Metabase card {card_id} attempt {attempt + 1}/{retries}: {repr(exc)}",
                file=sys.stderr,
            )
            if attempt + 1 < retries:
                # Backoff schedule: 10, 20, 40, 80, 120, 120s. Capped at 120 so
                # six attempts cumulate to ~5 min of sleep, leaving ample budget
                # within the 240-min job cap for the actual queries.
                time.sleep(min(120, 10 * 2**attempt))
    return None, repr(last_exc) if last_exc else "unknown error"


def fetch_metabase_card(card_id: int, *, retries: int = 1) -> Optional[List]:
    rows, _ = fetch_metabase_card_detailed(card_id, retries=retries)
    return rows


def fmt_academic(rows: Optional[List]) -> str:
    if rows is None:
        return "  _(unavailable)_"
    lines = []
    for row in rows:
        text = _slack_escape(str(row.get("feedback_text", "Unknown")))
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
        f"  {_slack_escape(str(cat))}: {cnt:,}"
        for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1])
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
                rk = _slack_escape(reason)
                reason_counts[rk] = reason_counts.get(rk, 0) + 1

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

def _dedupe_by_id(rows: List[dict]) -> List[dict]:
    """Avoid duplicate rows if API ignores page parameter."""
    out: List[dict] = []
    seen = set()
    for row in rows:
        rid = row.get("id")
        key = rid if rid is not None else None
        if key is None:
            out.append(row)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def fetch_langfuse_scores() -> Tuple[List, int, bool, bool]:
    """Paginate GET /api/public/scores. Returns (rows, dv_count_in_rows, ok, hit_item_cap)."""
    auth = {"Authorization": _langfuse_auth_header()}
    all_items: List = []
    ok = False
    hit_cap = False
    page = 0
    try:
        while True:
            page += 1
            if LANGFUSE_SCORE_MAX_PAGES > 0 and page > LANGFUSE_SCORE_MAX_PAGES:
                print(
                    f"[info] Langfuse scores: stopped at LANGFUSE_SCORE_MAX_PAGES={LANGFUSE_SCORE_MAX_PAGES}",
                    file=sys.stderr,
                )
                break
            url = (
                f"{LANGFUSE_HOST}/api/public/scores"
                f"?fromTimestamp={urllib.parse.quote(from_24h)}"
                f"&limit={LANGFUSE_PAGE_SIZE}&page={page}"
            )
            data = _http_get_langfuse(url, auth)
            batch = data.get("data", [])
            if not batch:
                break
            all_items.extend(batch)
            if len(all_items) >= LANGFUSE_SCORE_MAX_ITEMS:
                all_items = all_items[:LANGFUSE_SCORE_MAX_ITEMS]
                hit_cap = True
                print(
                    f"[warn] Langfuse scores: hit LANGFUSE_SCORE_MAX_ITEMS={LANGFUSE_SCORE_MAX_ITEMS}",
                    file=sys.stderr,
                )
                break
            if len(batch) < LANGFUSE_PAGE_SIZE:
                break
        ok = True
    except Exception as exc:
        print(f"[warn] Langfuse scores failed: {repr(exc)}", file=sys.stderr)
    all_items = _dedupe_by_id(all_items)
    dv_count = sum(1 for s in all_items if s.get("value") == 0)
    return all_items, dv_count, ok, hit_cap


def fetch_langfuse_errors() -> Tuple[List, int, bool, bool]:
    """Paginate error observations. Returns (observations, reported totalItems, ok, hit_item_cap)."""
    auth = {"Authorization": _langfuse_auth_header()}
    all_obs: List = []
    total_items = 0
    ok = False
    hit_cap = False
    page = 0
    try:
        while True:
            page += 1
            if LANGFUSE_ERROR_MAX_PAGES > 0 and page > LANGFUSE_ERROR_MAX_PAGES:
                print(
                    f"[info] Langfuse errors: stopped at LANGFUSE_ERROR_MAX_PAGES={LANGFUSE_ERROR_MAX_PAGES}",
                    file=sys.stderr,
                )
                break
            url = (
                f"{LANGFUSE_HOST}/api/public/observations"
                f"?fromTimestamp={urllib.parse.quote(from_24h)}"
                f"&limit={LANGFUSE_PAGE_SIZE}&page={page}&level=ERROR"
            )
            data = _http_get_langfuse(url, auth)
            if page == 1:
                total_items = int(data.get("meta", {}).get("totalItems") or 0)
            batch = data.get("data", [])
            if not batch:
                break
            all_obs.extend(batch)
            if len(all_obs) >= LANGFUSE_ERROR_MAX_ITEMS:
                all_obs = all_obs[:LANGFUSE_ERROR_MAX_ITEMS]
                hit_cap = True
                print(
                    f"[warn] Langfuse errors: hit LANGFUSE_ERROR_MAX_ITEMS={LANGFUSE_ERROR_MAX_ITEMS}",
                    file=sys.stderr,
                )
                break
            if len(batch) < LANGFUSE_PAGE_SIZE:
                break
        ok = True
    except Exception as exc:
        print(f"[warn] Langfuse errors failed: {repr(exc)}", file=sys.stderr)
    all_obs = _dedupe_by_id(all_obs)
    if total_items == 0 and all_obs:
        total_items = len(all_obs)
    return all_obs, total_items, ok, hit_cap


def fetch_langfuse_traces_total() -> Tuple[int, bool]:
    url = (
        f"{LANGFUSE_HOST}/api/public/traces"
        f"?fromTimestamp={urllib.parse.quote(from_24h)}&limit=1"
    )
    try:
        data = _http_get_langfuse(url, {"Authorization": _langfuse_auth_header()})
        n = int(data.get("meta", {}).get("totalItems") or 0)
        return n, True
    except Exception as exc:
        print(f"[warn] Langfuse traces failed: {repr(exc)}", file=sys.stderr)
        return 0, False

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


def fmt_errors(
    error_obs: List,
    total_errors: int,
    *,
    hit_item_cap: bool = False,
    errors_ok: bool = True,
) -> str:
    if not errors_ok:
        return (
            "  _Langfuse *error observations* fetch failed — this is not the same as zero errors. "
            "Check Actions logs for `[warn] Langfuse errors failed` (401/403/host/project keys)._"
        )
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

    cap_note = ""
    if hit_item_cap:
        cap_note = (
            f"\n\n_Stopped at {n_sample:,} observations (LANGFUSE_ERROR_MAX_ITEMS cap); "
            "percentages above are for this set._"
        )

    return (
        f"Error observations (reported total in project): {total_errors:,}\n"
        f"Breakdown uses {n_sample:,} observations retrieved from the API (paginated); "
        f"{unique_traces:,} unique trace ids in that set.\n\n"
        f"Error type breakdown (from retrieved set, sorted by count):\n{cat_block}\n\n"
        f"{dominant}"
        f"{cap_note}"
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
    metabase_error_hint: Optional[str] = None,
) -> str:
    """Format single-row summary from vcp_stream_logs_digest_summary.sql."""
    if not card_configured:
        return (
            "  _(Not configured — save `sql/vcp_stream_logs_digest_summary.sql` as a Metabase "
            "question and set `METABASE_STREAM_LOGS_CARD_ID` in GitHub secrets.)_"
        )
    if rows is None:
        hint = ""
        if metabase_error_hint:
            safe = _slack_escape(metabase_error_hint[:200])
            hint = f"\n  _Last error (truncated): {safe}_"
        return "  _(unavailable — Metabase fetch failed after retries. Check Actions logs.)_" + hint
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


def fmt_scores(
    score_items: list,
    dv_in_sample: int,
    total_traces: int,
    *,
    hit_score_cap: bool = False,
    scores_ok: bool = True,
) -> str:
    """dv_in_sample = count of csat=0 within fetched score rows."""
    if not scores_ok:
        return (
            "  _Langfuse *scores* fetch failed — empty or sparse-looking results may be an API error, "
            "not “no downvotes.” Check Actions logs for `[warn] Langfuse scores failed`._"
        )
    n_scores = len(score_items)
    downvote_line = (
        f"Downvotes (csat=0) in Langfuse score fetch: *{dv_in_sample:,}* "
        f"(from *{n_scores:,}* score rows retrieved, last 24h)"
    )

    if n_scores >= DIGEST_MIN_SCORE_ROWS_FOR_RATE and total_traces > 0:
        rate = dv_in_sample / total_traces * 100
        rate_block = (
            f"\nTotal traces (last 24h, Langfuse): *{total_traces:,}*  |  "
            f"Downvotes per trace (using retrieved scores only): *{rate:.2f}%*\n\n"
        )
    elif total_traces > 0:
        rate_block = (
            f"\nTotal traces (last 24h, Langfuse): *{total_traces:,}*\n"
            f"_Rate vs all traces not shown — fewer than *{DIGEST_MIN_SCORE_ROWS_FOR_RATE}* score rows "
            f"in this API pull (*{n_scores:,}* retrieved). CSAT scores are sparse; "
            "see Metabase downvote sections for volume._\n\n"
        )
    else:
        rate_block = "\n_Total traces (last 24h) unavailable from Langfuse._\n\n"

    downvote_sample = [s for s in score_items if s.get("value") == 0]
    comments = [
        s.get("comment")
        for s in downvote_sample
        if s.get("comment") and str(s.get("comment")).strip()
    ][:10]

    if comments:
        comment_lines = "\n".join(
            f'  "{_slack_escape(str(c))}"' for c in comments
        )
    else:
        comment_lines = "  _(no free-text comments)_"

    cap_note = ""
    if hit_score_cap:
        cap_note = (
            f"\n\n_Stopped at {n_scores:,} score rows (LANGFUSE_SCORE_MAX_ITEMS cap)._"
        )

    if (
        dv_in_sample == 0
        and not comments
        and n_scores < DIGEST_MIN_SCORE_ROWS_FOR_RATE
    ):
        return (
            f"{downvote_line}{rate_block}"
            f"_No csat=0 scores with comments in this pull. Langfuse score coverage is thin; "
            f"use Metabase downvote blocks for volume._"
            f"{cap_note}"
        )

    return (
        f"{downvote_line}{rate_block}"
        f"Sample free-text comments (verbatim):\n{comment_lines}"
        f"{cap_note}"
    )


def load_eval_summary(summary_path: str) -> Optional[dict]:
    """Load JSON written by daily_eval.py (formatting_hotspot_chapters, axial_fail_pct, etc.)."""
    if not summary_path:
        return None
    try:
        with open(summary_path) as _sf:
            data = json.load(_sf)
        if isinstance(data, dict):
            data.setdefault("formatting_hotspot_chapters", [])
            if "n_sampled" not in data and "n_metabase_rows" in data:
                data["n_sampled"] = data["n_metabase_rows"]
        return data
    except Exception:
        return None


FEEDBACK_CLASSIFICATIONS_PATH = "/tmp/daily_feedback_classifications.json"


def load_classifier_snapshot(path: str = FEEDBACK_CLASSIFICATIONS_PATH) -> Optional[dict]:
    """Load the free-text classifier snapshot written by daily_feedback_classifier.py.

    Returns None on any error (missing file, parse failure, wrong shape). The digest
    treats None as "skip the section silently" — failures here must never block posting.
    """
    try:
        with open(path) as _sf:
            data = json.load(_sf)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def fmt_freetext_classification(snapshot: Optional[dict]) -> Optional[dict]:
    """Render the free-text classifier Slack section block, or None to omit.

    Omits silently when snapshot is missing or stopped_reason == "no_metabase_card"
    (i.e. the feature is intentionally disabled).
    """
    if not snapshot:
        return None
    if snapshot.get("stopped_reason") == "no_metabase_card":
        return None

    n_classified = snapshot.get("n_classified") or 0
    counts = snapshot.get("category_counts") or {}
    other_samples = snapshot.get("other_samples") or []

    if not isinstance(counts, dict) or not isinstance(other_samples, list):
        return None

    header = f"*Free-text feedback breakdown (yesterday)*  —  n={int(n_classified):,}"

    sorted_counts = sorted(
        ((str(k), int(v)) for k, v in counts.items() if isinstance(v, (int, float)) and v > 0),
        key=lambda x: (-x[1], x[0]),
    )
    if sorted_counts:
        count_lines = "\n".join(
            f"  • {_slack_escape(cat)}: {cnt:,}" for cat, cnt in sorted_counts
        )
    else:
        count_lines = "  _(no classified rows)_"

    sample_lines = []
    for s in other_samples[:3]:
        if not isinstance(s, dict):
            continue
        subj = str(s.get("subject") or "").strip()
        ch = str(s.get("chapter") or "").strip()
        ft = str(s.get("free_text") or "").strip()
        if not ft:
            continue
        ctx_bits = [b for b in (subj, ch) if b]
        ctx = f" _({_slack_escape(' / '.join(ctx_bits))})_" if ctx_bits else ""
        sample_lines.append(f"  • \"{_slack_escape(ft)}\"{ctx}")

    body = f"{header}\n{count_lines}"
    if sample_lines:
        body += "\n*Sample \"Other\" feedback:*\n" + "\n".join(sample_lines)

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": _truncate_section(body)},
    }


def _eval_sample_counts(eval_summary: dict) -> Tuple[Optional[int], Optional[int]]:
    """M = Metabase pull size, N = judged count (`n_sampled` alias if present)."""

    def _coerce(v: object) -> Optional[int]:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return None

    m = _coerce(eval_summary.get("n_sampled"))
    if m is None:
        m = _coerce(eval_summary.get("n_metabase_rows"))
    n = _coerce(eval_summary.get("n_judged"))
    return m, n


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


def fmt_behavior_proxy(
    rows: Optional[List], *, card_configured: bool, setting_name: str
) -> str:
    if not card_configured:
        return (
            f"  _(not configured — set Actions secret `{setting_name}` (digits only), "
            "and pass it under `env:` on the digest workflow step. "
            "Settings → Secrets does not inject vars by itself.)_"
        )
    if rows is None:
        return (
            "  _(Metabase fetch failed for this behaviour card — see GitHub Actions logs "
            "for `[warn] Metabase card`.)_"
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
        lines.append(f"  • *{_slack_escape(ch)}*: {pct:.2f}%{nq_s}")
    if not lines:
        return "  _(no chapter column in result — check SQL aliases)_"
    return "\n".join(lines)


def fmt_eval_coverage_note(eval_summary: Optional[dict]) -> str:
    """Neutral M vs N from the eval snapshot for C12 / WoW (no partial-failure framing)."""
    if not eval_summary:
        return ""
    sr = str(eval_summary.get("stopped_reason") or "complete").strip()
    m, n = _eval_sample_counts(eval_summary)

    if m is None or n is None:
        if sr == "complete":
            return ""
        return f"_Daily eval: stopped_reason=`{sr}` (see eval Slack thread)._ \n\n"

    if n < m:
        if sr == "complete":
            return (
                f"_Sample: {m} traces from Metabase; this run judged {n}. "
                f"C12 uses the judged set._ \n\n"
            )
        return (
            f"_Sample: {m} traces from Metabase; this run judged {n} "
            f"(C12 uses the judged set). Stop: `{sr}`._ \n\n"
        )

    if sr != "complete":
        return f"_Daily eval: stopped_reason=`{sr}` (see eval Slack thread)._ \n\n"
    return ""


def fmt_confirmed_regressions(
    follow_rows: Optional[List],
    rephrase_rows: Optional[List],
    eval_summary: Optional[dict],
    rephrase_threshold: float = 3.0,
    follow_threshold: float = 5.0,
    *,
    eval_snapshot_path: str = "",
) -> str:
    """C12 — chapters that are both formatting hotspots (judge) and behavioral spikes."""
    if eval_summary is None:
        path = (eval_snapshot_path or "").strip()
        if not path:
            return (
                "  _(No eval snapshot — `EVAL_SUMMARY_PATH` is unset. "
                "Use the **Daily Automation** workflow (eval job produces the artifact → digest consumes it); "
                "a standalone digest run will not load `formatting_hotspot_chapters`.)_"
            )
        safe_path = _slack_escape(path)
        if not os.path.isfile(path):
            return (
                f"  _(No eval snapshot file at `{safe_path}`. "
                "Confirm the Daily Eval job succeeded before digest and the workflow downloads the summary "
                "(or run digest from the full automation chain).)_"
            )
        return (
            f"  _(Eval snapshot at `{safe_path}` could not be read (empty or invalid JSON). "
            "Check Daily Eval logs and that the artifact matches this path.)_"
        )

    # Legacy snapshots may omit this key; treat like an empty list (no judge hotspots to cross).
    fmt_hot = set(eval_summary.get("formatting_hotspot_chapters") or [])
    if not fmt_hot:
        return (
            "  _Daily eval reported *no* formatting hotspot chapters in this run "
            "(or snapshot predates the key — C12 cross-check uses an empty judge hotspot set)._"
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
        f"  • *{_slack_escape(c)}* — formatting FAIL hotspot ∩ behavioral spike"
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

def build_plain_fallback(
    *,
    errors_ok: bool,
    total_errors: int,
    err_fetched_n: int,
    traces_n: int,
    traces_ok: bool,
    scores_ok: bool,
    n_scores_fetched: int,
    dv_in_sample: int,
    stream_logs_ok: bool,
    stream_logs_configured: bool,
    academic_ok: bool,
    nonacademic_ok: bool,
    dump_ok: bool,
    follow_cfg: bool,
    follow_ok: bool,
    rephrase_cfg: bool,
    rephrase_ok: bool,
) -> str:
    """Plain-text multiline fallback for notifications and when blocks are not shown."""
    lf_err = (
        f"{total_errors:,} reported total; {err_fetched_n:,} obs fetched for breakdown."
        if errors_ok
        else "Langfuse errors fetch failed."
    )
    lf_scores = (
        f"{dv_in_sample:,} downvotes in {n_scores_fetched:,} score rows."
        if scores_ok
        else "Langfuse scores fetch failed."
    )
    tr = f"{traces_n:,} traces." if traces_ok else "Traces total fetch failed."
    sl = (
        "not configured"
        if not stream_logs_configured
        else ("ok" if stream_logs_ok else "Metabase fetch failed (see Actions logs).")
    )
    mb_core = (
        f"Metabase core cards: academic={'ok' if academic_ok else 'fail'}, "
        f"nonacademic={'ok' if nonacademic_ok else 'fail'}, dump={'ok' if dump_ok else 'fail'}."
    )
    beh = []
    if follow_cfg:
        beh.append(f"follow-up card={'ok' if follow_ok else 'fetch failed'}")
    if rephrase_cfg:
        beh.append(f"rephrase card={'ok' if rephrase_ok else 'fetch failed'}")
    beh_s = "; ".join(beh) if beh else "behaviour cards not configured."
    parts = [
        f"Ask AI Daily Digest — {today_str}",
        "",
        "Full message uses Slack blocks; open the message in the channel for layout.",
        "",
        f"Langfuse errors (24h): {lf_err}",
        "",
        f"Langfuse scores / downvotes: {lf_scores}",
        f"Langfuse traces (24h): {tr}",
        "",
        f"Stream logs summary (yesterday): {sl}",
        "",
        mb_core,
        f"Behaviour Metabase: {beh_s}",
    ]
    return "\n".join(parts)


def build_blocks(
    academic_rows,
    nonacademic_rows,
    dump_rows,
    score_items,
    dv_in_sample,
    error_obs,
    total_errors,
    total_traces,
    behavior_follow_rows: Optional[List] = None,
    behavior_rephrase_rows: Optional[List] = None,
    eval_summary: Optional[dict] = None,
    stream_logs_rows: Optional[List] = None,
    stream_logs_card_id: str = "",
    stream_logs_error_hint: Optional[str] = None,
    follow_card_configured: bool = False,
    rephrase_card_configured: bool = False,
    errors_hit_cap: bool = False,
    scores_hit_cap: bool = False,
    errors_ok: bool = True,
    scores_ok: bool = True,
    eval_snapshot_path: str = "",
) -> list:
    academic_block    = fmt_academic(academic_rows)
    nonacademic_block = fmt_nonacademic(nonacademic_rows)
    dump_block        = fmt_downvote_dump(dump_rows)
    scores_block      = fmt_scores(
        score_items,
        dv_in_sample,
        total_traces,
        hit_score_cap=scores_hit_cap,
        scores_ok=scores_ok,
    )
    errors_block      = fmt_errors(
        error_obs, total_errors, hit_item_cap=errors_hit_cap, errors_ok=errors_ok
    )
    sl_cfg = bool(stream_logs_card_id and stream_logs_card_id.isdigit())
    stream_logs_block = fmt_stream_logs_summary(
        stream_logs_rows,
        card_configured=sl_cfg,
        day_label=yesterday,
        metabase_error_hint=stream_logs_error_hint,
    )

    follow_txt = fmt_behavior_proxy(
        behavior_follow_rows,
        card_configured=follow_card_configured,
        setting_name="METABASE_BEHAVIOR_FOLLOWUP_CARD_ID",
    )
    rephrase_txt = fmt_behavior_proxy(
        behavior_rephrase_rows,
        card_configured=rephrase_card_configured,
        setting_name="METABASE_BEHAVIOR_REPHRASE_CARD_ID",
    )
    reg_txt = fmt_eval_coverage_note(eval_summary) + fmt_confirmed_regressions(
        behavior_follow_rows,
        behavior_rephrase_rows,
        eval_summary,
        eval_snapshot_path=eval_snapshot_path,
    )

    def section(text: str) -> dict:
        return {"type": "section", "text": {"type": "mrkdwn", "text": _truncate_section(text)}}

    divider: dict = {"type": "divider"}

    # Order: system health + student voice first (stakeholder onboarding); then context blocks.
    blocks: list = [
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
    ]

    # Optional free-text classifier section — fail-soft: any error in this path is swallowed
    # and the digest continues without it. The classifier job runs independently in CI.
    try:
        snap = load_classifier_snapshot()
        ft_block = fmt_freetext_classification(snap)
        if ft_block:
            blocks.append(divider)
            blocks.append(ft_block)
    except Exception as e:
        print(f"[warn] freetext classifier section skipped: {e}", file=sys.stderr)

    blocks.extend([
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
                    "text": _truncate_section(_digest_footer_links(stream_logs_card_id)),
                }
            ],
        },
    ])
    return blocks


def post_to_slack(blocks: list, fallback_text: str) -> bool:
    # Production-only Slack post. GitHub Actions sets GITHUB_ACTIONS=true on every
    # job (github-hosted AND self-hosted). Local shells do not. This guard prevents
    # accidental Slack posts from `python3 daily_digest.py` runs on developer
    # machines (which may have SLACK_WEBHOOK_URL in .env for testing).
    # To force a local post (rare; debugging only): export GITHUB_ACTIONS=true.
    if os.environ.get("GITHUB_ACTIONS", "").strip().lower() != "true":
        print(
            "[info] Not running in GitHub Actions — skipping Slack post. "
            "Set GITHUB_ACTIONS=true to override (debugging only).",
            file=sys.stderr,
        )
        return False
    if not SLACK_WEBHOOK:
        print("[warn] SLACK_WEBHOOK_URL not set — skipping Slack post.", file=sys.stderr)
        return False
    payload = json.dumps(
        {
            "text": fallback_text,
            "blocks": blocks,
            "unfurl_links": False,
            "unfurl_media": False,
        }
    ).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.getcode()
            raw = resp.read().decode().strip()
    except Exception as exc:
        print(f"[error] Slack request failed: {repr(exc)}", file=sys.stderr)
        return False
    if status != 200:
        print(f"[error] Slack HTTP {status}: {raw}", file=sys.stderr)
        return False
    if raw == "ok":
        return True
    try:
        body = json.loads(raw)
        if body.get("ok") is True:
            return True
        print(f"[error] Slack webhook response: {raw}", file=sys.stderr)
        return False
    except json.JSONDecodeError:
        print(f"[error] Slack unexpected response body: {raw}", file=sys.stderr)
        return False


def main() -> int:
    print(f"[info] Fetching data for digest ({today_str}, yesterday={yesterday}) …", file=sys.stderr)

    _preflight_langfuse_or_exit()

    _warn_metabase_card_env_var("METABASE_BEHAVIOR_FOLLOWUP_CARD_ID", BEHAVIOR_FOLLOWUP_CARD_ID)
    _warn_metabase_card_env_var("METABASE_BEHAVIOR_REPHRASE_CARD_ID", BEHAVIOR_REPHRASE_CARD_ID)
    # Per-var diagnostic: name exactly which behaviour card env var was empty/unset.
    # The env reads at module scope already `.strip()`, so whitespace-only secrets
    # are treated as empty here. If a digest shows "behaviour cards not configured",
    # these lines tell the operator which GitHub Actions secret needs fixing.
    if not BEHAVIOR_FOLLOWUP_CARD_ID:
        print(
            "[warn] METABASE_BEHAVIOR_FOLLOWUP_CARD_ID is empty after strip — "
            "check the secret exists in GitHub Settings → Secrets and is wired "
            "into the digest workflow `env:` block.",
            file=sys.stderr,
        )
    if not BEHAVIOR_REPHRASE_CARD_ID:
        print(
            "[warn] METABASE_BEHAVIOR_REPHRASE_CARD_ID is empty after strip — "
            "check the secret exists in GitHub Settings → Secrets and is wired "
            "into the digest workflow `env:` block.",
            file=sys.stderr,
        )

    academic_rows = fetch_metabase_card(24973, retries=METABASE_CARD_RETRIES)
    nonacademic_rows = fetch_metabase_card(24974, retries=METABASE_CARD_RETRIES)
    dump_rows = fetch_metabase_card(23036, retries=METABASE_CARD_RETRIES)

    score_items, dv_in_sample, scores_ok, scores_hit_cap = fetch_langfuse_scores()
    _assert_langfuse_or_exit(scores_ok, "fetch_langfuse_scores")
    error_obs, total_errors, errors_ok, errors_hit_cap = fetch_langfuse_errors()
    _assert_langfuse_or_exit(errors_ok, "fetch_langfuse_errors")
    total_traces, traces_ok = fetch_langfuse_traces_total()
    _assert_langfuse_or_exit(traces_ok, "fetch_langfuse_traces_total")

    follow_cfg = BEHAVIOR_FOLLOWUP_CARD_ID.isdigit()
    follow_rows = (
        fetch_metabase_card(int(BEHAVIOR_FOLLOWUP_CARD_ID), retries=METABASE_CARD_RETRIES)
        if follow_cfg
        else None
    )
    rephrase_cfg = BEHAVIOR_REPHRASE_CARD_ID.isdigit()
    rephrase_rows = (
        fetch_metabase_card(int(BEHAVIOR_REPHRASE_CARD_ID), retries=METABASE_CARD_RETRIES)
        if rephrase_cfg
        else None
    )
    eval_summary = load_eval_summary(EVAL_SUMMARY_PATH)

    # Per-fetch fail-fast gating is handled by `_assert_langfuse_or_exit` immediately
    # after each fetch above — by this point all three Langfuse fetches succeeded
    # (or the strict gate is off and we accept degraded blocks).

    sl_cfg = STREAM_LOGS_CARD_ID.isdigit()
    stream_logs_rows: Optional[List] = None
    stream_logs_err: Optional[str] = None
    if sl_cfg:
        stream_logs_rows, stream_logs_err = fetch_metabase_card_detailed(
            int(STREAM_LOGS_CARD_ID), retries=METABASE_CARD_RETRIES
        )

    mb_academic_ok = academic_rows is not None
    mb_nonacademic_ok = nonacademic_rows is not None
    mb_dump_ok = dump_rows is not None
    follow_ok = (follow_rows is not None) if follow_cfg else False
    rephrase_ok = (rephrase_rows is not None) if rephrase_cfg else False
    stream_logs_ok = (stream_logs_rows is not None) if sl_cfg else False

    fallback_text = build_plain_fallback(
        errors_ok=errors_ok,
        total_errors=total_errors,
        err_fetched_n=len(error_obs),
        traces_n=total_traces,
        traces_ok=traces_ok,
        scores_ok=scores_ok,
        n_scores_fetched=len(score_items),
        dv_in_sample=dv_in_sample,
        stream_logs_ok=stream_logs_ok,
        stream_logs_configured=sl_cfg,
        academic_ok=mb_academic_ok,
        nonacademic_ok=mb_nonacademic_ok,
        dump_ok=mb_dump_ok,
        follow_cfg=follow_cfg,
        follow_ok=follow_ok,
        rephrase_cfg=rephrase_cfg,
        rephrase_ok=rephrase_ok,
    )

    blocks = build_blocks(
        academic_rows,
        nonacademic_rows,
        dump_rows,
        score_items,
        dv_in_sample,
        error_obs,
        total_errors,
        total_traces,
        behavior_follow_rows=follow_rows,
        behavior_rephrase_rows=rephrase_rows,
        eval_summary=eval_summary,
        stream_logs_rows=stream_logs_rows,
        stream_logs_card_id=STREAM_LOGS_CARD_ID,
        stream_logs_error_hint=stream_logs_err,
        follow_card_configured=follow_cfg,
        rephrase_card_configured=rephrase_cfg,
        errors_hit_cap=errors_hit_cap,
        scores_hit_cap=scores_hit_cap,
        errors_ok=errors_ok,
        scores_ok=scores_ok,
        eval_snapshot_path=EVAL_SUMMARY_PATH,
    )
    print(f"[info] {fallback_text.splitlines()[0]}\n[{len(blocks)} blocks]", file=sys.stderr)

    if DRY_RUN:
        import pprint
        pprint.pprint(blocks)
        print("\n[info] --dry-run: Slack post skipped.", file=sys.stderr)
        print(
            f"[digest] metabase academic={'ok' if mb_academic_ok else 'fail'} "
            f"nonacademic={'ok' if mb_nonacademic_ok else 'fail'} dump={'ok' if mb_dump_ok else 'fail'} "
            f"stream_logs={'ok' if stream_logs_ok else ('na' if not sl_cfg else 'fail')} "
            f"langfuse scores={'ok' if scores_ok else 'fail'} errors={'ok' if errors_ok else 'fail'} "
            f"traces={'ok' if traces_ok else 'fail'} slack_post=skipped",
            file=sys.stderr,
        )
        return 0

    # Idempotency: skip the Slack post if we already posted for today's UTC
    # date. The check runs only when we'd actually post (we're past --dry-run
    # and intend to call the webhook), so cron retries on the same UTC day
    # become no-ops. FORCE_REPOST=1 bypasses for debugging.
    if _already_posted_today("digest-posted"):
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print(
            f"[info] already posted {today_utc}, skipping",
            file=sys.stderr,
        )
        return 0

    posted = post_to_slack(blocks, fallback_text)
    exit_code = 0
    if not posted:
        exit_code = 1
    else:
        # Record the successful post so a second invocation on this UTC day
        # is a no-op. Write AFTER 200 OK so failed posts can be retried.
        _write_posted_marker("digest-posted")
    if not mb_academic_ok and not mb_nonacademic_ok and not mb_dump_ok:
        exit_code = 1
    if DIGEST_STRICT_STREAM_LOGS and sl_cfg and stream_logs_rows is None:
        exit_code = 1

    print(
        f"[digest] metabase academic={'ok' if mb_academic_ok else 'fail'} "
        f"nonacademic={'ok' if mb_nonacademic_ok else 'fail'} dump={'ok' if mb_dump_ok else 'fail'} "
        f"stream_logs={'ok' if stream_logs_ok else ('na' if not sl_cfg else 'fail')} "
        f"langfuse scores={'ok' if scores_ok else 'fail'} errors={'ok' if errors_ok else 'fail'} "
        f"traces={'ok' if traces_ok else 'fail'} slack_post={'ok' if posted else 'fail'}",
        file=sys.stderr,
    )
    if posted:
        print("[info] Message posted to Slack.", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
