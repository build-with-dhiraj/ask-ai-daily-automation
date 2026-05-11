# `sql/` — Metabase query source-of-truth

All Metabase saved-question SQL used by Daily Eval and Daily Digest lives here. **The repo is the source-of-truth; Metabase mirrors the repo.** Never edit a query in Metabase first — open a PR against this directory, get it merged, then paste the new SQL into the Metabase question editor.

## Card → file map

| Metabase card | Repo file | Used by | Time window |
|---------------|-----------|---------|-------------|
| [Q33193](https://metabase-prod.penpencil.co/question/33193) — Daily Stratified Eval Sample | [`daily_stratified_sample.sql`](daily_stratified_sample.sql) | Daily Eval (`daily_eval.py`) | yesterday |
| [Q24973](https://metabase-prod.penpencil.co/question/24973) — Top Downvote Reasons Academic | [`downvote_reasons_academic_21d.sql`](downvote_reasons_academic_21d.sql) | Daily Digest (`daily_digest.py fmt_academic`) | rolling 21d |
| [Q24974](https://metabase-prod.penpencil.co/question/24974) — Top Downvote Reasons Non-Academic | [`downvote_reasons_nonacademic_21d.sql`](downvote_reasons_nonacademic_21d.sql) | Daily Digest (`daily_digest.py fmt_nonacademic`) | rolling 21d |
| [Q23036](https://metabase-prod.penpencil.co/question/23036) — Downvoted Queries Dump | [`downvote_queries_dump_yesterday.sql`](downvote_queries_dump_yesterday.sql) | Daily Digest (`daily_digest.py fmt_downvote_dump`) | rolling 15d (Python filters to yesterday) |
| [Q33282](https://metabase-prod.penpencil.co/question/33282) — Behavior Followup | [`behavior_followup_burst.sql`](behavior_followup_burst.sql) | Daily Digest (silent-failure proxy: multi-turn burst) | yesterday |
| [Q33283](https://metabase-prod.penpencil.co/question/33283) — Behavior Rephrase | [`behavior_rephrase_keywords.sql`](behavior_rephrase_keywords.sql) | Daily Digest (silent-failure proxy: rephrase/lang-switch rate) | yesterday |
| [Q33285](https://metabase-prod.penpencil.co/question/33285) — VCP Stream Logs Summary | [`vcp_stream_logs_digest_summary.sql`](vcp_stream_logs_digest_summary.sql) | Daily Digest (API health) | yesterday |

## Conventions

- **Metabase optional-parameter syntax `[[and field = {{var}}]]`** is preserved verbatim. Removing these brackets breaks the Metabase UI's parameter widgets.
- **No `LIMIT` changes** in this repo without coordinating with the corresponding Python formatter in `daily_digest.py` / `daily_eval.py` (a smaller limit may make a section sparse).
- **Date predicates**: prefer explicit `date('2025-09-12')` floor (when the data source was deployed) PLUS the rolling window, so backfills don't accidentally scan years of data.
- **Internal test users**: `user_id not in ('66ac8d5822e7707c7312c5e8')` is the canonical exclusion. Add any new internal users to all 3 downvote queries together.

## Change workflow

1. Branch from `main`, edit the SQL file
2. Open PR — describe what changes and why
3. Merge to `main`
4. Open the corresponding Metabase question, paste the new SQL, save
5. Re-run the next scheduled Daily Automation to confirm the digest looks right

If you edit the Metabase question first and forget to mirror into the repo, the next person to read this file will not see your change — and the next PR against it will silently overwrite your work. **Don't.**
