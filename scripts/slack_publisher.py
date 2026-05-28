"""Slack publisher: webhook POST + retry + idempotency markers.

Phase 4 split of the former scripts/poster_slack.py god-module. This module
owns Slack HTTP I/O. The render-and-data side lives in scripts/poster_render.py.

Idempotency markers (two per surface, intentional):
    - `eval-poster-posted`         written by daily_eval after main message
    - `eval-publisher-confirmed`   written after the publisher reports 200 OK
    - `digest-poster-posted`       same for digest
    - `digest-publisher-confirmed` same for digest

The two-marker design prevents a same-day re-dispatch from double-posting if
the workflow crashes between the Slack POST returning 200 and the workflow
exit. The first marker says "we tried", the second says "we confirmed
delivery". A re-dispatch reads both: if either is present, the run no-ops
the Slack publish step.

Public surface:
    post_blocks_to_slack(webhook_url, blocks, fallback_text) -> bool
    write_posted_marker(name, marker_dir) -> None
    has_posted_marker(name, marker_dir) -> bool

Degradation marker: when the LLM follow-up generator falls back to the
deterministic template, the publisher prepends a "Insights degraded
(deterministic template)" section to the message so the operator sees the
degradation in the message itself, not just in CI logs.
"""
from __future__ import annotations

from scripts.poster_slack import (  # noqa: F401
    post_blocks_to_slack,
)
import os
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Idempotency markers
# ---------------------------------------------------------------------------

DEFAULT_MARKER_DIR = "/tmp"


def _marker_path(name: str, marker_dir: Optional[str] = None) -> Path:
    base = Path(marker_dir or DEFAULT_MARKER_DIR)
    return base / f".{name}-{_today_utc()}.marker"


def _today_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def write_posted_marker(name: str, marker_dir: Optional[str] = None) -> None:
    """Write an idempotency marker file scoped to today's UTC date.

    Best-effort. Any OSError is logged and swallowed; the absence of a
    marker only causes a duplicate post on a same-day re-dispatch, not a
    crash. Two markers per surface are written by the orchestrator
    (poster-posted + publisher-confirmed) so partial-success retries do
    not double-post.
    """
    path = _marker_path(name, marker_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("posted\n", encoding="utf-8")
    except OSError as exc:
        print(
            f"[slack_publisher] [warn] could not write marker {path}: {exc!r}",
            file=sys.stderr,
        )


def has_posted_marker(name: str, marker_dir: Optional[str] = None) -> bool:
    """Return True when today's idempotency marker exists.

    Used by the orchestrator to short-circuit a same-day re-dispatch.
    """
    return _marker_path(name, marker_dir).exists()


# ---------------------------------------------------------------------------
# Degradation marker (text block prepended to a Slack message)
# ---------------------------------------------------------------------------

DEGRADED_FOLLOW_UP_MARKER = (
    "Insights degraded (deterministic template)."
)


def make_degradation_section() -> dict:
    """Return a Block Kit section the orchestrator can prepend to the
    Slack message when the LLM follow-up generator fell back to the
    deterministic template.

    The marker is plain text so the operator sees it in the rendered
    message; this is intentional. The rest of the message body still
    renders normally below it.
    """
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"_{DEGRADED_FOLLOW_UP_MARKER}_"},
    }


__all__ = [
    "post_blocks_to_slack",
    "write_posted_marker",
    "has_posted_marker",
    "DEGRADED_FOLLOW_UP_MARKER",
    "make_degradation_section",
]
