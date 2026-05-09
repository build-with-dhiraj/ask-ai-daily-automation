# Ask AI — Daily LLM Judge Eval (one-pager)

## What this is

Every morning (08:30 IST) a GitHub Action runs on a **self-hosted runner** inside the PW network. It pulls a **stratified sample** of yesterday’s Video Co-Pilot **academic** doubts from Metabase, runs the **v8 rubric judge** (Azure OpenAI), optionally writes scores to **Langfuse**, and posts a **Slack scorecard** (`#ask-ai-evals` or your configured webhook).

This is **production monitoring**, not training-data labeling. The goal is **directional** health: catch regressions early at manageable cost.

## What it is not

- Not SME review of every query.
- Not a guarantee of statistical power at **per-chapter** granularity for tiny effect sizes (see thresholds below).

## Rubric bands (reporting)

- **PASS** / **NEUTRAL** / **FAIL** — overall judge verdict on the answer trace.
- **ACCURACY track** — grounded in the **academic** axial (calibrate to SME, not raw CSAT).
- **EXPERIENCE track** — intent, formatting, pedagogy, tone (calibrate to product/CSAT signals).

## Detection thresholds (how to read WoW)

| Scope | What you can claim |
|--------|-------------------|
| **Product-level** | ~±5 pp swings with ~1–2k judgable traces/day are **directionally meaningful** (Wilson CIs on the scorecard bracket this). |
| **Per-chapter** | Treat **10–15%** FAIL-rate moves as the realistic resolution unless you **over-sample** that chapter. |

## Cost

Order of magnitude **~$2–5 USD/day** at current sample volume (~3k traces, model-dependent). The Slack footer shows **estimated USD + INR** from token counts.

## Operations checklist

### Metabase Question **33193** (daily stratified sample)

1. Open [Metabase](https://metabase-prod.penpencil.co) → Question **33193**.
2. Edit SQL → replace the **entire** query with the contents of [sql/daily_stratified_sample.sql](sql/daily_stratified_sample.sql) in this repo.
3. Save → run once; expect on the order of **~3,000 rows** for a high-traffic day (caps in the SQL bound the maximum).

`METABASE_QUESTION_ID` in GitHub Actions secrets must stay **`33193`** unless you intentionally point to a clone.

### Step 2 — Behavioral Metabase cards + GitHub secrets (optional digest)

This powers the **Silent-failure proxies** block in the 09:30 IST digest ([`daily_digest.py`](daily_digest.py)). Skip until you want the cross-check; the digest shows a “not configured” note until then.

1. **Metabase → New → SQL query** → paste [sql/behavior_followup_burst.sql](sql/behavior_followup_burst.sql) → run → **Save**. Name e.g. `Ask AI — follow-up burst by chapter`. Copy the question id from the URL: `/question/<ID>`.
2. **Repeat** for [sql/behavior_rephrase_keywords.sql](sql/behavior_rephrase_keywords.sql) → e.g. `Ask AI — rephrase keywords by chapter`.
3. **GitHub** → repo **ask-ai-daily-automation** → **Settings → Secrets and variables → Actions** → **New repository secret**:
   - Name: `METABASE_BEHAVIOR_FOLLOWUP_CARD_ID` → value: **digits only** (e.g. `33201`).
   - Name: `METABASE_BEHAVIOR_REPHRASE_CARD_ID` → value: **digits only** (e.g. `33283`).
4. Next **Daily Digest** run (schedule or **Actions → Daily Digest → Run workflow**) will fetch both cards. No code change needed.

**Production (Physics Wallah Metabase):** follow-up **[33282](https://metabase-prod.penpencil.co/question/33282-metabase-behavior-followup-card)** · rephrase **[33283](https://metabase-prod.penpencil.co/question/33283-metabase-behavior-rephrase-card)** — set GitHub secrets to **`33282`** and **`33283`** respectively.

**Stream logs (E2E API health):** **[33285](https://metabase-prod.penpencil.co/question/33285-metabase-stream-logs-card)** — SQL in [sql/vcp_stream_logs_digest_summary.sql](sql/vcp_stream_logs_digest_summary.sql); GitHub secret **`METABASE_STREAM_LOGS_CARD_ID`** = **`33285`**. Powers the *Video co-pilot API health* block in the digest (calendar yesterday vs Langfuse rolling 24h).

The digest also reads **`EVAL_SUMMARY_PATH`** (`/tmp/daily_eval_yesterday_summary.json` on the runner). For the **Confirmed regression signal** line, the **Daily Eval** job should succeed on that machine **before** digest so the snapshot file exists.

### Step 3 — Confirm Daily Eval Slack (after a green run)

When [Daily Eval](.github/workflows/daily-eval.yml) finishes successfully, the webhook message should include:

- **Per-Chapter Hotspots** (top accuracy + experience FAIL)
- **±…pp** Wilson intervals on rates
- **WoW** deltas or **_(first run — no WoW deltas)_**
- **Run cost** with **By stratum** token/cost lines
- **Eval one-pager** link in the footer

If a workflow stays **queued** for a long time, another job may be holding the self-hosted runner — check **Actions** and cancel or wait for the **in_progress** run. Then re-run **Daily Eval** via **Run workflow**.

### Step 4 — Golden set (`golden_set.json`)

[SME workflow](golden_set.json): only rows with `"enabled": true` and non-empty `doubt` + `ai_answer` run every **Monday** ([`golden-smoke.yml`](.github/workflows/golden-smoke.yml)).

1. Pick production or lab traces; SMEs lock **`expected_overall_band`** (`PASS` / `NEUTRAL` / `FAIL` / `NOT_JUDGABLE`).
2. Replace placeholder objects (currently `golden-008` … `golden-050`): set fields and `"enabled": true`.
3. Commit to `main` (or PR); smoke test fails the workflow if the judge disagrees with the baseline — **Slack alert on failure** if `SLACK_WEBHOOK_URL` is set.

### GitHub / runner

- Workflows require **`runs-on: self-hosted`** so the runner can reach **Metabase** on the internal network.
- If jobs queue forever, re-check runner registration (online + not busy blocking other jobs).

## Phase 2 (roadmap)

Quarterly judge–human calibration, second-judge disagreements, embedding-cluster sampling, and judge cost trending — revisit once the daily eval loop has weeks of stable WoW data.

---

*Repo: [ask-ai-daily-automation](https://github.com/build-with-dhiraj/ask-ai-daily-automation)*
