# How To Add The Rubric Block To Tomorrow's Cowork Digest

**Goal:** Tomorrow morning's 9 AM IST digest in `#C0B2KT5RQ0H` includes a Rubric Scoreboard block alongside the existing CSAT / downvote / errors blocks.

There are two paths. Path A is what I'd ship tonight to be safe. Path B is the long-term Cowork integration.

---

## Path A — Manual paste tomorrow morning (lowest risk, ships tomorrow)

Run the judge against ~30 of yesterday's downvoted-academic traces tonight, paste the resulting Slack block into the digest channel manually after Cowork posts the regular digest.

### Step 1: Pull yesterday's downvoted academic samples (Metabase)

In Metabase, run a query like this against `Q23036` filters or a fresh query:

```sql
SELECT
  q.aiintentid AS trace_id,
  q.query AS doubt,
  q.answer AS ai_answer,
  json_extract_scalar(q.output, '$.metadata[0].subject[0].subject') AS subject,
  json_extract_scalar(q.output, '$.metadata[0].chapter[0].chapter') AS chapter,
  json_extract_scalar(q.output, '$.user_message.author_metadata.classes') AS student_class,
  json_extract_scalar(q.output, '$.user_message.author_metadata.exam') AS exam,
  json_extract_scalar(q.output, '$.metadata[0].image_url') AS image_url,
  CAST(json_extract_scalar(q.output, '$.metadata[0].is_annotated') AS BOOLEAN) AS is_annotated,
  '' AS transcript,
  '' AS ideal_answer
FROM astracdc.silver_conversational_query_table q
JOIN astracdc.silver_prod_feedback_by_user_entity f
  ON f.entity_id = q.aiintentid
WHERE q.intenttype = 'VIDEO_CO_PILOT'
  AND date(q.createdat) = current_date - INTERVAL '1' DAY
  AND f.rating = 0                                              -- downvotes only
  AND f.type = 'copilot_message'
  AND json_extract_scalar(q.output, '$.metadata[0].category_name') = 'academic'
  AND coalesce(json_extract_scalar(q.output, '$.metadata[0].category_name'), '') <> 'onboarding'
ORDER BY random()
LIMIT 30
```

Export the result as JSON. Save to `/tmp/samples.json`.

### Step 2: Reshape Metabase JSON to runner input shape

Metabase exports column-oriented JSON. The runner expects a list of objects with the keys above. A one-liner:

```bash
# If Metabase gave you {"data":{"rows":[[...],[...]],"columns":["trace_id","doubt",...]}}
python -c '
import json, sys
m = json.load(open("/tmp/samples_metabase.json"))
cols = [c["name"] if isinstance(c, dict) else c for c in m["data"]["cols"]]
rows = m["data"]["rows"]
out = [dict(zip(cols, r)) for r in rows]
json.dump(out, open("/tmp/samples.json", "w"), indent=2)
print(f"reshaped {len(out)} rows")
'
```

(Adjust based on your actual Metabase JSON shape — sometimes it's `data.rows` + `data.cols`, sometimes flat list.)

### Step 3: Run the judge

```bash
cd "/Users/pw/PW Claude Skills/local-agents/eval-sampler"
export OPENAI_API_KEY=sk-...
python judge_runner.py input /tmp/samples.json --output /tmp/results.json
```

The Slack block prints to stdout at the end. Approximately:

```
🎯 *Rubric Scoreboard (sample n=30)* [PM, DS]
Overall: PASS 8 (29.6%) | NEUTRAL 12 (44.4%) | FAIL 7 (25.9%)
NOT_JUDGABLE: 3

By axial (% of judgable with non-PASS):
  Academic Correctness  25.9%
  Intent Binding         3.7%
  Presentation          14.8%
  Pedagogical Fit       40.7%
  Look & Feel / Tone    33.3%

Top open codes fired:
  D1 Too advanced................. 8
  E1 Too long..................... 6
  A1 Conceptual error............. 5
  C1 Equation unreadable.......... 4
  D3 No direct answer............. 4
  A4 Calculation error............ 3
```

### Step 4: Paste into Slack (`C0B2KT5RQ0H`)

Either:
- Reply in thread to the Cowork digest with the rubric block + a note: "Rubric Scoreboard from yesterday's downvoted-academic sample — manual run tonight while we wire this into the daily task"
- Or post as a top-level follow-up message right after the Cowork digest lands

Cost for 30 samples: **~$0.15**.

---

## Path B — Wire into Cowork SKILL.md (sustainable, do later this week)

The Cowork scheduled task lives at:
`/Users/pw/Documents/Claude/Scheduled/ask-ai-daily-digest/SKILL.md`

To make tomorrow's digest auto-include the rubric block, the SKILL.md needs a new step that:
1. Pulls ~30 downvoted-academic samples from Metabase (via existing Metabase REST auth)
2. Calls `judge_runner.py input /tmp/samples.json` and captures stdout
3. Inserts that block into the digest message before the Slack post step

Pseudo-patch (apply by hand — I'm not auto-editing the live scheduled task):

```diff
  ## Steps

  1. Auth Metabase
  2. Pull Q23036, Q23037, Q24973, Q24974
  3. Pull Langfuse scores + observations + traces (24h)
+ 4. Pull 30 downvoted-academic samples (new SQL — see below)
+ 5. Reshape to runner input shape
+ 6. Run: python /Users/pw/PW\ Claude\ Skills/local-agents/eval-sampler/judge_runner.py \
+        input /tmp/cowork_samples.json --output /tmp/cowork_results.json
+ 7. Capture the Slack block from stdout (lines after `🎯 *Rubric Scoreboard`)
  8. Format full digest message — INSERT rubric block between User Voice and Errors blocks
  9. Post to Slack `C0B2KT5RQ0H`
```

Required env in the Cowork task:
- `OPENAI_API_KEY` (NEW — add to `~/.cowork-env` or wherever Cowork reads from)
- `METABASE_USER` / `METABASE_PASS` (existing)
- Optional: `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` (for tracing — recommended)

The judge will run for ~30 samples × 2s ≈ 1 minute on the cron schedule. Cost ~$0.15/day = ~$55/year.

---

## Recommendation

- **Tonight:** run Path A as a one-off. Validate the scoreboard makes sense on real production data.
- **Tomorrow morning (8:30 AM IST):** post the Path-A result to Slack as a thread reply on Cowork's digest. Note it as "manual preview — auto-integration coming this week."
- **This week:** integrate Path B once the scoreboard format is validated.

Two reasons for this order:
1. **Validates the prompt before automation.** If v8 surfaces something weird (e.g. all academic FAILs), you fix the prompt before it auto-posts to a channel where engineers will see it.
2. **Cowork SKILL.md edit is irreversible-ish.** Once the new step lives in the daily task, every morning ships the rubric. Better to do one manual run first.
