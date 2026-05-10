"""
v8 Master Judge runner — Ask AI online evaluation.

Runs the locked 5-axial / 18-code rubric against a list of production traces.

Usage:
    # 1) Set keys in env (NEVER paste keys into chat or commit them)
    export OPENAI_API_KEY=sk-...
    export LANGFUSE_PUBLIC_KEY=pk-lf-...     # optional — enables tracing
    export LANGFUSE_SECRET_KEY=sk-lf-...     # optional
    export LANGFUSE_HOST=https://cloud.langfuse.com  # optional, default

    # 2) Quick test on synthetic cases
    python judge_runner.py --test

    # 3) Run on a JSON file of real production samples
    python judge_runner.py --input samples.json --output results.json

    # 4) Format last results as a Slack-ready rubric block
    python judge_runner.py --slack-block results.json

This is an internal evaluation tool — does NOT touch the production answer pipeline,
does NOT alter what students see, and does NOT mutate any production system.
It only reads traces and writes scores to Langfuse (the eval system of record).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants — the locked taxonomy (mirrors v8_master_judge_prompt.md)
# ---------------------------------------------------------------------------

ACADEMIC_CODES = {"A1", "A2", "A3", "A4", "A5", "A6"}
INTENT_CODES = {"B1"}
FORMATTING_CODES = {"C1", "C2", "C3", "C4"}
PEDAGOGY_CODES = {"D1", "D2", "D3", "D4"}
TONE_CODES = {"E1", "E2", "E3"}

ALL_CODES = (
    ACADEMIC_CODES | INTENT_CODES | FORMATTING_CODES | PEDAGOGY_CODES | TONE_CODES
)
AXIAL_TO_CODES = {
    "academic": ACADEMIC_CODES,
    "intent": INTENT_CODES,
    "formatting": FORMATTING_CODES,
    "pedagogy": PEDAGOGY_CODES,
    "tone": TONE_CODES,
}

CODE_LABELS = {
    "A1": "Conceptual error",
    "A2": "Misunderstood doubt",
    "A3": "Wrong OCR",
    "A4": "Calculation error",
    "A5": "Answer incomplete",
    "A6": "Incorrect validation",
    "B1": "Ambiguous, badly handled",
    "C1": "Equation unreadable",
    "C2": "Steps not structured",
    "C3": "Symbols corrupted",
    "C4": "Chem notation broken",
    "D1": "Too advanced",
    "D2": "Too basic",
    "D3": "No direct answer",
    "D4": "No clarification asked",
    "E1": "Too long",
    "E2": "Minor details missing",
    "E3": "Tone / naturalness",
}

DEFAULT_MODEL = "gpt-4.1-2025-04-14"


# ---------------------------------------------------------------------------
# System prompt — kept inline so the script is self-contained
# Mirrors v8_master_judge_prompt.md "SYSTEM PROMPT" section
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a strict STEM academic answer evaluator for PhysicsWallah's Ask AI tutor.

You evaluate AI-generated answers to student doubts using a locked 5-axial / 18-code rubric. Output strict JSON only.

INPUTS YOU RECEIVE:
- DOUBT (required): the student's question
- AI_ANSWER (required): the response to evaluate
- TRANSCRIPT (optional): conversation history; may be empty
- IDEAL_ANSWER (optional): SME-verified correct answer; may be absent in production
- SLIDE_IMAGE (optional): visual context; may be attached as image

OPERATING RULES:
1. Be strict, objective, and checklist-driven. Do not assume correctness.
2. If IDEAL_ANSWER is provided: it is your primary correctness reference, but verify it independently from first principles before relying on it.
3. If IDEAL_ANSWER is NOT provided: rely on established STEM domain knowledge and apply a more generous standard for completeness (A5).
4. Slides may contain teacher errors. Do NOT treat slide content as automatically correct. The AI must demonstrate INDEPENDENT REASONING.
5. STUDENT ANNOTATIONS (red bounding boxes on slide) are auxiliary, not primary. Combine with DOUBT text. Do NOT penalize the AI for misaligned student boxes.
6. AI ANSWER STRUCTURE TO IGNORE WHEN SCORING:
   - Engagement openers (greeting, acknowledgment) at the beginning
   - Follow-up question at the end
   These are pedagogical framing — exclude from conciseness (E1) scoring. Score only the CORE ACADEMIC EXPLANATION.
7. LANGUAGE: Hinglish, Hindi, English, code-switching are all valid. NEVER penalize for language mixing.
8. APPROACH: Do NOT penalize for a different but mathematically valid approach than IDEAL_ANSWER uses.

WHAT FIRES EACH OPEN CODE:

== A. ACADEMIC (Binary Kill Switch — ANY fire → overall FAIL) ==
A1 Conceptual error — Any concept, principle, theorem stated wrongly
A2 Misunderstood doubt — Doubt was clear, AI answered tangentially
A3 Wrong OCR — Misread numbers, symbols, equations from slide
A4 Calculation error — Arithmetic / algebraic mistake
A5 Answer incomplete (crucial) — Core steps missing, doubt unresolved
A6 Incorrect validation — AI defends slide error OR validates student's wrong claim with faulty reasoning OR fails to think independently from first principles

== B. INTENT BINDING ==
B1 Ambiguous query, badly handled — Doubt was unclear AND AI silently assumed one interpretation. NOT B1 if AI asked clarification or stated assumptions explicitly.

== C. PRESENTATION & FORMATTING ==
C1 Equation unreadable — Equations / formulas broken
C2 Steps not structured — No clear logical order
C3 Symbols corrupted — Symbols / characters wrong
C4 Chemistry notation broken — Subscript / superscript / charge / arrow wrong

== D. PEDAGOGICAL FIT ==
D1 Too advanced — Above student's class level
D2 Too basic — Below student's class level
D3 No direct answer upfront — Student asked for direct answer, AI buried it
D4 No clarification asked — Doubt was unclear, AI proceeded without asking

== E. LOOK & FEEL / TONE ==
E1 Too long — Correct but verbose / repetitive (excl. opener/follow-up)
E2 Minor details missing — Fine but minor enriching details absent
E3 Tone & naturalness — Robotic, rude, condescending

NOT_JUDGABLE: Set true when doubt is incomplete, needs missing transcript/slide context, is non-academic, or unreadable.

DECISION LOGIC (apply exactly):
1. If not_judgable: overall_band="NOT_JUDGABLE", overall_score=null
2. academic.passed = (no A1..A6 fired)
3. experience.passed = (no codes fired across intent, formatting, pedagogy, tone)
4. If not academic.passed: overall_band="FAIL", overall_score=0.0  [Binary Kill Switch]
5. Else if experience.passed: overall_band="PASS", overall_score=1.0
6. Else: overall_band="NEUTRAL", overall_score=0.5

OUTPUT FORMAT (STRICT JSON ONLY — no extra text before or after):
{
  "academic":   {"passed": <bool>, "open_codes_fired": [<A1..A6>],     "reasoning": "<≤30 words>"},
  "intent":     {"passed": <bool>, "open_codes_fired": [<B1>],         "reasoning": "<≤30 words>"},
  "formatting": {"passed": <bool>, "open_codes_fired": [<C1..C4>],     "reasoning": "<≤30 words>"},
  "pedagogy":   {"passed": <bool>, "open_codes_fired": [<D1..D4>],     "reasoning": "<≤30 words>"},
  "tone":       {"passed": <bool>, "open_codes_fired": [<E1..E3>],     "reasoning": "<≤30 words>"},
  "overall_band": "PASS" | "NEUTRAL" | "FAIL" | "NOT_JUDGABLE",
  "overall_score": 1.0 | 0.5 | 0.0 | null,
  "all_open_codes_fired": [<flattened union>],
  "not_judgable": <bool>,
  "not_judgable_reason": <string or null>,
  "confidence": "low" | "med" | "high"
}
"""


def build_user_message(sample: dict) -> str:
    """Render a single sample into the user-message string."""
    return f"""\
DOUBT:
{sample.get('doubt', '').strip()}

TRANSCRIPT:
{sample.get('transcript', '').strip() or '(none)'}

IDEAL_ANSWER:
{sample.get('ideal_answer', '').strip() or '(not provided — judge from first principles)'}

AI_ANSWER:
{sample.get('ai_answer', '').strip()}

CONTEXT:
- subject: {sample.get('subject', 'unknown')}
- chapter: {sample.get('chapter', 'unknown')}
- student_class: {sample.get('student_class', 'unknown')}
- exam: {sample.get('exam', 'unknown')}
- has_image: {bool(sample.get('image_url'))}
- is_annotated: {sample.get('is_annotated', False)}

Evaluate using the rubric. Output strict JSON.
"""


# ---------------------------------------------------------------------------
# Validation — check the model output before trusting any score
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def validate_judge_output(parsed: dict) -> ValidationResult:
    errs: list[str] = []

    # Required top-level keys
    for k in (
        "academic", "intent", "formatting", "pedagogy", "tone",
        "overall_band", "overall_score", "all_open_codes_fired",
        "not_judgable", "confidence",
    ):
        if k not in parsed:
            errs.append(f"missing key: {k}")

    if errs:
        return ValidationResult(False, errs)

    # Per-axial structure + open-code legality
    for axial, allowed in AXIAL_TO_CODES.items():
        block = parsed.get(axial)
        if not isinstance(block, dict):
            errs.append(f"{axial} not an object")
            continue
        if "passed" not in block or not isinstance(block["passed"], bool):
            errs.append(f"{axial}.passed missing or non-bool")
        codes = block.get("open_codes_fired", [])
        if not isinstance(codes, list):
            errs.append(f"{axial}.open_codes_fired not a list")
        else:
            for c in codes:
                if c not in allowed:
                    errs.append(f"{axial}.open_codes_fired has illegal code {c!r}")
        # Cross-check: passed == empty list
        if isinstance(codes, list) and "passed" in block:
            if block["passed"] and codes:
                errs.append(f"{axial}.passed=True but open_codes_fired={codes}")
            if not block["passed"] and not codes:
                errs.append(f"{axial}.passed=False but open_codes_fired empty")

    # Decision logic determinism
    if not parsed.get("not_judgable"):
        academic_pass = parsed.get("academic", {}).get("passed", False)
        experience_pass = all(
            parsed.get(ax, {}).get("passed", False)
            for ax in ("intent", "formatting", "pedagogy", "tone")
        )
        expected_band = (
            "FAIL" if not academic_pass
            else "PASS" if experience_pass
            else "NEUTRAL"
        )
        if parsed.get("overall_band") != expected_band:
            errs.append(
                f"overall_band={parsed.get('overall_band')} != expected {expected_band} "
                f"(academic_pass={academic_pass}, experience_pass={experience_pass})"
            )

    # Flattened union check
    flat = parsed.get("all_open_codes_fired", [])
    expected_flat = sorted({
        c
        for ax in AXIAL_TO_CODES
        for c in parsed.get(ax, {}).get("open_codes_fired", []) or []
    })
    if sorted(flat) != expected_flat:
        errs.append(
            f"all_open_codes_fired={flat} doesn't match union of axial codes={expected_flat}"
        )

    # Confidence
    if parsed.get("confidence") not in ("low", "med", "high"):
        errs.append(f"invalid confidence: {parsed.get('confidence')!r}")

    return ValidationResult(not errs, errs)


# ---------------------------------------------------------------------------
# OpenAI / Azure OpenAI client — lazy import so --test runs without openai installed
#
# Auto-detection:
#   • If AZURE_ENDPOINT is set → use AzureOpenAI (PW prod path)
#   • Else if OPENAI_API_KEY is set → use direct OpenAI
#
# When Azure is used, the `model` param to chat.completions.create
# is the Azure DEPLOYMENT_NAME, NOT the underlying model id.
# ---------------------------------------------------------------------------

def _is_azure() -> bool:
    return bool(os.environ.get("AZURE_ENDPOINT") or os.environ.get("AZURE_OPENAI_ENDPOINT"))


def judge_http_timeout_seconds() -> float:
    """Per-request HTTP timeout for chat.completions (read-heavy judge calls).

    Without this, a single hung Azure/OpenAI connection can stall the entire eval
    with no log lines for many minutes. Override via JUDGE_HTTP_TIMEOUT_SEC.
    """
    raw = os.environ.get("JUDGE_HTTP_TIMEOUT_SEC", "240")
    try:
        v = float(raw)
    except ValueError:
        return 240.0
    return max(30.0, min(v, 900.0))


def get_openai_client():
    use_langfuse = (
        os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
    )

    if _is_azure():
        # Azure path — used by PW production (Satyam's AzureChatOpenAI).
        endpoint = os.environ.get("AZURE_ENDPOINT") or os.environ["AZURE_OPENAI_ENDPOINT"]
        api_key = os.environ.get("AZURE_API_KEY") or os.environ.get("AZURE_OPENAI_API_KEY")
        api_version = os.environ.get("AZURE_API_VERSION") or "2024-08-01-preview"
        if not api_key:
            sys.exit(
                "ERROR: AZURE_API_KEY (or AZURE_OPENAI_API_KEY) not set.\n"
                "  export AZURE_ENDPOINT=https://<resource>.openai.azure.com\n"
                "  export AZURE_API_KEY=...\n"
                "  export AZURE_API_VERSION=2024-08-01-preview\n"
                "  export DEPLOYMENT_NAME=<your-gpt-4.1-deployment-name>"
            )
        if use_langfuse:
            try:
                from langfuse.openai import AzureOpenAI as TracedAzureOpenAI
                return TracedAzureOpenAI(
                    azure_endpoint=endpoint,
                    api_key=api_key,
                    api_version=api_version,
                    timeout=judge_http_timeout_seconds(),
                )
            except ImportError:
                print("(note: langfuse not installed; running Azure without tracing)")
        from openai import AzureOpenAI
        return AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            timeout=judge_http_timeout_seconds(),
        )

    # Direct OpenAI path
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit(
            "ERROR: No credentials. Set ONE of:\n"
            "  Azure (PW prod path):\n"
            "    export AZURE_ENDPOINT=https://<resource>.openai.azure.com\n"
            "    export AZURE_API_KEY=...\n"
            "    export AZURE_API_VERSION=2024-08-01-preview\n"
            "    export DEPLOYMENT_NAME=<your-gpt-4.1-deployment>\n"
            "  OR direct OpenAI:\n"
            "    export OPENAI_API_KEY=sk-..."
        )
    if use_langfuse:
        try:
            from langfuse.openai import OpenAI as TracedOpenAI
            return TracedOpenAI(timeout=judge_http_timeout_seconds())
        except ImportError:
            print("(note: langfuse not installed; running without tracing)")
    from openai import OpenAI
    return OpenAI(timeout=judge_http_timeout_seconds())


def _resolve_model_param(requested_model: str) -> str:
    """On Azure, the chat.completions `model` param is the deployment name.

    If user passed --model gpt-4.1-... and AZURE_ENDPOINT is set,
    prefer DEPLOYMENT_NAME from env. This makes the script behave correctly
    against PW's Azure deployment without code changes.
    """
    if _is_azure():
        deployment = os.environ.get("DEPLOYMENT_NAME")
        if not deployment:
            sys.exit(
                "ERROR: AZURE_ENDPOINT is set but DEPLOYMENT_NAME is missing.\n"
                "  export DEPLOYMENT_NAME=<your-gpt-4.1-deployment-name>"
            )
        return deployment
    return requested_model


# ---------------------------------------------------------------------------
# Langfuse score writer — push judge verdicts back to the PRODUCTION trace
# ---------------------------------------------------------------------------
#
# Per LANGFUSE_TRACING_PLAN.md §3.2: scores attach to the production trace_id
# being judged, NOT to the judge run trace. That way the production trace
# carries CSAT (existing) + judge axials (new) + SME band (when ingested) —
# all on one trace_id queryable from the Langfuse UI.
#
# This function is a no-op if Langfuse keys aren't set, so it's safe to call
# unconditionally from the runner.

_LANGFUSE_CLIENT_CACHE: dict = {}
# Serialize Langfuse create_score bursts from concurrent judge workers (daily_eval ThreadPoolExecutor).
_LANGFUSE_SCORE_WRITE_LOCK = threading.Lock()


def _get_langfuse_writer():
    """Returns a Langfuse client capable of create_score, or None if disabled."""
    if "client" in _LANGFUSE_CLIENT_CACHE:
        return _LANGFUSE_CLIENT_CACHE["client"]
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        _LANGFUSE_CLIENT_CACHE["client"] = None
        return None
    try:
        from langfuse import Langfuse
        _h = (os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL") or "").strip()
        c = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=_h or "https://cloud.langfuse.com",
        )
        _LANGFUSE_CLIENT_CACHE["client"] = c
        return c
    except Exception as e:
        print(f"(langfuse score writer disabled: {e})")
        _LANGFUSE_CLIENT_CACHE["client"] = None
        return None


def write_judge_scores_to_langfuse(production_trace_id: str, parsed: dict,
                                    judge_run_id: str, judge_model: str) -> int:
    """Write all axial + open-code scores back to the production trace_id.

    Returns: number of scores written (0 if Langfuse disabled or trace_id missing).

    Thread-safe when daily_eval runs judges concurrently (lock around create_score burst).
    """
    if not production_trace_id:
        return 0
    lf = _get_langfuse_writer()
    if lf is None:
        return 0

    written = 0
    band = parsed.get("overall_band")
    band_to_num = {"PASS": 1.0, "NEUTRAL": 0.5, "FAIL": 0.0, "NOT_JUDGABLE": None}

    base_kwargs = {
        "trace_id": production_trace_id,
        "comment": f"judge_run_id={judge_run_id}; model={judge_model}",
    }

    try:
        with _LANGFUSE_SCORE_WRITE_LOCK:
            # Headline scores
            if band in band_to_num and band_to_num[band] is not None:
                lf.create_score(name="judge_overall_score", value=band_to_num[band],
                                data_type="NUMERIC", **base_kwargs)
                written += 1
            if band:
                lf.create_score(name="judge_overall_band", value=band,
                                data_type="CATEGORICAL", **base_kwargs)
                written += 1

            if parsed.get("not_judgable"):
                lf.create_score(name="judge_not_judgable", value=1,
                                data_type="BOOLEAN",
                                comment=parsed.get("not_judgable_reason") or base_kwargs["comment"],
                                trace_id=production_trace_id)
                written += 1

            # Per-axial PASS/FAIL (binary 1.0/0.0 + categorical label)
            for ax in AXIAL_TO_CODES:
                block = parsed.get(ax) or {}
                if "passed" not in block:
                    continue
                passed = bool(block["passed"])
                lf.create_score(
                    name=f"judge_axial_{ax}",
                    value=1.0 if passed else 0.0,
                    data_type="NUMERIC",
                    comment=(block.get("reasoning") or "")[:500],
                    trace_id=production_trace_id,
                )
                written += 1
                lf.create_score(
                    name=f"judge_axial_{ax}_band",
                    value="PASS" if passed else "FAIL",
                    data_type="CATEGORICAL",
                    trace_id=production_trace_id,
                )
                written += 1

            # One boolean score per fired open code (sparse — only fired ones written)
            for code in parsed.get("all_open_codes_fired", []) or []:
                if code in ALL_CODES:
                    lf.create_score(
                        name=f"judge_code_{code}",
                        value=1,
                        data_type="BOOLEAN",
                        comment=CODE_LABELS.get(code, code),
                        trace_id=production_trace_id,
                    )
                    written += 1

            # Confidence
            conf = parsed.get("confidence")
            if conf in ("low", "med", "high"):
                lf.create_score(name="judge_confidence", value=conf,
                                data_type="CATEGORICAL", trace_id=production_trace_id)
                written += 1

            # Judge run id (for grouping)
            lf.create_score(name="judge_run_id", value=judge_run_id,
                            data_type="CATEGORICAL", trace_id=production_trace_id)
            written += 1
    except Exception as e:
        # Never fail the judge run because Langfuse write failed; log + continue
        print(f"  (langfuse score write partial failure on {production_trace_id}: {e})")

    return written


def call_judge(client, sample: dict, model: str = DEFAULT_MODEL) -> tuple[dict, dict]:
    """Returns (parsed_output, raw_response_meta)."""
    user_msg_text = build_user_message(sample)

    # If image_url present, build multi-modal user message
    if sample.get("image_url"):
        user_content = [
            {"type": "text", "text": user_msg_text},
            {"type": "image_url", "image_url": {"url": sample["image_url"]}},
        ]
    else:
        user_content = user_msg_text

    # On Azure, `model` must be the deployment name (not the underlying model id).
    effective_model = _resolve_model_param(model)
    provider = "azure" if _is_azure() else "openai"

    t0 = time.monotonic()
    to = judge_http_timeout_seconds()

    resp = client.chat.completions.create(
        model=effective_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        timeout=to,
    )
    raw_content = resp.choices[0].message.content
    usage = getattr(resp, "usage", None)

    latency_s = time.monotonic() - t0

    parsed = json.loads(raw_content)

    def _get_token(key: str):
        if usage is None:
            return None
        # Pydantic obj has attribute access; dict has .get
        return getattr(usage, key, None) if not isinstance(usage, dict) else usage.get(key)

    meta = {
        "latency_s": round(latency_s, 3),
        "provider": provider,
        "model_param": effective_model,
        "model_requested": model,
        "input_tokens": _get_token("prompt_tokens"),
        "output_tokens": _get_token("completion_tokens"),
    }
    return parsed, meta


# ---------------------------------------------------------------------------
# Aggregation — turn N judge outputs into digest-ready summary
# ---------------------------------------------------------------------------

@dataclass
class DigestSummary:
    n_total: int
    n_judgable: int
    n_pass: int
    n_neutral: int
    n_fail: int
    n_not_judgable: int
    n_parse_error: int
    axial_fail_pct: dict[str, float]   # axial → % of judgable samples that failed it
    open_codes_fired_count: dict[str, int]  # code → count of times it fired
    top_open_codes: list[tuple[str, int]]  # sorted descending


def wilson_ci_pp(p: float, n: int, z: float = 1.96) -> float:
    """Returns half-width of Wilson 95% CI in percentage points.

    Input `p` is already in percent (e.g. 22.0 for 22%).
    Output is the half-width in percentage points (e.g. 4.1 means ±4.1pp).
    """
    if n == 0:
        return 0.0
    p = p / 100.0  # input is already in percent
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom  # noqa: F841 (kept for readability)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return round(half * 100, 1)


def _delta_marker(curr_pct: float, prev_pct: float | None,
                   higher_is_bad: bool = True) -> str:
    """Build a `↑X.Xpp 🔴` style annotation for a metric vs its previous value.

    higher_is_bad=True means "increase = regression" (FAIL%, error%).
    higher_is_bad=False means "increase = improvement" (PASS%).
    Returns "" if prev_pct is None.
    """
    if prev_pct is None:
        return ""
    delta = round(curr_pct - prev_pct, 1)
    if abs(delta) < 0.05:  # treat as flat
        return " (flat vs prev)"
    arrow = "↑" if delta > 0 else "↓"
    if higher_is_bad:
        emoji = "🔴" if delta > 0 else "🟢"
    else:
        emoji = "🟢" if delta > 0 else "🔴"
    return f" {arrow}{abs(delta):.1f}pp {emoji}"


def aggregate(results: list[dict]) -> DigestSummary:
    n_total = len(results)
    n_pass = sum(1 for r in results if r.get("overall_band") == "PASS")
    n_neutral = sum(1 for r in results if r.get("overall_band") == "NEUTRAL")
    n_fail = sum(1 for r in results if r.get("overall_band") == "FAIL")
    n_not_judgable = sum(1 for r in results if r.get("overall_band") == "NOT_JUDGABLE")
    n_parse_error = sum(1 for r in results if r.get("_parse_error"))
    n_judgable = n_pass + n_neutral + n_fail

    # Per-axial fail % among judgable
    axial_fail_pct = {}
    for ax in AXIAL_TO_CODES:
        if n_judgable == 0:
            axial_fail_pct[ax] = 0.0
            continue
        n_axial_fail = sum(
            1 for r in results
            if r.get("overall_band") in ("PASS", "NEUTRAL", "FAIL")
            and not r.get(ax, {}).get("passed", True)
        )
        axial_fail_pct[ax] = round(100.0 * n_axial_fail / n_judgable, 1)

    # Open code firings
    code_counter: Counter = Counter()
    for r in results:
        for c in r.get("all_open_codes_fired", []) or []:
            code_counter[c] += 1

    top_codes = code_counter.most_common(10)

    return DigestSummary(
        n_total=n_total,
        n_judgable=n_judgable,
        n_pass=n_pass,
        n_neutral=n_neutral,
        n_fail=n_fail,
        n_not_judgable=n_not_judgable,
        n_parse_error=n_parse_error,
        axial_fail_pct=axial_fail_pct,
        open_codes_fired_count=dict(code_counter),
        top_open_codes=top_codes,
    )


def render_slack_block(summary: DigestSummary, run_label: str = "yesterday",
                        results: list[dict] | None = None,
                        prev_snapshot: dict | None = None) -> str:
    """Format the rubric scoreboard as a Slack-ready block.

    Per EVAL_STRATEGY.md §3.2: split into Accuracy track (academic axial only,
    calibrate to SME) and Experience track (intent + formatting + pedagogy +
    tone, calibrate to CSAT). Two separate calibration loops, two separate
    health numbers — never collapsed into one.

    `prev_snapshot` (optional): a dict shaped like {pass_pct, fail_pct,
    neutral_pct, acc_fail_pct, exp_fail_pct, axial_fail_pct: {ax: pct}, date}.
    When supplied, every percentage gets a `↑X.Xpp 🔴/🟢` WoW-delta annotation.
    When absent, deltas are omitted and a `_(first run)_` note is shown.
    """
    n = summary.n_judgable or 1
    has_prev = isinstance(prev_snapshot, dict) and prev_snapshot

    # Per-axial fail counts (need raw counts for the new layout)
    if results is None:
        results = []
    judgable_results = [
        r for r in results
        if r.get("overall_band") in ("PASS", "NEUTRAL", "FAIL")
    ]

    def axial_fail_count(ax: str) -> int:
        return sum(1 for r in judgable_results
                   if not r.get(ax, {}).get("passed", True))

    def axial_codes(ax: str) -> Counter:
        c: Counter = Counter()
        for r in judgable_results:
            for code in r.get(ax, {}).get("open_codes_fired", []) or []:
                c[code] += 1
        return c

    n_acc_fail = axial_fail_count("academic")
    exp_axials = ("intent", "formatting", "pedagogy", "tone")
    # Experience FAIL = ANY of the experience axials failed
    n_exp_fail = sum(
        1 for r in judgable_results
        if any(not r.get(ax, {}).get("passed", True) for ax in exp_axials)
    )

    # Per-experience-axial fail counts
    exp_axial_lines = []
    exp_axial_labels = {
        "intent": "Intent Binding",
        "formatting": "Presentation  ",
        "pedagogy": "Pedagogy      ",
        "tone": "Tone / Feel   ",
    }
    prev_axial_pct = (prev_snapshot or {}).get("axial_fail_pct") or {}
    for ax in exp_axials:
        cnt = axial_fail_count(ax)
        pct = round(100.0 * cnt / n, 1) if summary.n_judgable else 0.0
        ci = wilson_ci_pp(pct, summary.n_judgable)
        prev_p = prev_axial_pct.get(ax) if has_prev else None
        delta = _delta_marker(pct, prev_p, higher_is_bad=True)
        exp_axial_lines.append(
            f"    {exp_axial_labels[ax]} {cnt:>3} ({pct:>4.1f}% ±{ci}pp){delta}"
        )

    # Top codes by track
    acc_codes = axial_codes("academic")
    exp_codes_combined: Counter = Counter()
    for ax in exp_axials:
        exp_codes_combined.update(axial_codes(ax))

    def fmt_codes(counter: Counter, k: int = 4) -> str:
        if not counter:
            return "(none)"
        parts = []
        for code, cnt in counter.most_common(k):
            label = CODE_LABELS.get(code, code)
            parts.append(f"{code} {label.lower()} ({cnt})")
        return " | ".join(parts)

    n_total = summary.n_total
    pass_pct = round(100.0 * summary.n_pass / n, 1) if summary.n_judgable else 0.0
    neutral_pct = round(100.0 * summary.n_neutral / n, 1) if summary.n_judgable else 0.0
    fail_pct = round(100.0 * summary.n_fail / n, 1) if summary.n_judgable else 0.0
    acc_fail_pct = round(100.0 * n_acc_fail / n, 1) if summary.n_judgable else 0.0
    exp_fail_pct = round(100.0 * n_exp_fail / n, 1) if summary.n_judgable else 0.0

    parse_err = f" | PARSE_ERROR: {summary.n_parse_error}" if summary.n_parse_error else ""

    # ---- Per-stratum split (if any sample carries _stratum) ----
    strata_present = sorted({(r.get("_stratum") or "all") for r in results}) if results else []
    has_strata = len(strata_present) > 1 or (strata_present and strata_present != ["all"])
    stratum_lines = []
    if has_strata:
        # We display: for each stratum, n / Acc-FAIL% / Exp-FAIL% (with Wilson CI)
        for stratum in strata_present:
            srows = [r for r in judgable_results if (r.get("_stratum") or "all") == stratum]
            n_s = len(srows)
            if n_s == 0:
                continue
            n_s_acc = sum(1 for r in srows if not r.get("academic", {}).get("passed", True))
            n_s_exp = sum(
                1 for r in srows
                if any(not r.get(ax, {}).get("passed", True) for ax in exp_axials)
            )
            acc_p = round(100.0 * n_s_acc / n_s, 1)
            exp_p = round(100.0 * n_s_exp / n_s, 1)
            acc_ci = wilson_ci_pp(acc_p, n_s)
            exp_ci = wilson_ci_pp(exp_p, n_s)
            stratum_lines.append(
                f"  {stratum:<10} n={n_s:<4} "
                f"acc-FAIL {n_s_acc} ({acc_p}% ±{acc_ci}pp)  "
                f"exp-FAIL {n_s_exp} ({exp_p}% ±{exp_ci}pp)"
            )
    stratum_block = (
        f"\n🎚️ *By stratum* (calibration signal — accuracy FAIL should drop by stratum)\n"
        f"{chr(10).join(stratum_lines)}\n"
        if stratum_lines else ""
    )

    # ---- Per-chapter hotspots (top 5 worst by acc-FAIL and exp-FAIL) ----
    # Group judgable results by chapter; require >= 5 samples to qualify (noise floor).
    chapter_stats: dict[str, dict[str, int]] = {}
    for r in judgable_results:
        ch = r.get("_chapter") or "unknown"
        d = chapter_stats.setdefault(ch, {"n": 0, "acc_fail": 0, "exp_fail": 0})
        d["n"] += 1
        if not r.get("academic", {}).get("passed", True):
            d["acc_fail"] += 1
        if any(not r.get(ax, {}).get("passed", True) for ax in exp_axials):
            d["exp_fail"] += 1

    eligible_chapters = [
        (ch, d) for ch, d in chapter_stats.items()
        if d["n"] >= 5 and ch != "unknown"
    ]

    chapter_block = ""
    if eligible_chapters:
        worst_acc = sorted(
            eligible_chapters,
            key=lambda kv: (kv[1]["acc_fail"] / kv[1]["n"], kv[1]["n"]),
            reverse=True,
        )[:5]
        worst_exp = sorted(
            eligible_chapters,
            key=lambda kv: (kv[1]["exp_fail"] / kv[1]["n"], kv[1]["n"]),
            reverse=True,
        )[:5]

        def _ch_line(ch: str, d: dict, kind: str) -> str:
            cnt = d[f"{kind}_fail"]
            n_ch = d["n"]
            pct = round(100.0 * cnt / n_ch, 1)
            ci = wilson_ci_pp(pct, n_ch)
            # Truncate chapter name for fixed-width readability
            label = (ch[:32] + "…") if len(ch) > 33 else ch
            return f"    {label:<34} n={n_ch:<4} {kind}-FAIL {cnt} ({pct}% ±{ci}pp)"

        acc_lines = [_ch_line(ch, d, "acc") for ch, d in worst_acc]
        exp_lines = [_ch_line(ch, d, "exp") for ch, d in worst_exp]
        chapter_block = (
            "\n🏫 *Per-Chapter Hotspots* (top 5 by FAIL rate, min 5 samples)\n"
            "  Worst by ACCURACY FAIL:\n"
            f"{chr(10).join(acc_lines)}\n"
            "  Worst by EXPERIENCE FAIL:\n"
            f"{chr(10).join(exp_lines)}\n"
        )

    # CI annotations + WoW deltas for top-line metrics
    acc_ci_top = wilson_ci_pp(acc_fail_pct, summary.n_judgable)
    exp_ci_top = wilson_ci_pp(exp_fail_pct, summary.n_judgable)
    pass_ci = wilson_ci_pp(pass_pct, summary.n_judgable)
    neutral_ci = wilson_ci_pp(neutral_pct, summary.n_judgable)
    fail_ci = wilson_ci_pp(fail_pct, summary.n_judgable)

    if has_prev:
        prev_acc = prev_snapshot.get("acc_fail_pct")
        prev_exp = prev_snapshot.get("exp_fail_pct")
        prev_pass = prev_snapshot.get("pass_pct")
        prev_neutral = prev_snapshot.get("neutral_pct")
        prev_fail = prev_snapshot.get("fail_pct")
        wow_header = (
            f"_WoW vs {prev_snapshot.get('date', 'previous run')}: "
            f"acc-FAIL {prev_acc}% → {acc_fail_pct}%"
            f"{_delta_marker(acc_fail_pct, prev_acc, higher_is_bad=True)} | "
            f"exp-FAIL {prev_exp}% → {exp_fail_pct}%"
            f"{_delta_marker(exp_fail_pct, prev_exp, higher_is_bad=True)}_\n"
        )
    else:
        prev_acc = prev_exp = prev_pass = prev_neutral = prev_fail = None
        wow_header = "_(first run — no WoW deltas)_\n"

    acc_delta = _delta_marker(acc_fail_pct, prev_acc, higher_is_bad=True) if has_prev else ""
    exp_delta = _delta_marker(exp_fail_pct, prev_exp, higher_is_bad=True) if has_prev else ""
    pass_delta = _delta_marker(pass_pct, prev_pass, higher_is_bad=False) if has_prev else ""
    neutral_delta = _delta_marker(neutral_pct, prev_neutral, higher_is_bad=True) if has_prev else ""
    fail_delta = _delta_marker(fail_pct, prev_fail, higher_is_bad=True) if has_prev else ""

    block = f"""\
🎯 *Rubric Scoreboard ({run_label}, n={n_total})*
{wow_header}
📚 *ACCURACY TRACK* [DS — calibrate to SME, NOT CSAT]
  Academic FAIL rate: {n_acc_fail}/{summary.n_judgable} ({acc_fail_pct}% ±{acc_ci_top}pp){acc_delta}
  Top codes: {fmt_codes(acc_codes, 4)}
  _Note: CSAT is silent on accuracy — only SME audit calibrates this._

✨ *EXPERIENCE TRACK* [PM, DS — calibrate to CSAT]
  Experience FAIL rate: {n_exp_fail}/{summary.n_judgable} ({exp_fail_pct}% ±{exp_ci_top}pp){exp_delta}
  By axial (count, % of judgable, ±Wilson 95% CI):
{chr(10).join(exp_axial_lines)}
  Top codes: {fmt_codes(exp_codes_combined, 5)}
  _Where leverage exists — prompt tuning targets these._
{stratum_block}{chapter_block}
📊 *Overall band* (derived; reporting only)
  PASS {summary.n_pass} ({pass_pct}% ±{pass_ci}pp){pass_delta} | NEUTRAL {summary.n_neutral} ({neutral_pct}% ±{neutral_ci}pp){neutral_delta} | FAIL {summary.n_fail} ({fail_pct}% ±{fail_ci}pp){fail_delta}
  NOT_JUDGABLE: {summary.n_not_judgable}{parse_err}
"""
    return block


# ---------------------------------------------------------------------------
# Command-line entry points
# ---------------------------------------------------------------------------

def cmd_test(args: argparse.Namespace) -> None:
    """Run the synthetic test cases shipped in test_cases.json."""
    test_path = Path(__file__).parent / "test_cases.json"
    if not test_path.exists():
        sys.exit(f"ERROR: {test_path} not found")
    with test_path.open() as f:
        cases = json.load(f)

    print(f"Running {len(cases)} synthetic test cases against {DEFAULT_MODEL}...\n")
    client = get_openai_client()

    results = []
    for i, case in enumerate(cases, 1):
        label = case.get("label", f"case-{i}")
        expected = case.get("expected_overall_band", "?")
        print(f"[{i}/{len(cases)}] {label} (expected {expected})... ", end="", flush=True)
        try:
            parsed, meta = call_judge(client, case, model=args.model)
        except Exception as e:
            print(f"ERROR: {e}")
            results.append({"_label": label, "_parse_error": True, "_error": str(e)})
            continue

        validation = validate_judge_output(parsed)
        actual = parsed.get("overall_band")
        match = actual == expected
        marker = "✅" if match else "⚠️ "
        val_marker = "✓" if validation.ok else "✗ schema"
        print(f"got {actual} {marker} {val_marker}  ({meta['latency_s']}s, "
              f"{meta.get('input_tokens')}→{meta.get('output_tokens')} tok)")
        if not validation.ok:
            for err in validation.errors:
                print(f"     ! {err}")
        if not match and validation.ok:
            codes = parsed.get("all_open_codes_fired", [])
            print(f"     codes fired: {codes}")

        parsed["_label"] = label
        parsed["_expected_band"] = expected
        parsed["_match"] = match
        parsed["_validation_ok"] = validation.ok
        parsed["_meta"] = meta
        results.append(parsed)

    # Summary
    print()
    summary = aggregate(results)
    print(render_slack_block(summary, run_label="test run", results=results))

    # Optional save
    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nSaved {len(results)} results to {args.output}")


def cmd_input(args: argparse.Namespace) -> None:
    """Run on a JSON file of real samples (one object per trace).

    Each sample may carry a `stratum` field (e.g. "downvote", "upvote",
    "no_vote") for per-stratum aggregation. If absent, all samples roll up
    under stratum "all".

    With --write-scores, judge verdicts are pushed to Langfuse as scores
    attached to the production trace_id (sample.trace_id).
    """
    with open(args.input) as f:
        samples = json.load(f)
    if not isinstance(samples, list):
        sys.exit("ERROR: input file must be a JSON list of sample objects")

    print(f"Running judge on {len(samples)} samples...\n")
    client = get_openai_client()

    # judge_run_id = single id for the whole batch — useful for grouping
    judge_run_id = args.run_id or f"daily-eval-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    write_scores = bool(args.write_scores)
    if write_scores:
        lf_check = _get_langfuse_writer()
        if lf_check is None:
            print("⚠️  --write-scores set but Langfuse keys missing/disabled. Continuing without writes.\n")
            write_scores = False
        else:
            print(f"📡 Writing judge scores to Langfuse. judge_run_id={judge_run_id}\n")

    results = []
    scores_written_total = 0
    for i, s in enumerate(samples, 1):
        tid = s.get("trace_id", f"sample-{i}")
        stratum = s.get("stratum", "all")
        print(f"[{i}/{len(samples)}] {stratum:<10} {tid[:36]}... ", end="", flush=True)
        try:
            parsed, meta = call_judge(client, s, model=args.model)
            validation = validate_judge_output(parsed)
            parsed["_trace_id"] = tid
            parsed["_stratum"] = stratum
            parsed["_validation_ok"] = validation.ok
            parsed["_validation_errors"] = validation.errors
            parsed["_meta"] = meta
            band = parsed.get("overall_band")
            print(f"{band}  ({meta['latency_s']}s)", end="")

            if write_scores and validation.ok:
                n_scores = write_judge_scores_to_langfuse(
                    production_trace_id=tid, parsed=parsed,
                    judge_run_id=judge_run_id, judge_model=meta.get("model_param", ""),
                )
                scores_written_total += n_scores
                print(f"  +{n_scores} scores")
            else:
                print()
        except Exception as e:
            parsed = {"_trace_id": tid, "_stratum": stratum, "_parse_error": True, "_error": str(e)}
            print(f"ERROR: {e}")
        results.append(parsed)

    if write_scores:
        try:
            _get_langfuse_writer().flush()
            print(f"\n📡 Langfuse flush complete. Wrote {scores_written_total} scores total.")
        except Exception as e:
            print(f"\n📡 Langfuse flush warning: {e}")

    out = args.output or "results.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {len(results)} results to {out}")

    summary = aggregate(results)
    print()
    print(render_slack_block(summary, run_label=f"sample n={len(results)}", results=results))


def cmd_slack_block(args: argparse.Namespace) -> None:
    """Render Slack block from a previously saved results.json."""
    with open(args.input) as f:
        results = json.load(f)
    summary = aggregate(results)
    print(render_slack_block(summary, run_label=args.label, results=results))


def main() -> None:
    p = argparse.ArgumentParser(description="v8 Master Judge runner")
    sub = p.add_subparsers(dest="cmd", required=False)

    p_test = sub.add_parser("test", help="Run synthetic test cases")
    p_test.add_argument("--model", default=DEFAULT_MODEL)
    p_test.add_argument("--output", help="Save results JSON")

    p_in = sub.add_parser("input", help="Run on a JSON file of samples")
    p_in.add_argument("input", help="Path to input JSON")
    p_in.add_argument("--model", default=DEFAULT_MODEL)
    p_in.add_argument("--output", help="Path to write results")
    p_in.add_argument("--write-scores", action="store_true",
                      help="Push judge verdicts to Langfuse as scores on the production trace_id")
    p_in.add_argument("--run-id", help="Judge run identifier (default: daily-eval-YYYY-MM-DD)")

    p_slack = sub.add_parser("slack-block", help="Render Slack block from results")
    p_slack.add_argument("input", help="Path to results JSON")
    p_slack.add_argument("--label", default="yesterday")

    # Convenience top-level flags
    p.add_argument("--test", action="store_true", help="alias for `test` subcommand")
    p.add_argument("--input", help="alias for `input` subcommand")
    p.add_argument("--slack-block", help="alias for `slack-block` subcommand")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--output", help="output path")
    p.add_argument("--write-scores", action="store_true",
                   help="Push judge verdicts to Langfuse (works with --input)")
    p.add_argument("--run-id", help="Judge run identifier (default: daily-eval-YYYY-MM-DD)")

    args = p.parse_args()

    # Allow --test / --input shortcuts
    if args.test or args.cmd == "test":
        cmd_test(args)
    elif args.input or args.cmd == "input":
        if not args.input:
            sys.exit("ERROR: --input PATH required")
        cmd_input(args)
    elif args.slack_block or args.cmd == "slack-block":
        if not args.slack_block:
            sys.exit("ERROR: --slack-block PATH required")
        args.input = args.slack_block
        args.label = getattr(args, "label", "yesterday")
        cmd_slack_block(args)
    else:
        p.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
