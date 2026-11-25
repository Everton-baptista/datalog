CREATE TABLE ai_traces (
  project TEXT NOT NULL,
  id TEXT NOT NULL,
  name TEXT NOT NULL,
  service TEXT NOT NULL,
  env TEXT NOT NULL,
  agent TEXT,
  session_id TEXT,
  user_id TEXT,
  timestamp DOUBLE PRECISION NOT NULL,
  end_time DOUBLE PRECISION,
  status TEXT NOT NULL CHECK (status IN ('ok','error','running')),
  input JSONB,
  output JSONB,
  metadata JSONB NOT NULL,
  PRIMARY KEY(project,id),
  CHECK (end_time IS NULL OR end_time >= timestamp)
);
CREATE INDEX ai_traces_time ON ai_traces(project,timestamp DESC,id);
CREATE INDEX ai_traces_session ON ai_traces(project,session_id,timestamp);
CREATE INDEX ai_traces_agent ON ai_traces(project,agent,timestamp DESC);

CREATE TABLE ai_spans (
  project TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  id TEXT NOT NULL,
  parent_id TEXT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('agent','llm','tool','retriever','workflow')),
  timestamp DOUBLE PRECISION NOT NULL,
  end_time DOUBLE PRECISION,
  status TEXT NOT NULL CHECK(status IN ('ok','error','running')),
  model TEXT,
  provider TEXT,
  input_tokens BIGINT,
  output_tokens BIGINT,
  cost_usd DOUBLE PRECISION,
  cost_source TEXT CHECK(cost_source IN ('reported','estimated')),
  prompt_name TEXT,
  prompt_version INTEGER,
  input JSONB,
  output JSONB,
  metadata JSONB NOT NULL,
  PRIMARY KEY(project,trace_id,id),
  FOREIGN KEY(project,trace_id) REFERENCES ai_traces(project,id) ON DELETE CASCADE,
  FOREIGN KEY(project,trace_id,parent_id) REFERENCES ai_spans(project,trace_id,id) DEFERRABLE INITIALLY DEFERRED,
  CHECK(end_time IS NULL OR end_time >= timestamp),
  CHECK(input_tokens IS NULL OR input_tokens >= 0),
  CHECK(output_tokens IS NULL OR output_tokens >= 0),
  CHECK(cost_usd IS NULL OR cost_usd >= 0),
  CHECK(kind='llm' OR (input_tokens IS NULL AND output_tokens IS NULL AND cost_usd IS NULL))
);
CREATE INDEX ai_spans_trace ON ai_spans(project,trace_id,timestamp);

CREATE TABLE ai_scores (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  value DOUBLE PRECISION NOT NULL CHECK(value BETWEEN 0 AND 1),
  source TEXT NOT NULL CHECK(source IN ('human','external')),
  comment TEXT NOT NULL,
  timestamp DOUBLE PRECISION NOT NULL,
  FOREIGN KEY(project,trace_id) REFERENCES ai_traces(project,id)
);
CREATE INDEX ai_scores_trace ON ai_scores(project,trace_id,timestamp DESC);

CREATE TABLE ai_prompts (
  project TEXT NOT NULL,
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  model TEXT,
  timestamp DOUBLE PRECISION NOT NULL,
  PRIMARY KEY(project,name,version)
);

CREATE VIEW ai_trace_rollups AS
SELECT t.*, (t.end_time-t.timestamp)*1000 AS duration_ms,
       coalesce(s.span_count,0) AS span_count,
       coalesce(s.llm_calls,0) AS llm_calls,
       coalesce(s.tool_calls,0) AS tool_calls,
       coalesce(s.span_errors,0) AS span_errors,
       s.tokens, s.cost_usd,
       coalesce(s.token_complete_calls,0) AS token_complete_calls,
       coalesce(s.cost_reported_calls,0) AS cost_reported_calls,
       coalesce(s.cost_estimated_calls,0) AS cost_estimated_calls,
       coalesce(s.cost_known_calls,0) AS cost_known_calls,
       coalesce(s.models,'[]'::jsonb) AS models
FROM ai_traces t
LEFT JOIN (
  SELECT project,trace_id,count(*) AS span_count,
         count(*) FILTER(WHERE kind='llm') AS llm_calls,
         count(*) FILTER(WHERE kind='tool') AS tool_calls,
         count(*) FILTER(WHERE status='error') AS span_errors,
         (sum(coalesce(input_tokens,0)+coalesce(output_tokens,0)) FILTER(WHERE kind='llm' AND (input_tokens IS NOT NULL OR output_tokens IS NOT NULL)))::bigint AS tokens,
         sum(cost_usd) FILTER(WHERE kind='llm') AS cost_usd,
         count(*) FILTER(WHERE kind='llm' AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL) AS token_complete_calls,
         count(*) FILTER(WHERE kind='llm' AND cost_usd IS NOT NULL) AS cost_known_calls,
         count(*) FILTER(WHERE kind='llm' AND cost_source='reported' AND cost_usd IS NOT NULL) AS cost_reported_calls,
         count(*) FILTER(WHERE kind='llm' AND cost_source='estimated' AND cost_usd IS NOT NULL) AS cost_estimated_calls,
         jsonb_agg(DISTINCT model) FILTER(WHERE kind='llm' AND model IS NOT NULL) AS models
  FROM ai_spans GROUP BY project,trace_id
) s ON s.project=t.project AND s.trace_id=t.id;
