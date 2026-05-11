-- Q33324 — Daily Downvote Free-Text Feedback (yesterday) — last 2 days (Python filters to yesterday)
-- ============================================================
-- Prod Metabase: https://metabase-prod.penpencil.co/question/33324
-- GitHub secret: METABASE_FREETEXT_CARD_ID=33324
-- Used by: daily_feedback_classifier.py (3rd job in Daily Automation)
-- Time window: rolling 2 days in SQL; Python filters to feedback_date == yesterday
--              (same robust pattern as Q23036 — wider net + Python filter survives TZ edges).
--
-- Pre-defined feedback tags stripped to derive the free-text portion (any prefix is removed):
--   Incorrect answer
--   Took too much time
--   Couldn't understand the question      (NOTE: curly apostrophe — copied verbatim from prod values)
--   Voice issues
--   Formatting issues (formula / equations)
--   Other
-- Each tag is iteratively regexp_replace'd from `text` (with trailing comma + whitespace),
-- yielding `free_text_feedback`. Rows where the residual is empty/whitespace are excluded.
--
-- Filters: type IN ('copilot_message','message'), rating=0, intent VIDEO_CO_PILOT,
--          category != 'onboarding', exclude internal test user 66ac8d5822e7707c7312c5e8
-- Optional Metabase parameters: {{doubt_source}}, {{exam}}, {{batch}}, {{class}}
-- Output columns: aiintentid, userid, subject, chapter, category, doubt_source,
--                 product_label, free_text_feedback, feedback_date
-- See sql/README.md for the file → card mapping.
-- ============================================================

with raw as (
    select
        f.user_id
      , f.entity_id                                              as aiintentid
      , cast(f.timestamp as date)                                as feedback_date
      , f.text                                                   as raw_text
      , trim(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(
                                regexp_replace(f.text,
                                    'Formatting issues \(formula / equations\)\s*,?\s*', ''),
                                'Couldn’t understand the question\s*,?\s*', ''),
                            'Took too much time\s*,?\s*', ''),
                        'Voice issues\s*,?\s*', ''),
                    'Incorrect answer\s*,?\s*', ''),
                'Other\s*,?\s*', '')
        )                                                        as free_text_feedback
      -- Tags actually PRESENT in raw text — comma-joined for readability in product_label
      , array_join(
            filter(
                array[
                    if(strpos(f.text, 'Incorrect answer') > 0,                       'Incorrect answer',                       null),
                    if(strpos(f.text, 'Took too much time') > 0,                      'Took too much time',                      null),
                    if(strpos(f.text, 'Couldn’t understand the question') > 0,        'Couldn’t understand the question',        null),
                    if(strpos(f.text, 'Voice issues') > 0,                            'Voice issues',                            null),
                    if(strpos(f.text, 'Formatting issues (formula / equations)') > 0, 'Formatting issues (formula / equations)', null),
                    if(strpos(f.text, 'Other') > 0,                                   'Other',                                   null)
                ],
                x -> x is not null
            ),
            ', '
        )                                                        as product_label
    from astracdc.silver_prod_feedback_by_user_entity f
    where f.type in ('copilot_message', 'message')
      and date(f.timestamp) >= date('2025-09-12')
      and date(f.timestamp) >= current_date - interval '2' day
      and f.user_id not in ('66ac8d5822e7707c7312c5e8')
      and f.rating = 0
      and f.text is not null
      and length(trim(f.text)) > 0
)

select
    k.aiintentid
  , r.user_id                                                       as userid
  , json_extract_scalar(k.output, '$.metadata[0].subject')          as subject
  , coalesce(
        json_extract_scalar(k.output, '$.metadata[0].chapter_name'),
        json_extract_scalar(k.output, '$.metadata[0].category_name')
    )                                                               as chapter
  , json_extract_scalar(k.output, '$.metadata[0].category_name')    as category
  , json_extract_scalar(k.output, '$.metadata[0].doubt_source')     as doubt_source
  , r.product_label                                                 as product_label
  , r.free_text_feedback                                            as free_text_feedback
  , r.feedback_date                                                 as feedback_date

from raw r

join
(select id
      , aiintentid
      , output
      , json_extract_scalar(output, '$.metadata[0].video_type_id') as scheduleid
 from astracdc.silver_conversational_query_table
 where intenttype = 'VIDEO_CO_PILOT'
   and date(createdat) >= date('2025-09-12')
   and json_extract_scalar(output, '$.metadata[0].category_name') != 'onboarding'
   [[and json_extract_scalar(output, '$.metadata[0].doubt_source') = {{doubt_source}}]]
) k
on r.aiintentid = k.aiintentid

left join
(select _id, batchsubjectid from cdp_revenue.gold_batch_subject_schedules) c
on k.scheduleid = c._id

left join
(select _id, batchid from user_experience.gold_batch_subjects) d
on c.batchsubjectid = d._id

left join
(select _id, name as batch, exam, class
 from cdp_revenue.gold_batches_pw
 where status = 'Active') e
on d.batchid = e._id

where length(trim(r.free_text_feedback)) > 0
  [[and e.exam = {{exam}}]]
  [[and e.batch = {{batch}}]]
  [[and e.class = {{class}}]]

order by r.feedback_date desc, k.aiintentid
