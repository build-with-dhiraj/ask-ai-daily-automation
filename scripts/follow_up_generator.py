"""LLM-driven conversational follow-up text for the Slack message body.

Phase 4 of the redesign cycle. Sits below the poster image in the same
Block Kit message. Plain English, day-to-day talking language, 4 to 7
short insights, acronyms expanded on first use, no em-dashes, verdict
sentence first.

Public surface:
    generate_follow_up(surface, snapshot, *, breach=False) -> FollowUp
    expand_acronyms_first_use(text) -> str
    breach_mention_prefix(breach_signal) -> str
    VERDICT_OPENING_RE (regex for the locked first-sentence shape)

Failure model (locked Phase 4 decision):
    - 3 retries x 30s timeout = up to ~90s wall clock.
    - On final failure: deterministic fallback synthesized from snapshot,
      `degraded=True` flagged on the returned dict, caller prepends the
      degradation marker to the Slack message.
    - No exception escapes to the caller; the deterministic fallback is
      the contract. Daily pipeline never crashes on LLM failure.

This module performs NO I/O at import time.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Verdict-opening regex (CI-checked by tests/test_verdict_opening_line.py)
# ---------------------------------------------------------------------------

# The follow-up text MUST start with one of two locked shapes:
#   "Top risk: ..."
#   "No urgent risks today, ..."
# Trailing whitespace tolerated. Case-sensitive: the verdict is structural.
VERDICT_OPENING_RE = re.compile(
    r"^(Top risk:\s|No urgent risks today,\s)",
)


# ---------------------------------------------------------------------------
# Acronym expansion table (first occurrence only)
# ---------------------------------------------------------------------------

# Each pair: (short, long-form on first use). Order matters: "VCP" before
# "CSAT" so a sentence containing both expands both.
_ACRONYMS: list[tuple[str, str]] = [
    ("TTFT", "TTFT (time to first token)"),
    ("VCP",  "VCP (Video Co-Pilot)"),
    ("CSAT", "CSAT (customer satisfaction)"),
    ("SLO",  "SLO (service-level objective)"),
    ("RPS",  "RPS (requests per second)"),
    # "pp" expands to "percentage points" on its first occurrence as a
    # standalone token (e.g. "+1.6pp"). The CI lint tolerates "pp" inside
    # a percentage-point span if "percentage points" appeared earlier in
    # the same message.
]


def expand_acronyms_first_use(text: str) -> str:
    """Expand each acronym on its first occurrence in `text` only.

    Subsequent occurrences in the same string stay as the short form.
    Idempotent if the long form is already present (won't double-expand).
    """
    if not text:
        return text
    out = text
    for short, long_form in _ACRONYMS:
        # Word-boundary match so VCP doesn't gobble VCPlate or similar.
        pattern = re.compile(rf"\b{re.escape(short)}\b")
        # If the long form is already present anywhere in the text, treat
        # the acronym as "already expanded once" and leave it alone.
        if long_form.split(" (")[0] + " (" in out:
            continue
        # Replace only the first occurrence.
        out = pattern.sub(long_form, out, count=1)
    # "pp" -> "percentage points (pp)" on first occurrence as a suffix.
    pp_pattern = re.compile(r"(\d)pp\b")
    if pp_pattern.search(out) and "percentage points" not in out:
        out = pp_pattern.sub(
            lambda m: f"{m.group(1)} percentage points",
            out,
            count=1,
        )
    return out


# ---------------------------------------------------------------------------
# Breach @-mention prefix (per axis-to-owner mapping in PRODUCT.md)
# ---------------------------------------------------------------------------

# Axis identifier -> list of Slack user IDs (resolved per PRODUCT.md).
_AXIS_OWNERS: dict[str, list[str]] = {
    "academic":  ["U03P01CHELQ", "U091F0LPG7Q"],   # Naresh + Deepesh (DS)
    "experience":["U03P01CHELQ", "U091F0LPG7Q"],
    "downvote":  ["U03P01CHELQ", "U091F0LPG7Q"],
    "multiturn": ["U03P01CHELQ", "U091F0LPG7Q"],
    "rephrase":  ["U03P01CHELQ", "U091F0LPG7Q"],
    "vcp":       ["U05D4FS3HB2"],                  # Ankita (Backend)
    "cost":      ["U05D4FS3HB2"],
    "latency":   ["U05D4FS3HB2"],
    "langfuse":  ["U05D4FS3HB2"],
    "ui_bug":    ["U085FBH4Q8Y", "U03NCBHSUAZ", "U039CQ75QGY"],  # Pankaj+Tarun+Vishal
    "app_bug":   ["U085FBH4Q8Y", "U03NCBHSUAZ", "U039CQ75QGY"],
    "test":      ["U05G8P8CGTH"],                  # Prince (QA)
    "wildcard":  [],  # falls through to Dhiraj (orchestrator) below
}


def breach_mention_prefix(breach_signal: Optional[str]) -> str:
    """Return a leading Slack <@USERID> string for a breach signal.

    `breach_signal` is one of the keys in `_AXIS_OWNERS`; unknown signals
    fall through to the wildcard route (orchestrator). Returns an empty
    string when `breach_signal` is None or empty (no @-mention prepended).

    The trailing space lets the caller concatenate directly with the
    verdict sentence:  prefix + " " + verdict.
    """
    if not breach_signal:
        return ""
    owners = _AXIS_OWNERS.get(str(breach_signal).lower(), [])
    if not owners:
        # Unrouted breach: ping the orchestrator (Dhiraj) so it's never
        # silently dropped. Orchestrator's Slack ID is overridable via env
        # for staging / fork deploys.
        wildcard = os.environ.get("BREACH_WILDCARD_OWNER", "").strip()
        if wildcard:
            owners = [wildcard]
        else:
            return ""
    return " ".join(f"<@{uid}>" for uid in owners) + " "


# ---------------------------------------------------------------------------
# FollowUp return shape
# ---------------------------------------------------------------------------

@dataclass
class FollowUp:
    """Conversational follow-up text + status flags for the Slack body."""

    text: str
    insights: list[str] = field(default_factory=list)
    degraded: bool = False
    reason: Optional[str] = None  # populated when degraded=True

    def as_block_kit_section(self) -> dict:
        """Render as a Block Kit section block, ready for the Slack payload."""
        return {
            "type": "section",
            "text": {"type": "mrkdwn", "text": self.text},
        }


# ---------------------------------------------------------------------------
# System prompt for the LLM call
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You write the morning conversational follow-up for the Ask AI eval pipeline at PhysicsWallah. Your output sits below a poster image in the same Slack message.

HARD RULES (enforced by automated post-process checks):

1. First sentence MUST start with EXACTLY one of these two openings:
   - "Top risk: " (when something is off-trend or breached)
   - "No urgent risks today, " (when all watch metrics are inside their bands)
   No other openings are permitted. Do not start with "Yesterday", "TLDR", or any other phrasing.

2. 4 to 7 short insights after the opening sentence, each:
   - One sentence, ending with a period.
   - Contains a number AND a comparison anchor (vs the 14-day median, vs yesterday, vs the watch line).
   - Plain English, day-to-day talking language. Numbers wrapped in prose.

3. Acronyms (TTFT, VCP, CSAT, SLO, RPS, pp) expanded on first occurrence only. After the first expansion, the short form is fine.

4. NO em-dashes. NO en-dashes. Use commas, semicolons, or sentence breaks.

5. No marketing language. No "exciting", "leveraging", "robust", "world-class". No emojis.

6. Voice anchors (few-shot examples of the right register):

   FiveThirtyEight: "The judge agreement rate climbed to 84% this week, up from 79% a week ago and the highest reading since the panel was re-calibrated in March."

   Stratechery: "The interesting thing about today's run is not the headline accuracy number, which is fine, but the shape of the failures."

   Construction Physics: "Throughput on the eval pipeline this week was about 1,420 traces per day, roughly 3.2x what we managed in the first week of April."

Output format: plain text, no JSON, no markdown headers. Just the verdict sentence followed by the insights.
"""


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def generate_follow_up(
    surface: str,
    snapshot: dict,
    *,
    breach: bool = False,
    breach_signal: Optional[str] = None,
    timeout_sec: int = 30,
    retries: int = 3,
    retry_gap_sec: int = 0,  # the 30s retry gap is the locked spec; tests
                              # override to 0 so the suite runs in seconds.
) -> FollowUp:
    """Generate the conversational follow-up text for a surface.

    Flow:
        1. Build the LLM prompt from snapshot.
        2. Attempt the LLM call up to `retries` times with `timeout_sec`
           per attempt; gap of `retry_gap_sec` between attempts. Default
           retry_gap_sec=0 so the unit test suite stays fast; production
           callers pass retry_gap_sec=30 per the locked spec.
        3. If the LLM returns text and the verdict regex passes, return it.
        4. Otherwise fall back to the deterministic builder.

    Never raises. On any LLM failure, returns a deterministic-fallback
    FollowUp with degraded=True and a `reason` string.
    """
    deterministic = _build_deterministic_follow_up(
        surface=surface, snapshot=snapshot, breach=breach,
    )
    prefix = breach_mention_prefix(breach_signal) if breach else ""

    last_reason: Optional[str] = None
    for attempt in range(max(1, retries)):
        try:
            text = _call_llm(
                surface=surface,
                snapshot=snapshot,
                breach=breach,
                timeout_sec=timeout_sec,
            )
            if not text:
                last_reason = "empty response"
                if attempt < retries - 1:
                    time.sleep(retry_gap_sec)
                continue
            text = expand_acronyms_first_use(text)
            if not VERDICT_OPENING_RE.match(text):
                last_reason = (
                    "verdict opening regex failed: "
                    f"first 60 chars = {text[:60]!r}"
                )
                if attempt < retries - 1:
                    time.sleep(retry_gap_sec)
                continue
            # The LLM passed all gates. Prepend breach mentions if any.
            return FollowUp(
                text=(prefix + text).strip(),
                insights=_extract_insight_lines(text),
                degraded=False,
                reason=None,
            )
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            ConnectionError,
            ValueError,
            RuntimeError,
        ) as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(retry_gap_sec)
            continue
        except Exception as exc:  # noqa: BLE001 - last-line defense
            last_reason = (
                f"unexpected {type(exc).__name__}: {exc}"
            )
            if attempt < retries - 1:
                time.sleep(retry_gap_sec)
            continue

    # All retries exhausted. Return the deterministic fallback.
    print(
        f"[follow_up_generator] [warn] LLM exhausted, reason={last_reason!r}",
        file=sys.stderr,
    )
    return FollowUp(
        text=(prefix + deterministic).strip(),
        insights=_extract_insight_lines(deterministic),
        degraded=True,
        reason=last_reason or "LLM unavailable",
    )


# ---------------------------------------------------------------------------
# LLM call (Azure OpenAI gpt-4.1, lazy-imported)
# ---------------------------------------------------------------------------

def _call_llm(
    *,
    surface: str,
    snapshot: dict,
    breach: bool,
    timeout_sec: int,
) -> str:
    """Single LLM call attempt. Raises on any failure; caller handles retries."""
    # Lazy import: keep this module importable in test environments that
    # don't have the openai SDK installed.
    try:
        from judge_runner import get_openai_client  # type: ignore
    except ImportError as exc:
        raise RuntimeError(f"openai SDK unavailable: {exc}") from exc

    deployment = (
        os.environ.get("DEPLOYMENT_NAME")
        or os.environ.get("AZURE_DEPLOYMENT_NAME")
        or ""
    ).strip()
    if not deployment:
        raise RuntimeError("DEPLOYMENT_NAME not set")

    # Compose the user payload: snapshot + breach flag, JSON-stringified.
    user_payload = json.dumps(
        {
            "surface": surface,
            "breach": bool(breach),
            "snapshot": snapshot,
        },
        ensure_ascii=False,
        default=str,
    )

    prev_timeout = os.environ.get("JUDGE_HTTP_TIMEOUT_SEC")
    os.environ["JUDGE_HTTP_TIMEOUT_SEC"] = str(timeout_sec)
    try:
        client = get_openai_client()
    finally:
        if prev_timeout is None:
            os.environ.pop("JUDGE_HTTP_TIMEOUT_SEC", None)
        else:
            os.environ["JUDGE_HTTP_TIMEOUT_SEC"] = prev_timeout

    resp = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_payload},
        ],
        temperature=0,
        max_tokens=900,
    )
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise ValueError("LLM returned empty text")
    return text


# ---------------------------------------------------------------------------
# Deterministic fallback builder
# ---------------------------------------------------------------------------

def _build_deterministic_follow_up(
    *, surface: str, snapshot: dict, breach: bool,
) -> str:
    """Synthesize a follow-up from snapshot data when the LLM is unavailable.

    Same opening-line shape as the LLM output so the verdict regex passes.
    Number of insights kept to the 4 to 7 range. Acronyms NOT pre-expanded
    here; the caller still runs `expand_acronyms_first_use` after.
    """
    snap = snapshot or {}
    if surface == "scoreboard":
        return _deterministic_scoreboard(snap, breach=breach)
    return _deterministic_digest(snap, breach=breach)


def _deterministic_scoreboard(snap: dict, *, breach: bool) -> str:
    standings = snap.get("standings") or []
    by_label = {
        str(row.get("label", "")): row for row in standings
    }
    acc_row = by_label.get("Academic FAIL") or {}
    exp_row = by_label.get("Experience FAIL") or {}
    pass_row = by_label.get("Overall PASS") or {}
    cost_row = by_label.get("Run cost") or {}
    judged_row = by_label.get("Judged") or {}

    if breach:
        opener = (
            f"Top risk: Academic FAIL hit {acc_row.get('yesterday', 'n/a')} "
            f"yesterday, "
            f"{acc_row.get('delta', 'n/a')} versus the 14-day median."
        )
    else:
        opener = (
            f"No urgent risks today, Academic FAIL {acc_row.get('yesterday', 'n/a')} "
            f"stays inside the 6 percent floor."
        )

    bullets = [
        (
            f"Experience FAIL was {exp_row.get('yesterday', 'n/a')}, "
            f"{exp_row.get('delta', 'n/a')} vs the 14-day median."
        ),
        (
            f"Overall PASS was {pass_row.get('yesterday', 'n/a')}, "
            f"{pass_row.get('delta', 'n/a')} vs the 14-day median."
        ),
        (
            f"Run cost was {cost_row.get('yesterday', 'n/a')} "
            f"({cost_row.get('delta', 'n/a')} vs the 14-day median)."
        ),
        (
            f"Judged {judged_row.get('yesterday', 'n/a')} traces, "
            f"{judged_row.get('delta', 'n/a')} vs the 14-day median."
        ),
    ]
    return opener + " " + " ".join(bullets)


def _deterministic_digest(snap: dict, *, breach: bool) -> str:
    standings = snap.get("standings") or []
    by_label = {str(row.get("label", "")): row for row in standings}
    dv_row = by_label.get("Downvote rate") or {}
    vcp_row = by_label.get("VCP success") or {}
    err_row = by_label.get("Error rate") or {}
    ttft_row = by_label.get("Student TTFT p90") or {}
    cost_row = by_label.get("Total cost") or {}

    if breach:
        opener = (
            f"Top risk: safety floor breached; downvote rate "
            f"{dv_row.get('yesterday', 'n/a')}, "
            f"{dv_row.get('delta', 'n/a')} versus the 14-day median."
        )
    else:
        opener = (
            "No urgent risks today, the four watch metrics are inside "
            "their bands."
        )

    bullets = [
        (
            f"VCP success was {vcp_row.get('yesterday', 'n/a')}, "
            f"{vcp_row.get('delta', 'n/a')} vs the 14-day median."
        ),
        (
            f"Error rate was {err_row.get('yesterday', 'n/a')}, "
            f"{err_row.get('delta', 'n/a')} vs the 14-day median."
        ),
        (
            f"Student TTFT at the 90th percentile was {ttft_row.get('yesterday', 'n/a')}, "
            f"{ttft_row.get('delta', 'n/a')} vs the 14-day median."
        ),
        (
            f"Total cost was {cost_row.get('yesterday', 'n/a')}, "
            f"{cost_row.get('delta', 'n/a')} vs the 14-day median."
        ),
    ]
    return opener + " " + " ".join(bullets)


def _extract_insight_lines(text: str) -> list[str]:
    """Split the follow-up text into one-per-sentence insight bullets.

    First sentence is the verdict; remaining sentences are insights.
    Best-effort sentence splitter (period followed by space + capital).
    """
    if not text:
        return []
    # Cheap splitter: split on '. ' but keep periods on each sentence.
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    # Drop the first (verdict) sentence and return the rest.
    return parts[1:] if len(parts) > 1 else []


__all__ = [
    "FollowUp",
    "VERDICT_OPENING_RE",
    "generate_follow_up",
    "expand_acronyms_first_use",
    "breach_mention_prefix",
]
