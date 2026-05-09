# eval-sampler — v8 Master Judge Runner

Internal evaluation tool for Ask AI / Chakra AI / Video Copilot. Runs the locked 5-axial / 18-code rubric against production samples and outputs Slack-ready aggregations for the daily digest.

**This tool does not touch the production answer pipeline.** It only reads traces and writes scores to Langfuse. Nothing students see is affected.

## What It Does

1. Loads sample(s) — synthetic test cases OR a JSON file of real production traces
2. Calls the v8 Master Judge prompt via OpenAI for each sample
3. Parses + schema-validates every judge output
4. Aggregates results into:
   - PASS / NEUTRAL / FAIL counts (overall_band)
   - Per-axial fail % (academic / intent / formatting / pedagogy / tone)
   - Top open codes fired (A1–A6, B1, C1–C4, D1–D4, E1–E3 — 18 codes)
5. Renders a Slack-ready block ready to paste into the daily digest

Optional: if Langfuse keys are set, traces every judge call automatically (via the Langfuse OpenAI drop-in).

## Setup

```bash
cd "/Users/pw/PW Claude Skills/local-agents/eval-sampler"

# Install deps
pip install openai

# Optional: enable Langfuse tracing
pip install langfuse python-dotenv

# Set keys in env (NEVER paste keys into chat or commit them)
export OPENAI_API_KEY=sk-...

# Optional Langfuse tracing
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_HOST=https://cloud.langfuse.com
```

## Quick test (synthetic cases)

Runs the 7 hand-crafted test cases in `test_cases.json` — covering PASS / FAIL via A1, A4, A6 / NEUTRAL / NOT_JUDGABLE / good intent handling. Each has an `expected_overall_band`; the runner reports match/mismatch.

```bash
python judge_runner.py --test
```

Sample output:

```
Running 7 synthetic test cases against gpt-4.1-2025-04-14...

[1/7] PASS — clean academic answer (Physics) (expected PASS)... got PASS ✅ ✓  (2.1s, 1124→320 tok)
[2/7] FAIL — A1 Conceptual error (Chemistry) (expected FAIL)... got FAIL ✅ ✓  (1.8s, 1158→284 tok)
[3/7] FAIL — A4 Calculation error (Maths) (expected FAIL)... got FAIL ✅ ✓  (1.6s, 1102→210 tok)
[4/7] NEUTRAL — academically correct but verbose ... (expected NEUTRAL)... got NEUTRAL ✅ ✓
[5/7] NOT_JUDGABLE — non-academic doubt (expected NOT_JUDGABLE)... got NOT_JUDGABLE ✅ ✓
[6/7] FAIL — A6 incorrectly validates student's wrong claim (expected FAIL)... got FAIL ✅ ✓
[7/7] PASS — clarifying question for ambiguous doubt (expected PASS)... got PASS ✅ ✓

🎯 *Rubric Scoreboard (test run, n=7)* [PM, DS]
Overall: PASS 2 (33.3%) | NEUTRAL 1 (16.7%) | FAIL 3 (50.0%)
NOT_JUDGABLE: 1

By axial (% of judgable with non-PASS):
  Academic Correctness  50.0%
  Intent Binding         0.0%
  Presentation           0.0%
  Pedagogical Fit       16.7%
  Look & Feel / Tone    16.7%

Top open codes fired:
  A1 Conceptual error............. 1
  A4 Calculation error............ 1
  A6 Incorrect validation......... 1
  D3 No direct answer............. 1
  E1 Too long..................... 1
```

If something doesn't match (`⚠️ ` instead of `✅`), the prompt may need refinement — the `parsed.all_open_codes_fired` array is shown for analysis.

## Run on real production samples

Format your input JSON as a list of objects, one per trace:

```json
[
  {
    "trace_id": "a2879f2f-fa89-4334-b022-9f97d793fecb",
    "doubt": "What is electric charge?",
    "ai_answer": "Beta, electric charge ek fundamental property hai...",
    "transcript": "...",
    "ideal_answer": "",
    "subject": "Physics",
    "chapter": "Electric Charges and Fields",
    "student_class": "12",
    "exam": "JEE",
    "image_url": "https://static.pw.live/...",
    "is_annotated": false
  }
]
```

Then:

```bash
python judge_runner.py input samples.json --output results.json
```

The Slack block prints to stdout at the end; copy-paste into your digest channel.

## Re-render Slack block from saved results

```bash
python judge_runner.py slack-block results.json --label "Tue 7 May"
```

## What gets validated

Every judge output is checked for:
- All 5 axials present with `passed: bool` and `open_codes_fired: list`
- All open codes in the legal set (A1–A6 / B1 / C1–C4 / D1–D4 / E1–E3)
- Per-axial: `passed=true` ⇔ `open_codes_fired` empty
- Decision-logic determinism: `overall_band` matches the deterministic computation from per-axial passes (Binary Kill Switch on Academic)
- `all_open_codes_fired` equals the union of per-axial codes
- `confidence` ∈ {low, med, high}

If validation fails, the per-trace result carries `_validation_ok: false` + `_validation_errors: [...]`. The Slack block still renders, but parse_error rows are excluded from aggregations.

## Files

- `v8_master_judge_prompt.md` — the prompt spec (taxonomy, decision logic, output schema)
- `judge_runner.py` — the runner (system prompt embedded inline)
- `test_cases.json` — 7 synthetic cases covering the main verdicts
- `README.md` — this file

## Cost estimate

Per judge call (gpt-4.1-2025-04-14, ~1.1K input tokens, ~300 output tokens):
- Input: 1.1K × $2.00 / 1M = $0.0022
- Output: 0.3K × $8.00 / 1M = $0.0024
- **Total: ~$0.005 per judge call**

100 daily samples × $0.005 = **$0.50/day** ≈ **₹42/day** ≈ **₹15K/year**.

(Numbers approximate — re-verify against current OpenAI pricing.)

## Next steps for production rollout

1. **Wire to real production samples.** The samples JSON should come from `astracdc.silver_conversational_query_table` joined with `central.silver_stream_logs` and `silver_prod_feedback_by_user_entity` — see `EVAL_PULSE_TABLE_PLAN.md` for the canonical schema. For the first run, you can pull yesterday's ~50 downvoted academic traces from Metabase (Q23036) and the corresponding query/answer rows.
2. **Wire score writes to Langfuse.** When ready, extend `call_judge` to call `langfuse.create_score(trace_id=..., name="judge_overall_band", value=..., string_value=...)` for each axial + the overall band. This makes scores queryable in the Langfuse UI per `LANGFUSE_TRACING_PLAN.md` §3.2.
3. **Wire into the Cowork daily digest.** Add a step in `/Users/pw/Documents/Claude/Scheduled/ask-ai-daily-digest/SKILL.md` that runs this script with yesterday's sample, captures the Slack block from stdout, and appends it to the digest message before posting. See `digest_block_template.md` for the patch shape.
