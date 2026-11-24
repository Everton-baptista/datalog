CREATE TABLE browser_events (
  project TEXT NOT NULL REFERENCES projects(id),
  session_id TEXT NOT NULL, event_id TEXT NOT NULL,
  kind TEXT NOT NULL, timestamp DOUBLE PRECISION NOT NULL,
  trace_id TEXT, payload JSONB NOT NULL,
  PRIMARY KEY(project,session_id,event_id)
);
CREATE INDEX browser_events_recent ON browser_events(project,timestamp DESC);
CREATE INDEX browser_events_trace ON browser_events(project,trace_id) WHERE trace_id IS NOT NULL;

CREATE TABLE profiles (
  project TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL,
  service TEXT NOT NULL, kind TEXT NOT NULL, timestamp DOUBLE PRECISION NOT NULL,
  trace_id TEXT, span_id TEXT, stacks JSONB NOT NULL,
  PRIMARY KEY(project,id)
);
CREATE INDEX profiles_recent ON profiles(project,timestamp DESC);
CREATE INDEX profiles_trace ON profiles(project,trace_id) WHERE trace_id IS NOT NULL;

CREATE TABLE synthetic_checks (
  project TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL,
  title TEXT NOT NULL, destination TEXT NOT NULL, kind TEXT NOT NULL,
  cadence INTEGER NOT NULL, location TEXT NOT NULL, expected TEXT,
  next_at DOUBLE PRECISION NOT NULL, monitor_id TEXT NOT NULL REFERENCES monitors(id),
  PRIMARY KEY(project,id)
);
CREATE TABLE synthetic_results (
  project TEXT NOT NULL, check_id TEXT NOT NULL, timestamp DOUBLE PRECISION NOT NULL,
  ok BOOLEAN NOT NULL, duration_ms DOUBLE PRECISION NOT NULL,
  trace_id TEXT NOT NULL, span_id TEXT NOT NULL, step TEXT NOT NULL,
  PRIMARY KEY(project,check_id,timestamp),
  FOREIGN KEY(project,check_id) REFERENCES synthetic_checks(project,id)
);
