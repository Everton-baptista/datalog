ALTER TABLE logs ADD COLUMN span_id TEXT;
ALTER TABLE logs ADD COLUMN otlp_id BYTEA;
CREATE UNIQUE INDEX logs_otlp_identity ON logs(project,otlp_id) WHERE otlp_id IS NOT NULL;
CREATE INDEX logs_span ON logs(project,lower(trace_id),span_id,timestamp DESC) WHERE span_id IS NOT NULL;

CREATE TABLE otlp_metrics (
  project TEXT NOT NULL REFERENCES projects(id),
  point_id BYTEA NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  service TEXT NOT NULL,
  env TEXT NOT NULL,
  time_ns BIGINT NOT NULL,
  temporality INTEGER,
  monotonic BOOLEAN,
  resource JSONB NOT NULL,
  scope JSONB NOT NULL,
  descriptor JSONB NOT NULL,
  point JSONB NOT NULL,
  PRIMARY KEY(project, point_id)
);
CREATE INDEX otlp_metrics_recent ON otlp_metrics(project,time_ns DESC,point_id);
CREATE INDEX otlp_metrics_name ON otlp_metrics(project,name,time_ns DESC);

CREATE INDEX ai_traces_expiry ON ai_traces(project,(coalesce(end_time,timestamp)));

CREATE TABLE datasets (
  project TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  cases JSONB NOT NULL,
  PRIMARY KEY(project,name,version)
);

CREATE TABLE agent_evaluations (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  dataset TEXT NOT NULL,
  dataset_version INTEGER NOT NULL,
  case_id TEXT NOT NULL,
  execution_version TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  span_id TEXT NOT NULL,
  prompt_name TEXT,
  prompt_version INTEGER,
  criterion TEXT NOT NULL,
  score DOUBLE PRECISION NOT NULL CHECK(score BETWEEN 0 AND 1),
  timestamp DOUBLE PRECISION NOT NULL,
  FOREIGN KEY(project,dataset,dataset_version) REFERENCES datasets(project,name,version),
  FOREIGN KEY(project,trace_id,span_id) REFERENCES otlp_spans(project,trace_id,span_id) ON DELETE CASCADE,
  FOREIGN KEY(project,prompt_name,prompt_version) REFERENCES ai_prompts(project,name,version)
);
CREATE INDEX agent_evaluations_trace ON agent_evaluations(project,trace_id,span_id);
CREATE INDEX agent_evaluations_dataset ON agent_evaluations(project,dataset,dataset_version,criterion,case_id);
CREATE UNIQUE INDEX agent_evaluations_identity ON agent_evaluations
  (project,dataset,dataset_version,case_id,execution_version,criterion,trace_id,span_id);
