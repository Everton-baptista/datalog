ALTER TABLE profiles ADD COLUMN runtime TEXT;
ALTER TABLE profiles ADD COLUMN started_at DOUBLE PRECISION;
CREATE INDEX profiles_service_window ON profiles(project,service,timestamp);
