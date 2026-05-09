#!/usr/bin/env python3
"""Smoke-test Metabase cards used by daily_digest (same POST as daily_digest.py).

Usage:
  export METABASE_URL=https://metabase.example.com
  export METABASE_API_KEY=...
  python3 scripts/test_metabase_digest_cards.py

Exit 0 if every card returns a list; non-zero if any request fails.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def run_card(base_url: str, api_key: str, card_id: int, timeout: float | None = 300.0) -> list:
    url = f"{base_url.rstrip('/')}/api/card/{card_id}/query/json"
    req = urllib.request.Request(
        url,
        data=json.dumps({}).encode(),
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    if not isinstance(body, list):
        raise RuntimeError(f"Expected list, got {type(body)}")
    return body


def main() -> int:
    base = (os.environ.get("METABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("METABASE_API_KEY") or "").strip()
    if not base or not key:
        print(
            "Set METABASE_URL and METABASE_API_KEY, then re-run.",
            file=sys.stderr,
        )
        return 1

    cards: list[tuple[int, str]] = [
        (24973, "core academic reasons"),
        (24974, "core non-academic reasons"),
        (23036, "core downvote dump"),
        (33285, "stream_logs digest"),
        (33282, "behaviour follow-up burst"),
        (33283, "behaviour rephrase keywords"),
    ]

    env_stream = os.environ.get("METABASE_STREAM_LOGS_CARD_ID", "").strip()
    env_follow = os.environ.get("METABASE_BEHAVIOR_FOLLOWUP_CARD_ID", "").strip()
    env_reph = os.environ.get("METABASE_BEHAVIOR_REPHRASE_CARD_ID", "").strip()
    if env_stream.isdigit():
        cards = [(cid, lab) for cid, lab in cards if lab != "stream_logs digest"]
        cards.append((int(env_stream), "stream_logs digest (from env)"))
    if env_follow.isdigit():
        cards = [(cid, lab) for cid, lab in cards if lab != "behaviour follow-up burst"]
        cards.append((int(env_follow), "behaviour follow-up (from env)"))
    if env_reph.isdigit():
        cards = [(cid, lab) for cid, lab in cards if lab != "behaviour rephrase keywords"]
        cards.append((int(env_reph), "behaviour rephrase (from env)"))

    # Dedupe by card id while keeping order
    seen: set[int] = set()
    ordered: list[tuple[int, str]] = []
    for cid, lab in cards:
        if cid not in seen:
            seen.add(cid)
            ordered.append((cid, lab))
    cards = ordered

    failed = False
    for cid, label in cards:
        try:
            rows = run_card(base, key, cid)
            print(f"OK   card {cid} ({label}): {len(rows)} rows")
        except Exception as exc:
            failed = True
            print(f"FAIL card {cid} ({label}): {exc!r}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
