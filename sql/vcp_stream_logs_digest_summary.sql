-- Daily digest: video co-pilot API lifecycle summary (stream_logs → Trino sink)
-- =============================================================================
-- Prod Metabase: https://metabase-prod.penpencil.co/question/33285-metabase-stream-logs-card
-- GitHub secret: METABASE_STREAM_LOGS_CARD_ID=33285
-- Table: central.silver_stream_logs
-- Schema reference: ask-ai-behavioural-onboarding / 01_stream_logs_reference.docx
--
-- Time window: calendar YESTERDAY in the Trino session timezone (matches
-- behavior_rephrase_keywords.sql and other digest cards). The Langfuse block in
-- the same Slack message uses rolling last-24h UTC — intentionally different.
--
-- Output contract (single row) — consumed by daily_digest.py fmt_stream_logs_summary:
--   n_requests, n_failure, n_success, failure_pct,
--   n_http_400, n_http_499, n_http_500,
--   n_failure_http_200, n_stream_flow_failed, n_success_with_handled_errors,
--   n_cancelled_error
-- =============================================================================

SELECT
  COUNT(*) AS n_requests,
  SUM(CASE WHEN status = 'FAILURE' THEN 1 ELSE 0 END) AS n_failure,
  SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS n_success,
  ROUND(100.0 * SUM(CASE WHEN status = 'FAILURE' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2)
    AS failure_pct,
  SUM(CASE WHEN http_status_code = 400 THEN 1 ELSE 0 END) AS n_http_400,
  SUM(CASE WHEN http_status_code = 499 THEN 1 ELSE 0 END) AS n_http_499,
  SUM(CASE WHEN http_status_code = 500 THEN 1 ELSE 0 END) AS n_http_500,
  SUM(
    CASE
      WHEN status = 'FAILURE' AND http_status_code = 200 THEN 1
      ELSE 0
    END
  ) AS n_failure_http_200,
  SUM(
    CASE
      WHEN steps_completed IS NOT NULL
        AND strpos(CAST(steps_completed AS VARCHAR), 'stream_flow_failed') > 0
      THEN 1
      ELSE 0
    END
  ) AS n_stream_flow_failed,
  SUM(
    CASE
      WHEN status = 'SUCCESS'
        AND COALESCE(
          TRY(json_array_length(CAST(handled_errors AS JSON))),
          TRY(json_array_length(json_parse(trim(CAST(handled_errors AS VARCHAR))))),
          0
        ) > 0
      THEN 1
      ELSE 0
    END
  ) AS n_success_with_handled_errors,
  SUM(
    CASE
      WHEN error_type IS NOT NULL
        AND strpos(LOWER(CAST(error_type AS VARCHAR)), 'cancelled') > 0
      THEN 1
      ELSE 0
    END
  ) AS n_cancelled_error
FROM central.silver_stream_logs
WHERE DATE(created_at) = CURRENT_DATE - INTERVAL '1' DAY
  AND endpoint = '/v1/nebula/video-co-pilot'
  AND intent_type = 'VIDEO_CO_PILOT'
