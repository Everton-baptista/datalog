-- Preserve the producer's root state; derive the same displayed/filter state
-- for every consumer without interpreting children again in JavaScript.
CREATE FUNCTION ai_execution_status(root_status TEXT, error_count BIGINT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT AS $$
  SELECT CASE
    WHEN root_status = 'running' THEN 'running'
    WHEN root_status = 'error' OR error_count > 0 THEN 'error'
    ELSE 'ok'
  END
$$;

CREATE TABLE demo_refresh (
  project TEXT PRIMARY KEY CHECK(project = 'demo'),
  refreshed_at DOUBLE PRECISION NOT NULL,
  seed_id TEXT NOT NULL
);
