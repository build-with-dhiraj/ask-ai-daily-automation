"""14-day rolling history for eval + digest snapshots.

The daily_eval and daily_digest writers persist a single "yesterday" snapshot
at `/tmp/daily_eval_yesterday_summary.json` (overwritten each run). To compute
the 14-day median column on the Variant D standings table, we need history.

This module appends each finalized snapshot to a JSON Lines file alongside the
single-file snapshot, then computes per-metric medians from the last 14 entries
when the poster renders.

Failure mode policy:
- All write paths swallow OSError to never crash the daily pipeline.
- Read paths return an empty dict and a "n/a" median when history is missing,
  rather than fabricating a number. The Variant D template tolerates "n/a".

History files (one per surface):
- `/tmp/daily_eval_history.jsonl`       (one snapshot per line)
- `/tmp/daily_digest_history.jsonl`     (one snapshot per line)

These paths are env-overridable so tests can isolate to tmp_path. The defaults
match the existing `/tmp/daily_eval_yesterday_summary.json` convention.

This module performs NO I/O at import time.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any, Optional


def _eval_history_path() -> Path:
    return Path(os.environ.get(
        "EVAL_HISTORY_PATH",
        "/tmp/daily_eval_history.jsonl",
    ))


def _digest_history_path() -> Path:
    return Path(os.environ.get(
        "DIGEST_HISTORY_PATH",
        "/tmp/daily_digest_history.jsonl",
    ))


def append_eval_snapshot(snapshot: dict) -> None:
    """Append today's eval snapshot to the rolling history JSONL file.

    Best-effort. Any OSError is logged and swallowed, never raised.
    """
    _append(snapshot, _eval_history_path())


def append_digest_snapshot(snapshot: dict) -> None:
    """Append today's digest snapshot to the rolling history JSONL file.

    Best-effort. Any OSError is logged and swallowed, never raised.
    """
    _append(snapshot, _digest_history_path())


def _append(snapshot: dict, path: Path) -> None:
    if not snapshot:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, default=str))
            f.write("\n")
    except OSError as exc:
        print(
            f"[snapshot_history] [warn] append to {path} failed: {exc!r}",
            file=sys.stderr,
        )


def _read_recent(path: Path, n: int = 14) -> list[dict]:
    """Read the last `n` JSONL records. Missing file => [].

    Dedupes by `date` field, keeping the most recent value per date so a
    same-day re-run does not skew the median.
    """
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    parsed: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except (ValueError, json.JSONDecodeError):
            continue
    # Dedupe by date (last write wins) so a same-day rerun overwrites that
    # day's row instead of double-counting it in the median.
    by_date: dict[str, dict] = {}
    for record in parsed:
        d = str(record.get("date") or "")
        if d:
            by_date[d] = record
        else:
            # No date field; key by a synthetic counter to keep it in the set.
            by_date[f"_anon_{len(by_date)}"] = record
    # Return chronological order (sorted by date string), most recent N.
    sorted_records = sorted(by_date.values(), key=lambda r: str(r.get("date") or ""))
    return sorted_records[-n:]


def eval_median(metric_key: str, n: int = 14) -> Optional[float]:
    """Return the median of `metric_key` from the last `n` eval snapshots.

    Returns None when fewer than 3 data points are available (refuse to
    pretend a median exists with one or two readings).
    """
    return _median(_eval_history_path(), metric_key, n)


def digest_median(metric_key: str, n: int = 14) -> Optional[float]:
    """Return the median of `metric_key` from the last `n` digest snapshots.

    Returns None when fewer than 3 data points are available.
    """
    return _median(_digest_history_path(), metric_key, n)


def _median(path: Path, metric_key: str, n: int) -> Optional[float]:
    records = _read_recent(path, n=n)
    values: list[float] = []
    for record in records:
        v = record.get(metric_key)
        if v is None:
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(values) < 3:
        return None
    return float(statistics.median(values))


def eval_series(metric_key: str, n: int = 14) -> list[float]:
    """Return the raw 14-day rolling series for `metric_key`.

    Variant D does NOT render a sparkline, but a future variant might, and
    the data layer should be wired so the swap is template-only. Returns
    [] when history is missing.
    """
    return _series(_eval_history_path(), metric_key, n)


def digest_series(metric_key: str, n: int = 14) -> list[float]:
    """Return the raw 14-day rolling series for a digest metric_key."""
    return _series(_digest_history_path(), metric_key, n)


def _series(path: Path, metric_key: str, n: int) -> list[float]:
    records = _read_recent(path, n=n)
    out: list[float] = []
    for record in records:
        v = record.get(metric_key)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out
