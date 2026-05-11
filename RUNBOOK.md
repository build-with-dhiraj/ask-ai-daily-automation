# RUNBOOK — Self-Hosted Mac Runner Hygiene

Operator guide for keeping the Daily Automation pipeline (Daily Eval → Daily Digest)
reliable on the single self-hosted Mac runner. Only covers what the **operator** must
do. Python internals and workflow internals live in code/comments.

The runner: a Mac that hosts the GitHub Actions agent. Workspace lives at
`/Users/pw/actions-runner/_work/ask-ai-daily-automation/`. The Daily Eval job has
**no preset runtime** — it stops when all stratified rows have been judged
(`EVAL_MAX_RUNTIME_SEC=0`). The job's `timeout-minutes: 600` (10h) is a
runaway-job backstop, not a target.

---

## 0. Pre-run checklist (Mac runner) — run BEFORE every scheduled cron or manual dispatch

The single largest source of run failures has been the Mac dropping wifi or
going to sleep mid-eval. Apply these every time, especially the night before
the 08:30 IST scheduled run:

**Why 08:30 IST (03:00 UTC)?** Verified empirically on 2026-05-11 across 4 days
of historical data: silver ETL for `astracdc.silver_conversational_query_table`
(used by eval Q33193) consistently lands at ~00:55 UTC each day, and
`central.silver_stream_logs` (used by digest Q33285) lands at ~02:08 UTC. The
03:00 UTC cron gives a ~125-minute buffer past the silver ETL and ~52-minute
buffer past stream_logs, and it puts Python's `date.today() - 1` (Mac in IST)
in the same calendar day as Trino's UTC `CURRENT_DATE - 1`. The previous 22:30
UTC schedule fired before either ETL completed and crossed a timezone boundary,
which caused stale/duplicate Slack posts.

~~~bash
# 1. Disable display + system sleep on AC power
sudo pmset -c displaysleep 0 disksleep 0 sleep 0

# 2. Disable App Nap / Power Nap globally
sudo pmset -a powernap 0

# 3. Confirm Wake-for-Network-Access is ON
sudo pmset -a womp 1

# 4. Verify all settings
pmset -g | grep -E 'sleep|disksleep|displaysleep|powernap|womp'
# Expect: displaysleep 0, disksleep 0, sleep 0, powernap 0, womp 1

# 5. Run caffeinate as a 24h background daemon
caffeinate -d -i -m -s -t 86400 &
echo $! > /tmp/caffeinate.pid

# 6. Confirm runner agent is alive
launchctl list | grep actions.runner
# Should show: <pid>  0  actions.runner.<repo>.<runner-name>

# 7. Prefer ethernet over wifi for the runner Mac.
#    System Settings → Network → drag Ethernet above Wi-Fi in service order.
~~~

If `caffeinate` is already running, that's fine — multiple instances are harmless.

To stop caffeinate later: `kill $(cat /tmp/caffeinate.pid)`.

---

## 1. Mac sleep prevention (most common failure)

The runner agent loses heartbeat when the Mac sleeps. GitHub then cancels the
in-progress job and `upload-artifact` returns `403 Forbidden: job is completed`,
which kills the downstream digest. Keep the Mac awake during the **08:30–13:00
IST** run window every day.

**System Settings**

- Open **System Settings → Battery → Options**
- Set **Prevent automatic sleeping when the display is off** → ON (plugged-in)
- Lid behavior: keep the lid **open**, OR run in clamshell mode with an
  external display + power + keyboard/mouse. A closed lid without clamshell
  sleeps even when plugged in.

**One-shot keep-awake for a single run** (run in a terminal before kicking off
a workflow dispatch):

```bash
caffeinate -d -i -m -s -t 14400 &
```

Flags: `-d` (display), `-i` (idle), `-m` (disk), `-s` (system on AC), `-t 14400`
(4 hours, matches `EVAL_MAX_RUNTIME_SEC`).

**Pre-bedtime verification** — confirm display / disk / system sleep are off:

```bash
pmset -g | grep -E 'sleep|disksleep|displaysleep'
```

Expect `sleep 0`, `displaysleep 0` (or disabled). If non-zero, re-check the
Battery panel or rerun `caffeinate`.

---

## 2. Don't manually cancel runs

Daily Eval runs to completion. There is no preset duration — it stops only
when all stratified rows have been judged. With ~2,500 rows at 8-way
concurrency this is currently **~50min–6h** depending on Azure throughput
and per-call latency.

Don't cancel mid-run unless the run logs show **no progress markers for
>10 minutes** (the script logs `[N/M] stratum trace_id verdict +K scores`
every few seconds). Cancelling kills the `upload-artifact` step, which in
turn kills the downstream digest (no artifact = no digest). Make a note of
the run ID and the last log line before cancelling, and open a GitHub
issue so the failure has a trail.

The workflow's `timeout-minutes: 600` (10h) is a runaway-job backstop,
not a goal. `EVAL_MAX_RUNTIME_SEC=0` (default since
`cleanup-mess/eval-env-tuning`) means the script does **not** impose its
own soft cap — the only stop conditions are "all rows judged" or the 10h
backstop.

Symptom != root cause. "Long" is not the same as "stuck."

---

## 3. One run at a time (concurrency group)

All three workflows now share the GitHub Actions concurrency group
`daily-automation` with `cancel-in-progress: false`:

- `.github/workflows/daily-automation.yml` (cron + dispatch)
- `.github/workflows/daily-eval.yml` (dispatch only)
- `.github/workflows/daily-digest.yml` (dispatch only)

Behavior: a second dispatch **queues** behind the in-flight run. The first run
is **not** cancelled. You can never have two of these competing for the single
runner.

Operator implication: if you fire a manual `workflow_dispatch` while the
08:30 IST cron run is in progress, your manual run will sit in the queue until
the cron run finishes. That's intentional. Don't fire dispatches expecting
parallelism — there is one runner.

`golden-smoke.yml` and `github-hosted-connectivity-smoke.yml` are deliberately
**not** in this group (Monday-only / GitHub-hosted ubuntu-latest).

### Free-text feedback classifier (third job)

Daily Automation runs a third job — **Free-text Classifier** — between `eval` and
`digest`. It classifies yesterday's free-text downvote comments into 11 categories
and uploads a JSON artifact the digest reads. **Failure of this job does NOT block
the digest.** The digest's `actions/download-artifact` step is `continue-on-error`,
and the in-process read (`load_classifier_snapshot`) returns `None` on any error —
the new Slack section is silently omitted. To disable the classifier cleanly, omit
the `METABASE_FREETEXT_CARD_ID` secret: the job logs `[info] … skipping
classification`, writes a minimal snapshot with `stopped_reason="no_metabase_card"`,
and the digest renders identically to before.

---

## 4. Runner workspace hygiene

When `actions/checkout@v4` fails with `terminal prompts disabled` or hangs on
`git submodule foreach`, the workspace is in a bad state. Nuke it:

```bash
# 1. Stop the runner agent first.
#    If you ran ./svc.sh install:
cd /Users/pw/actions-runner && sudo ./svc.sh stop
#    Or, if running interactively / via LaunchAgent: stop the agent process.

# 2. Wipe the workspace.
sudo rm -rf /Users/pw/actions-runner/_work/ask-ai-daily-automation

# 3. Restart the runner.
cd /Users/pw/actions-runner && sudo ./svc.sh start
```

The next workflow run does a fresh clone. Schedule this cleanup **quarterly**,
or any time the checkout step throws prompt/submodule errors.

---

## 5. Gitconfig note — `insteadOf` rule

The user's `~/.gitconfig` currently has:

```
[url "https://github.com/"]
    insteadOf = git@github.com:
```

This rewrites SSH-style URLs to HTTPS. **No action needed** — `actions/checkout@v4`
already uses HTTPS with an injected `extraheader` for auth, so the rewrite is
benign.

If checkout *does* start failing in a way that looks auth-related, try removing
the rule and re-testing:

```bash
git config --global --unset url."https://github.com/".insteadOf
```

---

## 6. Monday morning checklist

Run this every Monday before stakeholders check Slack. ~5 minutes.

1. **Last 3 workflow runs green?**

   ```bash
   gh run list \
     --repo build-with-dhiraj/ask-ai-daily-automation \
     --limit 3 \
     --json conclusion,name,createdAt
   ```

   Expect the most recent **Daily Automation** = `success`.

2. **Slack `#ask-ai-evals` (`C0B2KT5RQ0H`)** — open the channel.
   Expect **two** messages timestamped between **08:30 and 12:00 IST today**:
   one eval scoreboard + one digest.

3. **Digest health** — scroll today's digest message. None of these strings
   should appear:
   - `_fetch failed_`
   - `_unavailable_`
   - `_not configured_`

4. **Eval scoreboard health** — today's eval message should show stratum counts
   (e.g. `behavior=...`, `stream=...`) and a **non-zero** `N`.

5. **Last week's golden-smoke** — confirm Monday's `golden-smoke` run was green.
   (Monday-only workflow; quick sanity check on the golden set.)

If any of (2)–(5) fail, start with §2 (don't cancel anything) and check the
most recent run's logs in the Actions UI.
