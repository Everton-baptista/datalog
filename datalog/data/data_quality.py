"""Read-only PostgreSQL data quality scans and agent retrieval evidence."""

from contextlib import contextmanager
from decimal import Decimal
from statistics import median
import json
import math
import os
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from datalog.auth.access import AccessError, audit, integer
from datalog.data.dashboard_frames import frames, mapping
from datalog.data.data_lineage import (
    DataLineage,
    array_field,
    asset,
    dataset,
    describe,
    digest,
    identity,
    label,
    object_field,
)
from datalog.auth.privacy import SECRETS
from datalog.data.reconcile_rows import ReconcileLimit, compare as compare_rows, settings as reconcile_settings
from datalog.data.warehouse_sources import CloudWarehouse, ENGINES
from datalog.data.sql_history import event as sql_event
from datalog.core.validation import Invalid, number


class DataQuality:
    def __init__(self, connect):
        self.connect = connect
        self.lineage = DataLineage(connect)
        self.sources = object_field(json.loads(os.environ.get("DATALOG_DATA_SOURCES", "{}")), "DATALOG_DATA_SOURCES")
        for alias, config in self.sources.items():
            label(alias, "source", 80)
            config = object_field(config, "source")
            engine = config.get("engine", "postgres")
            if engine not in ENGINES:
                raise Invalid("Invalid source engine")
            label(config.get("dsn_env"), "DSN variable", 100)
            if engine != "postgres":
                label(config.get("catalog"), "catalog", 127)
            projects = array_field(config.get("projects"), "projetos", 100)
            if not projects or any(not isinstance(project, str) for project in projects):
                raise Invalid("Provide the allowed projects at the source")
            for name, table in object_field(config.get("tables"), "tabelas").items():
                identity({"namespace": config.get("namespace"), "name": name})
                table = object_field(table, "table")
                for field in ("schema", "table"):
                    label(table.get(field), field, 63)
                if "lineage_columns" in table:
                    columns = object_field(table["lineage_columns"], "lineage columns")
                    if not 1 <= len(columns) <= 200:
                        raise Invalid("Provide up to 200 lineage columns per table")
                    for field, kind in columns.items():
                        label(field, "column", 200)
                        label(kind, "type", 100)
                for field in array_field(table.get("reconcile_columns", []), "reconcilable columns", 50):
                    label(field, "column", 63)
                    if SECRETS.search(field):
                        raise Invalid("Reconciliation column contains a sensitive name")
                for field in array_field(table.get("agent_columns", []), "columns", 20):
                    label(field, "column", 63)
                for field in array_field(table.get("dashboard_columns", []), "panel columns", 20):
                    label(field, "column", 63)
                    if SECRETS.search(field):
                        raise Invalid("Panel column contains a sensitive name")
                for adapter in array_field(table.get("agent_adapters", []), "adaptadores", 20):
                    label(adapter, "adapter", 100)
                if "lookup_column" in table:
                    label(table["lookup_column"], "lookup key", 63)

    def source(self, project, source, name):
        config = self.sources.get(source, {})
        if project not in config.get("projects", []) and "*" not in config.get("projects", []):
            raise AccessError(403, "Data source unavailable in this project")
        table = config.get("tables", {}).get(name)
        if not table:
            raise AccessError(403, "Table unavailable in this source")
        return config, table

    def query_history(self, project, body):
        body = object_field(body, "SQL record")
        source = label(body.get("source"), "source", 80)
        config = self.sources.get(source, {})
        allowed = config.get("projects", [])
        if (project not in allowed and "*" not in allowed) or not config.get("sql_history"):
            raise AccessError(403, "SQL history is not authorized for this source")
        event = sql_event(body, config, source)
        if event is None:
            return {"accepted": 0, "state": "ignored"}
        return self.lineage.receive(project, event, single=True)

    @contextmanager
    def reader(self, config):
        if config.get("engine", "postgres") != "postgres":
            raise AccessError(409, "This operation requires a PostgreSQL source")
        dsn = os.environ.get(config.get("dsn_env", ""))
        if not dsn:
            raise AccessError(503, "Data connection unavailable")
        with psycopg.connect(dsn, connect_timeout=4, row_factory=dict_row) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            db.execute("SET LOCAL statement_timeout = '5s'")
            db.execute("SET LOCAL lock_timeout = '2s'")
            db.execute("SET LOCAL search_path = pg_catalog")
            db.execute("SET LOCAL TIME ZONE 'UTC'")
            yield db

    @staticmethod
    def policy(value):
        value = object_field(value, "policy")
        if value.keys() - {"freshness", "volume", "checks", "reconcile", "reconcile_rows"}:
            raise Invalid("Unknown quality policy")
        result = {}
        if "freshness" in value:
            rule = object_field(value["freshness"], "freshness")
            result["freshness"] = {
                "column": label(rule.get("column"), "column", 63),
                "max_age_seconds": integer(rule.get("max_age_seconds"), "idade", 1, 31536000),
            }
        if "volume" in value:
            rule = object_field(value["volume"], "volume")
            result["volume"] = {k: integer(rule[k], k, 0, 2**53 - 1) for k in ("min_rows", "max_rows") if k in rule}
            v = result["volume"]
            if v.get("min_rows", 0) > v.get("max_rows", 2**53 - 1):
                raise Invalid("min_rows cannot exceed max_rows")
            if "max_change_percent" in rule:
                v["max_change_percent"] = integer(rule["max_change_percent"], "variation", 1, 1000)
        if "checks" in value:
            checks = []
            for rule in array_field(value["checks"], "checks", 12):
                rule = object_field(rule, "rule")
                kind = rule.get("kind")
                if kind not in ("not_null", "unique"):
                    raise Invalid("Available rule types: not_null and unique")
                checks.append(
                    {
                        "kind": kind,
                        "column": label(rule.get("column"), "column", 63),
                        "max_violations": integer(rule.get("max_violations", 0), "violations", 0, 2**53 - 1),
                    }
                )
            result["checks"] = checks
        if "reconcile" in value:
            rule = object_field(value["reconcile"], "reconcile")
            if rule.get("metric", "count") not in ("count", "sum"):
                raise Invalid("Reconciliation supports count and sum")
            metric = rule.get("metric", "count")
            result["reconcile"] = {
                "target": label(rule.get("target"), "destination", 32),
                "metric": metric,
                "tolerance": number(rule.get("tolerance", 0), "tolerance"),
            }
            if result["reconcile"]["tolerance"] < 0:
                raise Invalid("Tolerance cannot be negative")
            if metric == "sum":
                result["reconcile"].update(
                    column=label(rule.get("column"), "source column", 63),
                    target_column=label(rule.get("target_column"), "destination column", 63),
                )
        if "reconcile_rows" in value:
            result["reconcile_rows"] = reconcile_settings(value["reconcile_rows"])
        return result

    def available(self, project):
        items = []
        for name, config in sorted(self.sources.items()):
            projects = config.get("projects", [])
            if project in projects or "*" in projects:
                items.extend(
                    {"source": name, "namespace": config["namespace"], "name": table}
                    for table in sorted(config.get("tables", {}))
                )
        return {"items": items}

    @staticmethod
    def monitor(db, project, actor, ident, title, source, settings):
        monitor_id = digest(project, source, ident)[:32]
        db.execute(
            """INSERT INTO monitors(id,project,title,metric,service,env,op,threshold,window_seconds,state,configuration)
            VALUES(%s,%s,%s,%s,'dados','','>',0,300,'NO_DATA',%s)
            ON CONFLICT(id) DO UPDATE SET configuration=excluded.configuration""",
            (monitor_id, project, title[:150], source, Jsonb({"source": source, "severity": "warning", **settings})),
        )
        audit(db, project, actor, "data.monitor", ident)
        return monitor_id

    def register(self, project, actor, body):
        name = label(body.get("name"), "dataset")
        source = label(body.get("source"), "source", 80)
        config, _ = self.source(project, source, name)
        namespace, name = identity({"namespace": config["namespace"], "name": name})
        rules = self.policy(body.get("policy", {}))
        interval = integer(body.get("interval_seconds", 3600), "interval", 60, 86400)
        with self.connect() as db:
            ident = dataset(db, project, namespace, name)
            reconcile = rules.get("reconcile")
            if reconcile and (
                config.get("engine", "postgres") != "postgres"
                or reconcile["target"] == ident
                or asset(db, project, reconcile["target"])["source"] != source
            ):
                raise Invalid("Choose another asset on the same connection to reconcile")
            rows = rules.get("reconcile_rows")
            if rows:
                target = asset(db, project, rows["target"])
                if target["id"] == ident or not target["source"]:
                    raise Invalid("Choose another registered asset to reconcile by key")
                _, left = self.source(project, source, name)
                _, right = self.source(project, target["source"], target["name"])
                self.reconciliation_fields(left, rows, "source")
                self.reconciliation_fields(right, rows, "target")
            before = asset(db, project, ident)
            if before["source"] not in (None, source):
                raise AccessError(409, "The asset already uses another connection")
            if not db.execute(
                """UPDATE data_assets SET source=%s,policy=%s,interval_seconds=%s,next_scan=0,scan_lease=NULL
                WHERE project=%s AND id=%s AND (source IS NULL OR source=%s)""",
                (source, Jsonb(rules), interval, project, ident, source),
            ).rowcount:
                raise AccessError(409, "The asset already uses another connection")
            monitor = self.monitor(
                db, project, actor, ident, f"Data quality · {name}", "data_quality", {"asset_id": ident}
            )
        return {"id": ident, "monitor_id": monitor}

    def assets(self, project, params):
        with self.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if params.get("id"):
                row = asset(db, project, params["id"])
                history = db.execute(
                    """SELECT id,measured_at,state,result FROM data_observations
                    WHERE project=%s AND asset_id=%s ORDER BY measured_at DESC,id LIMIT 30""",
                    (project, row["id"]),
                ).fetchall()
                use = db.execute(
                    """SELECT a.trace_id,a.span_id,s.name,s.service,(s.end_ns/1e9)::float8 at
                    FROM data_agent_sources a JOIN otlp_spans s USING (project,trace_id,span_id)
                    WHERE a.project=%s AND a.sources @> %s ORDER BY s.end_ns DESC,a.trace_id,a.span_id LIMIT 30""",
                    (project, Jsonb([{"asset": {"id": row["id"]}}])),
                ).fetchall()
                downstream = db.execute(
                    """SELECT o.asset_id,a.name,r.id run_id,r.name job,o.completed_at
                    FROM data_job_runs r JOIN data_job_outputs o ON (r.project,r.id)=(o.project,o.run_id)
                    JOIN data_assets a ON (a.project,a.id)=(o.project,o.asset_id)
                    WHERE r.project=%s AND r.state='COMPLETE' AND r.details->'inputs' @> %s AND o.asset_id<>%s
                      AND NOT EXISTS (SELECT 1 FROM data_job_outputs n WHERE n.project=o.project AND n.asset_id=o.asset_id
                        AND (n.completed_at>o.completed_at OR (n.completed_at=o.completed_at AND n.run_id<o.run_id)))
                    ORDER BY o.completed_at DESC,o.asset_id LIMIT 50""",
                    (project, Jsonb([{"id": row["id"]}]), row["id"]),
                ).fetchall()
                table = self.sources.get(row["source"], {}).get("tables", {}).get(row["name"], {})
                report = db.execute(
                    "SELECT url FROM data_bi_reports WHERE project=%s AND asset_id=%s", (project, row["id"])
                ).fetchone()
                return {
                    "item": {
                        **describe(row),
                        "source": row["source"],
                        "policy": row["policy"],
                        "interval_seconds": row["interval_seconds"],
                        "schema_baseline": row["schema_baseline"],
                        "lookup_column": table.get("lookup_column"),
                        "agent_adapters": table.get("agent_adapters", []),
                        "report_url": report["url"] if report else None,
                    },
                    "observations": history,
                    "lineage": self.lineage.upstream(db, project, row["id"], time.time()),
                    "downstream": downstream,
                    "agent_outputs": use,
                }
            rows = db.execute(
                """SELECT a.id,a.namespace,a.name,a.source,s.state,s.measured_at
                FROM data_assets a LEFT JOIN LATERAL (
                   SELECT state,measured_at FROM data_observations WHERE project=a.project AND asset_id=a.id
                   ORDER BY measured_at DESC,id LIMIT 1
                ) s ON true WHERE a.project=%s ORDER BY a.namespace,a.name LIMIT 200""",
                (project,),
            ).fetchall()
            for row in rows:
                config = self.sources.get(row["source"], {})
                if project in config.get("projects", []) or "*" in config.get("projects", []):
                    row["dashboard_columns"] = (
                        config.get("tables", {}).get(row["name"], {}).get("dashboard_columns", [])
                    )
            return {"items": rows}

    def panel_source(self, project, asset_id, fields):
        fields = mapping(fields)
        with self.connect() as db:
            item = asset(db, project, asset_id)
        config, table = self.source(project, item["source"], item["name"])
        columns = list(dict.fromkeys(fields.values()))
        if not set(columns) <= set(table.get("dashboard_columns", [])):
            raise AccessError(403, "Columns unavailable for panels in this project")
        return config, table, fields, columns

    def panel_frames(self, project, item):
        config, table, fields, columns = self.panel_source(project, item["asset_id"], item["mapping"])
        with self.reader(config) as db:
            schema = self.schema(db, table)
            for key, column in fields.items():
                category = schema.get(column, {}).get("category")
                if category != ("S" if key in ("name", "unit") else "N"):
                    raise Invalid(f"Invalid column type for {key}")
            select = []
            for column in columns:
                identifier = sql.Identifier(column)
                select.append(
                    sql.SQL("left({}::text,512) AS {}").format(identifier, identifier)
                    if schema[column]["category"] == "S"
                    else identifier
                )
            rows = db.execute(
                sql.SQL("SELECT {} FROM {} ORDER BY {}::text COLLATE \"C\" LIMIT 101").format(
                    sql.SQL(",").join(select), self.relation(table), sql.Identifier(fields["name"])
                )
            ).fetchall()
        for row in rows:
            for key, value in row.items():
                if isinstance(value, Decimal):
                    row[key] = value = float(value)
                if type(value) in (int, float) and (not math.isfinite(value) or abs(value) > 2**53 - 1):
                    raise Invalid("Panel value outside the supported numeric range")
        return frames(columns, rows[:100], fields, len(rows) > 100)

    @staticmethod
    def schema(db, table):
        cols = db.execute(
            """SELECT a.attname name,format_type(a.atttypid,a.atttypmod) type, NOT a.attnotnull nullable,
                    t.typcategory category
            FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
              JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
              JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
            WHERE n.nspname=%s AND c.relname=%s AND c.relkind IN ('r','p','v','m','f')
              AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attname""",
            (table["schema"], table["table"]),
        ).fetchall()
        if not cols:
            raise Invalid("Table unavailable or has no columns")
        return {item["name"]: item for item in cols}

    @staticmethod
    def relation(table):
        return sql.Identifier(table["schema"], table["table"])

    def postgres_measurements(self, config, table, policy, peer, project):
        issues = []
        reconciliation = None
        with self.reader(config) as db:
            schema = self.schema(db, table)
            expressions = [sql.SQL("count(*) AS rows")]
            rule = policy.get("freshness")
            if rule:
                field = schema.get(rule["column"])
                if field and field["type"].startswith(("timestamp", "date")):
                    expressions.append(
                        sql.SQL("extract(epoch FROM max({})::timestamptz)::float8 AS latest").format(
                            sql.Identifier(rule["column"])
                        )
                    )
                else:
                    issues.append({"kind": "freshness_column", "column": rule["column"]})
            for index, check in enumerate(policy.get("checks", [])):
                field = check["column"]
                if field not in schema:
                    issues.append({"kind": "check_column", "column": field})
                    continue
                col = sql.Identifier(field)
                agg = (
                    sql.SQL("count(*)-count({})")
                    if check["kind"] == "not_null"
                    else sql.SQL("count({0})-count(DISTINCT {0})")
                ).format(col)
                expressions.append(sql.SQL("{} AS {}").format(agg, sql.Identifier(f"check_{index}")))
            values = db.execute(
                sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(expressions), self.relation(table))
            ).fetchone()
            rule = policy.get("reconcile")
            if rule:
                if not peer:
                    raise Invalid("Reconciliation target unavailable")
                _, other = self.source(project, peer["source"], peer["name"])
                if rule["metric"] == "sum":
                    target_schema = self.schema(db, other)
                    left = self.total(db, table, schema, rule["column"])
                    right = self.total(db, other, target_schema, rule["target_column"])
                else:
                    left, right = values["rows"], self.total(db, other)
                delta = abs(left - right)
                reconciliation = {**rule, "source_value": left, "target_value": right, "difference": delta}
                if delta > rule["tolerance"]:
                    issues.append({"kind": "reconcile", "target": peer["id"], "difference": delta})
        return schema, values, issues, reconciliation

    @staticmethod
    def reconciliation_fields(table, rule, side):
        fields = [rule["key" if side == "source" else "target_key"], *[pair[side] for pair in rule["columns"]]]
        if not set(fields) <= set(table.get("reconcile_columns", [])):
            raise AccessError(403, "Columns not authorized for reconciliation")
        return fields

    def comparison_snapshot(self, item, rule, side):
        config, table = self.source(item["project"], item["source"], item["name"])
        fields = self.reconciliation_fields(table, rule, side)
        maximum = rule["limit"] + 1
        if config.get("engine", "postgres") != "postgres":
            return CloudWarehouse(config).rows(table, fields, maximum)
        columns = list(dict.fromkeys(fields))
        with self.reader(config) as db:
            rows = db.execute(
                sql.SQL("SELECT {} FROM {} LIMIT %s").format(
                    sql.SQL(",").join(map(sql.Identifier, columns)), self.relation(table)
                ),
                (maximum,),
            ).fetchall()
        return [tuple(row[field] for field in fields) for row in rows]

    def inspect(self, item, peer, now, baseline, row_peer=None):
        config, table = self.source(item["project"], item["source"], item["name"])
        policy = item["policy"]
        reconciliation, row_reconciliation, checks = None, None, []
        if config.get("engine", "postgres") == "postgres":
            schema, values, issues, reconciliation = self.postgres_measurements(
                config, table, policy, peer, item["project"]
            )
        else:
            schema, values, issues = CloudWarehouse(config).measurements(table, policy)
        rule = policy.get("freshness")
        if rule and "latest" in values:
            latest = values["latest"]
            if latest is not None and not math.isfinite(latest):
                latest = values["latest"] = None
            age = max(0, now - latest) if latest is not None else None
            if age is None or age > rule["max_age_seconds"]:
                issues.append({"kind": "freshness", "age_seconds": age, "max_age_seconds": rule["max_age_seconds"]})
        for index, check in enumerate(policy.get("checks", [])):
            count = values.get(f"check_{index}")
            if count is not None:
                checks.append({**check, "violations": count})
                if count > check["max_violations"]:
                    issues.append({"kind": check["kind"], "column": check["column"], "violations": count})
        if row_peer:
            rule = policy["reconcile_rows"]
            try:
                row_reconciliation = compare_rows(
                    self.comparison_snapshot(item, rule, "source"),
                    self.comparison_snapshot(row_peer, rule, "target"),
                    rule,
                )
            except ReconcileLimit:
                issues.append({"kind": "reconcile_limit", "target": row_peer["id"]})
            else:
                if not row_reconciliation["ok"]:
                    issues.append(
                        {
                            "kind": "reconcile_rows",
                            "target": row_peer["id"],
                            "mismatches": row_reconciliation["mismatches"],
                        }
                    )
        count = values["rows"]
        volume = policy.get("volume", {})
        if not volume.get("min_rows", 0) <= count <= volume.get("max_rows", 2**53 - 1):
            issues.append({"kind": "volume", "rows": count})
        change = volume.get("max_change_percent")
        if change and baseline is not None and abs(count - baseline) * 100 > baseline * change:
            issues.append({"kind": "volume_change", "rows": count, "baseline": baseline})
        previous = item["schema_baseline"]
        clean_schema = {name: {k: col[k] for k in ("type", "nullable")} for name, col in schema.items()}
        if previous is not None and previous != clean_schema:
            issues.append(
                {
                    "kind": "schema_drift",
                    "added": sorted(clean_schema.keys() - previous.keys()),
                    "removed": sorted(previous.keys() - clean_schema.keys()),
                    "changed": sorted(
                        k for k in previous.keys() & clean_schema.keys() if previous[k] != clean_schema[k]
                    ),
                }
            )
        return {
            "rows": count,
            "latest": values.get("latest"),
            "schema": clean_schema,
            "volume_baseline": baseline,
            "checks": checks,
            "reconcile": reconciliation,
            "reconcile_rows": row_reconciliation,
            "issues": issues,
        }

    def total(self, db, table, schema=None, column=None):
        if column is None:
            expr = sql.SQL("count(*)")
        else:
            if schema.get(column, {}).get("category") != "N":
                raise Invalid("Sum reconciliation requires a numeric column")
            expr = sql.SQL("coalesce(sum({})::float8,0)").format(sql.Identifier(column))
        return number(
            db.execute(sql.SQL("SELECT {} AS total FROM {}").format(expr, self.relation(table))).fetchone()["total"],
            "reconciliation total",
        )

    def scan(self, project=None, ident=None):
        now = time.time()
        lease = uuid.uuid4().hex
        with self.connect() as db:
            if ident:
                asset(db, project, ident)
            item = db.execute(
                """SELECT * FROM data_assets WHERE source IS NOT NULL
                AND (%s::text IS NULL OR project=%s) AND (%s::text IS NULL OR id=%s)
                AND (next_scan<=%s OR (%s::text IS NOT NULL AND scan_lease IS NULL))
                ORDER BY next_scan,project,id LIMIT 1 FOR UPDATE SKIP LOCKED""",
                (project, project, ident, ident, now, ident),
            ).fetchone()
            if not item:
                return None
            key = (item["project"], item["id"])
            db.execute(
                "UPDATE data_assets SET next_scan=%s,scan_lease=%s WHERE project=%s AND id=%s", (now + 120, lease, *key)
            )
            rules = item["policy"]
            peer = asset(db, item["project"], rules["reconcile"]["target"]) if "reconcile" in rules else None
            row_peer = (
                asset(db, item["project"], rules["reconcile_rows"]["target"]) if "reconcile_rows" in rules else None
            )
            prior = db.execute(
                """SELECT (result->>'rows')::bigint rows FROM data_observations
                WHERE project=%s AND asset_id=%s AND state='ok' ORDER BY measured_at DESC LIMIT 12""",
                key,
            ).fetchall()
        baseline = median(row["rows"] for row in prior) if len(prior) >= 3 else None
        try:
            result = self.inspect(item, peer, now, baseline, row_peer)
            state = "alert" if result["issues"] else "ok"
        except (psycopg.Error, AccessError, Invalid):
            result, state = {"issues": [{"kind": "scan_unavailable"}]}, "error"
        observation = uuid.uuid4().hex
        with self.connect() as db:
            if not db.execute(
                """UPDATE data_assets SET next_scan=%s,scan_lease=NULL,
                schema_baseline=coalesce(schema_baseline,%s)
                WHERE project=%s AND id=%s AND scan_lease=%s""",
                (now + item["interval_seconds"], Jsonb(result["schema"]) if "schema" in result else None, *key, lease),
            ).rowcount:
                return None
            db.execute(
                "INSERT INTO data_observations VALUES(%s,%s,%s,%s,%s,%s)",
                (*key, observation, now, state, Jsonb(result)),
            )
        return {"project": key[0], "asset_id": key[1], "id": observation, "state": state, "result": result}

    def accept_schema(self, project, actor, body):
        with self.connect() as db:
            row = asset(db, project, body.get("id"))
            expected = label(body.get("observation_id"), "observation", 32)
            db.execute("SELECT id FROM data_assets WHERE project=%s AND id=%s FOR UPDATE", (project, row["id"]))
            latest = db.execute(
                """SELECT id,result->'schema' schema FROM data_observations
                WHERE project=%s AND asset_id=%s ORDER BY measured_at DESC,id LIMIT 1""",
                (project, row["id"]),
            ).fetchone()
            if not latest or latest["id"] != expected or latest["schema"] is None:
                raise AccessError(409, "The observation changed or has no schema. Refresh the page.")
            db.execute(
                "UPDATE data_assets SET schema_baseline=%s,next_scan=0,scan_lease=NULL WHERE project=%s AND id=%s",
                (Jsonb(latest["schema"]), project, row["id"]),
            )
            audit(db, project, actor, "data.schema.accept", row["id"])
        return {"id": row["id"]}

    def job_monitor(self, project, actor, body):
        run = self.lineage.runs(project, {"id": label(body.get("id"), "runId", 100)})["item"]
        duration = integer(body.get("max_duration_seconds", 3600), "duration", 1, 86400)
        settings = {"namespace": run["namespace"], "job": run["name"], "max_duration_seconds": duration}
        with self.connect() as db:
            ident = self.monitor(
                db,
                project,
                actor,
                digest(run["namespace"], run["name"]),
                f"Data job · {run['name']}",
                "data_job",
                settings,
            )
        return {"id": ident}

    @staticmethod
    def evaluate_monitor(db, monitor, now):
        config = monitor["configuration"]
        project = monitor["project"]
        if config["source"] == "data_quality":
            row = db.execute(
                """SELECT a.id asset_id,a.interval_seconds,o.id,o.state,o.measured_at,o.result
                FROM data_assets a JOIN LATERAL (
                    SELECT id,state,measured_at,result FROM data_observations
                    WHERE project=a.project AND asset_id=a.id AND measured_at<=%s
                    ORDER BY measured_at DESC,id LIMIT 1
                ) o ON true WHERE a.project=%s AND a.id=%s""",
                (now, project, config["asset_id"]),
            ).fetchone()
            if not row or now - row["measured_at"] > 2 * row["interval_seconds"] + 120:
                return None, {"asset_id": config["asset_id"]}
            return int(row["state"] != "ok"), {
                "asset_id": row["asset_id"],
                "observation_id": row["id"],
                "issues": [issue["kind"] for issue in row["result"]["issues"]],
            }
        run = db.execute(
            """SELECT id,state,details,updated_at FROM data_job_runs
            WHERE project=%s AND namespace=%s AND name=%s AND updated_at<=%s
            ORDER BY updated_at DESC,id LIMIT 1""",
            (project, config["namespace"], config["job"], now),
        ).fetchone()
        if not run:
            return None, {}
        group = run["details"].get("attempt_group")
        attempts = DataLineage.attempts(db, project, group, now) if group else []
        if attempts:
            run = attempts[0]
        details = run["details"]
        duration = (details["ended_at"] or now) - details["started_at"] if details["started_at"] is not None else None
        previous_failed = any(a["state"] in ("FAIL", "ABORT") for a in attempts[1:])
        failed = (
            run["state"] in ("FAIL", "ABORT")
            or (run["state"] != "COMPLETE" and previous_failed)
            or (duration is not None and duration > config["max_duration_seconds"])
        )
        return int(failed), {
            "run_id": run["id"],
            "run_state": run["state"],
            "duration_seconds": duration,
            "attempt": details.get("attempt"),
            "recovered": run["state"] == "COMPLETE" and previous_failed,
        }

    def retrieve(self, project, requested, adapters):
        loaded, evidence = [], []
        for entry in array_field(requested, "retrieval", 5):
            entry = object_field(entry, "query")
            key = entry.get("key")
            if type(key) is str:
                key = label(key, "key", 256)
            elif type(key) is not int or not -(2**63) <= key < 2**63:
                raise Invalid("The lookup key must be a string or integer")
            with self.connect() as db:
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                item = asset(db, project, entry.get("asset_id"))
                config, table = self.source(project, item["source"], item["name"])
                columns = table.get("agent_columns", [])
                permitted = table.get("agent_adapters", [])
                if (
                    not columns
                    or len(columns) > 20
                    or not table.get("lookup_column")
                    or any(SECRETS.search(field) for field in columns)
                    or not set(adapters).issubset(permitted)
                ):
                    raise AccessError(403, "Agent lookup unavailable for this asset or adapter")
                try:
                    with self.reader(config) as reader:
                        at = time.time()
                        query = sql.SQL(
                            "SELECT CASE WHEN octet_length(row_to_json(t)::text)<=16384 THEN row_to_json(t) END data "
                            "FROM (SELECT {} FROM {} WHERE {}=%s LIMIT 2) t"
                        ).format(
                            sql.SQL(",").join(map(sql.Identifier, columns)),
                            self.relation(table),
                            sql.Identifier(table["lookup_column"]),
                        )
                        found = reader.execute(query, (key,)).fetchall()
                except psycopg.Error:
                    raise AccessError(503, "Data source query failed") from None
                if not found:
                    raise AccessError(404, "Record not found in the data source")
                if len(found) != 1 or found[0]["data"] is None:
                    raise AccessError(409, "The lookup requires a single result up to 16 KiB")
                if any(isinstance(value, (dict, list)) for value in found[0]["data"].values()):
                    raise AccessError(409, "The agent context requires scalar columns")
                observed = db.execute(
                    """SELECT id,state,measured_at FROM data_observations
                    WHERE project=%s AND asset_id=%s AND measured_at<=%s
                    ORDER BY measured_at DESC,id LIMIT 1""",
                    (project, item["id"], at),
                ).fetchone()
                lineage = self.lineage.upstream(db, project, item["id"], at, columns)
                loaded.append({"asset": describe(item), "row": found[0]["data"]})
                evidence.append(
                    {
                        "asset": describe(item),
                        "columns": columns,
                        "read_at": at,
                        "row_count": 1,
                        "observation": observed,
                        "lineage": lineage,
                    }
                )
        return loaded, evidence
