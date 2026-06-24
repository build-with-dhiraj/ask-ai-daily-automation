# REVIVE — Ask AI Daily Automation

> **Status as of 2026-06-24: DOWN.** Last healthy posts: Daily Digest ~2026-05-29, Rubric Scoreboard ~2026-05-26.
> **Root cause: the self-hosted runner is offline. The code, secrets, and credentials are intact — only the execution host is gone.**
> This is a host problem, not a code problem. Reviving = restoring a runner, not rebuilding the pipeline.
> Owner (interim): **Anshik Bansal**. Companion doc: WS3, *Ask AI Daily Automation Pipeline* (handover pack / Confluence). Operational depth: `RUNBOOK.md`.

---

## 1. What happened (confirmed diagnosis)

The `Daily Eval`, `Daily Digest`, `Daily Automation`, and `Golden judge smoke` workflows are pinned to `runs-on: self-hosted` because they need VPN-internal access to Metabase (Trino-Prod) and Langfuse.

There is exactly one registered runner — **`PWKTQD7G9KQ1`** (macOS / ARM64), a personal PW MacBook — and it is **offline** (the machine was decommissioned as the original maintainer offboarded).

With no runner to pick them up, scheduled jobs sit `queued` for 24–95 hours and are then force-cancelled by GitHub. They never post. The GitHub-hosted jobs (`Prune old posters`, `GitHub-hosted connectivity smoke`) keep succeeding in seconds, which is why the channel isn't completely silent.

**Verify for yourself:**
```bash
# Runner status — will show "offline"
gh api repos/build-with-dhiraj/ask-ai-daily-automation/actions/runners

# Stuck runs — note the multi-hour/day durations on Daily * workflows
gh run list -R build-with-dhiraj/ask-ai-daily-automation --limit 20
```

---

## 2. Revive — fastest path to green

### Step 0 (do this first): clear the stuck queue
Old runs will collide with new ones. Cancel anything queued/in-progress before attaching a runner:
```bash
gh run list -R build-with-dhiraj/ask-ai-daily-automation --status queued     --limit 30
gh run list -R build-with-dhiraj/ask-ai-daily-automation --status in_progress --limit 30
# Cancel each stuck id:
gh run cancel <run_id> -R build-with-dhiraj/ask-ai-daily-automation
```

### Option A — quickest (throwaway): re-attach any VPN-connected machine as the runner
Use when you need a digest tomorrow morning and the VM isn't ready yet. Any macOS/Linux box that (a) is inside PW VPN with Trino + Langfuse reachable and (b) stays awake at 02:30–03:30 UTC works.

1. Repo → **Settings → Actions → Runners → New self-hosted runner**. Copy the token'd `./config.sh` command it shows.
2. On the machine: run `./config.sh ...`, then install it as a service so it survives reboots/sleep:
   ```bash
   ./svc.sh install
   ./svc.sh start
   ```
3. Confirm it shows `online`:
   ```bash
   gh api repos/build-with-dhiraj/ask-ai-daily-automation/actions/runners
   ```
4. Smoke it (Step 4 below). The next 03:00/03:30 UTC cron will then post to `#ask-ai-evals` on its own.

### Option B — durable (target): Azure VM
This is the real fix from WS3 §"Mac → VM migration". Anshik confirmed VM availability; **Ayan** (Anshik's team) is the wire-up contact.

1. Provision VM, Python 3.11, register the GitHub Actions runner against the repo (~1 day).
2. Ensure VPN/network route to Trino + Langfuse + the Azure OpenAI endpoint.
3. Confirm GH Secrets (§4) are present — they inject at workflow time; nothing to copy onto the VM manually.
4. Run as a service so it never sleeps through the 03:00 UTC fire.
5. Parallel-run to staging for a few days, then cut over and **remove the old Mac runner from the repo's runner list**.

---

## 3. Verify it's healthy (do this after attaching any runner)

Staging-first is non-negotiable: **manual fires (`workflow_dispatch`) route to `#test-channel-auto`; only the cron routes to `#ask-ai-evals`.**

```bash
# Manual fire → lands in STAGING (#test-channel-auto), safe
gh workflow run daily-digest.yml -R build-with-dhiraj/ask-ai-daily-automation
gh workflow run daily-eval.yml   -R build-with-dhiraj/ask-ai-daily-automation
gh run watch -R build-with-dhiraj/ask-ai-daily-automation
```
Confirm both posts appear in `#test-channel-auto`. Then let one real 03:00 UTC cron cycle post to `#ask-ai-evals` and eyeball it. Done.

---

## 4. Credentials — the fragile part (READ THIS)

GitHub Secrets are **write-only**: the team cannot read existing values back out. They are still configured in the repo and will keep working as long as the *underlying accounts* are valid. The risk is any secret whose only human holder has left.

| Secret(s) | Source of truth / who can re-issue | Fragility |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Satyam Yadav + Naresh Saini (Langfuse owners) | Low — team-owned |
| `AZURE_OPENAI_API_KEY`, `_ENDPOINT`, `_DEPLOYMENT`, `_API_VERSION` | AlakhAI Azure subscription (`northcentralus0125alakhai`), DS team | Low–Med — rotate key from Azure portal if needed |
| `METABASE_USER`, `METABASE_PASS` | **Verify this is a service account, not a personal login.** If personal, provision a service login via Anand Mishra (Eng-Data) and re-add. | **HIGH — may die with the maintainer** |
| `SLACK_WEBHOOK_URL`, `SLACK_WEBHOOK_URL_TEST` | Incoming webhooks for `#ask-ai-evals` / `#test-channel-auto`. Originally created by the maintainer. | **HIGH — confirm Anshik holds them or recreate via Slack app config** |

**Action for the departing maintainer (today):** hand Anshik the two HIGH-fragility values (or recreate them under team ownership) so cutover doesn't silently fail.

---

## 5. Repo ownership (do before access is revoked)

Canonical repo is on a **personal** GitHub account: `build-with-dhiraj/ask-ai-daily-automation`. SSO offboarding will NOT delete it, but the team can't manage runners/secrets without access, and PW-critical infra shouldn't live on an ex-employee's personal account.

Pick one, today:
- **Transfer** the repo to the PW GitHub org (Settings → General → Transfer ownership), **or**
- Add **Anshik (+ Ayan)** as repo **admins** now and schedule the transfer post-cutover.

---

## 6. If the team decides NOT to continue it

Don't leave it half-alive (stuck queued runs, periodic smoke alerts). Cleanly pause:
```bash
gh workflow disable "Daily Eval"               -R build-with-dhiraj/ask-ai-daily-automation
gh workflow disable "Daily Digest"             -R build-with-dhiraj/ask-ai-daily-automation
gh workflow disable "Daily Automation"         -R build-with-dhiraj/ask-ai-daily-automation
gh workflow disable "Golden judge smoke (Monday)" -R build-with-dhiraj/ask-ai-daily-automation
```
Post a one-line note in `#ask-ai-evals` so the channel knows it's intentionally paused, and unpin the onboarding message.

---

## 7. Contacts

| Role | Person |
|---|---|
| Interim owner (cron + runner + GH secrets) | Anshik Bansal |
| VM wire-up | Ayan (Anshik's team) |
| Judge prompt | Deepesh Kumawat + Naresh Saini |
| Langfuse | Satyam Yadav + Naresh Saini |
| Metabase queries | Anand Mishra (Eng-Data) |
| Slack delivery / product signal | Shyam Prasad |
