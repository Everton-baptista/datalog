CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  retention_days INTEGER DEFAULT 30 CHECK (retention_days BETWEEN 1 AND 3650),
  request_quota INTEGER NOT NULL DEFAULT 600 CHECK (request_quota BETWEEN 1 AND 60000),
  byte_quota BIGINT NOT NULL DEFAULT 134217728 CHECK (byte_quota BETWEEN 1048576 AND 10737418240),
  capture_content BOOLEAN NOT NULL DEFAULT false,
  allowed_attributes TEXT[] NOT NULL DEFAULT '{}'
);

-- Imported projects require a deliberate retention choice before deleting data.
INSERT INTO projects(id,retention_days)
SELECT project,NULL FROM (
  SELECT 'local' AS project UNION SELECT 'demo'
  UNION SELECT project FROM logs UNION SELECT project FROM metric_definitions
  UNION SELECT project FROM dashboards UNION SELECT project FROM monitors
  UNION SELECT project FROM ai_traces UNION SELECT project FROM ai_prompts
  UNION SELECT project FROM otlp_spans
) existing;

CREATE TABLE datalog_users (
  username TEXT PRIMARY KEY,
  salt BYTEA NOT NULL,
  password_hash BYTEA NOT NULL
);

CREATE TABLE project_members (
  project TEXT NOT NULL REFERENCES projects(id),
  username TEXT NOT NULL REFERENCES datalog_users(username),
  role TEXT NOT NULL CHECK (role IN ('admin','editor','viewer')),
  PRIMARY KEY(project, username)
);
CREATE INDEX project_members_user ON project_members(username);

CREATE TABLE browser_sessions (
  digest BYTEA PRIMARY KEY,
  username TEXT NOT NULL REFERENCES datalog_users(username) ON DELETE CASCADE,
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX browser_sessions_expiry ON browser_sessions(expires_at);

CREATE TABLE ingestion_keys (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  digest BYTEA NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ
);
CREATE INDEX ingestion_keys_project ON ingestion_keys(project, created_at DESC);

CREATE TABLE ingestion_quota (
  project TEXT PRIMARY KEY REFERENCES projects(id),
  minute BIGINT NOT NULL,
  requests INTEGER NOT NULL,
  bytes BIGINT NOT NULL
);

ALTER TABLE ai_scores DROP CONSTRAINT ai_scores_project_trace_id_fkey;
ALTER TABLE ai_scores ADD FOREIGN KEY(project,trace_id) REFERENCES ai_traces(project,id) ON DELETE CASCADE;
CREATE INDEX metrics_retention ON metrics(project,timestamp);
CREATE INDEX otlp_spans_retention ON otlp_spans(project,end_ns);
CREATE INDEX ai_scores_retention ON ai_scores(project,timestamp);
