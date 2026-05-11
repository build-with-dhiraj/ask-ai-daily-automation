# `#ask-ai-evals` — channel onboarding (pin this)

**Ops:** **Daily Automation** runs on **`main`** on a **self-hosted** runner (keep the machine online). Schedule: **~08:30 IST** (`0 3 * * *` UTC). Both **Daily Eval** and **Daily Digest** are **one workflow**: eval first, then digest. Eval writes a small **snapshot JSON**; the workflow uploads it as an **artifact** so digest always picks up judge hotspots for the cross-check. Expect the heaviest work in **eval** (LLM judges + optional **4h** soft time budget); digest usually follows shortly after unless eval runs long.

---

## What this channel is

The **automated daily evaluation stream** for **Ask AI** (Video Co-Pilot academic path and related quality signals). **Product** owns the evaluation program definitions and what ships in the two posts; engineering and DS use the same feed for regressions, data health, and deeper analysis.

---

## The two posts (read the full Slack message for detail)

**Daily Eval**  
**Metabase** returns a **stratified sample** of traces for **calendar yesterday** (not every query). An **LLM judge** scores each row with a **fixed rubric** (PASS / NEUTRAL / FAIL–style bands and axes). Results can be **written back to Langfuse** as scores. The post is the scorecard: strata, counts, WoW context where configured, cost line. Runs are **time-bounded**: if the soft clock stops before every row is judged, the post still reflects **N** completed judgements (see snapshot / footer wording)—that is expected capacity behaviour, not a discard of work.

**Daily Digest**  
Rolls **Metabase** product analytics (downvote reasons with mixed windows—**rolling 21d** on the reason rollups, **calendar yesterday** on the downvote snapshot and stream_logs-style blocks where applicable) and **Langfuse** API data (**errors**, **scores**, **traces** for the **last 24h** where noted in the headers). Optional **behaviour proxy** cards (Metabase). Ends with **silent-failure proxies** plus a **judge × behaviour** cross-check when the eval snapshot is present.

**Time windows (why two numbers appear):** **Eval** is built around **yesterday’s** pulled sample. **Digest** mixes **yesterday**, **last 24h** (Langfuse blocks), and **rolling 21d** (reason breakdown cards)—check each section title.

---

## How they are produced (one sentence each)

**Daily Automation** (GitHub Actions) **job 1** runs `daily_eval.py`: checkout → Metabase question → **Azure OpenAI** judge (up to **8** concurrent in-flight calls on the runner) → optional Langfuse score writes → **one** Slack post + snapshot file. **Job 2** runs `daily_digest.py` in the **same workflow**: download eval snapshot artifact if present → Metabase + Langfuse fetches → **one** Slack digest (`blocks`). Standalone workflows exist for manual runs only.

---

## Digest section order (match the live layout)

1. **Langfuse errors** — last **24h**  
2. **Video co-pilot API health (`stream_logs`)** — **yesterday** (Metabase)  
3. **User comments on downvotes** — Langfuse sample, **last 24h**  
4. **Downvote reasons — Academic** — Metabase, **rolling 21d**  
5. **Downvote reasons — Non-academic** — Metabase, **rolling 21d**  
6. **Yesterday’s downvoted queries snapshot** — Metabase, **yesterday**  
7. **Silent-failure proxies** — behaviour cards (Metabase) + optional neutral **sample vs judged** line + **confirmed regression** signal when eval hotspots overlap behaviour spikes  

Open the message in Slack for **Block Kit** layout; notifications sometimes show only a short preview.

**Prod Metabase (PW) digest cards:** follow-up **33282**, rephrase **33283**, stream_logs **33285** (secrets as digit-only ids)—see [`ONE_PAGER.md`](ONE_PAGER.md) for links.

---

## Who often cares about which blocks

- **Engineering:** Langfuse errors, `stream_logs` / API health, release regressions.  
- **Product / design:** Downvote comments, reason mix, UX patterns.  
- **Data science:** Sampling, judge metrics, WoW interpretation, caveats on window mix.  
- **QA / on-call:** Repro, `trace_id`, tying Langfuse to server logs.  

---

## Norms

Use **threads** for follow-ups so the main timeline stays scannable. Deeper rubric, thresholds, and Metabase Q **33193** (eval sample): [`ONE_PAGER.md`](ONE_PAGER.md).

---

## Slack paste (`mrkdwn`)

Copy into a pinned message.

```
*#ask-ai-evals — daily evaluation feed*

This channel is the *automated* quality stream for *Ask AI*: two posts per *Daily Automation* run (~08:30 IST start, self-hosted runner). *Product* owns what the eval program measures; the posts are the source of truth for the daily snapshot.

*Post 1 — Daily Eval*  
*Metabase* hands a *stratified sample* of traces for *calendar yesterday*. An *LLM judge* (fixed rubric on *Azure*) scores each row; optional writebacks to *Langfuse*. You get pass/neutral/fail-style health, strata, and cost. If the run hits a *time budget*, numbers reflect *N judgements completed*—by design.

*Post 2 — Daily Digest*  
*Metabase* (downvotes, reasons—note *rolling 21d* vs *yesterday* in headers, plus optional behaviour + *stream_logs* health) and *Langfuse* (*errors* / *scores* / *traces*, often *last 24h* where labeled). The last block mixes *behaviour proxies* with a *judge × behaviour* hint when eval’s snapshot is available.

*Same GitHub workflow:* `daily_eval.py` then `daily_digest.py`; digest downloads eval’s snapshot artifact so the cross-check stays aligned. Open full messages for *Block Kit*.

*Digest — top to bottom*  
1. Langfuse errors (24h)  
2. API health / stream_logs (yesterday)  
3. Downvote comments (Langfuse, 24h)  
4. Downvote reasons — academic (21d)  
5. Downvote reasons — non-academic (21d)  
6. Downvoted queries snapshot (yesterday)  
7. Silent-failure proxies + regression cross-check  

`trace_id` links Langfuse to server-side investigation.

*Roles (rough)* Eng: errors & API health · Product/UX: reasons & comments · DS: sampling & metrics · QA: repro  

Threads for follow-ups. Details: repo `ONE_PAGER.md`.
```
