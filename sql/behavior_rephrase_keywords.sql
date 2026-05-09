-- Digest: per-chapter rate of queries matching "rephrase / shorter / language switch" heuristics.
-- Prod Metabase: https://metabase-prod.penpencil.co/question/33283-metabase-behavior-rephrase-card
-- Secret: METABASE_BEHAVIOR_REPHRASE_CARD_ID=33283
--
-- Output columns: chapter, rephrase_keyword_pct, n_queries

SELECT
  json_extract_scalar(q.output, '$.metadata[0].chapter[0].chapter') AS chapter,
  100.0 * SUM(
    IF(
      REGEXP_LIKE(
        LOWER(q.query),
        '(explain again|say it differently|different way|shorter|too long|too verbose|fir se|phir se|dubara|hindi me|in hindi|हिंदी)'
      ),
      1,
      0
    )
  ) / NULLIF(COUNT(*), 0) AS rephrase_keyword_pct,
  COUNT(*) AS n_queries
FROM astracdc.silver_conversational_query_table q
WHERE q.intenttype = 'VIDEO_CO_PILOT'
  AND DATE(q.createdat) = CURRENT_DATE - INTERVAL '1' DAY
  AND COALESCE(json_extract_scalar(q.output, '$.metadata[0].category_name'), '') = 'academic'
GROUP BY 1
HAVING COUNT(*) >= 20
ORDER BY rephrase_keyword_pct DESC
LIMIT 25
