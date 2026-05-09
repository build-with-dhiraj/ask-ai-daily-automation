#!/usr/bin/env python3
"""Monday golden-set smoke test — re-run v8 judge on SME-locked traces; fail if overall_band flips."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from judge_runner import DEFAULT_MODEL, call_judge, get_openai_client, validate_judge_output


def _sample_from_golden(entry: dict) -> dict:
    return {
        "trace_id": entry.get("trace_id") or "golden-unknown",
        "stratum": "golden",
        "doubt": str(entry.get("doubt") or ""),
        "ai_answer": str(entry.get("ai_answer") or ""),
        "transcript": str(entry.get("transcript") or ""),
        "ideal_answer": str(entry.get("ideal_answer") or ""),
        "subject": str(entry.get("subject") or ""),
        "chapter": str(entry.get("chapter") or ""),
        "student_class": str(entry.get("student_class") or ""),
        "exam": str(entry.get("exam") or ""),
        "image_url": str(entry.get("image_url") or ""),
        "is_annotated": bool(entry.get("is_annotated", False)),
    }


def main() -> int:
    path = Path(os.environ.get("GOLDEN_SET_PATH", SCRIPT_DIR / "golden_set.json"))
    model = os.environ.get("DEPLOYMENT_NAME", DEFAULT_MODEL)
    with open(path) as f:
        cases: list[dict] = json.load(f)
    client = get_openai_client()
    mismatches: list[tuple[str, str, str]] = []
    ran = 0
    for c in cases:
        if c.get("enabled") is False:
            continue
        if not (c.get("doubt") or "").strip() or not (c.get("ai_answer") or "").strip():
            continue
        label = c.get("label") or c.get("trace_id") or "?"
        exp = c.get("expected_overall_band")
        sample = _sample_from_golden(c)
        parsed, _meta = call_judge(client, sample, model=model)
        v = validate_judge_output(parsed)
        got = parsed.get("overall_band")
        if not v.ok:
            mismatches.append((label, str(exp), f"INVALID_JUDGE_OUTPUT: {v.errors}"))
            continue
        ran += 1
        if got != exp:
            mismatches.append((label, str(exp), str(got)))
    print(f"[golden-smoke] evaluated {ran} enabled cases with model={model}")
    if mismatches:
        print("[golden-smoke] FAIL — verdict drift:", file=sys.stderr)
        for label, exp, got in mismatches:
            print(f"  • {label}: expected {exp}, got {got}", file=sys.stderr)
        return 1
    print("[golden-smoke] OK — no SME baseline mismatches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
