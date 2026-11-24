CREATE TABLE data_assets (
    project text NOT NULL REFERENCES projects(id),
    id text NOT NULL,
    namespace text NOT NULL,
    name text NOT NULL,
    source text,
    policy jsonb NOT NULL DEFAULT '{}',
    schema_baseline jsonb,
    interval_seconds integer,
    next_scan double precision,
    scan_lease text,
    PRIMARY KEY (project,id),
    UNIQUE (project,namespace,name)
);
CREATE INDEX data_assets_due ON data_assets(next_scan) WHERE next_scan IS NOT NULL;

CREATE TABLE data_observations (
    project text NOT NULL,
    asset_id text NOT NULL,
    id text NOT NULL,
    measured_at double precision NOT NULL,
    state text NOT NULL CHECK (state IN ('ok','alert','error')),
    result jsonb NOT NULL,
    PRIMARY KEY (project,asset_id,id),
    FOREIGN KEY (project,asset_id) REFERENCES data_assets(project,id) ON DELETE CASCADE
);
CREATE INDEX data_observations_recent ON data_observations(project,asset_id,measured_at DESC);
CREATE INDEX data_observations_retention ON data_observations(project,measured_at);

CREATE TABLE data_job_runs (
    project text NOT NULL REFERENCES projects(id),
    id text NOT NULL,
    namespace text NOT NULL,
    name text NOT NULL,
    state text NOT NULL,
    updated_at double precision NOT NULL,
    details jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (project,id)
);
CREATE INDEX data_job_runs_recent ON data_job_runs(project,updated_at DESC);
CREATE INDEX data_job_runs_by_job ON data_job_runs(project,namespace,name,updated_at DESC);
CREATE INDEX data_job_inputs ON data_job_runs USING gin ((details->'inputs') jsonb_path_ops)
    WHERE state='COMPLETE';

CREATE TABLE data_job_events (
    project text NOT NULL,
    run_id text NOT NULL,
    digest text NOT NULL,
    event_at double precision NOT NULL,
    state text NOT NULL,
    details jsonb NOT NULL,
    PRIMARY KEY (project,run_id,digest),
    FOREIGN KEY (project,run_id) REFERENCES data_job_runs(project,id) ON DELETE CASCADE
);

CREATE TABLE data_job_outputs (
    project text NOT NULL,
    run_id text NOT NULL,
    asset_id text NOT NULL,
    completed_at double precision NOT NULL,
    PRIMARY KEY (project,run_id,asset_id),
    FOREIGN KEY (project,run_id) REFERENCES data_job_runs(project,id) ON DELETE CASCADE,
    FOREIGN KEY (project,asset_id) REFERENCES data_assets(project,id)
);
CREATE INDEX data_job_outputs_history ON data_job_outputs(project,asset_id,completed_at DESC);

CREATE TABLE data_agent_sources (
    project text NOT NULL,
    trace_id text NOT NULL,
    span_id text NOT NULL,
    sources jsonb NOT NULL,
    PRIMARY KEY (project,trace_id,span_id),
    FOREIGN KEY (project,trace_id,span_id) REFERENCES otlp_spans(project,trace_id,span_id) ON DELETE CASCADE
);
CREATE INDEX data_agent_sources_assets ON data_agent_sources USING gin (sources jsonb_path_ops);
