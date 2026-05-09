-- Daily stratified eval sample for v8 judge runner
-- =============================================================================
-- Dialect: Trino / Athena (uses json_extract_scalar, PERCENT_RANK, random()).
-- Saved as Metabase Question 33193. Fetched via POST /api/card/{id}/query/json.
--
-- Strategy: chapter-stratified sampling + outlier sweeps for silent failures.
--
-- Why this shape?
--   1. Per-chapter stratification matches the PM's prior human-audit
--      methodology — every chapter is represented, so a long-tail chapter
--      with 12 doubts/day still gets surfaced instead of being drowned out
--      by a few high-volume chapters.
--   2. Downvotes (rating=0) are 100% sampled (capped) because they are the
--      highest-signal failure mode we have direct evidence for.
--   3. Upvotes are 3% sampled with a per-chapter floor — upvotes are noisy
--      (users upvote ~indiscriminately) but we still need a positive-class
--      baseline for the judge's calibration.
--   4. No-vote rows are 15% sampled (heavier than upvotes) because Amazon
--      Science's 2024 production-LLM study found <5% of users leave any
--      feedback at all — the silent majority is where most real failures
--      hide. A 15% sweep gives the judge enough no-vote volume to detect
--      systematic issues that downvotes miss.
--   5. Outlier strata follow Hamel Husain's recommendation ("sort by
--      length, sort by latency, sort by tool-call count — the tails are
--      where your bugs live"). We add `outlier_long` to catch
--      runaway / hallucinatory generations regardless of user feedback.
--
-- Strata & caps:
--   downvote        : 100% of rating=0   | cap 50/chapter, 1500 total
--   upvote          : 3%   of rating=6   | floor min(30, chapter_count), cap 30/chapter, 600 total
--   no_vote         : 15%  of rating=NULL| floor min(30, chapter_count), cap 30/chapter, 1500 total
--   outlier_long    : top 1% by length(ai_answer)              | cap 50 total
--   outlier_latency : OMITTED — no latency column visible in
--                     astracdc.silver_conversational_query_table
--                     (no latency_ms / response_time / duration / *_ms field
--                      referenced in the existing schema we have access to).
--                     If a latency field is added later, plug it into a
--                     PERCENT_RANK() block mirroring outlier_long.
--   outlier_followup: OMITTED — single-table query has no reliable
--                     session_id / userid grouping field exposed at SQL
--                     level (q.aiintentid is per-trace, not per-session).
--                     Implementing ≥3-messages-in-60s requires either a
--                     join to a session/event table or a userid + timestamp
--                     pair we don't currently surface. Add when available.
--
-- De-duplication: a trace can qualify for multiple strata (e.g. a downvoted
-- long answer). We keep ONE row per trace_id, preferring the most-informative
-- stratum via this priority:
--     downvote (1) > outlier_long (2) > upvote (3) > no_vote (4)
--
-- Cost ceiling:
--   Total target: ~3,000 samples/day (≈$3/day @ $0.001/sample)
--   Hard ceiling: 4,200 samples (1500 + 600 + 1500 + 50 + 50 + 50 + 150 buffer)
--
-- Output schema (matches judge_runner.py expected input):
--   stratum, trace_id, doubt, ai_answer, transcript, ideal_answer,
--   subject, chapter, student_class, exam, image_url, is_annotated,
--   _sample_meta (debug-only stratum-specific metadata)
-- =============================================================================

WITH base AS (
  SELECT
    q.aiintentid                                                            AS trace_id,
    q.query                                                                 AS doubt,
    q.answer                                                                AS ai_answer,
    ''                                                                      AS transcript,
    ''                                                                      AS ideal_answer,
    json_extract_scalar(q.output, '$.metadata[0].subject[0].subject')       AS subject,
    json_extract_scalar(q.output, '$.metadata[0].chapter[0].chapter')       AS chapter,
    json_extract_scalar(q.output, '$.user_message.author_metadata.classes') AS student_class,
    json_extract_scalar(q.output, '$.user_message.author_metadata.exam')    AS exam,
    json_extract_scalar(q.output, '$.metadata[0].image_url')                AS image_url,
    CAST(json_extract_scalar(q.output, '$.metadata[0].is_annotated') AS BOOLEAN) AS is_annotated,
    f.rating
  FROM astracdc.silver_conversational_query_table q
  LEFT JOIN astracdc.silver_prod_feedback_by_user_entity f
    ON f.entity_id = q.aiintentid
   AND f.type IN ('copilot_message', 'message')
  WHERE q.intenttype = 'VIDEO_CO_PILOT'
    AND date(q.createdat) = current_date - INTERVAL '1' DAY
    AND coalesce(json_extract_scalar(q.output, '$.metadata[0].category_name'), '') = 'academic'
),

-- ---------------------------------------------------------------------------
-- Stratum 1: downvote — 100% of rating=0, capped per chapter (50) and global (1500)
-- ---------------------------------------------------------------------------
downvote_ranked AS (
  SELECT
    b.*,
    ROW_NUMBER() OVER (PARTITION BY b.chapter ORDER BY random()) AS rn_chapter
  FROM base b
  WHERE b.rating = 0
),
downvote_picked AS (
  SELECT
    'downvote' AS stratum,
    b.*,
    CAST(NULL AS VARCHAR) AS _sample_meta,
    ROW_NUMBER() OVER (ORDER BY random()) AS rn_global
  FROM downvote_ranked b
  WHERE b.rn_chapter <= 50
),
downvote_final AS (
  SELECT * FROM downvote_picked WHERE rn_global <= 1500
),

-- ---------------------------------------------------------------------------
-- Stratum 4: outlier_long — top 1% by length(ai_answer), regardless of rating
-- (Excludes anything already picked as a downvote — see priority order.)
-- ---------------------------------------------------------------------------
outlier_long_ranked AS (
  SELECT
    b.*,
    length(b.ai_answer) AS answer_len,
    PERCENT_RANK() OVER (ORDER BY length(b.ai_answer) DESC) AS pr_len
  FROM base b
),
outlier_long_picked AS (
  SELECT
    'outlier_long' AS stratum,
    o.trace_id, o.doubt, o.ai_answer, o.transcript, o.ideal_answer,
    o.subject, o.chapter, o.student_class, o.exam, o.image_url, o.is_annotated,
    o.rating,
    CONCAT('answer_len=', CAST(o.answer_len AS VARCHAR)) AS _sample_meta,
    ROW_NUMBER() OVER (ORDER BY o.answer_len DESC) AS rn_global
  FROM outlier_long_ranked o
  WHERE o.pr_len <= 0.01
    AND o.trace_id NOT IN (SELECT trace_id FROM downvote_final)
),
outlier_long_final AS (
  SELECT * FROM outlier_long_picked WHERE rn_global <= 50
),

-- ---------------------------------------------------------------------------
-- Stratum 2: upvote — 3% sample of rating=6, per-chapter floor min(30, count),
-- cap 30/chapter and 600 global. Excludes traces already picked above.
-- ---------------------------------------------------------------------------
upvote_pool AS (
  SELECT b.*
  FROM base b
  WHERE b.rating = 6
    AND b.trace_id NOT IN (SELECT trace_id FROM downvote_final)
    AND b.trace_id NOT IN (SELECT trace_id FROM outlier_long_final)
),
upvote_chapter_counts AS (
  SELECT chapter, COUNT(*) AS chapter_total FROM upvote_pool GROUP BY chapter
),
upvote_ranked AS (
  -- For each chapter, pick at least min(30, chapter_total) AND at least
  -- ceil(3% * chapter_total) — implemented as: take top-N by random() where
  -- N = greatest( min(30, chapter_total), ceil(0.03 * chapter_total) ),
  -- then cap at 30/chapter.
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
  SELECT * FROM upvote_picked WHERE rn_global <= 600
),

-- ---------------------------------------------------------------------------
-- Stratum 3: no_vote — 15% sample of rating IS NULL, per-chapter floor
-- min(30, count), cap 30/chapter and 1500 global.
-- ---------------------------------------------------------------------------
no_vote_pool AS (
  SELECT b.*
  FROM base b
  WHERE b.rating IS NULL
    AND b.trace_id NOT IN (SELECT trace_id FROM downvote_final)
    AND b.trace_id NOT IN (SELECT trace_id FROM outlier_long_final)
    AND b.trace_id NOT IN (SELECT trace_id FROM upvote_final)
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
  SELECT * FROM no_vote_picked WHERE rn_global <= 1500
)

-- ---------------------------------------------------------------------------
-- Final union — one row per trace_id, output schema unchanged plus _sample_meta
-- ---------------------------------------------------------------------------
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
