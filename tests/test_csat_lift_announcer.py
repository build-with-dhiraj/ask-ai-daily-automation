"""Tests for scripts/csat_lift_announcer.py.

Run with:
    pytest tests/test_csat_lift_announcer.py

No network calls. Dry-run mode is the only path exercised; the live POST path
is gated behind DRY_RUN != "true" and is not unit-tested by design.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

# Repo root is the parent of tests/. Add scripts/ to import path.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import csat_lift_announcer as announcer  # noqa: E402


def test_payload_is_well_formed_block_kit() -> None:
    """Slack payload must have a top-level text fallback and a blocks list."""
    payload = announcer.build_payload()
    assert isinstance(payload, dict)
    assert "text" in payload and isinstance(payload["text"], str) and payload["text"]
    assert "blocks" in payload and isinstance(payload["blocks"], list)
    assert len(payload["blocks"]) > 0

    valid_block_types = {"header", "section", "divider", "context"}
    for i, block in enumerate(payload["blocks"]):
        assert "type" in block, f"block {i} missing type"
        assert block["type"] in valid_block_types, f"block {i} unexpected type {block['type']}"
        if block["type"] == "section":
            assert "text" in block, f"section block {i} missing text"
            assert block["text"]["type"] in {"mrkdwn", "plain_text"}
            assert block["text"]["text"], f"section block {i} has empty text"
        if block["type"] == "header":
            assert block["text"]["type"] == "plain_text"


def test_payload_serialises_to_json() -> None:
    """Slack requires JSON-serialisable payload."""
    payload = announcer.build_payload()
    serialised = json.dumps(payload)
    # Round-trip
    assert json.loads(serialised) == payload


def test_payload_contains_key_phrases() -> None:
    """Lock in the headline numbers + findings so a regression is obvious."""
    payload = announcer.build_payload()
    flat = json.dumps(payload)

    expected_phrases = [
        "15,114",
        "2,889",
        "591 downvoted",
        "2,298 upvoted",
        "NEUTRAL drives downvotes",
        "Too long",
        "31% of downvoted traces are SME=PASS",
        "605 upvotes on academically wrong answers",
        "csat-lift-staging.yml",
    ]
    for phrase in expected_phrases:
        assert phrase in flat, f"missing expected phrase in payload: {phrase!r}"


def test_dry_run_prints_payload_and_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """DRY_RUN=true must print JSON payload, never need the webhook, and exit 0."""
    monkeypatch.setenv("DRY_RUN", "true")
    # Deliberately unset the webhook to prove dry-run path doesn't read it.
    monkeypatch.delenv("SLACK_WEBHOOK_URL_STAGING", raising=False)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = announcer.main()

    assert rc == 0
    output = buf.getvalue()
    assert "DRY RUN: no actual post made" in output
    assert "15,114" in output
    assert "NEUTRAL drives downvotes" in output


def test_missing_webhook_in_post_mode_returns_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No webhook + DRY_RUN!=true must fail loudly with exit 1, no POST attempted."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("SLACK_WEBHOOK_URL_STAGING", raising=False)

    rc = announcer.main()
    captured = capsys.readouterr()

    assert rc == 1
    assert "SLACK_WEBHOOK_URL_STAGING" in captured.err
