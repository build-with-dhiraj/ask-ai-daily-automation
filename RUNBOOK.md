# RUNBOOK — Self-Hosted Mac Runner Hygiene

Operator guide for keeping the Daily Automation pipeline (Daily Eval → Daily Digest)
reliable on the single self-hosted Mac runner. Only covers what the **operator** must
do. Python internals and workflow internals live in code/comments.

The runner: a Mac that hosts the GitHub Actions agent. Workspace lives at
`/Users/pw/actions-runner/_work/ask-ai-daily-automation/`. The Daily Eval job
intentionally runs up to **4 hours** (`EVAL_MAX_RUNTIME_SEC=14400`); the job
timeout is **600 minutes** to leave headroom for finalize + artifact upload.

---

## 1. Mac sleep prevention (most common failure)

The runner agent loses heartbeat when the Mac sleeps. GitHub then cancels the
in-progress job and `upload-artifact` returns `403 Forbidden: job is completed`,
which kills the downstream digest. Keep the Mac awake during the **04:00–09:00
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

## 2. Do NOT manually cancel runs

Daily Eval intentionally runs up to **4 hours**. A run that has been "stuck" for
30, 60, or 90 minutes is almost certainly **fine, just slow** — the judge stage
is sequential LLM calls across hundreds of samples. The `finalize_eval_run` path
always posts a partial-sample Slack message when it hits the soft cap, so even a
slow run produces output.

Cancelling mid-run kills the `upload-artifact` step → no `eval-summary`
artifact → downstream digest has nothing to read.

**If a run genuinely looks stuck** (no new log line for **>5 minutes** in the
GitHub Actions live log, not just "running for a while"):

1. Copy the run ID (e.g. `25655569039`).
2. Copy the last log line shown.
3. Open a GitHub issue with both. Then — only then — cancel.

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
04:00 IST cron run is in progress, your manual run will sit in the queue until
the cron run finishes. That's intentional. Don't fire dispatches expecting
parallelism — there is one runner.

`golden-smoke.yml` and `github-hosted-connectivity-smoke.yml` are deliberately
**not** in this group (Monday-only / GitHub-hosted ubuntu-latest).

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
   Expect **two** messages timestamped between **04:00 and 08:00 IST today**:
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
