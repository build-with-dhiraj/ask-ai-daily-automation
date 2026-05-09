# `#ask-ai-evals` — channel onboarding (pin this)

Use as the channel description or a pinned message. Adjust times if your cron differs.

---

## Purpose

This channel is for **monitoring Ask AI quality and incidents**. Use it to notice problems early and fix the product and systems — not to judge individuals.

---

## The two daily messages

**Daily Eval** (about 08:30 IST)  
Summarises how yesterday’s **sampled** Video Co-Pilot (academic) answers score on a **fixed checklist** (accuracy and experience). Shows counts, stratum split, and week-over-week style context where configured. A `?` link may point to a short doc on definitions and cost.

**Daily Digest** (about 09:30 IST)  
Pulls together **downvote and reason data from Metabase**, **errors and comments from Langfuse**, **yesterday’s API request summary from `stream_logs` (via Metabase)**, and optional **behaviour proxy cards** (also Metabase). Sections are in a fixed order so the same screen is easy to scan every day.  
The digest is **Slack Block Kit** (`blocks`); mobile search and some notifications may show only the **plain-text summary line** — open the full thread in-channel to read every section.

---

## How each message is created (short)

**Daily Eval**  
A GitHub Action on a **self-hosted runner** runs `daily_eval.py`. It executes a **saved Metabase SQL question** that returns a **stratified list of traces** from yesterday. For each row, an **LLM judge** (same rubric each run) produces PASS / NEUTRAL / FAIL style results; those can be **written back to Langfuse** as scores. The script then **builds one Slack payload** and sends it to the webhook.

**Daily Digest**  
A separate GitHub Action runs `daily_digest.py`. It **calls Metabase** for several saved questions (downvote reasons, yesterday’s dump, optional follow-up/rephrase chapters, optional `stream_logs` summary). It **calls the Langfuse HTTP API** for recent error observations, downvote-related scores/comments, and trace counts. It may **read a small JSON file** on the runner (written by the last eval) for cross-checks. It **assembles Slack blocks in a fixed order** and posts to the webhook.

Neither message is typed by hand; both are **fully automated** from data + scripts in the repo.

---

## How to read the Digest (order of sections)

1. **Langfuse errors** — last 24 hours, from Langfuse “error” observations (LLM / tracing layer).  
2. **Video co-pilot API health** — yesterday, from **`central.silver_stream_logs`** via Metabase (one row summary: failures, HTTP codes, etc.). Different source and time window from the block above.  
3. **User comments on downvotes** — sample of free-text from Langfuse where available.  
4. **Downvote reasons and snapshots** — from Metabase.  
5. **Silent-failure proxies** — from Metabase behaviour questions when configured; “confirmed regression” lines use overlap with the eval snapshot when that file exists on the runner.

**Prod Metabase (PW, digest):** GitHub secrets must map to saved questions **33282** (follow-up burst), **33283** (rephrase keywords), **33285** (`stream_logs` summary); see repo `ONE_PAGER.md` for links.

---

## Who usually looks at what

- **Engineering:** Error blocks, API health, latency, outages, release regressions.  
- **Product / design:** Student comments, confusion patterns, UX.  
- **Data science:** Sampling logic, metrics, deeper analysis when numbers shift.  
- **QA:** Reproduction and linking issues to releases.

---

## House rules

Reply in a **thread** if you see something worth acting on. Short notes are fine.

---

## Slack paste (mrkdwn)

Copy the block below into a pinned message.

```
*#ask-ai-evals — what this channel is for*

Monitoring Ask AI quality and incidents. Use it to fix the product and systems, not to blame individuals.

*Two automated posts each day*

*Daily Eval (~08:30 IST)*  
Summary of how a *sample* of yesterday’s Video Co-Pilot academic answers scores on a *fixed checklist*. Optional `?` link to definitions and cost.

*Daily Digest (~09:30 IST)*  
Combines Metabase (downvotes, reasons, snapshots, optional behaviour + stream_logs summary) and Langfuse (errors, comments, trace counts). Optional cross-check with a small file from the last eval on the same runner. Same section order every day.

*How they are built (logic)*  
• *Eval:* GitHub Action → Metabase pulls a stratified trace list → LLM judge per row → optional Langfuse score writes → one Slack post.  
• *Digest:* GitHub Action → Metabase (multiple questions) + Langfuse API + optional eval JSON on disk → assemble blocks → one Slack post.

*Digest — read top to bottom*  
1. Langfuse errors (24h)  
2. API health from stream_logs / Metabase (yesterday)  
3. Downvote comments (Langfuse sample)  
4. Metabase reason / dump sections  
5. Behaviour proxies when configured  

Use `trace_id` to line up Langfuse with server-side logs when debugging.

*Who looks at what*  
Engineering: errors, API health, regressions. Product/design: comments and UX. DS: sampling and metrics. QA: repro and releases.

*House rule:* use threads for follow-ups.
```
