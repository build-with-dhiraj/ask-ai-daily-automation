"""
Daily eval orchestrator: runs every morning, posts a single Slack message
that complements (does NOT touch) the existing Cowork daily digest.

Pipeline:
  1. Pull yesterday's stratified sample from Metabase
       (saved question id → JSON via REST API)
       OR from a local samples.json (--samples flag, for dry runs)
  2. Run the v8 judge against every sample (judge_runner.call_judge)
  3. Write per-axial + per-open-code scores to Langfuse,
     attached to the production trace_id
  4. Render the dual-track Slack block + per-stratum split
  5. Post to Slack channel via webhook (or dry-run to stdout)

Sampling strategy (see `sql/daily_stratified_sample.sql` + Metabase Q33193):
  • Chapter-stratified downvotes / upvotes / no-votes + outlier_long; caps in SQL
  • Runner pulls the saved Metabase question as JSON

Required env (set in Cowork SKILL.md or shell before invoking):
  AZURE_ENDPOINT
  AZURE_API_KEY
  AZURE_API_VERSION
  DEPLOYMENT_NAME
  METABASE_URL                # e.g. https://metabase-prod.penpencil.co
  METABASE_API_KEY            # preferred, use for SSO accounts (X-Api-Key auth)
  METABASE_USERNAME           # fallback, only needed when METABASE_API_KEY is not set
  METABASE_PASSWORD           # fallback, only needed when METABASE_API_KEY is not set
  METABASE_QUESTION_ID        # the saved question id for daily_stratified_sample.sql
  LANGFUSE_PUBLIC_KEY         # optional, enables score writes + tracing
  LANGFUSE_SECRET_KEY         # optional
  LANGFUSE_HOST               # optional (default https://cloud.langfuse.com)
  SLACK_WEBHOOK_URL           # the incoming-webhook for the eval channel
                              # (separate from the existing digest channel,
                              # OR same channel, your call)
  JUDGE_HTTP_TIMEOUT_SEC      # optional, per LLM call HTTP timeout (default 240s;
                              # prevents one hung Azure request from stalling the whole run)
  METABASE_QUERY_TIMEOUT_SEC  # optional, Metabase card query HTTP timeout (default 600s;
                              # prevents socket timeout if stratified sample query is slow)
  JUDGE_CONCURRENCY           # optional, concurrent LLM judges (default 1; e.g. 8 in CI)
  JUDGE_CHUNK_SIZE            # optional, samples per ThreadPool batch (default max(32, 4×concurrency))
  EVAL_MAX_RUNTIME_SEC        # optional, soft time budget; stop between chunks & finalize (graceful vs SIGKILL)

Usage:
  # Full daily run (Metabase pull → judge → Slack post)
  python3 daily_eval.py

  # Dry run from a local samples.json (skips Metabase + Slack post)
  python3 daily_eval.py --samples samples.json --dry-run

  # Use cached samples from a previous Metabase pull
  python3 daily_eval.py --samples /tmp/yesterday_sample.json
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from concurrent.futures import (
    CancelledError,
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin


# ---------------------------------------------------------------------------
# Idempotency guard: prevents duplicate eval Slack posts on the same UTC day.
# Mirrors the guard in daily_digest.py. Marker is written ONLY after a
# successful Slack post so failed posts can be retried. FORCE_REPOST=1
# bypasses (debugging only).
# ---------------------------------------------------------------------------

def _eval_marker_path(prefix: str = "eval-posted") -> Path:
    base = (
        os.environ.get("DIGEST_STATE_DIR", "").strip()
        or str(Path.home() / ".ask-ai-daily-automation" / "state")
    )
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Scope the marker by SLACK_TARGET so a staging (workflow_dispatch) run
    # cannot block the next prod (schedule) cron, and vice versa. Default to
    # "prod" so locally-run posts use the safest fallback (skip same-day repost).
    target = (os.environ.get("SLACK_TARGET") or "prod").strip().lower()
    if target not in ("prod", "staging"):
        target = "prod"
    return Path(base) / f"{prefix}-{target}-{today_utc}.marker"


def _eval_is_staging_target() -> bool:
    # Staging is a test surface: operators must be able to re-dispatch within
    # the same UTC day and see fresh posts. The day-level marker is therefore
    # disabled entirely when SLACK_TARGET=staging. Prod (and the implicit-prod
    # fallback) keep the original idempotency behavior.
    return (os.environ.get("SLACK_TARGET") or "").strip().lower() == "staging"


def _eval_already_posted_today(prefix: str = "eval-posted") -> bool:
    if _eval_is_staging_target():
        return False
    if os.environ.get("FORCE_REPOST", "").strip() == "1":
        return False
    return _eval_marker_path(prefix).exists()


def _eval_write_posted_marker(prefix: str = "eval-posted") -> None:
    if _eval_is_staging_target():
        # Staging keeps no day-level state; see _eval_is_staging_target.
        return
    marker = _eval_marker_path(prefix)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            datetime.now(timezone.utc).isoformat() + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(
            f"[warn] Could not write idempotency marker {marker}: {repr(exc)}",
            file=sys.stderr,
        )

# Ensure judge_runner is importable when called from Cowork or cron
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from judge_runner import (  # noqa: E402
    aggregate, render_slack_block, call_judge, get_openai_client,
    validate_judge_output, write_judge_scores_to_langfuse,
    _get_langfuse_writer, DEFAULT_MODEL,
)


# ---------------------------------------------------------------------------
# Concurrent judge loop (ThreadPoolExecutor)
# ---------------------------------------------------------------------------

_PRINT_LOCK = threading.Lock()
_CHECKPOINT_LOCK = threading.Lock()
_SIGTERM_REQUESTED = threading.Event()


def _handle_sigterm(_signum: int, _frame: Any) -> None:
    print(
        "\n⚠️  SIGTERM received, finishing current chunk, then finalizing.",
        file=sys.stderr,
    )
    _SIGTERM_REQUESTED.set()


def _judge_concurrency() -> int:
    raw = os.environ.get("JUDGE_CONCURRENCY", "1").strip()
    try:
        n = int(raw)
        return max(1, min(n, 64))
    except ValueError:
        return 1


def _eval_max_runtime_sec() -> float | None:
    """Read EVAL_MAX_RUNTIME_SEC. Returns None for missing/blank/"0"/negative
    (interpreted as "no soft cap, run to completion"). Positive values are
    floored at 60s to avoid pathological tiny budgets.

    Workflow default is "0" (no cap); the job's GitHub Actions
    `timeout-minutes: 600` (10h) is the runaway backstop.
    """
    raw = os.environ.get("EVAL_MAX_RUNTIME_SEC", "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    if val <= 0:
        return None
    return max(60.0, val)


def _judge_chunk_size(concurrency: int) -> int:
    raw = os.environ.get("JUDGE_CHUNK_SIZE", "").strip()
    if raw:
        try:
            c = int(raw)
            return max(concurrency, c)
        except ValueError:
            pass
    return max(32, concurrency * 4)


@dataclass
class JudgeLoopOutcome:
    new_results: list[dict]
    stopped_reason: str  # "complete" | "time_budget" | "signal"
    n_langfuse_scores: int
    judge_phase_sec: float = 0.0


def _write_checkpoint(
    checkpoint_path: str,
    prefix: list[dict],
    results: list[dict],
    n_samples_this_run: int,
) -> None:
    """Thread-safe checkpoint (prefix from resume + new results this invocation)."""
    with _CHECKPOINT_LOCK:
        with open(checkpoint_path, "w") as _f:
            json.dump(prefix + results, _f)
    done = len(prefix) + len(results)
    total = len(prefix) + n_samples_this_run
    print(f"  💾 checkpoint saved ({done}/{total})")


def _maybe_checkpoint_every_n(
    checkpoint_path: str | None,
    prefix: list[dict],
    results: list[dict],
    n_samples_this_run: int,
    *,
    step: int = 50,
) -> None:
    if not checkpoint_path:
        return
    done = len(prefix) + len(results)
    if done > 0 and done % step == 0:
        _write_checkpoint(checkpoint_path, prefix, results, n_samples_this_run)
def _judge_one_sample(
    client: Any,
    global_idx: int,
    n_total: int,
    s: dict,
    *,
    judge_run_id: str,
    model: str,
    write_scores: bool,
) -> tuple[dict, int]:
    tid = s.get("trace_id") or f"sample-{global_idx}"
    stratum = s.get("stratum") or "all"
    n_written = 0
    try:
        parsed, meta = call_judge(client, s, model=model)
        v = validate_judge_output(parsed)
        parsed["_trace_id"] = tid
        parsed["_stratum"] = stratum
        parsed["_chapter"] = s.get("chapter") or "unknown"
        parsed["_subject"] = s.get("subject") or "unknown"
        parsed["_validation_ok"] = v.ok
        parsed["_validation_errors"] = v.errors
        parsed["_meta"] = meta
        band = parsed.get("overall_band")
        tail = ""
        if write_scores and v.ok:
            n_written = write_judge_scores_to_langfuse(
                production_trace_id=tid,
                parsed=parsed,
                judge_run_id=judge_run_id,
                judge_model=meta.get("model_param", ""),
            )
            tail = f"  +{n_written} scores"
        with _PRINT_LOCK:
            print(f"  [{global_idx:>4}/{n_total}] {stratum:<10} {tid[:36]} {band}{tail}")
        return parsed, n_written
    except Exception as e:
        with _PRINT_LOCK:
            print(
                f"  [{global_idx:>4}/{n_total}] {stratum:<10} {tid[:36]} ERROR: {e}"
            )
        return (
            {
                "_trace_id": tid,
                "_stratum": stratum,
                "_chapter": s.get("chapter") or "unknown",
                "_subject": s.get("subject") or "unknown",
                "_parse_error": True,
                "_error": str(e),
            },
            0,
        )


# ---------------------------------------------------------------------------
# Metabase fetch
# ---------------------------------------------------------------------------

def metabase_session_token(base_url: str, username: str, password: str,
                            timeout: float | None = None) -> str:
    import urllib.request
    if timeout is None:
        # Bumped 30 → 120 to absorb transient TLS handshake / VPN reconnect
        # latency seen in 2026-05-11 incident; auth endpoint normally <2s.
        timeout = float(os.environ.get("METABASE_SESSION_TIMEOUT_SEC", "120.0"))
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        urljoin(base_url, "/api/session"),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "id" not in data:
        raise RuntimeError(f"Metabase session: unexpected response: {data}")
    return data["id"]


def _metabase_eval_total_attempts() -> int:
    """Total attempt count for the eval Metabase fetch.

    Env semantics match digest's METABASE_CARD_RETRIES: the env value is the
    number of TOTAL attempts (not retries-after-the-first). Internally we
    convert to ``max_retries = total - 1`` for the existing range loop.

    Default 5 (i.e. 4 retries): yields 4 sleeps of 10/20/40/80s = 150s total
    backoff budget, within the 600-min job cap.
    """
    raw = (os.environ.get("METABASE_EVAL_RETRIES") or "").strip()
    try:
        total = int(raw) if raw else 5
    except ValueError:
        total = 5
    return max(1, min(10, total))


def metabase_run_card(base_url: str, card_id: int, auth_header: dict,
                       timeout: float | None = None,
                       max_retries: int | None = None) -> list[dict]:
    """Run a saved Metabase question and return rows as list[dict].

    Retries up to max_retries times on socket timeout with bounded exponential
    backoff (10/20/40/80/120s, capped at 120s).

    timeout defaults to METABASE_QUERY_TIMEOUT_SEC env var (default 1800s).
    max_retries defaults to METABASE_EVAL_RETRIES-1 (env value is total attempts;
    default env value 5 → max_retries=4).
    """
    import urllib.request
    import urllib.error

    if timeout is None:
        # Bumped 600 → 1800 (30 min). Eval Metabase questions can take 15+ min
        # when astracdc.silver_conversational_query_table is hot; the previous
        # 10-min cap was the proximate cause of the 2026-05-11 silent miss.
        timeout = float(os.environ.get("METABASE_QUERY_TIMEOUT_SEC", "1800.0"))
    if max_retries is None:
        max_retries = _metabase_eval_total_attempts() - 1

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                urljoin(base_url, f"/api/card/{card_id}/query/json"),
                data=b"",
                headers={"Content-Type": "application/json", **auth_header},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                rows = json.loads(resp.read().decode("utf-8"))
            if not isinstance(rows, list):
                raise RuntimeError(f"Metabase card {card_id}: unexpected response shape: {type(rows)}")
            return rows
        except (socket.timeout, urllib.error.URLError) as e:
            if attempt < max_retries:
                # Bounded exponential backoff: 10, 20, 40, 80, 120, 120…s.
                # Capped at 120 so a long retry tail stays inside the job cap.
                wait_time = min(120, 10 * 2 ** attempt)
                print(f"⚠️  Metabase card query timeout (attempt {attempt + 1}/{max_retries + 1}, will retry in {wait_time}s): {e}")
                time.sleep(wait_time)
            else:
                raise


def normalize_metabase_rows(rows: list[dict]) -> list[dict]:
    """Coerce a Metabase JSON dump into the runner's expected sample shape.

    Metabase column names sometimes have spaces/casing variations. This
    function maps whatever it gets to: trace_id, stratum, doubt, ai_answer,
    transcript, ideal_answer, subject, chapter, student_class, exam,
    image_url, is_annotated.
    """
    if not rows:
        return []

    # Build a case-insensitive key map from the first row
    sample_keys = {k.lower(): k for k in rows[0].keys()}

    def get(d: dict, *candidates: str, default: Any = "") -> Any:
        for cand in candidates:
            actual = sample_keys.get(cand.lower())
            if actual and d.get(actual) not in (None, ""):
                return d[actual]
        return default

    out: list[dict] = []
    for r in rows:
        out.append({
            "trace_id":     str(get(r, "trace_id", "aiintentid")),
            "stratum":      str(get(r, "stratum", default="all")) or "all",
            "doubt":        str(get(r, "doubt", "query")),
            "ai_answer":    str(get(r, "ai_answer", "answer")),
            "transcript":   str(get(r, "transcript", default="")),
            "ideal_answer": str(get(r, "ideal_answer", default="")),
            "subject":      str(get(r, "subject", default="")),
            "chapter":      str(get(r, "chapter", default="")),
            "student_class": str(get(r, "student_class", "class", default="")),
            "exam":         str(get(r, "exam", "exam_name", default="")),
            "image_url":    str(get(r, "image_url", default="")),
            "is_annotated": bool(get(r, "is_annotated", default=False)),
        })
    return out


# ---------------------------------------------------------------------------
# Slack post (incoming webhook)
# ---------------------------------------------------------------------------

def post_blocks_to_slack(
    webhook_url: str, blocks: list, fallback_text: str, timeout: float = 120.0
) -> bool:
    """Block Kit variant of post_to_slack, used by the C1.3 poster pipeline.

    Parallel to post_to_slack(webhook, text) below; the text-only function is
    preserved for the fallback path and existing tests. Returns True on Slack
    `ok` body, False on any error or local-shell guard.
    """
    if os.environ.get("GITHUB_ACTIONS", "").strip().lower() != "true":
        print(
            "[info] Not running in GitHub Actions, skipping Slack post.",
            file=sys.stderr,
        )
        return False
    import urllib.request
    import urllib.error

    payload = json.dumps({"blocks": blocks, "text": fallback_text}).encode("utf-8")
    retryable_codes = (429, 502, 503, 504)
    for attempt in range(2):
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            if body.strip() == "ok":
                return True
            print(f"⚠️  Slack webhook returned non-ok: {body}")
            return False
        except urllib.error.HTTPError as exc:
            if exc.code in retryable_codes and attempt == 0:
                time.sleep(5)
                continue
            print(f"⚠️  Slack webhook HTTP {exc.code}: {exc!r}")
            return False
        except urllib.error.URLError as exc:
            if attempt == 0:
                time.sleep(5)
                continue
            print(f"⚠️  Slack webhook failed after retry: {exc!r}")
            return False
    return False


def post_to_slack(webhook_url: str, text: str, timeout: float = 120.0) -> bool:
    """Post to Slack incoming webhook with bounded retry. Returns True on success.

    Returns False (rather than the previous silent `None`) when:
      - the GITHUB_ACTIONS guard fires (local run, no post attempted)
      - the webhook responds with a non-`ok` body (Slack validation error)
      - a non-retryable error is raised
      - the single retry is exhausted

    Retry policy mirrors `daily_digest.post_to_slack`: at most one retry on
    `URLError` (covers connect/handshake failures) or HTTPError code in
    {429, 502, 503, 504}. We deliberately do NOT retry on read-timeout after
    sending bytes: urllib can't distinguish "Slack got the payload" from "Slack
    didn't", and a duplicate post is worse than a miss.

    The bool return is a contract change from the previous `-> None`; the
    marker write at the call site is now conditional on a True return so a
    silent non-`ok` body no longer suppresses the next-day repost.
    """
    if os.environ.get("GITHUB_ACTIONS", "").strip().lower() != "true":
        print(
            "[info] Not running in GitHub Actions, skipping Slack post. "
            "Set GITHUB_ACTIONS=true to override (debugging only).",
            file=sys.stderr,
        )
        return False
    import urllib.request
    import urllib.error

    body_bytes = json.dumps({"text": text}).encode("utf-8")
    retryable_codes = (429, 502, 503, 504)

    for attempt in range(2):  # initial + at most 1 retry
        req = urllib.request.Request(
            webhook_url, data=body_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_body = resp.read().decode("utf-8")
            if resp_body.strip() == "ok":
                return True
            # 200 with a non-`ok` body == Slack validation error; never
            # retryable (re-sending the same payload will fail identically).
            print(f"⚠️  Slack webhook returned non-ok: {resp_body}")
            return False
        except urllib.error.HTTPError as exc:
            if exc.code in retryable_codes and attempt == 0:
                print(
                    f"[warn] Slack HTTP {exc.code} on attempt {attempt + 1}; "
                    "sleeping 5s before single retry",
                    file=sys.stderr,
                )
                time.sleep(5)
                continue
            print(f"⚠️  Slack webhook HTTP {exc.code}: {exc!r}")
            return False
        except urllib.error.URLError as exc:
            # Treat as pre-send connect/handshake failure, safe to retry.
            # urllib does not distinguish before/after-send for URLError, but a
            # duplicate Slack post on a connect-side flake is the lesser evil
            # vs a silent miss; bounded to 1 retry caps duplicate risk.
            if attempt == 0:
                print(
                    f"[warn] Slack URLError on attempt {attempt + 1}: {exc!r}; "
                    "sleeping 5s before single retry",
                    file=sys.stderr,
                )
                time.sleep(5)
                continue
            print(f"⚠️  Slack webhook failed after retry: {exc!r}")
            return False

    return False


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def fetch_samples_from_metabase() -> list[dict]:
    base = os.environ["METABASE_URL"].rstrip("/") + "/"
    card = int(os.environ["METABASE_QUESTION_ID"])
    api_key = os.environ.get("METABASE_API_KEY")
    if api_key:
        print(f"📥 Metabase API key auth → {base}")
        auth_header = {"X-Api-Key": api_key}
    else:
        user = os.environ["METABASE_USERNAME"]
        pw = os.environ["METABASE_PASSWORD"]
        print(f"📥 Metabase session auth → {base}")
        token = metabase_session_token(base, user, pw)
        auth_header = {"X-Metabase-Session": token}
    print(f"📥 Running card {card}...")
    rows = metabase_run_card(base, card, auth_header)
    print(f"📥 Got {len(rows)} rows")
    return normalize_metabase_rows(rows)


def run_judge_loop(
    samples: list[dict],
    judge_run_id: str,
    write_scores: bool,
    model: str = DEFAULT_MODEL,
    checkpoint_path: str | None = None,
    checkpoint_prefix: list[dict] | None = None,
    *,
    stop_event: threading.Event | None = None,
) -> JudgeLoopOutcome:
    if not samples:
        return JudgeLoopOutcome(
            new_results=[],
            stopped_reason="complete",
            n_langfuse_scores=0,
            judge_phase_sec=0.0,
        )

    judge_conc = _judge_concurrency()
    chunk_sz = _judge_chunk_size(judge_conc)
    max_run = _eval_max_runtime_sec()
    deadline = time.monotonic() + max_run if max_run is not None else None
    evt = stop_event if stop_event is not None else _SIGTERM_REQUESTED

    client = get_openai_client()
    if write_scores:
        if _get_langfuse_writer() is None:
            print(
                "⚠️  --write-scores requested but Langfuse keys missing; continuing without writes"
            )
            write_scores = False
        else:
            print(
                f"📡 Writing scores to Langfuse (judge_run_id={judge_run_id}); "
                f"concurrency={judge_conc} chunk_size={chunk_sz}"
            )

    prefix: list[dict] = list(checkpoint_prefix) if checkpoint_prefix else []
    results: list[dict] = []
    n_total = len(samples)
    n_scores = 0
    stopped_reason = "complete"
    t_start = time.monotonic()

    for chunk_start in range(0, n_total, chunk_sz):
        if evt.is_set():
            stopped_reason = "signal"
            break
        if deadline is not None and time.monotonic() >= deadline:
            stopped_reason = "time_budget"
            break

        chunk = samples[chunk_start : chunk_start + chunk_sz]
        chunk_stopped_early = False

        if judge_conc <= 1:
            for j, s in enumerate(chunk):
                if evt.is_set():
                    stopped_reason = "signal"
                    chunk_stopped_early = True
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    stopped_reason = "time_budget"
                    chunk_stopped_early = True
                    break
                gidx = chunk_start + j + 1
                parsed, nw = _judge_one_sample(
                    client,
                    gidx,
                    n_total,
                    s,
                    judge_run_id=judge_run_id,
                    model=model,
                    write_scores=write_scores,
                )
                results.append(parsed)
                n_scores += nw
                _maybe_checkpoint_every_n(
                    checkpoint_path, prefix, results, n_total
                )
        else:
            slots: list[tuple[dict, int] | None] = [None] * len(chunk)
            with ThreadPoolExecutor(max_workers=judge_conc) as ex:
                futures = [
                    ex.submit(
                        _judge_one_sample,
                        client,
                        chunk_start + j + 1,
                        n_total,
                        s,
                        judge_run_id=judge_run_id,
                        model=model,
                        write_scores=write_scores,
                    )
                    for j, s in enumerate(chunk)
                ]
                future_to_j = {futures[k]: k for k in range(len(futures))}
                pending = set(futures)
                while pending:
                    if evt.is_set():
                        stopped_reason = "signal"
                        chunk_stopped_early = True
                        for f in pending:
                            f.cancel()
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        stopped_reason = "time_budget"
                        chunk_stopped_early = True
                        for f in pending:
                            f.cancel()
                        break
                    done, pending = wait(
                        pending,
                        timeout=2.0,
                        return_when=FIRST_COMPLETED,
                    )
                    for f in done:
                        jj = future_to_j[f]
                        try:
                            slots[jj] = f.result()
                        except CancelledError:
                            slots[jj] = None
            for item in slots:
                if item is None:
                    continue
                results.append(item[0])
                n_scores += item[1]
                _maybe_checkpoint_every_n(
                    checkpoint_path, prefix, results, n_total
                )
            if stopped_reason != "complete":
                chunk_stopped_early = True

        if checkpoint_path:
            _write_checkpoint(checkpoint_path, prefix, results, n_total)

        if chunk_stopped_early:
            break

        if evt.is_set():
            stopped_reason = "signal"
            break
        if deadline is not None and time.monotonic() >= deadline:
            stopped_reason = "time_budget"
            break
        if chunk_start + len(chunk) >= n_total:
            break

    dur = time.monotonic() - t_start

    if write_scores:
        try:
            _get_langfuse_writer().flush()
            print(f"📡 Wrote {n_scores} Langfuse scores total. flush ok.")
        except Exception as e:
            print(f"📡 Langfuse flush warning: {e}")

    in_tok = sum((r.get("_meta") or {}).get("input_tokens") or 0 for r in results)
    out_tok = sum((r.get("_meta") or {}).get("output_tokens") or 0 for r in results)
    est_usd = in_tok * 2e-6 + out_tok * 8e-6
    print(
        f"⏱  Judge phase: {dur:.1f}s | stopped={stopped_reason} | "
        f"tokens {in_tok}/{out_tok} | est ~${est_usd:.2f} (₹{est_usd*83:.0f})"
    )

    return JudgeLoopOutcome(
        new_results=results,
        stopped_reason=stopped_reason,
        n_langfuse_scores=n_scores,
        judge_phase_sec=dur,
    )


def finalize_eval_run(
    *,
    output_path: str,
    results: list[dict],
    new_results: list[dict],
    checkpoint_results: list[dict],
    judge_outcome: JudgeLoopOutcome,
    n_sampled: int,
    yesterday_str: str,
    judge_run_id: str,
    prev_snapshot: dict | None,
    prev_snapshot_path: str,
    label: str | None,
) -> str:
    """Save full results JSON, build Slack block (incl. cost footer), write eval summary snapshot.

    Single finalization path for normal completion, soft time budget, and SIGTERM-after-chunk.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"💾 Saved {len(results)} results to {output_path}")

    summary = aggregate(results)
    run_label = label or judge_run_id
    block = render_slack_block(
        summary, run_label=run_label, results=results, prev_snapshot=prev_snapshot
    )

    n_judged_total = len(results)
    n_judged_new = len(new_results)
    in_tok = sum((r.get("_meta") or {}).get("input_tokens") or 0 for r in new_results)
    out_tok = sum((r.get("_meta") or {}).get("output_tokens") or 0 for r in new_results)
    est_usd = in_tok * 2e-6 + out_tok * 8e-6

    strata_counts: dict[str, int] = {}
    for r in results:
        st = r.get("_stratum") or "all"
        strata_counts[st] = strata_counts.get(st, 0) + 1

    strata_cost_lines = []
    for st in sorted(strata_counts.keys()):
        st_rows = [r for r in new_results if (r.get("_stratum") or "all") == st]
        st_n = strata_counts[st]
        st_in = sum((r.get("_meta") or {}).get("input_tokens") or 0 for r in st_rows)
        st_out = sum((r.get("_meta") or {}).get("output_tokens") or 0 for r in st_rows)
        st_tokens = st_in + st_out
        st_cost = st_in * 2e-6 + st_out * 8e-6
        strata_cost_lines.append(
            f"     {st:<14}  n={st_n:<5}  tokens={st_tokens:>9,}   ~${st_cost:.2f}"
        )

    resumed_note = (
        f"   _(resumed, {n_judged_total - n_judged_new} from checkpoint, cost reflects new judgements only)_"
        if checkpoint_results
        else ""
    )

    nl = "\n"
    one_pager = os.environ.get(
        "EVAL_ONE_PAGER_URL",
        "https://github.com/build-with-dhiraj/ask-ai-daily-automation/blob/main/ONE_PAGER.md",
    )
    cost_footer = (
        f"\n💰 *Run cost*\n"
        f"   Total: {n_judged_total} samples | {in_tok:,} in / {out_tok:,} out | "
        f"~${est_usd:.2f} (₹{est_usd*83:.0f})\n"
        f"   By stratum:\n"
        f"{nl.join(strata_cost_lines)}"
        f"{(nl + resumed_note) if resumed_note else ''}"
        f"\n❓ *What is this?* <{one_pager}|Eval one-pager: thresholds, cost, Metabase Q33193>\n"
    )
    if judge_outcome.stopped_reason != "complete":
        cost_footer += (
            f"\n⏱ _Run ended with `{judge_outcome.stopped_reason}` before every pending sample "
            f"was judged. Sample: {n_sampled} traces (Metabase pull); this aggregate has "
            f"{len(results)} judged traces. Metrics use completed judgements; resume uses "
            f"the checkpoint file._\n"
        )
    block = block + cost_footer

    try:
        n_j = summary.n_judgable or 1
        n_acc = sum(
            1
            for r in results
            if r.get("overall_band") in ("PASS", "NEUTRAL", "FAIL")
            and not r.get("academic", {}).get("passed", True)
        )
        exp_axials_for_snap = ("intent", "formatting", "pedagogy", "tone")
        n_exp = sum(
            1
            for r in results
            if r.get("overall_band") in ("PASS", "NEUTRAL", "FAIL")
            and any(not r.get(ax, {}).get("passed", True) for ax in exp_axials_for_snap)
        )
        pass_pct_snap = round(100.0 * summary.n_pass / n_j, 1) if summary.n_judgable else 0.0
        neutral_pct_snap = round(100.0 * summary.n_neutral / n_j, 1) if summary.n_judgable else 0.0
        fail_pct_snap = round(100.0 * summary.n_fail / n_j, 1) if summary.n_judgable else 0.0
        acc_fail_pct_snap = round(100.0 * n_acc / n_j, 1) if summary.n_judgable else 0.0
        exp_fail_pct_snap = round(100.0 * n_exp / n_j, 1) if summary.n_judgable else 0.0

        def _hotspot_chapters_for_axial(ax: str, min_n: int = 5, top_k: int = 5) -> list[str]:
            stats: dict[str, dict[str, int]] = {}
            for r in results:
                if r.get("overall_band") not in ("PASS", "NEUTRAL", "FAIL"):
                    continue
                ch = (r.get("_chapter") or "").strip() or "unknown"
                d = stats.setdefault(ch, {"n": 0, "fail": 0})
                d["n"] += 1
                if not r.get(ax, {}).get("passed", True):
                    d["fail"] += 1
            eligible = [(c, d) for c, d in stats.items() if d["n"] >= min_n and c != "unknown"]
            worst = sorted(
                eligible,
                key=lambda kv: (kv[1]["fail"] / kv[1]["n"], kv[1]["n"]),
                reverse=True,
            )[:top_k]
            return [c for c, d in worst if d["fail"] > 0]

        formatting_hotspot_chapters = _hotspot_chapters_for_axial("formatting")

        summary_snapshot = {
            "date": yesterday_str,
            "n_sampled": n_sampled,
            "n_metabase_rows": n_sampled,
            "n_judged": len(results),
            "time_window_sec": round(judge_outcome.judge_phase_sec, 2),
            "eval_completed": True,
            "stopped_reason": judge_outcome.stopped_reason,
            "n_judgable": summary.n_judgable,
            "pass_pct": pass_pct_snap,
            "neutral_pct": neutral_pct_snap,
            "fail_pct": fail_pct_snap,
            "acc_fail_pct": acc_fail_pct_snap,
            "exp_fail_pct": exp_fail_pct_snap,
            "axial_fail_pct": dict(summary.axial_fail_pct),
            "formatting_hotspot_chapters": formatting_hotspot_chapters,
            # Run cost so the scoreboard Ops stripe can render real numbers
            # without re-parsing the slack block text.
            "run_cost_usd": round(float(est_usd), 4),
            "run_tokens_in": int(in_tok),
            "run_tokens_out": int(out_tok),
        }
        with open(prev_snapshot_path, "w") as _sf:
            json.dump(summary_snapshot, _sf, indent=2)
        print(f"📸 Saved today's snapshot to {prev_snapshot_path} (for tomorrow's WoW deltas)")
    except Exception as _e:
        print(f"⚠️  Could not save today's snapshot ({_e}). WoW deltas may be missing tomorrow.")

    return block


def _build_scoreboard_ops_stripe_text(snapshot: dict) -> str:
    """Scoreboard Ops stripe one-line body. Shape:
       `$X.XX run cost · N traces judged · Wilson CI +-X.Xpp on academic`

    Sourced entirely from the eval snapshot (no slack-block re-parsing).
    """
    from judge_runner import wilson_ci_pp
    snap = snapshot or {}
    cost = float(snap.get("run_cost_usd") or 0.0)
    n_judged = int(snap.get("n_judged") or 0)
    n_judgable = int(snap.get("n_judgable") or n_judged)
    acc_fail = float(snap.get("acc_fail_pct") or 0.0)
    ci_pp = wilson_ci_pp(acc_fail, n_judgable) if n_judgable else 0.0
    return (
        f"   ${cost:.2f} run cost · {n_judged:,} traces judged · "
        f"Wilson CI ±{ci_pp:.1f}pp on academic"
    )


def _build_scoreboard_safety_stripe_text(snapshot: dict, floor_pct: float = 6.0) -> str:
    """Scoreboard Safety floor stripe one-line body. Shape:
       `Academic FAIL X.X% (floor 6%) · Experience FAIL X.X%`
    """
    snap = snapshot or {}
    acc_fail = float(snap.get("acc_fail_pct") or 0.0)
    exp_fail = float(snap.get("exp_fail_pct") or 0.0)
    return (
        f"   Academic FAIL {acc_fail:.1f}% (floor {floor_pct:.0f}%) · "
        f"Experience FAIL {exp_fail:.1f}%"
    )


def write_minimal_eval_snapshot(path: str, *, yesterday_str: str, reason: str) -> None:
    """Write digest-readable summary so artifact upload does not ship stale /tmp JSON.

    When eval exits before the main snapshot block (e.g. zero Metabase rows), an older
    file on the runner may lack keys such as formatting_hotspot_chapters; the digest job
    then misreports C12. Daily Automation uploads this path with ``if: always()``."""
    payload = {
        "date": yesterday_str,
        "n_sampled": 0,
        "n_metabase_rows": 0,
        "n_judged": 0,
        "time_window_sec": 0.0,
        "eval_completed": True,
        "stopped_reason": reason,
        "n_judgable": 0,
        "pass_pct": 0.0,
        "neutral_pct": 0.0,
        "fail_pct": 0.0,
        "acc_fail_pct": 0.0,
        "exp_fail_pct": 0.0,
        "axial_fail_pct": {},
        "formatting_hotspot_chapters": [],
        "run_cost_usd": 0.0,
        "run_tokens_in": 0,
        "run_tokens_out": 0,
        "_eval_note": reason,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as _sf:
        json.dump(payload, _sf, indent=2)
    print(f"📸 Wrote minimal eval snapshot ({reason}) to {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """Daily eval entry point.

    Poster pipeline is the default per Locked Decision #6 (single design,
    single ship). Set POSTER_DISABLE=1 only for emergency rollback to the
    legacy text-only Slack post.
    """
    p = argparse.ArgumentParser(description="Daily eval orchestrator")
    p.add_argument("--samples", help="Use this samples JSON instead of pulling from Metabase")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip Slack post; print block to stdout")
    p.add_argument("--no-write-scores", action="store_true",
                   help="Skip Langfuse score writes")
    p.add_argument("--output", default="/tmp/daily_eval_results.json",
                   help="Where to save full per-trace results JSON")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--label", default=None,
                   help="Slack-block label (default: daily-eval-YYYY-MM-DD)")
    args = p.parse_args()

    _SIGTERM_REQUESTED.clear()
    if getattr(signal, "SIGTERM", None) is not None:
        signal.signal(signal.SIGTERM, _handle_sigterm)

    # 1. Load samples
    if args.samples:
        with open(args.samples) as f:
            samples = json.load(f)
        if not isinstance(samples, list):
            sys.exit("ERROR: samples file must be a JSON list")
        print(f"📂 Loaded {len(samples)} samples from {args.samples}")
    else:
        required = ["METABASE_URL", "METABASE_QUESTION_ID"]
        if not os.environ.get("METABASE_API_KEY"):
            required += ["METABASE_USERNAME", "METABASE_PASSWORD"]
        for k in required:
            if not os.environ.get(k):
                sys.exit(f"ERROR: {k} not set; either export it or use --samples PATH")
        samples = fetch_samples_from_metabase()

    n_metabase_pulled = len(samples)

    if not samples:
        print("⚠️  Zero samples to judge. Exiting cleanly.")
        yesterday_str = date.fromordinal(date.today().toordinal() - 1).isoformat()
        write_minimal_eval_snapshot(
            "/tmp/daily_eval_yesterday_summary.json",
            yesterday_str=yesterday_str,
            reason="zero_samples",
        )
        return 0

    # B10: Load yesterday's snapshot for WoW deltas (if it exists).
    # We load BEFORE judging today's run so we can pass it to render_slack_block.
    prev_snapshot_path = "/tmp/daily_eval_yesterday_summary.json"
    prev_snapshot: dict | None = None
    if os.path.exists(prev_snapshot_path):
        try:
            with open(prev_snapshot_path) as _f:
                prev_snapshot = json.load(_f)
            print(f"📈 Loaded previous snapshot from {prev_snapshot_path} "
                  f"(date={prev_snapshot.get('date', '?')}) for WoW deltas")
        except Exception as _e:
            print(f"⚠️  Could not load previous snapshot ({_e}). First-run mode.")
            prev_snapshot = None
    else:
        print(f"📈 No previous snapshot at {prev_snapshot_path}, first-run mode (no WoW deltas).")

    # Distribution by stratum
    by_strat: dict[str, int] = {}
    for s in samples:
        by_strat[s.get("stratum", "all")] = by_strat.get(s.get("stratum", "all"), 0) + 1
    print("📊 Sample distribution:")
    for k, v in sorted(by_strat.items()):
        print(f"     {k:<10} {v}")

    # Auto-resume: if a checkpoint exists for this judge_run_id, skip already-evaluated samples
    checkpoint_file = args.output + ".checkpoint"
    checkpoint_results: list[dict] = []
    already_done_ids: set[str] = set()
    if os.path.exists(checkpoint_file):
        try:
            with open(checkpoint_file) as _cf:
                checkpoint_results = json.load(_cf)
            already_done_ids = {r["_trace_id"] for r in checkpoint_results if r.get("_trace_id")}
            print(f"♻️  Checkpoint found: {len(checkpoint_results)} samples already evaluated. Skipping them.")
        except Exception as _e:
            print(f"⚠️  Could not load checkpoint ({_e}). Starting fresh.")
            checkpoint_results = []
            already_done_ids = set()

    if already_done_ids:
        before = len(samples)
        samples = [s for s in samples if s.get("trace_id") not in already_done_ids]
        print(f"♻️  {before - len(samples)} skipped (already done). {len(samples)} remaining to judge.")

    # 2. Judge loop
    yesterday = (date.today().toordinal() - 1)
    yesterday_str = date.fromordinal(yesterday).isoformat()
    judge_run_id = f"daily-eval-{yesterday_str}"
    write_scores = not args.no_write_scores
    judge_outcome = run_judge_loop(
        samples,
        judge_run_id=judge_run_id,
        write_scores=write_scores, model=args.model,
        checkpoint_path=args.output + ".checkpoint",
        checkpoint_prefix=checkpoint_results if checkpoint_results else None,
        stop_event=_SIGTERM_REQUESTED,
    )
    new_results = judge_outcome.new_results
    results = checkpoint_results + new_results  # full combined set for aggregation

    block = finalize_eval_run(
        output_path=args.output,
        results=results,
        new_results=new_results,
        checkpoint_results=checkpoint_results,
        judge_outcome=judge_outcome,
        n_sampled=n_metabase_pulled,
        yesterday_str=yesterday_str,
        judge_run_id=judge_run_id,
        prev_snapshot=prev_snapshot,
        prev_snapshot_path=prev_snapshot_path,
        label=args.label,
    )

    print("\n" + "=" * 60)
    print(block)
    print("=" * 60)

    # 5. Slack post
    if args.dry_run:
        print("(dry-run, skipping Slack post)")
        if (
            judge_outcome.stopped_reason == "complete"
            and os.path.exists(checkpoint_file)
        ):
            try:
                os.remove(checkpoint_file)
                print(f"🗑️  Checkpoint cleaned up: {checkpoint_file}")
            except Exception:
                pass
        return 0

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("⚠️  SLACK_WEBHOOK_URL not set. Block printed above only.")
        if (
            judge_outcome.stopped_reason == "complete"
            and os.path.exists(checkpoint_file)
        ):
            try:
                os.remove(checkpoint_file)
                print(f"🗑️  Checkpoint cleaned up: {checkpoint_file}")
            except Exception:
                pass
        return 0

    # Idempotency: skip the Slack post if we already posted for today's UTC
    # date. Cron retries on the same UTC day become no-ops. FORCE_REPOST=1
    # bypasses for debugging.
    if _eval_already_posted_today("eval-posted"):
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print(f"[info] already posted {today_utc}, skipping")
        if (
            judge_outcome.stopped_reason == "complete"
            and os.path.exists(checkpoint_file)
        ):
            try:
                os.remove(checkpoint_file)
                print(f"🗑️  Checkpoint cleaned up: {checkpoint_file}")
            except Exception:
                pass
        return 0

    # C1.3 / Locked Decision #6: Poster pipeline is the DEFAULT (single design,
    # single ship). The legacy text-only path is reachable only via:
    #   (a) POSTER_DISABLE=1 (reserved kill-switch for emergency rollback), or
    #   (b) the try/except fallback below when render or publish fails.
    # POSTER_DRY_RUN=1 renders but skips the gh-pages push.
    poster_disabled = os.environ.get("POSTER_DISABLE", "").strip() == "1"
    posted = False
    poster_error: Optional[str] = None
    if poster_disabled:
        poster_error = "cause=disabled"
    else:
        try:
            from scripts import poster_slack  # type: ignore
            # Reload today's snapshot from disk (finalize_eval_run wrote it).
            today_snap = {}
            try:
                with open(prev_snapshot_path) as _sf:
                    today_snap = json.load(_sf)
            except Exception as exc:
                poster_error = f"cause=snapshot reason={exc!r}"
            poster_input = poster_slack.build_scoreboard_poster_input(today_snap)
            date_str = today_snap.get("date") or date.today().isoformat()
            try:
                image_url = poster_slack.render_and_publish(
                    "scoreboard", poster_input, date_str
                )
            except Exception as exc:
                image_url = None
                poster_error = poster_error or f"cause=render reason={exc!r}"
            if image_url is None:
                poster_error = poster_error or (
                    "cause=render reason=render_and_publish returned None"
                )
            else:
                alt_text = poster_slack._alt_text_for(poster_input, "scoreboard")
                ops_stripe = _build_scoreboard_ops_stripe_text(today_snap)
                safety_stripe = _build_scoreboard_safety_stripe_text(today_snap)
                blocks: list = [
                    poster_slack.make_image_block(image_url, alt_text),
                    poster_slack.make_divider(),
                    poster_slack.make_section(
                        f"⚙️ *Ops* (yesterday)\n{ops_stripe}"
                    ),
                    poster_slack.make_section(
                        f"🛟 *Safety floor*\n{safety_stripe}"
                    ),
                    poster_slack.make_section("🧵 Full breakdown in thread"),
                    poster_slack.make_context(poster_slack.scoreboard_footer_links()),
                ]
                fallback_text = poster_input["headline"]
                try:
                    posted = poster_slack.post_blocks_to_slack(
                        webhook, blocks, fallback_text
                    )
                except Exception as exc:
                    posted = False
                    poster_error = poster_error or f"cause=publish reason={exc!r}"
                if posted:
                    # Thread-reply (~2s later) with the full text breakdown.
                    time.sleep(2)
                    thread_blocks = [
                        poster_slack.make_section(
                            f"🧵 *Deep dive · {date_str}*\n```{block}```"
                        )
                    ]
                    try:
                        poster_slack.post_blocks_to_slack(
                            webhook, thread_blocks, "thread"
                        )
                    except Exception as exc:
                        print(f"[warn] thread reply failed: {exc!r}", file=sys.stderr)
                elif poster_error is None:
                    poster_error = "cause=post reason=post_blocks_to_slack returned False"
        except Exception as exc:
            poster_error = poster_error or f"cause=render reason={exc!r}"
            print(f"[poster] [warn] pipeline failed: {exc!r}", file=sys.stderr)

    if poster_disabled or poster_error is not None or not posted:
        if poster_error:
            print(
                f"[poster] [warn] degraded {poster_error}",
                file=sys.stderr,
            )
        degraded_block = (
            "⚠️ Poster degraded (see workflow logs)\n\n" + block
        )
        try:
            posted = post_to_slack(webhook, degraded_block)
        except Exception as e:
            print(f"❌ Slack post failed: {e}")
            return 1
    if not posted:
        # post_to_slack already logged the reason (non-ok body, HTTP error,
        # local-run guard, etc.). Mirror digest behaviour: exit 1 so the
        # workflow surfaces a red run, and DO NOT write the idempotency
        # marker, that way a same-day re-dispatch can actually retry.
        print("❌ Slack post did not succeed (see warnings above).")
        return 1
    print("✅ Posted to Slack.")
    # Record successful post so a second invocation on this UTC day is a
    # no-op. Write AFTER post_to_slack returns True.
    _eval_write_posted_marker("eval-posted")

    if judge_outcome.stopped_reason == "complete" and os.path.exists(checkpoint_file):
        try:
            os.remove(checkpoint_file)
            print(f"🗑️  Checkpoint cleaned up: {checkpoint_file}")
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
