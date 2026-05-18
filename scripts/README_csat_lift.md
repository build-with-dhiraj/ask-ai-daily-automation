# CSAT Lift Announcer

One-shot Slack announcement of the SME-audit vs production CSAT correlation
findings (15,114 audited traces; 2,889 with explicit student feedback).

## What it does

`scripts/csat_lift_announcer.py` builds a Slack Block Kit payload encoding the
three headline findings (NEUTRAL drives downvotes, "Too long" doesn't predict
CSAT, 31% of downvotes are SME=PASS) plus the implications for judge training,
and POSTs it to a staging Slack channel via incoming webhook.

The script is invoked by `.github/workflows/csat-lift-staging.yml`.

## How to trigger

GitHub repo -> Actions tab -> **CSAT Lift Analysis (Staging)** -> **Run
workflow**. Select the branch and choose `dry_run`:

- `false` (default): post to the staging Slack channel
- `true`: print the JSON payload to the runner log; do not POST

## Required secret

Before the first non-dry-run trigger, add the staging webhook URL as a repo
secret:

1. Repo -> Settings -> Secrets and variables -> Actions -> **New repository secret**
2. Name: `SLACK_WEBHOOK_URL_STAGING`
3. Value: the Slack incoming-webhook URL for `#chakra-ai-product-ds-staging`
   (channel ID `C0B473ARXPS`)

The workflow refuses to POST without the secret. Dry-run mode does not need it.

## Local dry-run

```bash
DRY_RUN=true python scripts/csat_lift_announcer.py
```

Prints the full Block Kit payload to stdout and exits 0. No network call.

## Relationship to daily-automation.yml

This workflow is intentionally isolated from `daily-automation.yml`,
`daily-eval.yml`, and `daily-digest.yml`:

- Different runner (`ubuntu-latest`, not `self-hosted`)
- No shared concurrency group
- No shared secrets beyond the staging webhook
- Does not import or execute `daily_eval.py`, `judge_runner.py`, or the v7/v8
  master judge prompt
- Manual `workflow_dispatch` only; no `schedule:` trigger

## Future work

This PR ships the one-shot announcement only. A v2 follow-up will pull live
production CSAT data and recompute lift on a schedule; that work is out of
scope here and will live in its own workflow + script pair.
