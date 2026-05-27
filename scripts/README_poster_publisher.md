# poster_publisher — operational contract

Publisher module for daily Slack poster PNGs. Part of C1.2c (poster-format redesign, Path C1).

## One-time setup (FIRST RUN ONLY)

Before the first daily workflow run that publishes a poster, an operator must:

1. **Push the `gh-pages` branch to origin** (the branch exists locally but the workflow needs the remote ref):
   ```bash
   git push -u origin gh-pages
   ```
2. **Enable GitHub Pages** in repo settings:
   - Settings → Pages → Source: `Deploy from a branch`
   - Branch: `gh-pages` / `/ (root)` → Save
3. **Verify** by visiting `https://build-with-dhiraj.github.io/ask-ai-daily-automation/` after ~1 minute. A 404 with the Pages footer is expected until the first poster is published.

If the workflow is triggered before step 1, `publish_poster()` will fail with a clear hint pointing back to this section.

## GitHub Pages publish lag

First-publish lag for a new file is typically 30s to 2 minutes. Subsequent updates to existing files settle in ~30s. `_verify_url_reachable()` defaults to a 120s timeout to cover the first-publish case; callers that know the file is already cached can pass a shorter timeout.

## What it does

`scripts/poster_publisher.py:publish_poster()` writes a PNG to a checkout of the `gh-pages` branch, commits it, and (optionally) pushes to origin. GitHub Pages then serves it at a predictable URL that the daily Slack message embeds via an `image_url` block.

## URL pattern

```
https://build-with-dhiraj.github.io/ask-ai-daily-automation/posters/{surface}/{filename}
```

Where:

- `{surface}` is `scoreboard` or `digest`
- `{filename}` is `YYYY-MM-DD.png` for normal scheduled runs
- `{filename}` is `YYYY-MM-DD-{short_sha}.png` for manual `workflow_dispatch` runs (so dogfooding does not overwrite production posters)

## Publishing flow

1. Renderer produces PNG bytes (Playwright, C1.2b).
2. Caller invokes:
   ```python
   from scripts.poster_publisher import publish_poster, _verify_url_reachable
   url = publish_poster(png_bytes, "scoreboard", "2026-05-27")
   if not _verify_url_reachable(url, timeout=60):
       # Soft failure: file is pushed, GH Pages hasn't served it yet.
       # Caller should fall back to text-only Slack block per plan §"Failure modes".
       ...
   ```
3. Slack webhook is posted referencing `url`.

## Push gating — `POSTER_AUTO_PUSH`

`publish_poster()` commits locally always, but **only pushes when `POSTER_AUTO_PUSH=1` is set in the environment.** This is a deliberate guardrail so:

- Local dev / tests never accidentally push to `origin/gh-pages`.
- The GitHub Actions workflow is the only place that sets `POSTER_AUTO_PUSH=1`.

If you forget to set it, the commit sits on the local `gh-pages` worktree and you'll see a log line telling you the exact `git push` command to run.

## GitHub Pages propagation delay

A freshly-pushed PNG typically becomes reachable at the public URL **30–60 seconds** after push. `_verify_url_reachable(url, timeout=60)` polls with exponential backoff (2s → 10s capped) and returns `False` on timeout. Returning `False` is NOT a hard failure — the file is committed and pushed; GH Pages will catch up. The caller should decide whether to:

- Wait longer and re-check, or
- Fall back to a text-only Slack message and post the image later, or
- Post the message anyway (Slack will re-fetch the unfurl on its own).

## Retention — 90 days

`.github/workflows/prune-posters.yml` runs daily at 04:00 UTC. It checks out `gh-pages`, deletes any `posters/**/*.png` with mtime older than 90 days, commits, and pushes. No new secrets needed — uses the workflow's built-in `GITHUB_TOKEN`.

## Manual operations

### Delete a specific poster

```bash
git fetch origin gh-pages
git worktree add /tmp/ghp gh-pages
cd /tmp/ghp
rm posters/scoreboard/2026-05-27.png
git commit -am "manual: remove 2026-05-27 scoreboard poster"
git push origin gh-pages
cd - && git worktree remove /tmp/ghp
```

### Inspect what's currently published

```bash
git fetch origin gh-pages
git ls-tree -r origin/gh-pages posters/
```

## Security caveat (locked, accepted)

Filenames are **date-based, not unguessable**. Anyone who knows the URL pattern can construct yesterday's URL (`https://build-with-dhiraj.github.io/ask-ai-daily-automation/posters/scoreboard/2026-05-26.png`) without seeing the Slack message. Per locked decision #8 in `here-is-the-sixth-buzzing-sundae.md`:

- The poster image carries **only** headline + insight cards + sparklines.
- Cost figures, raw quoted student feedback, and competitively sensitive numbers stay in the text companion Block Kit (channel-private), **never** in the rendered PNG.
- Upstream PII regex/NER scrub runs on any free-text that survives into the image.

If a future threat model demands ACL'd storage, migrate to Slack `files.upload` (requires breaking the locked Path A constraint — bot token needed) and revisit.

## Files

- `scripts/poster_publisher.py` — the publisher module
- `scripts/README_poster_publisher.md` — this file
- `.github/workflows/prune-posters.yml` — daily 90-day pruning workflow
- `gh-pages` branch — hosts the PNGs + a minimal `index.html` landing page

## Local smoke test

```bash
python -m pytest tests/test_poster_publisher.py -v
```

The test calls `publish_poster()` with `POSTER_AUTO_PUSH=0` (default) against fake PNG bytes and confirms the file lands in `.gh-pages-worktree/posters/scoreboard/<date>.png` without contacting origin.
