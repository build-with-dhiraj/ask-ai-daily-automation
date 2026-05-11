-- Q23036 — Downvoted Queries Dump — last 15 days
-- ============================================================
-- Prod Metabase: https://metabase-prod.penpencil.co/question/23036
-- Used by: daily_digest.py fmt_downvote_dump (rows fetched via fetch_metabase_card(23036))
--
-- IMPORTANT: SQL time window is rolling 15 days, NOT "yesterday".
-- The daily digest section is titled "Yesterday's Downvoted Queries Snapshot" because
-- daily_digest.py filters these rows down to feedback_date == yesterday in Python.
-- This wider SQL window is intentional — wider net + Python filter is more robust
-- against timezone edge cases than relying on a 1-day SQL predicate.
-- Do NOT "fix" this to interval '1' day without coordinating with daily_digest.py.
--
-- Filters: type IN ('copilot_message','message'), rating=0, intent VIDEO_CO_PILOT,
--          category != 'onboarding'
-- Excludes internal test user 66ac8d5822e7707c7312c5e8
-- Optional Metabase parameters: {{doubt_source}}, {{exam}}, {{batch}}, {{class}}
-- Output: per-query row with user, video, category, rating, feedback text, batch info
-- ============================================================

select k. id
, f.user_id
, cast(f.timestamp as date) as feedback_date
, f.question_text
, f.answer_text
, json_extract_scalar(output, '$.metadata[0].subject') as subject
, json_extract_scalar(output, '$.metadata[0].category_name') as category
, f.text user_feedback
, cast(json_extract(output, '$.metadata[0].is_annotated') as boolean) as is_annotated
, cast(json_extract(output, '$.metadata[0].image_url') as varchar) img_url
, f.rating
, u.firstname || ' ' || lastname as user_name
, u.primarynumber as user_phone
, json_extract_scalar(output, '$.metadata[0].doubt_source') as doubt_source
, e.batch
, e.exam

from
(select user_id
, question_text
, answer_text
, text
, rating
, entity_id
, timestamp
from astracdc.silver_prod_feedback_by_user_entity
where type in ('copilot_message', 'message')
and date(timestamp) >= date('2025-09-12')
and date(timestamp) >= current_date - interval '15' day
and user_id not in ('66ac8d5822e7707c7312c5e8')
and rating = 0) f

join
(select id
, output
, aiintentid
, json_extract_scalar(output, '$.metadata[0].video_type_id') scheduleid
from astracdc.silver_conversational_query_table
where intenttype = 'VIDEO_CO_PILOT'
and date(createdat) >= date('2025-09-12')
and json_extract_scalar(output, '$.metadata[0].category_name') != 'onboarding'
[[and json_extract_scalar(output, '$.metadata[0].doubt_source') = {{doubt_source}}]]) k
on f.entity_id = k.aiintentid

join
(select _id
, firstname
, lastname
, primarynumber
from cdp_revenue.gold_users) u
on f.user_id = u._id

left join
(select _id
, batchsubjectid
from cdp_revenue.gold_batch_subject_schedules) c
on k.scheduleid = c._id

left join
(select _id
, batchid
from user_experience.gold_batch_subjects) d
on c.batchsubjectid = d._id

left join
(select _id
, name batch
, exam
, class
from cdp_revenue.gold_batches_pw
where status = 'Active') e
on d.batchid = e._id

where 1=1
[[and e.exam = {{exam}}]]
[[and e.name = {{batch}}]]
[[and e.class = {{class}}]]

order by 2 desc
