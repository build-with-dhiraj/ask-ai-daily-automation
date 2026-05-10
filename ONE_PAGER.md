# Ask AI — Daily LLM Judge Eval (one-pager)

## What this is

Each day (**04:00 IST** schedule, cron `30 22 * * *` UTC) a GitHub Action runs on a **self-hosted runner** inside the PW network. It pulls a **stratified sample** of yesterday’s Video Co-Pilot **academic** doubts from Metabase, runs the **v8 rubric judge** (Azure OpenAI), optionally writes scores to **Langfuse**, and posts a **Slack scorecard** (`#ask-ai-evals` or your configured webhook). **Daily Digest** runs in the same workflow immediately after eval.

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

### Step 2 — Digest Metabase cards + GitHub secrets (behaviour + stream logs)

This powers the **Silent-failure proxies** and **Video co-pilot API health** blocks in the 09:30 IST digest ([`daily_digest.py`](daily_digest.py)). The workflow maps secrets under `jobs.digest.env` in [`.github/workflows/daily-digest.yml`](.github/workflows/daily-digest.yml).

**Production (Physics Wallah Metabase)** — use these question ids as secret values (**digits only**, no quotes):

| Actions secret | Metabase question |
|----------------|-------------------|
| `METABASE_BEHAVIOR_FOLLOWUP_CARD_ID` | **[33282](https://metabase-prod.penpencil.co/question/33282-metabase-behavior-followup-card)** — SQL: [sql/behavior_followup_burst.sql](sql/behavior_followup_burst.sql) |
| `METABASE_BEHAVIOR_REPHRASE_CARD_ID` | **[33283](https://metabase-prod.penpencil.co/question/33283-metabase-behavior-rephrase-card)** — SQL: [sql/behavior_rephrase_keywords.sql](sql/behavior_rephrase_keywords.sql) |
| `METABASE_STREAM_LOGS_CARD_ID` | **[33285](https://metabase-prod.penpencil.co/question/33285-metabase-stream-logs-card)** — SQL: [sql/vcp_stream_logs_digest_summary.sql](sql/vcp_stream_logs_digest_summary.sql) |

Setup checklist:

1. **GitHub** → **Settings → Secrets and variables → Actions** → create or update the three secrets above with **`33282`**, **`33283`**, **`33285`** respectively.
2. **Metabase:** confirm each saved question’s SQL matches the linked file in this repo (replace entire query if you cloned or recreated a card).
3. **Daily Digest** run (schedule or **Actions → Daily Digest → Run workflow**) picks up the ids from secrets; no ids are hardcoded in the app.

The digest is delivered as **Slack Block Kit** (`blocks`) plus a **multi-line plain-text summary** for notifications and accessibility; if a section looks compressed in search, open the message in the channel.

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

- **Scheduled prod chain:** [`.github/workflows/daily-automation.yml`](.github/workflows/daily-automation.yml) runs **Daily Eval** then **Daily Digest** on one cron; the eval snapshot JSON is passed as an **artifact** so digest always sees `formatting_hotspot_chapters` even across multiple runners. [`daily-eval.yml`](.github/workflows/daily-eval.yml) and [`daily-digest.yml`](.github/workflows/daily-digest.yml) keep **`workflow_dispatch` only** for ad-hoc runs.
- Workflows require **`runs-on: self-hosted`** so the runner can reach **Metabase** on the internal network.
- If jobs queue forever, re-check runner registration (online + not busy blocking other jobs).

## Phase 2 (roadmap)

Quarterly judge–human calibration, second-judge disagreements, embedding-cluster sampling, and judge cost trending — revisit once the daily eval loop has weeks of stable WoW data.

---

*Repo: [ask-ai-daily-automation](https://github.com/build-with-dhiraj/ask-ai-daily-automation)*
