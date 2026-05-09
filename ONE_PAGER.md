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

### Optional: behavioral Metabase cards (daily digest)

To populate the **Silent-failure proxies** section in `daily_digest.py`, save these queries as Metabase questions and set:

- `METABASE_BEHAVIOR_FOLLOWUP_CARD_ID`
- `METABASE_BEHAVIOR_REPHRASE_CARD_ID`

Source SQL:

- [sql/behavior_followup_burst.sql](sql/behavior_followup_burst.sql)
- [sql/behavior_rephrase_keywords.sql](sql/behavior_rephrase_keywords.sql)

### GitHub / runner

- Workflows require **`runs-on: self-hosted`** so the runner can reach **Metabase** on the internal network.
- If jobs queue forever, re-check runner registration (online + not busy blocking other jobs).

### Golden judge smoke (Monday 09:00 IST)

Workflow [.github/workflows/golden-smoke.yml](.github/workflows/golden-smoke.yml) runs `golden_smoke.py` against [golden_set.json](golden_set.json). **Only entries with `"enabled": true`** and non-empty doubt/answer are judged. Expand the file to 50 SME-blessed traces over time; placeholder rows stay `enabled: false`.

On failure, the workflow posts to `SLACK_WEBHOOK_URL` if configured.


Quarterly judge–human calibration, second-judge disagreements, embedding-cluster sampling, and judge cost trending — tracked on the roadmap once the daily loop has weeks of stable WoW data.

---

*Repo: [ask-ai-daily-automation](https://github.com/build-with-dhiraj/ask-ai-daily-automation)*
