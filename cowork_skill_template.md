# Cowork Scheduled Task Template — `ask-ai-eval-daily`

This is the SKILL.md to drop into Cowork as a **second** scheduled task — separate from `ask-ai-daily-digest`. The existing digest stays untouched.

## Suggested Schedule
- **Time:** 08:30 IST weekdays (30 min before the existing digest at 09:00 IST)
- **Channel:** can be the same `C0B2KT5RQ0H`, OR a separate eval channel — your call
- **Naming convention:** `ask-ai-eval-daily`

## SKILL.md Body

Save the following into `/Users/pw/Documents/Claude/Scheduled/ask-ai-eval-daily/SKILL.md`:

```markdown
---
name: ask-ai-eval-daily
description: Runs the v8 master judge against yesterday's stratified Ask AI sample (all downvotes + 10% upvotes + 10% no-votes), writes scores to Langfuse, posts a single Slack message with per-stratum FAIL rates and top open codes. Complements (does NOT replace) the existing ask-ai-daily-digest.
---

# Ask AI — Daily Eval Drop

Runs every weekday at 08:30 IST. Posts a single Slack message that gives PMs and DS the previous day's eval signal split by stratum.

## What it does

1. Pull yesterday's stratified academic sample from Metabase question `${METABASE_QUESTION_ID}` (saved query at `~/PW Claude Skills/local-agents/eval-sampler/sql/daily_stratified_sample.sql`):
   - 100% of downvotes (rating=0)
   - 10% random sample of upvotes (rating=6)
   - 10% random sample of no-votes (rating IS NULL)
   - Hard caps in SQL: 1000 / 500 / 500
2. Run v8 master judge against every sample (Azure gpt-4.1 deployment)
3. Write per-axial + per-open-code scores to Langfuse, attached to each production trace_id (so they appear alongside CSAT in the Langfuse UI)
4. Post a Slack message with the dual-track scoreboard + per-stratum split

## How to run

```bash
cd /Users/pw/PW\ Claude\ Skills/local-agents/eval-sampler
python3 daily_eval.py
```

Cost: ~$5–10/day (gpt-4.1 Azure) for ~1000–2000 samples. Latency: ~10–25 min batch run depending on volume.

## Required env (set in Cowork before invoking)

```bash
# Azure OpenAI (judge)
AZURE_ENDPOINT=https://<resource>.openai.azure.com
AZURE_API_KEY=<azure-key>
AZURE_API_VERSION=2024-08-01-preview
DEPLOYMENT_NAME=<gpt-4.1-deployment-name>

# Metabase (sample pull)
METABASE_URL=https://metabase-prod.penpencil.co
METABASE_API_KEY=<your Metabase API key>   # preferred — use for SSO/Google accounts
# METABASE_USERNAME=<your metabase email>  # fallback if no API key
# METABASE_PASSWORD=<your metabase password>  # fallback if no API key
METABASE_QUESTION_ID=<id of the saved daily_stratified_sample question>

# Langfuse (score writes — recommended)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com

# Slack (post target)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

## Failure handling

- If Metabase auth fails → task aborts cleanly, no Slack post
- If individual judge calls error → those rows are recorded as `_parse_error` and excluded from aggregations; the rest of the batch continues
- If Langfuse score write fails → judge results still saved to JSON; only the Langfuse leg is degraded
- If Slack webhook returns non-ok → task exits with non-zero code (Cowork will surface this)

## Where things live

- Runner: `/Users/pw/PW Claude Skills/local-agents/eval-sampler/judge_runner.py`
- Orchestrator: `/Users/pw/PW Claude Skills/local-agents/eval-sampler/daily_eval.py`
- Stratified-sample SQL: `/Users/pw/PW Claude Skills/local-agents/eval-sampler/sql/daily_stratified_sample.sql`
- Daily output: `/tmp/daily_eval_results.json` (overwritten each run)
```

## One-time setup before the first scheduled run

1. **Save the SQL as a Metabase question.** Open https://metabase-prod.penpencil.co → New question → SQL editor → paste from `sql/daily_stratified_sample.sql` → save with name *"Ask AI Daily Stratified Eval Sample"*. Note the question id from the URL (e.g. `metabase-prod.penpencil.co/question/12345` → id is `12345`).

2. **Create a Slack incoming webhook.** Slack → Apps → Incoming Webhooks → pick channel (suggested: same `C0B2KT5RQ0H` as the existing digest, OR new `#ask-ai-eval` if you want isolation) → copy webhook URL.

3. **Verify Langfuse keys are reachable.** They were shared by Satyam on Apr 23 — should already be in your `.langfuse-env` or accessible via the existing eval-dashboard `lf:fetch` flow.

4. **Dry run before scheduling:**
   ```bash
   cd /Users/pw/PW\ Claude\ Skills/local-agents/eval-sampler
   export AZURE_ENDPOINT=...   # all 4 Azure vars
   export AZURE_API_KEY=...
   export AZURE_API_VERSION=...
   export DEPLOYMENT_NAME=...
   export METABASE_URL=...     # 4 Metabase vars
   export METABASE_USERNAME=...
   export METABASE_PASSWORD=...
   export METABASE_QUESTION_ID=...
   export LANGFUSE_PUBLIC_KEY=...   # 3 Langfuse vars (optional but recommended)
   export LANGFUSE_SECRET_KEY=...
   export LANGFUSE_HOST=https://cloud.langfuse.com
   # NO SLACK_WEBHOOK_URL → forces dry-run path automatically

   python3 daily_eval.py --dry-run
   ```
   You'll see Metabase pull, judge loop, Langfuse score writes (or skip), and the Slack block printed to stdout — but nothing posted to Slack. **Eyeball the block carefully.** Once it looks right, set `SLACK_WEBHOOK_URL` and remove `--dry-run`.

5. **Register with Cowork.** Open Cowork → Scheduled section → New scheduled task → paste the SKILL.md above → schedule for 08:30 IST weekdays.

## Why a separate task (not extending the existing digest)

You said "I love the existing daily digest" — and that's exactly the right instinct. Keeping eval as a separate post means:

- **Audience clarity.** The existing digest is daily-pulse-of-students. The new post is daily-pulse-of-quality-system. Two different mental models, two messages.
- **Failure isolation.** If the eval pull breaks (Metabase auth issue, Langfuse outage, judge timeout), the digest still ships. Each task fails independently.
- **Iteration safety.** You can change the eval message format without touching the digest. We'll likely refine the rubric block several times in the first 2 weeks; you don't want to risk the digest each iteration.
- **Different schedules possible.** Eval at 08:30, digest at 09:00 → eval lands first so a PM scrolling at 09:00 sees both. Or run eval weekly if daily turns out to be too much signal.

## What this looks like once running

Every weekday morning, a Slack message like:

```
🎯 Rubric Scoreboard (daily-eval-2026-05-08, n=1247)

📚 ACCURACY TRACK [DS — calibrate to SME, NOT CSAT]
  Academic FAIL rate: 88/1247 (7.1%)
  Top codes: A5 answer incomplete (52) | A4 calculation error (18) | ...

✨ EXPERIENCE TRACK [PM, DS — calibrate to CSAT]
  Experience FAIL rate: 134/1247 (10.7%)
  By axial:
    Intent Binding   12 (1.0%)
    Presentation     19 (1.5%)
    Pedagogy         71 (5.7%)
    Tone / Feel      48 (3.8%)
  Top codes: D1 too advanced (35) | E1 too long (30) | D3 no direct answer (24) | ...

🎚️ By stratum (calibration signal)
  downvote   n=485   acc-FAIL 78 (16.1%)  exp-FAIL 91 (18.8%)
  no_vote    n=412   acc-FAIL 7  (1.7%)   exp-FAIL 28 (6.8%)
  upvote     n=350   acc-FAIL 3  (0.9%)   exp-FAIL 15 (4.3%)

📊 Overall band (derived; reporting only)
  PASS 1025 (82.2%) | NEUTRAL 134 (10.7%) | FAIL 88 (7.1%)
```

The "By stratum" line is the **calibration health metric** — academic FAIL rate should drop from downvote → no_vote → upvote. If those numbers ever invert (e.g., upvote acc-FAIL > downvote acc-FAIL), the judge has a bias problem and we know to retune.

## Future enhancements (not blocking initial rollout)

- Pull the same trace_ids' classifier-disagreement scores from Anshik's pipeline once it's wired, render a third "Classifier Track" block
- Pull silver_stream_logs for the same trace_ids, render an "Architecture Health" block (per `DIGEST_V2_PLAN.md`)
- Replace Metabase poll with a query against `gold_ask_ai_quality_pulse` once that table lands (per `EVAL_PULSE_TABLE_PLAN.md`) — single SELECT instead of stratified-sample SQL
- Add 7-day rolling delta to each metric (`E1 too long: 30 (-4 vs 7d avg)`)
- Auto-thread spike alerts into `#chakra-ai-product-ds` if any metric breaches a rolling-mean threshold
