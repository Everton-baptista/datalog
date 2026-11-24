ALTER TABLE projects ADD COLUMN signal_retention JSONB NOT NULL DEFAULT '{}';
ALTER TABLE projects ADD COLUMN log_tail_watermark BIGINT NOT NULL DEFAULT 0;
CREATE INDEX logs_tail ON logs(project,id);
CREATE INDEX logs_tags_search ON logs USING gin(tags jsonb_path_ops);

CREATE TABLE project_audit (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project TEXT NOT NULL REFERENCES projects(id),
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  timestamp DOUBLE PRECISION NOT NULL DEFAULT extract(epoch FROM now()),
  result TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX project_audit_recent ON project_audit(project,id DESC);

CREATE TABLE project_tokens (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL REFERENCES projects(id),
  username TEXT NOT NULL REFERENCES datalog_users(username),
  digest BYTEA NOT NULL UNIQUE,
  capability TEXT NOT NULL CHECK(capability IN ('read','evaluate','invoke')),
  expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '30 days'
);

CREATE TABLE oidc_flows (
  state BYTEA PRIMARY KEY, verifier TEXT NOT NULL, nonce TEXT NOT NULL,
  binding BYTEA NOT NULL, expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE oidc_identities (
  issuer TEXT NOT NULL, subject TEXT NOT NULL,
  username TEXT NOT NULL REFERENCES datalog_users(username),
  PRIMARY KEY(issuer,subject)
);

ALTER TABLE otlp_metrics ADD COLUMN stream_id BYTEA;
CREATE INDEX otlp_metrics_stream ON otlp_metrics(project,name,stream_id,time_ns DESC);
ALTER TABLE dashboards ADD COLUMN panels JSONB;
ALTER TABLE monitors ADD COLUMN configuration JSONB NOT NULL DEFAULT '{}';
ALTER TABLE evaluations ADD COLUMN evidence JSONB;

CREATE TABLE alert_routes (
  project TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL,
  destination TEXT NOT NULL, service TEXT NOT NULL, severity TEXT NOT NULL,
  PRIMARY KEY(project,id)
);
CREATE TABLE alert_silences (
  project TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL,
  service TEXT NOT NULL, starts_at DOUBLE PRECISION NOT NULL,
  ends_at DOUBLE PRECISION NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY(project,id), CHECK(ends_at > starts_at)
);
CREATE TABLE alert_deliveries (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL REFERENCES projects(id),
  destination TEXT NOT NULL, payload JSONB NOT NULL,
  group_key TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt DOUBLE PRECISION NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  last_error TEXT
);
CREATE INDEX alert_deliveries_pending ON alert_deliveries(next_attempt) WHERE state='pending';
CREATE INDEX alert_deliveries_group ON alert_deliveries(project,destination,group_key) WHERE state='pending' AND attempts=0;
CREATE INDEX alert_deliveries_retention ON alert_deliveries(project,next_attempt) WHERE state<>'pending';

CREATE TABLE issue_triage (
  project TEXT NOT NULL REFERENCES projects(id), fingerprint TEXT NOT NULL,
  assignee TEXT REFERENCES datalog_users(username),
  resolved BOOLEAN NOT NULL DEFAULT false,
  PRIMARY KEY(project,fingerprint)
);

CREATE TABLE model_prices (
  project TEXT NOT NULL REFERENCES projects(id),
  provider TEXT NOT NULL, model TEXT NOT NULL, effective_at DOUBLE PRECISION NOT NULL,
  input_per_million DOUBLE PRECISION NOT NULL, output_per_million DOUBLE PRECISION NOT NULL,
  PRIMARY KEY(project,provider,model,effective_at)
);
CREATE TABLE prompt_aliases (
  project TEXT NOT NULL, name TEXT NOT NULL, alias TEXT NOT NULL, version INTEGER NOT NULL,
  PRIMARY KEY(project,name,alias),
  FOREIGN KEY(project,name,version) REFERENCES ai_prompts(project,name,version)
);

CREATE TABLE agent_experiment_runs (
  project TEXT NOT NULL, id TEXT NOT NULL,
  dataset TEXT NOT NULL, dataset_version INTEGER NOT NULL,
  execution_version TEXT NOT NULL, criterion TEXT NOT NULL,
  configuration JSONB NOT NULL,
  PRIMARY KEY(project,id),
  FOREIGN KEY(project,dataset,dataset_version) REFERENCES datasets(project,name,version)
);
CREATE TABLE agent_experiment_cases (
  project TEXT NOT NULL, run_id TEXT NOT NULL, case_id TEXT NOT NULL,
  result JSONB NOT NULL,
  PRIMARY KEY(project,run_id,case_id),
  FOREIGN KEY(project,run_id) REFERENCES agent_experiment_runs(project,id) ON DELETE CASCADE
);
CREATE TABLE online_evaluators (
  project TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL, version INTEGER NOT NULL,
  configuration JSONB NOT NULL, created_ns BIGINT NOT NULL,
  PRIMARY KEY(project,name,version)
);
CREATE TABLE online_scores (
  project TEXT NOT NULL, evaluator TEXT NOT NULL, version INTEGER NOT NULL,
  trace_id TEXT NOT NULL, span_id TEXT NOT NULL,
  value DOUBLE PRECISION, outcome TEXT NOT NULL, measured_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY(project,evaluator,version,trace_id,span_id),
  FOREIGN KEY(project,evaluator,version) REFERENCES online_evaluators(project,name,version),
  FOREIGN KEY(project,trace_id,span_id) REFERENCES otlp_spans(project,trace_id,span_id) ON DELETE CASCADE
);
CREATE TABLE review_cases (
  project TEXT NOT NULL, id TEXT NOT NULL, trace_id TEXT NOT NULL, span_id TEXT NOT NULL,
  criterion TEXT NOT NULL, rubric TEXT NOT NULL, assignee TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL, reviewer TEXT, score DOUBLE PRECISION,
  reviewed_at DOUBLE PRECISION,
  PRIMARY KEY(project,id),
  FOREIGN KEY(project,trace_id,span_id) REFERENCES otlp_spans(project,trace_id,span_id) ON DELETE CASCADE
);
CREATE INDEX online_scores_retention ON online_scores(project,measured_at);
CREATE INDEX review_cases_retention ON review_cases(project,created_at);
