-- Q24973 — Top Downvote Reasons — Academic — last 21 days
-- ============================================================
-- Prod Metabase: https://metabase-prod.penpencil.co/question/24973
-- Used by: daily_digest.py fmt_academic (rows fetched via fetch_metabase_card(24973))
-- Time window: rolling 21d from CURRENT_DATE
-- Filters: type IN ('copilot_message','message'), rating=0, intent VIDEO_CO_PILOT, category=academic
-- Excludes internal test user 66ac8d5822e7707c7312c5e8
-- Optional Metabase parameters: {{doubt_source}}, {{exam}}, {{batch}}, {{class}}
-- Output: feedback_text, downvotes — top 10 by downvote count
-- ============================================================

select feedback_text
, count(distinct a.entity_id) downvotes

from
(select id
, entity_id
, rating
, timestamp
, text
, trim(feedback) AS feedback_text
from astracdc.silver_prod_feedback_by_user_entity
cross join unnest(regexp_split(text, ',')) as t(feedback)
where type in ('copilot_message', 'message')
and date(timestamp) >= date('2025-09-12')
and date(timestamp) >= CURRENT_DATE - Interval '21' day
and user_id not in ('66ac8d5822e7707c7312c5e8')
and rating = 0
and trim(feedback) <> '') a

join
(select id
, aiintentid
, json_extract_scalar(output, '$.metadata[0].category_name') as type
, json_extract_scalar(output, '$.metadata[0].video_type_id') scheduleid
from astracdc.silver_conversational_query_table
where intenttype = 'VIDEO_CO_PILOT'
and date(createdat) >= date('2025-09-12')
and json_extract_scalar(output, '$.metadata[0].category_name') != 'onboarding'
and json_extract_scalar(output, '$.metadata[0].category_name') = 'academic'
[[and json_extract_scalar(output, '$.metadata[0].doubt_source') = {{doubt_source}}]]) b
on a.entity_id = b.aiintentid

left join
(select _id
, batchsubjectid
from cdp_revenue.gold_batch_subject_schedules) c
on b.scheduleid = c._id

left join
(select _id
, batchid
from user_experience.gold_batch_subjects) d
on c.batchsubjectid = d._id

left join
(select _id
, exam
, name
, class
from cdp_revenue.gold_batches_pw
where status = 'Active') e
on d.batchid = e._id

where 1=1
[[and e.exam = {{exam}}]]
[[and e.name = {{batch}}]]
[[and e.class = {{class}}]]

group by 1
order by 2 desc

limit 10
