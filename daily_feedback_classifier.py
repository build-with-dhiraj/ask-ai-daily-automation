#!/usr/bin/env python3
"""
Daily feedback classifier — classifies free-text downvote comments into 11 categories.

Reads from Metabase via METABASE_FREETEXT_CARD_ID, classifies each non-empty
free_text_feedback row via Azure OpenAI, writes /tmp/daily_feedback_classifications.json.

Designed for fail-soft operation: missing config / API failures DO NOT block the
daily digest. The digest reads the snapshot only if present and valid.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# Reuse the Azure client factory to avoid duplicating credential/endpoint logic.
from judge_runner import get_openai_client


SNAPSHOT_PATH = "/tmp/daily_feedback_classifications.json"
PROMPT_PATH = "prompts/classifier_v1.md"
PROMPT_VERSION = "v1"

CATEGORIES: List[str] = [
    "Couldn't understand the question",
    "Incorrect / hallucinated answer",
    "Too long / verbose answer",
    "Poor / unclear explanation",
    "Took too much time (latency)",
    "Voice issues (TTS)",
    "Language / localization issues",
    "UI / App bugs",
    "Formatting issues (equations/formulas)",
    "Noise / gibberish / off-topic input",
    "Other",
]

NOISE_CATEGORY = "Noise / gibberish / off-topic input"
OTHER_CATEGORY = "Other"


# --- helpers ---------------------------------------------------------------


def _norm(s: str) -> str:
    """Normalise category text for comparison: lower, strip, unify apostrophes."""
    return s.strip().lower().replace("’", "'")


_CATEGORY_BY_NORM = {_norm(c): c for c in CATEGORIES}


def _yesterday_str() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _http_timeout_sec() -> float:
    raw = os.environ.get("FEEDBACK_CLASSIFIER_HTTP_TIMEOUT_SEC", "30")
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 30.0


def _concurrency() -> int:
    raw = os.environ.get("JUDGE_CONCURRENCY", "8")
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


def _load_prompt() -> str:
    repo_root = Path(__file__).resolve().parent
    return (repo_root / PROMPT_PATH).read_text(encoding="utf-8")


def _write_snapshot(snap: dict) -> None:
    """Always best-effort write. Never raises."""
    try:
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # pragma: no cover - filesystem rare-path
        print(f"[warn] failed to write snapshot: {exc!r}", file=sys.stderr)


def _empty_snapshot(stopped_reason: str) -> dict:
    return {
        "date": _yesterday_str(),
        "prompt_version": PROMPT_VERSION,
        "n_total": 0,
        "n_classified": 0,
        "n_errors": 0,
        "category_counts": {},
        "other_samples": [],
        "stopped_reason": stopped_reason,
    }


# --- Metabase fetch (mirrors daily_digest.fetch_metabase_card) -------------


def _fetch_metabase_rows(card_id: int) -> Tuple[Optional[List[dict]], Optional[str]]:
    base = os.environ.get("METABASE_URL", "").rstrip("/")
    api_key = os.environ.get("METABASE_API_KEY", "")
    if not base or not api_key:
        return None, "METABASE_URL or METABASE_API_KEY not set"
    url = f"{base}/api/card/{card_id}/query/json"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        data=b"{}",
    )
    try:
        with urllib.request.urlopen(req) as resp:  # no timeout — match daily_digest
            payload = resp.read()
        result = json.loads(payload)
        if isinstance(result, list):
            return result, None
        return None, "Metabase returned non-list JSON"
    except Exception as exc:
        return None, repr(exc)


def _filter_to_yesterday(rows: List[dict], yesterday: str) -> List[dict]:
    """Mirror daily_digest.fmt_downvote_dump: any string field starting with yesterday."""
    out = []
    for row in rows:
        for v in row.values():
            if isinstance(v, str) and v.startswith(yesterday):
                out.append(row)
                break
    return out


# --- Classification --------------------------------------------------------


def _classify_text(client, deployment: str, prompt: str, text: str) -> str:
    """Single-row classify via Azure. Returns a category string (may be unknown)."""
    resp = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        temperature=0,
        max_tokens=20,
    )
    return (resp.choices[0].message.content or "").strip()


def _coerce_category(raw: str) -> Tuple[str, bool]:
    """Returns (category, was_valid). Falls back to Other when not in known set."""
    key = _norm(raw)
    if key in _CATEGORY_BY_NORM:
        return _CATEGORY_BY_NORM[key], True
    return OTHER_CATEGORY, False


def classify_rows(
    rows: List[dict],
    *,
    client=None,
    deployment: Optional[str] = None,
    prompt: Optional[str] = None,
    max_workers: Optional[int] = None,
) -> dict:
    """Classify a list of row dicts. Pure-function path used by tests + main."""
    yesterday = _yesterday_str()
    snap = _empty_snapshot("complete")
    snap["n_total"] = len(rows)

    if not rows:
        return snap

    if prompt is None:
        prompt = _load_prompt()
    if deployment is None:
        deployment = os.environ.get("DEPLOYMENT_NAME") or ""
    if max_workers is None:
        max_workers = _concurrency()

    # Pre-filter empty + short rows.
    work: List[Tuple[int, dict, str]] = []  # (idx, row, text)
    preclassified: List[Tuple[int, dict, str]] = []  # (idx, row, category)
    for i, row in enumerate(rows):
        text = (row.get("free_text_feedback") or "").strip()
        if not text:
            continue
        if len(text) < 2:
            preclassified.append((i, row, NOISE_CATEGORY))
            continue
        work.append((i, row, text))

    counts: dict = {}
    other_samples: List[dict] = []
    n_errors = 0
    n_classified = 0

    def _record(row: dict, category: str) -> None:
        nonlocal n_classified
        counts[category] = counts.get(category, 0) + 1
        n_classified += 1
        if category == OTHER_CATEGORY and len(other_samples) < 5:
            other_samples.append(
                {
                    "aiintentid": str(row.get("aiintentid") or ""),
                    "subject": str(row.get("subject") or ""),
                    "chapter": str(row.get("chapter") or ""),
                    "free_text": (row.get("free_text_feedback") or "").strip(),
                }
            )

    for _, row, cat in preclassified:
        _record(row, cat)

    if work:
        if client is None:
            client = get_openai_client()

        def _job(idx_row_text):
            idx, row, text = idx_row_text
            try:
                raw = _classify_text(client, deployment, prompt, text)
                cat, ok = _coerce_category(raw)
                if not ok:
                    print(
                        f"[warn] row {idx}: unknown category {raw!r} → Other",
                        file=sys.stderr,
                    )
                return idx, row, cat, None
            except Exception as exc:
                return idx, row, None, exc

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_job, item) for item in work]
            for fut in as_completed(futures):
                idx, row, cat, exc = fut.result()
                if exc is not None:
                    n_errors += 1
                    print(f"[warn] row {idx}: classify failed: {exc!r}", file=sys.stderr)
                    continue
                _record(row, cat)

    if n_classified == 0 and n_errors > 0:
        snap["stopped_reason"] = "all_errors"

    snap["n_classified"] = n_classified
    snap["n_errors"] = n_errors
    snap["category_counts"] = {k: v for k, v in counts.items() if v > 0}
    snap["other_samples"] = other_samples[:5]
    snap["date"] = yesterday
    return snap


# --- main ------------------------------------------------------------------


def main() -> int:
    card_id_raw = (os.environ.get("METABASE_FREETEXT_CARD_ID") or "").strip()
    if not card_id_raw:
        print(
            "[info] METABASE_FREETEXT_CARD_ID not set; skipping classification",
            file=sys.stderr,
        )
        _write_snapshot(_empty_snapshot("no_metabase_card"))
        return 0
    try:
        card_id = int(card_id_raw)
    except ValueError:
        print(
            f"[warn] METABASE_FREETEXT_CARD_ID is not digits-only ({card_id_raw!r}); skipping",
            file=sys.stderr,
        )
        _write_snapshot(_empty_snapshot("no_metabase_card"))
        return 0

    started = time.time()
    print(f"[info] fetching Metabase card {card_id}", file=sys.stderr)
    rows, err = _fetch_metabase_rows(card_id)
    if err is not None:
        print(f"[warn] Metabase fetch failed: {err}", file=sys.stderr)
        _write_snapshot(_empty_snapshot("metabase_fetch_failed"))
        return 0

    yesterday = _yesterday_str()
    filtered = _filter_to_yesterday(rows or [], yesterday)
    print(
        f"[info] {len(filtered)}/{len(rows or [])} rows match yesterday={yesterday}",
        file=sys.stderr,
    )

    try:
        snap = classify_rows(filtered)
    except Exception as exc:
        # Catastrophic: still write a snapshot so the digest's read is safe.
        print(f"[error] classify_rows crashed: {exc!r}", file=sys.stderr)
        emergency = _empty_snapshot("all_errors")
        emergency["n_total"] = len(filtered)
        _write_snapshot(emergency)
        return 1

    _write_snapshot(snap)
    print(
        f"[info] classified n={snap['n_classified']} errors={snap['n_errors']} "
        f"in {time.time() - started:.1f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
