CREATE TABLE trace_media (
    project text NOT NULL,
    trace_id text NOT NULL,
    span_id text NOT NULL,
    digest text NOT NULL,
    content_type text NOT NULL,
    content bytea NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 1048576),
    PRIMARY KEY (project,trace_id,span_id,digest),
    FOREIGN KEY (project,trace_id,span_id) REFERENCES otlp_spans(project,trace_id,span_id) ON DELETE CASCADE
);

CREATE TABLE data_products (
    project text NOT NULL REFERENCES projects(id),
    id text NOT NULL,
    name text NOT NULL,
    PRIMARY KEY (project,id),
    UNIQUE (project,name)
);

CREATE TABLE data_product_members (
    project text NOT NULL,
    product_id text NOT NULL,
    asset_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('table','consumer')),
    PRIMARY KEY (project,product_id,asset_id),
    FOREIGN KEY (project,product_id) REFERENCES data_products(project,id) ON DELETE CASCADE,
    FOREIGN KEY (project,asset_id) REFERENCES data_assets(project,id) ON DELETE CASCADE
);

CREATE TABLE data_bi_reports (
    project text NOT NULL,
    asset_id text NOT NULL,
    url text NOT NULL,
    PRIMARY KEY (project,asset_id),
    FOREIGN KEY (project,asset_id) REFERENCES data_assets(project,id) ON DELETE CASCADE
);

CREATE FUNCTION datalog_log_pattern(message text) RETURNS text LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN regexp_replace(
    regexp_replace(
        regexp_replace(
            regexp_replace(left(message,512), '\m[[:xdigit:]]{8}(-[[:xdigit:]]{4}){3}-[[:xdigit:]]{12}\M', '<uuid>', 'gi'),
            '\m([0-9]{1,3}\.){3}[0-9]{1,3}\M', '<ip>', 'g'),
        '\m(0x[[:xdigit:]]+|[[:xdigit:]]{16,})\M', '<hex>', 'gi'),
    '\m[0-9]+(\.[0-9]+)?\M', '<n>', 'g');
