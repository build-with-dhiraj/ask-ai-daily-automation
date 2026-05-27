# Poster pipeline rollback

The poster pipeline (gh-pages PNG + Slack image block, with a thread-reply
deep dive) is the default Slack surface for the daily eval and daily digest
since C1.3 (Locked Decision #6). The legacy text-only Block Kit path is the
fallback and is reachable in two ways:

1. Programmatic: a render / publish / verify / Slack-post failure auto-degrades
   the run with a `[poster] [warn] degraded cause=<bucket> reason=<details>`
   log line and a `⚠️ Poster degraded` prefix on the Slack message.
2. Operator kill-switch: `POSTER_DISABLE=1` skips the poster pipeline up-front
   and goes straight to the legacy text path. The kill-switch fires per-job
   (eval, digest) and per-run (workflow run).

This runbook covers the operator kill-switch. There are two procedures: one
for a single bad run, and one for an ongoing incident where you want the
kill-switch in place across multiple runs.

## Per-run rollback (preferred for one-off issues)

When to use: a single specific run is going sideways and you want the next
operator-initiated run to skip the poster path, but tomorrow's scheduled
run should go back to normal.

Procedure:

1. Open the Actions tab in GitHub, pick the workflow you want to re-run
   manually (Daily Automation / Daily Eval / Daily Digest).
2. Click "Run workflow".
3. In the dispatch form, set `poster_disable` to `true`.
4. Click "Run workflow" to start.

Scope:
* Affects ONLY this single dispatched run.
* The next scheduled run (or any subsequent dispatch where `poster_disable`
  is left at its default `false`) goes back to the poster path.
* No follow-up cleanup required.

You should see this in the run logs:
```
[poster] [warn] degraded cause=disabled
```
and the Slack message will start with `⚠️ Poster degraded (see workflow logs)`.

## Sticky rollback (incident response, multiple runs)

When to use: an ongoing incident where the poster path is broken (Playwright
fails to render, gh-pages is down, Slack rejects the image block, etc.) and
you want every future run, including the next scheduled cron, to bypass the
poster path until you flip it back.

Procedure:

1. Go to Settings -> Secrets and variables -> Actions -> Variables tab.
2. Click "New repository variable".
3. Name: `POSTER_DISABLE`. Value: `1`. Click "Add variable".
4. Confirm with `gh variable list` that the variable is set:
   ```
   gh variable list
   POSTER_DISABLE  1  ...
   ```

Scope:
* Affects ALL future runs (scheduled and dispatched) of Daily Automation,
  Daily Eval, and Daily Digest.
* Stays in effect until you explicitly unset the variable. There is no
  expiry, no auto-cleanup, no reminder.

REMEMBER TO UNSET: once the underlying incident is resolved, remove the
variable. Failing to do so means the poster pipeline is permanently
disabled and the team silently keeps getting the legacy text post.

Procedure to unset:

1. Go to Settings -> Secrets and variables -> Actions -> Variables tab.
2. Click the `POSTER_DISABLE` row, then the trash icon.
3. Confirm with `gh variable list` that the variable is gone:
   ```
   gh variable list
   ```
4. Trigger a manual dispatch with `poster_disable=false` (the default) to
   verify the poster path renders end-to-end before relying on tomorrow's
   scheduled run.

## Precedence

When both forms are in play the workflow evaluates:

```yaml
POSTER_DISABLE: ${{ github.event.inputs.poster_disable == 'true' && '1' || vars.POSTER_DISABLE || '' }}
```

That is: the per-run input wins when set to `true`; otherwise the repo
variable applies; otherwise the kill-switch is off and the poster path
runs. There is no scenario where a per-run input of `false` overrides a
sticky `vars.POSTER_DISABLE=1`. If you want to test the poster path while
the sticky rollback is in effect, you must unset the variable first.

## Verification

After flipping the kill-switch on or off, you can confirm the live state
with `gh variable list` (sticky form) or by reading the workflow run logs
of the dispatched run (per-run form). The `[poster] [warn] degraded`
log line plus the `⚠️ Poster degraded` prefix on the Slack message are
the canonical signal that the kill-switch fired.

## Related

* `daily_eval.py` and `daily_digest.py` for the gate-evaluation code path.
* `scripts/poster_slack.py` for the render + publish + verify + post
  orchestrator.
* `tests/test_eval_main_poster_path.py` and
  `tests/test_digest_main_poster_path.py` for the regression suite that
  pins the kill-switch behaviour.
