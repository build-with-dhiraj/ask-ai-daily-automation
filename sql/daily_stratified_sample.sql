-- Daily stratified eval sample for v8 judge runner
-- =============================================================================
-- Dialect: Trino (json_extract_scalar, PERCENT_RANK, random()).
-- Saved as Metabase Question 33193. Fetched via POST /api/card/{id}/query/json.
--
-- Planners note: prior version referenced `base` in four branches + many NOT IN
-- subqueries (~173 stages, Trino max 150). This rewrite:
--   • Single `enriched` CTE — answer_len + PERCENT_RANK (`pr_len`) in one scan
--   • Anti-joins instead of NOT IN (...)
--   • Shared exclusion unions (`taken_down_out`, `taken_all`)
--
-- (Older Trino/Starburst: no `AS MATERIALIZED` — unsupported on this cluster.)
-- If you again hit the stage limit, ask platform for session tweaks or a CTAS step.
-- =============================================================================

WITH enriched AS (
  SELECT
    q.aiintentid                                                            AS trace_id,
    q.query                                                                 AS doubt,
    q.answer                                                                AS ai_answer,
    ''                                                                      AS transcript,
    ''                                                                      AS ideal_answer,
    json_extract_scalar(q.output, '$.metadata[0].subject[0].subject')         AS subject,
    json_extract_scalar(q.output, '$.metadata[0].chapter[0].chapter')     AS chapter,
    json_extract_scalar(q.output, '$.user_message.author_metadata.classes') AS student_class,
    json_extract_scalar(q.output, '$.user_message.author_metadata.exam')    AS exam,
    json_extract_scalar(q.output, '$.metadata[0].image_url')                 AS image_url,
    CAST(json_extract_scalar(q.output, '$.metadata[0].is_annotated') AS BOOLEAN) AS is_annotated,
    f.rating,
    LENGTH(q.answer)                                                        AS answer_len,
    PERCENT_RANK() OVER (ORDER BY LENGTH(q.answer) DESC)                     AS pr_len
  FROM astracdc.silver_conversational_query_table q
  LEFT JOIN astracdc.silver_prod_feedback_by_user_entity f
    ON f.entity_id = q.aiintentid
   AND f.type IN ('copilot_message', 'message')
  WHERE q.intenttype = 'VIDEO_CO_PILOT'
    AND DATE(q.createdat) = CURRENT_DATE - INTERVAL '1' DAY
    AND COALESCE(json_extract_scalar(q.output, '$.metadata[0].category_name'), '') = 'academic'
),

downvote_ranked AS (
  SELECT
    e.*,
    ROW_NUMBER() OVER (PARTITION BY e.chapter ORDER BY random()) AS rn_chapter
  FROM enriched e
  WHERE e.rating = 0
),
downvote_picked AS (
  SELECT
    'downvote' AS stratum,
    b.trace_id, b.doubt, b.ai_answer, b.transcript, b.ideal_answer,
    b.subject, b.chapter, b.student_class, b.exam, b.image_url, b.is_annotated,
    b.rating, b.answer_len, b.pr_len,
    CAST(NULL AS VARCHAR) AS _sample_meta,
    ROW_NUMBER() OVER (ORDER BY random()) AS rn_global
  FROM downvote_ranked b
  WHERE b.rn_chapter <= 50
),
downvote_final AS (
  SELECT stratum, trace_id, doubt, ai_answer, transcript, ideal_answer,
         subject, chapter, student_class, exam, image_url, is_annotated,
         rating, _sample_meta
  FROM downvote_picked
  WHERE rn_global <= 1500
),

outlier_long_picked AS (
  SELECT
    'outlier_long' AS stratum,
    e.trace_id, e.doubt, e.ai_answer, e.transcript, e.ideal_answer,
    e.subject, e.chapter, e.student_class, e.exam, e.image_url, e.is_annotated,
    e.rating,
    CONCAT('answer_len=', CAST(e.answer_len AS VARCHAR)) AS _sample_meta,
    ROW_NUMBER() OVER (ORDER BY e.answer_len DESC) AS rn_global
  FROM enriched e
  LEFT JOIN downvote_final d ON e.trace_id = d.trace_id
  WHERE d.trace_id IS NULL
    AND e.pr_len <= 0.01
),
outlier_long_final AS (
  SELECT stratum, trace_id, doubt, ai_answer, transcript, ideal_answer,
         subject, chapter, student_class, exam, image_url, is_annotated,
         rating, _sample_meta
  FROM outlier_long_picked
  WHERE rn_global <= 50
),

taken_down_out AS (
  SELECT trace_id FROM downvote_final
  UNION ALL
  SELECT trace_id FROM outlier_long_final
),

upvote_pool AS (
  SELECT e.*
  FROM enriched e
  LEFT JOIN taken_down_out t ON e.trace_id = t.trace_id
  WHERE e.rating = 6
    AND t.trace_id IS NULL
),
upvote_chapter_counts AS (
  SELECT chapter, COUNT(*) AS chapter_total FROM upvote_pool GROUP BY chapter
),
upvote_ranked AS (
  SELECT
    p.*,
    c.chapter_total,
    GREATEST(
      LEAST(30, c.chapter_total),
      CAST(CEIL(0.03 * c.chapter_total) AS INTEGER)
    ) AS chapter_target,
    ROW_NUMBER() OVER (PARTITION BY p.chapter ORDER BY random()) AS rn_chapter
  FROM upvote_pool p
  JOIN upvote_chapter_counts c ON c.chapter = p.chapter
),
upvote_picked AS (
  SELECT
    'upvote' AS stratum,
    u.trace_id, u.doubt, u.ai_answer, u.transcript, u.ideal_answer,
    u.subject, u.chapter, u.student_class, u.exam, u.image_url, u.is_annotated,
    u.rating,
    CAST(NULL AS VARCHAR) AS _sample_meta,
    ROW_NUMBER() OVER (ORDER BY random()) AS rn_global
  FROM upvote_ranked u
  WHERE u.rn_chapter <= LEAST(30, u.chapter_target)
),
upvote_final AS (
  SELECT stratum, trace_id, doubt, ai_answer, transcript, ideal_answer,
         subject, chapter, student_class, exam, image_url, is_annotated,
         rating, _sample_meta
  FROM upvote_picked
  WHERE rn_global <= 600
),

taken_all AS (
  SELECT trace_id FROM downvote_final
  UNION ALL
  SELECT trace_id FROM outlier_long_final
  UNION ALL
  SELECT trace_id FROM upvote_final
),

no_vote_pool AS (
  SELECT e.*
  FROM enriched e
  LEFT JOIN taken_all t ON e.trace_id = t.trace_id
  WHERE e.rating IS NULL
    AND t.trace_id IS NULL
),
no_vote_chapter_counts AS (
  SELECT chapter, COUNT(*) AS chapter_total FROM no_vote_pool GROUP BY chapter
),
no_vote_ranked AS (
  SELECT
    p.*,
    c.chapter_total,
    GREATEST(
      LEAST(30, c.chapter_total),
      CAST(CEIL(0.15 * c.chapter_total) AS INTEGER)
    ) AS chapter_target,
    ROW_NUMBER() OVER (PARTITION BY p.chapter ORDER BY random()) AS rn_chapter
  FROM no_vote_pool p
  JOIN no_vote_chapter_counts c ON c.chapter = p.chapter
),
no_vote_picked AS (
  SELECT
    'no_vote' AS stratum,
    n.trace_id, n.doubt, n.ai_answer, n.transcript, n.ideal_answer,
    n.subject, n.chapter, n.student_class, n.exam, n.image_url, n.is_annotated,
    n.rating,
    CAST(NULL AS VARCHAR) AS _sample_meta,
    ROW_NUMBER() OVER (ORDER BY random()) AS rn_global
  FROM no_vote_ranked n
  WHERE n.rn_chapter <= LEAST(30, n.chapter_target)
),
no_vote_final AS (
  SELECT stratum, trace_id, doubt, ai_answer, transcript, ideal_answer,
         subject, chapter, student_class, exam, image_url, is_annotated,
         rating, _sample_meta
  FROM no_vote_picked
  WHERE rn_global <= 1500
)

SELECT stratum, trace_id, doubt, ai_answer, transcript, ideal_answer,
       subject, chapter, student_class, exam, image_url, is_annotated,
       _sample_meta
FROM downvote_final
UNION ALL
SELECT stratum, trace_id, doubt, ai_answer, transcript, ideal_answer,
       subject, chapter, student_class, exam, image_url, is_annotated,
       _sample_meta
FROM outlier_long_final
UNION ALL
SELECT stratum, trace_id, doubt, ai_answer, transcript, ideal_answer,
       subject, chapter, student_class, exam, image_url, is_annotated,
       _sample_meta
FROM upvote_final
UNION ALL
SELECT stratum, trace_id, doubt, ai_answer, transcript, ideal_answer,
       subject, chapter, student_class, exam, image_url, is_annotated,
       _sample_meta
FROM no_vote_final
