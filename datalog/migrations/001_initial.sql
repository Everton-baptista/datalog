CREATE TABLE logs (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project TEXT NOT NULL,
  timestamp DOUBLE PRECISION NOT NULL,
  ingested_at DOUBLE PRECISION NOT NULL,
  service TEXT NOT NULL,
  env TEXT NOT NULL,
  level TEXT NOT NULL CHECK (level IN ('debug','info','warn','error','critical')),
  message TEXT NOT NULL,
  tags JSONB NOT NULL DEFAULT '{}',
  trace_id TEXT,
  raw JSONB NOT NULL
);
CREATE INDEX logs_time ON logs (project, timestamp DESC, id DESC);
CREATE INDEX logs_service_time ON logs (project, service, env, timestamp DESC);
CREATE INDEX logs_trace ON logs (project, trace_id) WHERE trace_id IS NOT NULL;

CREATE TABLE metric_definitions (
  project TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('gauge','count')),
  PRIMARY KEY (project, name),
  UNIQUE (project, name, kind)
);
CREATE TABLE metrics (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  timestamp DOUBLE PRECISION NOT NULL,
  value DOUBLE PRECISION NOT NULL,
  service TEXT NOT NULL,
  env TEXT NOT NULL,
  tags JSONB NOT NULL DEFAULT '{}',
  FOREIGN KEY (project, name, kind) REFERENCES metric_definitions(project, name, kind),
  CHECK (kind <> 'count' OR value >= 0)
);
CREATE INDEX metrics_time ON metrics (project, name, timestamp);
CREATE INDEX metrics_service_time ON metrics (project, name, service, env, timestamp);

CREATE TABLE dashboards (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  title TEXT NOT NULL,
  metric TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX dashboards_project ON dashboards (project, created_at DESC);

CREATE TABLE monitors (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  title TEXT NOT NULL,
  metric TEXT NOT NULL,
  service TEXT NOT NULL,
  env TEXT NOT NULL,
  op TEXT NOT NULL CHECK (op IN ('>','>=','<','<=')),
  threshold DOUBLE PRECISION NOT NULL,
  window_seconds INTEGER NOT NULL CHECK (window_seconds BETWEEN 10 AND 86400),
  state TEXT NOT NULL CHECK (state IN ('OK','ALERT','NO_DATA')),
  value DOUBLE PRECISION,
  evaluated_at DOUBLE PRECISION,
  UNIQUE (id, project)
);
CREATE INDEX monitors_project ON monitors(project);

CREATE TABLE evaluations (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  monitor_id TEXT NOT NULL,
  project TEXT NOT NULL,
  timestamp DOUBLE PRECISION NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('OK','ALERT','NO_DATA')),
  value DOUBLE PRECISION,
  FOREIGN KEY (monitor_id, project) REFERENCES monitors(id, project)
);
CREATE INDEX evaluations_project ON evaluations(project, timestamp DESC);
