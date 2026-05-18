"""CSAT Lift announcer.

One-shot Slack announcement of the SME-audit vs production CSAT correlation
findings. Posts to a staging Slack channel via webhook (Block Kit payload).

Runs in parallel isolation from daily_eval.py / judge_runner.py — no shared
state, no shared secrets beyond `SLACK_WEBHOOK_URL_TEST`.

Env vars:
    SLACK_WEBHOOK_URL_TEST   Slack incoming webhook URL (required unless DRY_RUN=true)
    DRY_RUN                     "true" prints payload and exits 0; anything else POSTs

Exit codes:
    0   success (posted or dry-run printed)
    1   missing webhook in non-dry-run mode
    2   non-2xx HTTP response from Slack
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import requests


def build_blocks() -> list[dict]:
    """Return the Slack Block Kit blocks for the CSAT-lift findings message.

    Kept verbose and verbatim so reviewers can diff the rendered text against
    the canonical message Dhiraj posted to #chakra-ai-product-ds.
    """
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "FYI: SME verdicts vs production CSAT, what we found",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":bar_chart: *FYI: SME verdicts vs production CSAT, what we found*\n\n"
                    "*Context (for anyone new to this):*\n\n"
                    "• *15,114 SME-audited production traces* across JEE Physics, Chem, Maths\n"
                    "• *2,889 of these carry explicit student feedback*: 591 downvoted, 2,298 upvoted\n"
                    "• The remaining 12,225 had no student rating attached\n"
                    "• We checked whether SME judgment aligns with student satisfaction\n"
                    "• *How SMEs score each trace (per the GC-SOP rubric)*:\n"
                    "    ◦ *PASS*: No issues found across all 5 axials (Academic, Intent, Presentation, Pedagogy, Look & Feel)\n"
                    "    ◦ *NEUTRAL*: Academic is correct, but Experience axials have issues (length, tone, structure, pedagogy)\n"
                    "    ◦ *FAIL*: ANY academic correctness error (calculation, conceptual, OCR, misunderstanding, incomplete) auto-triggers FAIL"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*3 findings worth knowing*\n\n"
                    "*(1) NEUTRAL drives downvotes more than FAIL*\n"
                    "> *What:* 35% of student downvotes are SME=NEUTRAL (academically correct, experience issue). Only 6% of upvotes are NEUTRAL.\n"
                    "> *Why it matters:* Students mostly downvote on experience problems, not academic errors. Our judge has to catch these.\n\n"
                    "*(2) \"Too long\" does NOT predict CSAT (counterintuitive)*\n"
                    "> *What:* \"Too long\" is the single most-marked code by SMEs (612 fires across the corpus, ~5x the next most common code). But it fires on 102 upvoted and 25 downvoted answers, roughly equal rates.\n"
                    "> *Why it matters:* The issue SMEs flag the most is not what makes students unhappy. We should stop optimizing the judge for length-based codes, and possibly revisit whether \"Too long\" deserves its current weight in the SME rubric itself.\n\n"
                    "*(3) 31% of downvoted traces are SME=PASS*\n"
                    "> *What:* Almost a third of downvoted answers are ones SMEs marked as correct.\n"
                    "> *Why it matters:* This is subjective dissatisfaction the rubric does not capture. Even a perfect rubric-aligned judge would miss this chunk of CSAT signal."
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Codes that ACTUALLY drive downvotes (high lift over upvotes):*\n\n"
                    "• Ambiguous student query (intent)\n"
                    "• Minor details missing (look_feel)\n"
                    "• Answer Incomplete (academic)\n"
                    "• Misunderstood by AI (academic)\n"
                    "• Steps not structured (presentation)\n"
                    "• Conceptual error (academic)"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*What this means for the eval work:*\n"
                    "1. We are building an AI judge that predicts when students will be unhappy with an answer. Until now, the default plan was to train it on whatever issues SMEs flag most often.\n"
                    "2. Instead of training the judge on the most common SME codes, we should train it on the codes that fire more on downvoted answers than upvoted ones.\n"
                    "3. *One important caveat:*\n"
                    "> *Academic correctness* stays non-negotiable. We saw *605 upvotes on academically wrong answers*, which means students often cannot catch subtle errors themselves.\n"
                    "> For Ask AI, shipping incorrect math, calculations, or concepts is a learning-outcome failure even if students do not complain.\n"
                    "> So the judge must continue to catch academic errors as a hard safety floor, alongside the CSAT-driving codes."
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "_Posted from `csat-lift-staging.yml` workflow • staging channel test • not yet running on production data_"
                    ),
                }
            ],
        },
    ]


def build_payload() -> dict:
    """Wrap blocks in the Slack incoming-webhook payload envelope."""
    return {
        "text": "FYI: SME verdicts vs production CSAT, what we found",
        "blocks": build_blocks(),
    }


def main() -> int:
    payload = build_payload()
    dry_run = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

    if dry_run:
        print(json.dumps(payload, indent=2))
        print("\nDRY RUN: no actual post made")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(
                    f"### CSAT Lift Announcer (DRY RUN)\n\n"
                    f"No Slack post was made. Payload size: "
                    f"{len(json.dumps(payload))} bytes.\n"
                )
        return 0

    webhook = os.environ.get("SLACK_WEBHOOK_URL_TEST", "").strip()
    if not webhook:
        print(
            "ERROR: SLACK_WEBHOOK_URL_TEST is not set and DRY_RUN != 'true'. "
            "Add the secret in GitHub repo Settings -> Secrets and variables -> Actions, "
            "or rerun with DRY_RUN=true to print the payload locally.",
            file=sys.stderr,
        )
        return 1

    resp = requests.post(
        webhook,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if not (200 <= resp.status_code < 300):
        # Defense-in-depth: log only status + body snippet, never `resp` itself
        # or `resp.request.url` (which would leak the webhook URL via traceback).
        status = resp.status_code
        body_snippet = resp.text[:500]
        print(
            f"ERROR: Slack webhook returned non-2xx status {status}: {body_snippet}",
            file=sys.stderr,
        )
        return 2

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"Posted successfully to staging Slack at {ts}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(
                f"### CSAT Lift Announcer\n\n"
                f"Posted to staging Slack at {ts}.\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
