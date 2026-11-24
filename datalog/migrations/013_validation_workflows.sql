ALTER TABLE datasets ADD COLUMN splits jsonb NOT NULL DEFAULT '{}';
ALTER TABLE synthetic_checks ADD COLUMN steps jsonb NOT NULL DEFAULT '[]';
ALTER TABLE synthetic_results ADD COLUMN steps jsonb NOT NULL DEFAULT '[]';

CREATE INDEX data_job_attempt_group ON data_job_runs(project, (details->>'attempt_group'))
    WHERE details ? 'attempt_group';
