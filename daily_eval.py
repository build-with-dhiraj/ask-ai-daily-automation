"""
Daily eval orchestrator — runs every morning, posts a single Slack message
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

Sampling strategy (cost-optimised):
  • ALL of yesterday's downvoted-academic queries          (rating=0)
  • 10% random sample of yesterday's upvoted-academic       (rating=6)
  • 10% random sample of yesterday's no-vote academic       (rating IS NULL)
  • Hard caps: 1000 downvotes / 500 upvotes / 500 no-votes
  • Worst-case daily cost @ $0.005/call: ~$10/day = ~$3.6K/year

Required env (set in Cowork SKILL.md or shell before invoking):
  AZURE_ENDPOINT
  AZURE_API_KEY
  AZURE_API_VERSION
  DEPLOYMENT_NAME
  METABASE_URL                # e.g. https://metabase-prod.penpencil.co
  METABASE_API_KEY            # preferred — use for SSO accounts (X-Api-Key auth)
  METABASE_USERNAME           # fallback — only needed when METABASE_API_KEY is not set
  METABASE_PASSWORD           # fallback — only needed when METABASE_API_KEY is not set
  METABASE_QUESTION_ID        # the saved question id for daily_stratified_sample.sql
  LANGFUSE_PUBLIC_KEY         # optional — enables score writes + tracing
  LANGFUSE_SECRET_KEY         # optional
  LANGFUSE_HOST               # optional (default https://cloud.langfuse.com)
  SLACK_WEBHOOK_URL           # the incoming-webhook for the eval channel
                              # (separate from the existing digest channel,
                              # OR same channel — your call)

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
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

# Ensure judge_runner is importable when called from Cowork or cron
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from judge_runner import (  # noqa: E402
    aggregate, render_slack_block, call_judge, get_openai_client,
    validate_judge_output, write_judge_scores_to_langfuse,
    _get_langfuse_writer, DEFAULT_MODEL,
)


# ---------------------------------------------------------------------------
# Metabase fetch
# ---------------------------------------------------------------------------

def metabase_session_token(base_url: str, username: str, password: str,
                            timeout: float = 15.0) -> str:
    import urllib.request
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


def metabase_run_card(base_url: str, card_id: int, auth_header: dict,
                       timeout: float = 120.0) -> list[dict]:
    """Run a saved Metabase question and return rows as list[dict]."""
    import urllib.request
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

def post_to_slack(webhook_url: str, text: str, timeout: float = 15.0) -> None:
    import urllib.request
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        if body.strip() != "ok":
            print(f"⚠️  Slack webhook returned non-ok: {body}")


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


def run_judge_loop(samples: list[dict], judge_run_id: str, write_scores: bool,
                    model: str = DEFAULT_MODEL,
                    checkpoint_path: str | None = None) -> list[dict]:
    if not samples:
        return []
    client = get_openai_client()
    if write_scores:
        if _get_langfuse_writer() is None:
            print("⚠️  --write-scores requested but Langfuse keys missing; continuing without writes")
            write_scores = False
        else:
            print(f"📡 Writing scores to Langfuse (judge_run_id={judge_run_id})")

    results: list[dict] = []
    n = len(samples)
    n_scores = 0
    t_start = time.monotonic()
    for i, s in enumerate(samples, 1):
        tid = s.get("trace_id") or f"sample-{i}"
        stratum = s.get("stratum") or "all"
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
                added = write_judge_scores_to_langfuse(
                    production_trace_id=tid, parsed=parsed,
                    judge_run_id=judge_run_id,
                    judge_model=meta.get("model_param", ""),
                )
                n_scores += added
                tail = f"  +{added} scores"
            print(f"  [{i:>4}/{n}] {stratum:<10} {tid[:36]} {band}{tail}")
        except Exception as e:
            results.append({
                "_trace_id": tid,
                "_stratum": stratum,
                "_chapter": s.get("chapter") or "unknown",
                "_subject": s.get("subject") or "unknown",
                "_parse_error": True,
                "_error": str(e),
            })
            print(f"  [{i:>4}/{n}] {stratum:<10} {tid[:36]} ERROR: {e}")
            continue
        results.append(parsed)
        if checkpoint_path and i % 50 == 0:
            with open(checkpoint_path, "w") as _f:
                json.dump(results, _f)
            print(f"  💾 checkpoint saved ({i}/{n})")
    dur = time.monotonic() - t_start

    if write_scores:
        try:
            _get_langfuse_writer().flush()
            print(f"📡 Wrote {n_scores} Langfuse scores total. flush ok.")
        except Exception as e:
            print(f"📡 Langfuse flush warning: {e}")

    # Cost estimate (gpt-4.1 list price; Azure may differ)
    in_tok = sum((r.get("_meta") or {}).get("input_tokens") or 0 for r in results)
    out_tok = sum((r.get("_meta") or {}).get("output_tokens") or 0 for r in results)
    est_usd = in_tok * 2e-6 + out_tok * 8e-6
    print(f"⏱  Total run: {dur:.1f}s | tokens {in_tok}/{out_tok} | est ~${est_usd:.2f} (₹{est_usd*83:.0f})")
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
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

    if not samples:
        print("⚠️  Zero samples to judge. Exiting cleanly.")
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
        print(f"📈 No previous snapshot at {prev_snapshot_path} — first-run mode (no WoW deltas).")

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
    new_results = run_judge_loop(samples, judge_run_id=judge_run_id,
                                 write_scores=write_scores, model=args.model,
                                 checkpoint_path=args.output + ".checkpoint")
    results = checkpoint_results + new_results  # full combined set for aggregation

    # 3. Save full results
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"💾 Saved {len(results)} results to {args.output}")

    # 4. Render Slack block (B10: pass prev_snapshot for WoW deltas)
    summary = aggregate(results)
    label = args.label or judge_run_id
    block = render_slack_block(summary, run_label=label, results=results,
                                prev_snapshot=prev_snapshot)

    # B8: Per-stratum cost split footer.
    # Cost is computed from THIS run only (new_results); counts from all results.
    n_judged_total = len(results)
    n_judged_new = len(new_results)
    in_tok = sum((r.get("_meta") or {}).get("input_tokens") or 0 for r in new_results)
    out_tok = sum((r.get("_meta") or {}).get("output_tokens") or 0 for r in new_results)
    est_usd = in_tok * 2e-6 + out_tok * 8e-6

    # Stratum counts from ALL results (includes checkpoint)
    strata_counts: dict[str, int] = {}
    for r in results:
        st = r.get("_stratum") or "all"
        strata_counts[st] = strata_counts.get(st, 0) + 1

    # Per-stratum token + cost split (THIS run only — checkpoint rows have no fresh _meta cost)
    strata_cost_lines = []
    for st in sorted(strata_counts.keys()):
        st_rows = [r for r in new_results if (r.get("_stratum") or "all") == st]
        st_n = strata_counts[st]  # total count (incl. resumed)
        st_in = sum((r.get("_meta") or {}).get("input_tokens") or 0 for r in st_rows)
        st_out = sum((r.get("_meta") or {}).get("output_tokens") or 0 for r in st_rows)
        st_tokens = st_in + st_out
        st_cost = st_in * 2e-6 + st_out * 8e-6
        strata_cost_lines.append(
            f"     {st:<14}  n={st_n:<5}  tokens={st_tokens:>9,}   ~${st_cost:.2f}"
        )

    resumed_note = (
        f"   _(resumed, {n_judged_total - n_judged_new} from checkpoint — cost reflects new judgements only)_"
        if checkpoint_results else ""
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
        f"\n❓ *What is this?* <{one_pager}|Eval one-pager — thresholds, cost, Metabase Q33193>\n"
    )
    block = block + cost_footer

    # B10: Save today's snapshot for tomorrow's WoW deltas.
    try:
        n_j = summary.n_judgable or 1
        n_acc = sum(1 for r in results
                    if r.get("overall_band") in ("PASS", "NEUTRAL", "FAIL")
                    and not r.get("academic", {}).get("passed", True))
        exp_axials_for_snap = ("intent", "formatting", "pedagogy", "tone")
        n_exp = sum(1 for r in results
                    if r.get("overall_band") in ("PASS", "NEUTRAL", "FAIL")
                    and any(not r.get(ax, {}).get("passed", True)
                            for ax in exp_axials_for_snap))
        pass_pct_snap = round(100.0 * summary.n_pass / n_j, 1) if summary.n_judgable else 0.0
        neutral_pct_snap = round(100.0 * summary.n_neutral / n_j, 1) if summary.n_judgable else 0.0
        fail_pct_snap = round(100.0 * summary.n_fail / n_j, 1) if summary.n_judgable else 0.0
        acc_fail_pct_snap = round(100.0 * n_acc / n_j, 1) if summary.n_judgable else 0.0
        exp_fail_pct_snap = round(100.0 * n_exp / n_j, 1) if summary.n_judgable else 0.0

        # C12: chapter names with worst formatting FAIL rate (min 5 samples), top 5 — for digest cross-correlation
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
            "n_judgable": summary.n_judgable,
            "pass_pct": pass_pct_snap,
            "neutral_pct": neutral_pct_snap,
            "fail_pct": fail_pct_snap,
            "acc_fail_pct": acc_fail_pct_snap,
            "exp_fail_pct": exp_fail_pct_snap,
            "axial_fail_pct": dict(summary.axial_fail_pct),
            "formatting_hotspot_chapters": formatting_hotspot_chapters,
        }
        with open(prev_snapshot_path, "w") as _sf:
            json.dump(summary_snapshot, _sf, indent=2)
        print(f"📸 Saved today's snapshot to {prev_snapshot_path} (for tomorrow's WoW deltas)")
    except Exception as _e:
        print(f"⚠️  Could not save today's snapshot ({_e}). WoW deltas may be missing tomorrow.")

    print("\n" + "=" * 60)
    print(block)
    print("=" * 60)

    # 5. Slack post
    if args.dry_run:
        print("(dry-run — skipping Slack post)")
        # Clean up checkpoint on successful completion
        if os.path.exists(checkpoint_file):
            try:
                os.remove(checkpoint_file)
                print(f"🗑️  Checkpoint cleaned up: {checkpoint_file}")
            except Exception:
                pass
        return 0

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("⚠️  SLACK_WEBHOOK_URL not set. Block printed above only.")
        # Clean up checkpoint on successful completion
        if os.path.exists(checkpoint_file):
            try:
                os.remove(checkpoint_file)
                print(f"🗑️  Checkpoint cleaned up: {checkpoint_file}")
            except Exception:
                pass
        return 0

    try:
        post_to_slack(webhook, block)
        print("✅ Posted to Slack.")
    except Exception as e:
        print(f"❌ Slack post failed: {e}")
        return 1

    # Clean up checkpoint on successful completion
    if os.path.exists(checkpoint_file):
        try:
            os.remove(checkpoint_file)
            print(f"🗑️  Checkpoint cleaned up: {checkpoint_file}")
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
