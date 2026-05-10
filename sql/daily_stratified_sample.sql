-- Daily stratified eval sample for v8 judge runner
-- =============================================================================
-- Dialect: Trino (json_extract_scalar, window functions).
-- Metabase Question 33193 — POST /api/card/{id}/query/json
--
-- Performance (refresh when Metabase "spins" or times out):
--   1. n_day via day_stats CROSS JOIN — avoids COUNT(*) OVER () on every enriched row.
--   2. base CTE — filter silver_conversational_query_table once; json_extract only on that set.
--   3. Sampling order: xxhash64(trace_id) replaces random() (cheaper full sorts at scale).
--
-- If still slow: check Trino/Metabase query max time, table stats/partitions on
-- silver_conversational_query_table, and whether feedback join duplicates rows per
-- aiintentid (duplicates inflate work — dedupe in a staging table if needed).
--
-- Optional session (Metabase native query prefix if allowed):
--   SET SESSION distinct_aggregations_strategy = 'single_step';
-- =============================================================================

WITH
-- One scan of conversational rows for the day (narrow as early as possible).
base AS (
  SELECT
    q.aiintentid,
    q.query,
    q.answer,
    q.output,
    LENGTH(q.answer) AS answer_len
  FROM astracdc.silver_conversational_query_table q
  WHERE q.intenttype = 'VIDEO_CO_PILOT'
    AND DATE(q.createdat) = CURRENT_DATE - INTERVAL '1' DAY
    AND COALESCE(json_extract_scalar(q.output, '$.metadata[0].category_name'), '') = 'academic'
),

day_stats AS (
  SELECT COUNT(*) AS n_day FROM base
),

enriched AS (
  SELECT
    b.aiintentid                                                            AS trace_id,
    b.query                                                                 AS doubt,
    b.answer                                                                AS ai_answer,
    ''                                                                      AS transcript,
    ''                                                                      AS ideal_answer,
    json_extract_scalar(b.output, '$.metadata[0].subject[0].subject')       AS subject,
    json_extract_scalar(b.output, '$.metadata[0].chapter[0].chapter')        AS chapter,
    json_extract_scalar(b.output, '$.user_message.author_metadata.classes') AS student_class,
    json_extract_scalar(b.output, '$.user_message.author_metadata.exam')    AS exam,
    json_extract_scalar(b.output, '$.metadata[0].image_url')                 AS image_url,
    CAST(json_extract_scalar(b.output, '$.metadata[0].is_annotated') AS BOOLEAN) AS is_annotated,
    f.rating,
    b.answer_len,
    ds.n_day
  FROM base b
  CROSS JOIN day_stats ds
  LEFT JOIN astracdc.silver_prod_feedback_by_user_entity f
    ON f.entity_id = b.aiintentid
   AND f.type IN ('copilot_message', 'message')
),

downvote_final AS (
  SELECT
    dv.stratum,
    dv.trace_id,
    dv.doubt,
    dv.ai_answer,
    dv.transcript,
    dv.ideal_answer,
    dv.subject,
    dv.chapter,
    dv.student_class,
    dv.exam,
    dv.image_url,
    dv.is_annotated,
    dv.rating,
    dv._sample_meta
  FROM (
    SELECT
      'downvote' AS stratum,
      e.trace_id,
      e.doubt,
      e.ai_answer,
      e.transcript,
      e.ideal_answer,
      e.subject,
      e.chapter,
      e.student_class,
      e.exam,
      e.image_url,
      e.is_annotated,
      e.rating,
      CAST(NULL AS VARCHAR) AS _sample_meta,
      ROW_NUMBER() OVER (
        PARTITION BY e.chapter
        ORDER BY xxhash64(to_utf8(CAST(e.trace_id AS varchar)))
      ) AS rn_chapter,
      ROW_NUMBER() OVER (
        ORDER BY xxhash64(to_utf8(CAST(e.trace_id AS varchar)))
      ) AS rn_global
    FROM enriched e
    WHERE e.rating = 0
  ) dv
  WHERE dv.rn_chapter <= 50
    AND dv.rn_global <= 1500
),

outlier_long_final AS (
  SELECT
    o.stratum,
    o.trace_id,
    o.doubt,
    o.ai_answer,
    o.transcript,
    o.ideal_answer,
    o.subject,
    o.chapter,
    o.student_class,
    o.exam,
    o.image_url,
    o.is_annotated,
    o.rating,
    o._sample_meta
  FROM (
    SELECT
      'outlier_long' AS stratum,
      e.trace_id,
      e.doubt,
      e.ai_answer,
      e.transcript,
      e.ideal_answer,
      e.subject,
      e.chapter,
      e.student_class,
      e.exam,
      e.image_url,
      e.is_annotated,
      e.rating,
      CONCAT('answer_len=', CAST(e.answer_len AS VARCHAR)) AS _sample_meta,
      ROW_NUMBER() OVER (ORDER BY e.answer_len DESC) AS rn_len,
      e.n_day
    FROM enriched e
    LEFT JOIN downvote_final d ON e.trace_id = d.trace_id
    WHERE d.trace_id IS NULL
  ) o
  WHERE o.rn_len <= LEAST(50, CEIL(0.01 * o.n_day))
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

upvote_final AS (
  SELECT
    u2.stratum,
    u2.trace_id,
    u2.doubt,
    u2.ai_answer,
    u2.transcript,
    u2.ideal_answer,
    u2.subject,
    u2.chapter,
    u2.student_class,
    u2.exam,
    u2.image_url,
    u2.is_annotated,
    u2.rating,
    u2._sample_meta
  FROM (
    SELECT
      'upvote' AS stratum,
      u.trace_id,
      u.doubt,
      u.ai_answer,
      u.transcript,
      u.ideal_answer,
      u.subject,
      u.chapter,
      u.student_class,
      u.exam,
      u.image_url,
      u.is_annotated,
      u.rating,
      CAST(NULL AS VARCHAR) AS _sample_meta,
      ROW_NUMBER() OVER (
        ORDER BY xxhash64(to_utf8(CAST(u.trace_id AS varchar) || 'g'))
      ) AS rn_global
    FROM (
      SELECT
        x.trace_id,
        x.doubt,
        x.ai_answer,
        x.transcript,
        x.ideal_answer,
        x.subject,
        x.chapter,
        x.student_class,
        x.exam,
        x.image_url,
        x.is_annotated,
        x.rating,
        x.answer_len,
        x.n_day,
        GREATEST(
          LEAST(30, x.chapter_total),
          CAST(CEIL(0.03 * x.chapter_total) AS INTEGER)
        ) AS chapter_target,
        ROW_NUMBER() OVER (
          PARTITION BY x.chapter
          ORDER BY xxhash64(to_utf8(CAST(x.trace_id AS varchar)))
        ) AS rn_chapter
      FROM (
        SELECT p.*, COUNT(*) OVER (PARTITION BY p.chapter) AS chapter_total
        FROM upvote_pool p
      ) x
    ) u
    WHERE u.rn_chapter <= LEAST(30, u.chapter_target)
  ) u2
  WHERE u2.rn_global <= 600
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

no_vote_final AS (
  SELECT
    n2.stratum,
    n2.trace_id,
    n2.doubt,
    n2.ai_answer,
    n2.transcript,
    n2.ideal_answer,
    n2.subject,
    n2.chapter,
    n2.student_class,
    n2.exam,
    n2.image_url,
    n2.is_annotated,
    n2.rating,
    n2._sample_meta
  FROM (
    SELECT
      'no_vote' AS stratum,
      n.trace_id,
      n.doubt,
      n.ai_answer,
      n.transcript,
      n.ideal_answer,
      n.subject,
      n.chapter,
      n.student_class,
      n.exam,
      n.image_url,
      n.is_annotated,
      n.rating,
      CAST(NULL AS VARCHAR) AS _sample_meta,
      ROW_NUMBER() OVER (
        ORDER BY xxhash64(to_utf8(CAST(n.trace_id AS varchar) || 'h'))
      ) AS rn_global
    FROM (
      SELECT
        x.trace_id,
        x.doubt,
        x.ai_answer,
        x.transcript,
        x.ideal_answer,
        x.subject,
        x.chapter,
        x.student_class,
        x.exam,
        x.image_url,
        x.is_annotated,
        x.rating,
        x.answer_len,
        x.n_day,
        GREATEST(
          LEAST(30, x.chapter_total),
          CAST(CEIL(0.15 * x.chapter_total) AS INTEGER)
        ) AS chapter_target,
        ROW_NUMBER() OVER (
          PARTITION BY x.chapter
          ORDER BY xxhash64(to_utf8(CAST(x.trace_id AS varchar)))
        ) AS rn_chapter
      FROM (
        SELECT p.*, COUNT(*) OVER (PARTITION BY p.chapter) AS chapter_total
        FROM no_vote_pool p
      ) x
    ) n
    WHERE n.rn_chapter <= LEAST(30, n.chapter_target)
  ) n2
  WHERE n2.rn_global <= 1500
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
