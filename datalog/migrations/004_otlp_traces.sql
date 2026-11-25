CREATE TABLE otlp_spans (
  project TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  span_id TEXT NOT NULL,
  parent_span_id TEXT,
  name TEXT NOT NULL,
  service TEXT NOT NULL,
  env TEXT NOT NULL,
  kind INTEGER NOT NULL,
  status_code INTEGER NOT NULL,
  start_ns BIGINT NOT NULL CHECK (start_ns > 0),
  end_ns BIGINT NOT NULL CHECK (end_ns >= start_ns),
  resource JSONB NOT NULL,
  scope JSONB NOT NULL,
  payload JSONB NOT NULL,
  PRIMARY KEY (project, trace_id, span_id)
);

CREATE INDEX otlp_spans_recent ON otlp_spans (project, start_ns DESC);
CREATE INDEX otlp_spans_service ON otlp_spans (project, service, start_ns DESC);
CREATE INDEX otlp_spans_trace_timeline ON otlp_spans (project, trace_id, start_ns, span_id);
CREATE INDEX logs_otlp_correlation ON logs (project, lower(trace_id), timestamp DESC, id DESC) WHERE trace_id IS NOT NULL;
