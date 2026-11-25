ALTER TABLE online_scores ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE online_scores ADD COLUMN next_at DOUBLE PRECISION;
ALTER TABLE online_scores ADD COLUMN cost_usd DOUBLE PRECISION;
ALTER TABLE online_scores ADD COLUMN failure TEXT;
ALTER TABLE online_scores ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX online_scores_due ON online_scores(next_at) WHERE outcome='pending';
