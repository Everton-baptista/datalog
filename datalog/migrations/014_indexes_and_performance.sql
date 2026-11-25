-- Missing foreign-key cascade indexes and retention/expiry indexes.
CREATE INDEX IF NOT EXISTS online_scores_span_idx ON online_scores(project, trace_id, span_id);
CREATE INDEX IF NOT EXISTS review_cases_span_idx ON review_cases(project, trace_id, span_id);
CREATE INDEX IF NOT EXISTS agent_evaluations_project_ts_idx ON agent_evaluations(project, timestamp);
CREATE INDEX IF NOT EXISTS synthetic_results_project_ts_idx ON synthetic_results(project, timestamp);
CREATE INDEX IF NOT EXISTS oidc_flows_expires_idx ON oidc_flows(expires_at);
CREATE INDEX IF NOT EXISTS project_tokens_expires_idx ON project_tokens(expires_at);
CREATE INDEX IF NOT EXISTS logs_project_env_idx ON logs(project, env);
CREATE INDEX IF NOT EXISTS metrics_project_env_idx ON metrics(project, env);
CREATE INDEX IF NOT EXISTS otlp_spans_project_env_idx ON otlp_spans(project, env);
