-- Digest: per-chapter "triple follow-up in 60s" proxy (silent frustration / confusion).
-- Same academic VIDEO_CO_PILOT filter as daily stratified sample.
-- Uses silver_conversational_query_table.userid (see DATA_BIBLE).
-- Save as a Metabase question; set METABASE_BEHAVIOR_FOLLOWUP_CARD_ID in the digest workflow.
--
-- Output columns (flexible names for daily_digest.py): chapter, triple_followup_60s_pct, n_queries

WITH base AS (
  SELECT
    q.userid,
    CAST(q.createdat AS TIMESTAMP) AS ts,
    json_extract_scalar(q.output, '$.metadata[0].chapter[0].chapter') AS chapter
  FROM astracdc.silver_conversational_query_table q
  WHERE q.intenttype = 'VIDEO_CO_PILOT'
    AND DATE(q.createdat) = CURRENT_DATE - INTERVAL '1' DAY
    AND COALESCE(json_extract_scalar(q.output, '$.metadata[0].category_name'), '') = 'academic'
),
marked AS (
  SELECT
    chapter,
    userid,
    ts,
    COUNT(*) OVER (
      PARTITION BY userid
      ORDER BY ts
      RANGE BETWEEN INTERVAL '59' SECOND PRECEDING AND CURRENT ROW
    ) AS window_cnt
  FROM base
)
SELECT
  chapter,
  100.0 * COUNT_IF(window_cnt >= 3) / NULLIF(COUNT(*), 0) AS triple_followup_60s_pct,
  COUNT(*) AS n_queries
FROM marked
WHERE chapter IS NOT NULL AND TRIM(chapter) <> ''
GROUP BY 1
HAVING COUNT(*) >= 20
ORDER BY triple_followup_60s_pct DESC
LIMIT 25
