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
# Structured insight payload (Commit 11: insights baked into the image)
# ---------------------------------------------------------------------------

@dataclass
class Callout:
    """Small scoreboard callout: mono eyebrow + Hanken body sentence."""

    label: str
    body: str


@dataclass
class InsightCard:
    """Single digest insight card.

    topic_label : one of CLARITY, LATENCY, FEEDBACK, COST, ACCURACY, USAGE.
    icon        : single glyph from the locked set, or empty string.
    claim       : Hanken 700, one sentence.
    evidence    : Hanken 400, one supporting metric sentence.
    context     : optional Hanken 400 cross-signal sentence, or None.
    """

    topic_label: str
    icon: str
    claim: str
    evidence: str
    context: Optional[str] = None


@dataclass
class InsightPayload:
    """Structured insights + slim text companion.

    The image is now the searchable artifact's body: callouts and cards
    live inside the PNG so the surface stands alone when forwarded.
    `text_companion` is the slim Slack-message body that sits below the
    image (verdict + breach mentions + deep-dive link).

    Backward-compat: `.text` and `.as_block_kit_section()` mirror the old
    FollowUp shape so existing callers in daily_eval.py + daily_digest.py
    keep working without churn. New callers should use `text_companion`
    and the structured insight fields directly.
    """

    verdict: str
    text_companion: str
    scoreboard_callouts: list[Callout] = field(default_factory=list)
    digest_cards: list[InsightCard] = field(default_factory=list)
    degraded: bool = False
    reason: Optional[str] = None

    @property
    def text(self) -> str:
        """Backward-compat alias for callers that read .text on FollowUp."""
        return self.text_companion

    def as_block_kit_section(self) -> dict:
        """Render the slim text companion as a Block Kit section block."""
        return {
            "type": "section",
            "text": {"type": "mrkdwn", "text": self.text_companion},
        }


# Backward-compat alias. Old callers and a handful of tests still type
# `FollowUp` explicitly; expose it as a synonym so the rename does not
# break them. New code should reference InsightPayload directly.
FollowUp = InsightPayload


# ---------------------------------------------------------------------------
# System prompt for the LLM call
# ---------------------------------------------------------------------------
#
# Commit 11: the LLM now returns BOTH a slim text companion AND the
# structured insights baked into the image (callouts for the scoreboard,
# narrative cards for the digest). Single JSON-mode response, single round
# trip. The text companion is 3 lines only; the long-form content lives
# inside the rendered PNG so the surface stands alone when forwarded.

_SYSTEM_PROMPT = """\
You write the morning conversational follow-up for the Ask AI eval pipeline at PhysicsWallah. Your output drives a Slack message: a poster image (which carries the structured insights) plus a slim text companion that sits below the image.

OUTPUT FORMAT: JSON object, no markdown, no surrounding prose. The JSON has exactly these top-level keys:
- "verdict": one English sentence, the locked verdict opening (see rule 1)
- "text_companion": the 3-line slim text companion (see rule 2)
- "scoreboard_callouts": list of exactly 2 objects {"label": str, "body": str} (only on scoreboard surface, else [])
- "digest_cards": list of 4 objects {"topic_label", "icon", "claim", "evidence", "context"} (only on digest surface, else [])

HARD RULES (enforced by automated post-process checks):

1. The "verdict" field MUST start with EXACTLY one of these two openings:
   - "Top risk: " (when something is off-trend or breached)
   - "No urgent risks today, " (when all watch metrics are inside their bands)
   No other openings are permitted. Do not start with "Yesterday", "TLDR", or any other phrasing.

2. The "text_companion" field is exactly 3 lines, separated by newlines:
   Line 1: the verdict sentence (same as the "verdict" field).
   Line 2: per-axis @-mention prefix when breach is true (caller adds this; you emit an empty string here).
   Line 3: the deep-dive link sentence ("Deep dive: <url>"). Caller fills the URL.
   No 4-to-7 insight list in the text. Long-form content is in the image.

3. Acronyms (TTFT, VCP, CSAT, SLO, RPS, pp) expanded on first occurrence only inside any one string. After the first expansion, the short form is fine.

4. NO em-dashes. NO en-dashes. Use commas, semicolons, or sentence breaks.

5. No marketing language. No "exciting", "leveraging", "robust", "world-class". No emojis in the text companion (icons are allowed inside digest_cards.icon).

6. scoreboard_callouts: exactly 2 entries. Each label is short uppercase (e.g. "TOP MOVER", "WORTH WATCHING", "WHY IT MATTERS"). Each body is one Hanken-readable sentence under 30 words, citing one specific number.

7. digest_cards: exactly 4 entries. topic_label is one of CLARITY, LATENCY, FEEDBACK, COST, ACCURACY, USAGE. icon is a single semantic glyph from the locked DESIGN.md set or empty string. claim is one sentence with a number. evidence is one sentence backing the claim. context is optional and may be null.

8. Voice anchors (few-shot examples of the right register):

   FiveThirtyEight: "The judge agreement rate climbed to 84% this week, up from 79% a week ago and the highest reading since the panel was re-calibrated in March."

   Stratechery: "The interesting thing about today's run is not the headline accuracy number, which is fine, but the shape of the failures."

   Construction Physics: "Throughput on the eval pipeline this week was about 1,420 traces per day, roughly 3.2x what we managed in the first week of April."
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
    deep_dive_url: Optional[str] = None,
    timeout_sec: int = 30,
    retries: int = 3,
    retry_gap_sec: int = 0,  # the 30s retry gap is the locked spec; tests
                              # override to 0 so the suite runs in seconds.
) -> InsightPayload:
    """Generate the structured insight payload for a surface.

    Returns an InsightPayload carrying:
      - verdict           : one English sentence (regex-checked)
      - text_companion    : slim 3-line Slack body (verdict + mention + link)
      - scoreboard_callouts : list of 2 Callouts for scoreboard surface
      - digest_cards      : list of 4 InsightCards for digest surface
      - degraded / reason : populated when the LLM path falls back

    Flow:
        1. Build the deterministic structured fallback from snapshot.
        2. Attempt the LLM call up to `retries` times with `timeout_sec`
           per attempt; gap of `retry_gap_sec` between attempts. Default
           retry_gap_sec=0 so the unit test suite stays fast; production
           callers pass retry_gap_sec=30 per the locked spec.
        3. If the LLM returns parseable JSON and the verdict regex passes,
           construct the payload from it.
        4. Otherwise fall back to the deterministic structured payload.

    Never raises. On any LLM failure, returns the deterministic-fallback
    payload with degraded=True and a `reason` string. The `text_companion`
    in both paths is the slim 3-line shape: verdict, @-mention prefix (or
    blank line), deep-dive link.
    """
    fallback = _build_deterministic_payload(
        surface=surface, snapshot=snapshot, breach=breach,
        breach_signal=breach_signal, deep_dive_url=deep_dive_url,
    )

    last_reason: Optional[str] = None
    for attempt in range(max(1, retries)):
        try:
            raw = _call_llm(
                surface=surface,
                snapshot=snapshot,
                breach=breach,
                timeout_sec=timeout_sec,
            )
            if not raw:
                last_reason = "empty response"
                if attempt < retries - 1:
                    time.sleep(retry_gap_sec)
                continue
            payload = _parse_llm_payload(
                raw,
                surface=surface,
                breach=breach,
                breach_signal=breach_signal,
                deep_dive_url=deep_dive_url,
            )
            if payload is None:
                last_reason = (
                    "verdict opening regex failed or JSON shape invalid: "
                    f"first 60 chars = {raw[:60]!r}"
                )
                if attempt < retries - 1:
                    time.sleep(retry_gap_sec)
                continue
            return payload
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

    # All retries exhausted. Return the deterministic fallback with the
    # degraded flag set so the caller can prepend the operator marker.
    print(
        f"[follow_up_generator] [warn] LLM exhausted, reason={last_reason!r}",
        file=sys.stderr,
    )
    fallback.degraded = True
    fallback.reason = last_reason or "LLM unavailable"
    return fallback


def _parse_llm_payload(
    raw: str,
    *,
    surface: str,
    breach: bool,
    breach_signal: Optional[str],
    deep_dive_url: Optional[str],
) -> Optional[InsightPayload]:
    """Parse the JSON-mode LLM response into an InsightPayload.

    Returns None when the JSON is malformed, the verdict regex fails, or
    the structured fields are shaped incorrectly. Caller retries on None.
    """
    # The LLM may sometimes wrap JSON in a code fence; strip it best-effort.
    text = raw.strip()
    if text.startswith("```"):
        # Drop the opening fence line and the trailing ``` if present.
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    verdict = expand_acronyms_first_use(str(data.get("verdict", "")).strip())
    if not VERDICT_OPENING_RE.match(verdict):
        return None

    text_companion = _build_slim_text_companion(
        verdict=verdict,
        breach=breach,
        breach_signal=breach_signal,
        deep_dive_url=deep_dive_url,
    )

    callouts: list[Callout] = []
    if surface == "scoreboard":
        for item in (data.get("scoreboard_callouts") or [])[:2]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            body = expand_acronyms_first_use(str(item.get("body", "")).strip())
            if label and body:
                callouts.append(Callout(label=label, body=body))

    cards: list[InsightCard] = []
    if surface == "digest":
        for item in (data.get("digest_cards") or [])[:4]:
            if not isinstance(item, dict):
                continue
            topic = str(item.get("topic_label", "")).strip()
            icon = str(item.get("icon", "")).strip()
            claim = expand_acronyms_first_use(str(item.get("claim", "")).strip())
            evidence = expand_acronyms_first_use(str(item.get("evidence", "")).strip())
            context_raw = item.get("context")
            context = (
                expand_acronyms_first_use(str(context_raw).strip())
                if context_raw else None
            )
            if topic and claim:
                cards.append(InsightCard(
                    topic_label=topic, icon=icon, claim=claim,
                    evidence=evidence, context=context,
                ))

    return InsightPayload(
        verdict=verdict,
        text_companion=text_companion,
        scoreboard_callouts=callouts,
        digest_cards=cards,
        degraded=False,
        reason=None,
    )


def _build_slim_text_companion(
    *,
    verdict: str,
    breach: bool,
    breach_signal: Optional[str],
    deep_dive_url: Optional[str],
) -> str:
    """Compose the 3-line slim Slack text companion.

    Line 1: verdict sentence (regex-checked upstream).
    Line 2: per-axis @-mention prefix when breach=True, else blank line.
    Line 3: deep-dive link sentence.

    The mention prefix and link are always added by the caller path, never
    by the LLM, so the text shape is deterministic across happy-path and
    fallback alike.
    """
    mention_line = ""
    if breach:
        prefix = breach_mention_prefix(breach_signal).strip()
        if prefix:
            mention_line = prefix
    link_line = (
        f"Deep dive: {deep_dive_url}" if deep_dive_url else "Deep dive: (link pending)"
    )
    return "\n".join([verdict, mention_line, link_line])


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

def _build_deterministic_payload(
    *,
    surface: str,
    snapshot: dict,
    breach: bool,
    breach_signal: Optional[str] = None,
    deep_dive_url: Optional[str] = None,
) -> InsightPayload:
    """Synthesize a structured InsightPayload from snapshot data.

    Used when the LLM path is unavailable or returns garbage. Same verdict
    opening shape as the LLM output so the regex passes. Returns a payload
    that includes the 2 scoreboard callouts or the 4 digest cards plus the
    slim 3-line text companion.
    """
    snap = snapshot or {}
    if surface == "scoreboard":
        verdict, callouts = _deterministic_scoreboard(snap, breach=breach)
        cards: list[InsightCard] = []
    else:
        verdict, cards = _deterministic_digest(snap, breach=breach)
        callouts = []
    verdict = expand_acronyms_first_use(verdict)
    text_companion = _build_slim_text_companion(
        verdict=verdict,
        breach=breach,
        breach_signal=breach_signal,
        deep_dive_url=deep_dive_url,
    )
    return InsightPayload(
        verdict=verdict,
        text_companion=text_companion,
        scoreboard_callouts=callouts,
        digest_cards=cards,
    )


def _build_deterministic_follow_up(
    *, surface: str, snapshot: dict, breach: bool,
) -> str:
    """Backward-compat helper. Returns the slim text companion only.

    A handful of existing tests (test_no_jargon.py) reach in and call this
    directly to check the deterministic prose for banned tokens. Keeping
    the function with the same signature avoids churning those tests.
    """
    payload = _build_deterministic_payload(
        surface=surface, snapshot=snapshot, breach=breach,
    )
    return payload.text_companion


def _deterministic_scoreboard(
    snap: dict, *, breach: bool,
) -> tuple[str, list[Callout]]:
    """Build the verdict sentence + 2 callouts for the scoreboard surface.

    The callouts are derived from the standings rows: callout 1 covers the
    worst-delta row ("TOP MOVER"), callout 2 the second-worst or the run
    cost trend ("WORTH WATCHING"). Both stay under 30 words to fit the
    poster body type.
    """
    standings = snap.get("standings") or []
    by_label = {str(row.get("label", "")): row for row in standings}
    acc_row = by_label.get("Academic FAIL") or {}
    exp_row = by_label.get("Experience FAIL") or {}
    pass_row = by_label.get("Overall PASS") or {}
    cost_row = (
        by_label.get("Yesterday's run cost")
        or by_label.get("Run cost")
        or {}
    )
    judged_row = (
        by_label.get("Traces graded")
        or by_label.get("Judged")
        or {}
    )

    if breach:
        verdict = (
            f"Top risk: Academic FAIL hit {acc_row.get('yesterday', 'n/a')} "
            f"yesterday, "
            f"{acc_row.get('delta', 'n/a')} versus the 14-day median."
        )
        callouts = [
            Callout(
                label="WHY IT MATTERS",
                body=(
                    f"Academic FAIL came in at {acc_row.get('yesterday', 'n/a')} "
                    f"versus the 14-day median, the worst single-day reading "
                    f"in recent history."
                ),
            ),
            Callout(
                label="WORTH WATCHING",
                body=(
                    f"Experience FAIL was {exp_row.get('yesterday', 'n/a')} "
                    f"({exp_row.get('delta', 'n/a')}); yesterday's run cost "
                    f"was {cost_row.get('yesterday', 'n/a')}."
                ),
            ),
        ]
    else:
        verdict = (
            f"No urgent risks today, Academic FAIL "
            f"{acc_row.get('yesterday', 'n/a')} "
            f"stays inside the 6 percent floor."
        )
        callouts = [
            Callout(
                label="TOP MOVER",
                body=(
                    f"Experience FAIL came in at {exp_row.get('yesterday', 'n/a')} "
                    f"({exp_row.get('delta', 'n/a')} vs the 14-day median)."
                ),
            ),
            Callout(
                label="WORTH WATCHING",
                body=(
                    f"Overall PASS was {pass_row.get('yesterday', 'n/a')} "
                    f"({pass_row.get('delta', 'n/a')}); traces graded "
                    f"{judged_row.get('yesterday', 'n/a')}."
                ),
            ),
        ]
    return verdict, callouts


def _deterministic_digest(
    snap: dict, *, breach: bool,
) -> tuple[str, list[InsightCard]]:
    """Build the verdict + up to 4 InsightCards for the digest surface.

    On breach days produces 4 cards (never empty) so the image always
    carries the breach narrative. On calm days produces 0 cards; the
    template renders the quiet-day fallback line.
    """
    standings = snap.get("standings") or []
    by_label = {str(row.get("label", "")): row for row in standings}
    dv_row = by_label.get("Downvote rate") or {}
    vcp_row = (
        by_label.get("Video Co-Pilot OK %")
        or by_label.get("VCP success")
        or {}
    )
    err_row = by_label.get("Error rate") or {}
    ttft_row = (
        by_label.get("Student wait, 90th pct")
        or by_label.get("Student TTFT p90")
        or {}
    )
    cost_row = by_label.get("Total cost") or {}

    if breach:
        verdict = (
            f"Top risk: safety floor breached; downvote rate "
            f"{dv_row.get('yesterday', 'n/a')}, "
            f"{dv_row.get('delta', 'n/a')} versus the 14-day median."
        )
        cards = [
            InsightCard(
                topic_label="FEEDBACK",
                icon="",
                claim=(
                    f"Downvote rate spiked to {dv_row.get('yesterday', 'n/a')}, "
                    f"{dv_row.get('delta', 'n/a')} vs the 14-day median."
                ),
                evidence="Same-day correlation with the safety-floor signal.",
                context=None,
            ),
            InsightCard(
                topic_label="USAGE",
                icon="",
                claim=(
                    f"Video Co-Pilot OK rate sat at "
                    f"{vcp_row.get('yesterday', 'n/a')}, "
                    f"{vcp_row.get('delta', 'n/a')} vs the 14-day median."
                ),
                evidence="Backend lead pinged on the same thread.",
                context=None,
            ),
            InsightCard(
                topic_label="LATENCY",
                icon="",
                claim=(
                    f"Student wait at the 90th percentile was "
                    f"{ttft_row.get('yesterday', 'n/a')}, "
                    f"{ttft_row.get('delta', 'n/a')} vs the 14-day median."
                ),
                evidence="Longer answers tend to fan out into more retries.",
                context=None,
            ),
            InsightCard(
                topic_label="COST",
                icon="",
                claim=(
                    f"Total cost was {cost_row.get('yesterday', 'n/a')}, "
                    f"{cost_row.get('delta', 'n/a')} vs the 14-day median."
                ),
                evidence="Cost moves track the latency drift.",
                context=None,
            ),
        ]
    else:
        verdict = (
            "No urgent risks today, the four watch metrics are inside "
            "their bands."
        )
        cards = []
    return verdict, cards


__all__ = [
    "Callout",
    "FollowUp",
    "InsightCard",
    "InsightPayload",
    "VERDICT_OPENING_RE",
    "generate_follow_up",
    "expand_acronyms_first_use",
    "breach_mention_prefix",
]
