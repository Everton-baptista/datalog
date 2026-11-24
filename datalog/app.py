#!/usr/bin/env python3
"""Datalog self-hosted observability platform. PostgreSQL storage with Psycopg 3."""

import argparse
import operator
import os
import signal as os_signal
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit


from datalog.validation import Invalid, text, number, timestamp, tags, batch, attribute_filters
from datalog.ai import AIStore
from datalog.demo import DemoService
from datalog.access import AccessError, AccessStore, audit, integer
from datalog.agent_runs import AgentStore
from datalog.agent_workflows import AgentWorkflows
from datalog.data_quality import DataQuality
from datalog.data_products import DataProducts
from datalog.dashboard_frames import mapping
from datalog.investigation import Investigation
from datalog.platform_signals import Platform
from datalog.sso import OIDC
from datalog.operational import OperationalMetrics
from datalog.trace_media import TraceMedia
from datalog.otlp import TraceStore, checked_id
from datalog.server import DatalogHTTPServer, make_handler  # noqa: F401 (public API)
from datalog.retention import sweep
from datalog.signal_store import SignalStore, lock_logs
from datalog.alerts import Alerts, evaluate_online, evaluate_otlp, options as alert_options, span_evidence


# Monitors owned by synthetic checks or data assets change through their source.
ROOT = Path(__file__).resolve().parent
LEVELS = {"debug", "info", "warn", "error", "critical"}
OTLP_ERRORS = "@otlp.span.errors"
OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}

MANAGED_MONITORS = {"synthetic", "data_quality", "data_job"}


class Store:
    def __init__(self, database_url, pool_size=10):
        self.evaluation_lock = threading.Lock()
        self.pool = ConnectionPool(
            database_url, min_size=1, max_size=pool_size, kwargs={"row_factory": dict_row}, timeout=10, open=True
        )
        self.pool.wait(timeout=10)
        self.operational = OperationalMetrics()
        self.ai = AIStore(self.connect)
        self.tracing = TraceStore(self.connect, self.operational)
        self.media = TraceMedia(self.connect)
        self.signals = SignalStore(self.connect, self.operational)
        self.access = AccessStore(self.connect)
        self.agents = AgentStore(self.connect)
        self.data = DataQuality(self.connect)
        self.data_products = DataProducts(self.connect)
        self.workflows = AgentWorkflows(self)
        self.investigation = Investigation(self.connect)
        self.platform = Platform(self)
        self.alerts = Alerts(self.connect)
        self.demo_service = DemoService(self)
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(8103144310)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            for migration in sorted((ROOT / "migrations").glob("*.sql")):
                if not db.execute("SELECT 1 FROM schema_migrations WHERE version=%s", (migration.name,)).fetchone():
                    db.execute(migration.read_text())
                    db.execute("INSERT INTO schema_migrations(version) VALUES(%s)", (migration.name,))

    @contextmanager
    def connect(self):
        with self.pool.connection() as db:
            yield db

    def close(self):
        self.pool.close()

    def ingest_logs(self, p, body):
        rows = []
        for x in batch(body):
            level = text(x.get("level"), "level", "info", 20).lower()
            if level not in LEVELS:
                raise Invalid("level: debug, info, warn, error or critical")
            trace = x.get("trace_id")
            if trace is not None:
                trace = text(trace, "trace_id", limit=128)
            span = x.get("span_id")
            if span is not None:
                span = checked_id(span, 16)
                trace = checked_id(trace, 32)
            rows.append(
                (
                    p,
                    timestamp(x.get("timestamp")),
                    time.time(),
                    text(x.get("service"), "service", "application", 100),
                    text(x.get("env"), "env", "dev", 80),
                    level,
                    text(x.get("message"), "message", limit=64000),
                    Jsonb(tags(x.get("tags"))),
                    trace,
                    span,
                    Jsonb(x),
                )
            )
        with self.connect() as db:
            lock_logs(db, p)
            db.cursor().executemany(
                "INSERT INTO logs(project,timestamp,ingested_at,service,env,level,message,tags,trace_id,span_id,raw) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )
        return {"accepted": len(rows)}

    def ingest_metrics(self, p, body):
        rows = []
        for x in batch(body):
            kind = text(x.get("kind"), "kind", "gauge", 20)
            if kind not in {"gauge", "count"}:
                raise Invalid("kind: gauge or count (delta per sample)")
            value = number(x.get("value"), "value")
            if kind == "count" and value < 0:
                raise Invalid("count must be a non-negative delta")
            rows.append(
                (
                    p,
                    text(x.get("name"), "name", limit=200),
                    kind,
                    timestamp(x.get("timestamp")),
                    value,
                    text(x.get("service"), "service", "application", 100),
                    text(x.get("env"), "env", "dev", 80),
                    Jsonb(tags(x.get("tags"))),
                )
            )
        with self.connect() as db:
            kinds = {}
            for row in rows:
                name, kind = row[1:3]
                if name in kinds and kinds[name] != kind:
                    raise Invalid(f"{name}: metric type cannot change")
                kinds[name] = kind
            for name, kind in sorted(kinds.items()):
                db.execute(
                    "INSERT INTO metric_definitions(project,name,kind) VALUES(%s,%s,%s) ON CONFLICT(project,name) DO NOTHING",
                    (p, name, kind),
                )
                existing = db.execute(
                    "SELECT kind FROM metric_definitions WHERE project=%s AND name=%s FOR UPDATE", (p, name)
                ).fetchone()
                if existing["kind"] != kind:
                    raise Invalid(f"{name}: metric type cannot change")
            db.cursor().executemany(
                "INSERT INTO metrics(project,name,kind,timestamp,value,service,env,tags) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )
        self.evaluate(p)
        return {"accepted": len(rows)}

    @staticmethod
    def scope(p, params, table=""):
        prefix = f"{table}." if table else ""
        end = timestamp(float(params.get("to", time.time())))
        start = timestamp(float(params.get("from", end - 3600)))
        if start >= end or end - start > 31 * 86400:
            raise Invalid("range: from 1 second to 31 days")
        clauses = [f"{prefix}project=%s", f"{prefix}timestamp>=%s", f"{prefix}timestamp<=%s"]
        values = [p, start, end]
        for key in ("service", "env"):
            if params.get(key):
                clauses.append(f"{prefix}{key}=%s")
                values.append(params[key])
        return " AND ".join(clauses), values, start, end

    @staticmethod
    def log_filter(where, values, params):
        if params.get("level"):
            if params["level"] not in LEVELS:
                raise Invalid("invalid level")
            where += " AND level=%s"
            values.append(params["level"])
        if params.get("q"):
            where += " AND strpos(lower(message),lower(%s))>0"
            values.append(params["q"])
        if params.get("trace_id"):
            where += " AND lower(trace_id)=%s"
            values.append(params["trace_id"].lower())
        if params.get("span_id"):
            where += " AND span_id=%s"
            values.append(checked_id(params["span_id"], 16))
        attrs = attribute_filters(params.get("attrs"))
        if attrs:
            where += " AND tags @> %s"
            values.append(Jsonb(attrs))
        return where, values

    def log_patterns(self, p, params):
        where, values, _, _ = self.scope(p, params)
        where, values = self.log_filter(where, values, params)
        with self.connect() as db:
            db.execute("SET LOCAL statement_timeout = '5s'")
            result = db.execute(
                f"""WITH recent AS MATERIALIZED (
                SELECT message,timestamp,id FROM logs WHERE {where} ORDER BY timestamp DESC,id DESC LIMIT 10001
            ), selected AS MATERIALIZED (SELECT message,timestamp FROM recent ORDER BY timestamp DESC,id DESC LIMIT 10000),
            grouped AS (
                SELECT datalog_log_pattern(message) pattern,count(*) count,max(timestamp) last_seen
                FROM selected GROUP BY 1 ORDER BY count DESC,pattern LIMIT 50
            )
            SELECT (SELECT count(*) FROM recent) sampled,
                coalesce((SELECT jsonb_agg(to_jsonb(grouped) ORDER BY count DESC,pattern) FROM grouped),'[]') items""",
                values,
            ).fetchone()
        return {**result, "truncated": result["sampled"] > 10000, "sampled": min(10000, result["sampled"])}

    def logs(self, p, params):
        where, values, start, end = self.scope(p, params)
        where, values = self.log_filter(where, values, params)
        pattern = params.get("pattern")
        if pattern is not None:
            if len(pattern) > 2048:
                raise Invalid("Pattern exceeds the limit")
            where += " AND datalog_log_pattern(message)=%s"
            values.append(pattern)
        limit = max(1, min(200, int(params.get("limit", 80))))
        offset = max(0, int(params.get("offset", 0)))
        with self.connect() as db:
            if pattern is not None:
                db.execute("SET LOCAL statement_timeout = '5s'")
            total = db.execute(f"SELECT count(*) AS total FROM logs WHERE {where}", values).fetchone()["total"]
            rows = db.execute(
                f"SELECT id,project,timestamp,ingested_at,service,env,level,message,tags,trace_id,span_id,raw FROM logs WHERE {where} ORDER BY timestamp DESC,id DESC LIMIT %s OFFSET %s",
                values + [limit, offset],
            ).fetchall()
            histogram = self.histogram(db, where, values, start, end)
            facets = (
                db.execute(
                    f"""
                SELECT field.key,field.value,count(*) count
                FROM (SELECT tags FROM logs WHERE {where} AND tags IS NOT NULL
                      ORDER BY timestamp DESC,id DESC LIMIT 2000) selected
                CROSS JOIN LATERAL jsonb_each(selected.tags) field
                GROUP BY field.key,field.value ORDER BY count DESC,field.key,field.value LIMIT 50
            """,
                    values,
                ).fetchall()
                if params.get("facets") == "1"
                else []
            )
        items = [dict(r) for r in rows]
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "histogram": histogram,
            "facets": facets,
        }

    def tail(self, p, params):
        with self.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            position = db.execute(
                """SELECT log_tail_watermark watermark,
                greatest(log_tail_watermark,coalesce((SELECT max(id) FROM logs WHERE project=%s),0)) AS latest
                FROM projects WHERE id=%s""",
                (p, p),
            ).fetchone()
            latest = position["latest"]
            if "after" not in params:
                return {"items": [], "cursor": str(latest), "has_more": False}
            cursor = int(params["after"])
            if not 0 <= cursor <= 2**63 - 1:
                raise Invalid("Invalid cursor")
            expired = position["watermark"] > cursor
            where, values = self.log_filter("project=%s AND id>%s", [p, cursor], params)
            for key in ("service", "env"):
                if params.get(key):
                    where += f" AND {key}=%s"
                    values.append(params[key])
            rows = db.execute(
                f"""SELECT id,timestamp,ingested_at,service,env,level,message,tags,trace_id,span_id
                FROM logs WHERE {where} ORDER BY id LIMIT 201""",
                values,
            ).fetchall()
        more = len(rows) > 200
        return {
            "items": rows[:200],
            "cursor": str(rows[199]["id"] if more else max(latest, cursor)),
            "has_more": more,
            "retention_gap": expired,
            "ingestion_lag_seconds": max(
                (max(0, row["ingested_at"] - row["timestamp"]) for row in rows[:200]), default=None
            ),
        }

    @staticmethod
    def histogram(db, where, values, start, end):
        width = (end - start) / 40
        rows = db.execute(
            f"""SELECT least(39, greatest(0, floor((timestamp-%s)/%s)::int)) bucket,
            count(*) total,
            sum((level IN ('error','critical'))::int) errors,
            sum((level='warn')::int) warns,
            sum((level='info')::int) infos,
            sum((level='debug')::int) debugs
            FROM logs WHERE {where} GROUP BY bucket""",
            [start, width] + values,
        ).fetchall()
        counts = {r["bucket"]: r for r in rows}
        return [
            {
                "timestamp": start + i * width,
                "total": counts[i]["total"] if i in counts else 0,
                "errors": counts[i]["errors"] if i in counts else 0,
                "warns": counts[i]["warns"] if i in counts else 0,
                "infos": counts[i]["infos"] if i in counts else 0,
                "debugs": counts[i]["debugs"] if i in counts else 0,
            }
            for i in range(40)
        ]

    def overview(self, p, params):
        where, values, start, end = self.scope(p, params)
        span_service = params.get("service", "")
        span_env = params.get("env", "")
        window = end - start
        start_ns, end_ns = int(start * 1e9), int(end * 1e9)
        prev_start_ns = max(0, int((start - window) * 1e9))
        width = max(1.0, window / 40)
        with self.connect() as db:
            summary = dict(
                db.execute(
                    f"SELECT count(*) logs,coalesce(sum((level IN ('error','critical'))::int),0) errors,coalesce(sum((level='warn')::int),0) warns,count(DISTINCT service) services FROM logs WHERE {where}",
                    values,
                ).fetchone()
            )
            services = [
                dict(r)
                for r in db.execute(
                    f"SELECT service,env,count(*) logs,sum((level IN ('error','critical'))::int) errors,max(timestamp) last_seen FROM logs WHERE {where} GROUP BY service,env ORDER BY logs DESC LIMIT 100",
                    values,
                )
            ]
            histogram = self.histogram(db, where, values, start, end)
            summary["alerts"] = db.execute(
                "SELECT count(*) AS total FROM monitors WHERE project=%s AND state='ALERT'", (p,)
            ).fetchone()["total"]
            # Same-length window immediately before the selected one, for period-over-period deltas.
            to = float(params.get("to", time.time()))
            since = float(params.get("from", to - 3600))
            prev_where, prev_values, _, _ = self.scope(p, {**params, "from": since - (to - since), "to": since})
            previous = dict(
                db.execute(
                    f"SELECT count(*) logs,coalesce(sum((level IN ('error','critical'))::int),0) errors,count(DISTINCT service) services FROM logs WHERE {prev_where}",
                    prev_values,
                ).fetchone()
            )
            monitor_states = {
                r["state"]: r["total"]
                for r in db.execute(
                    "SELECT state,count(*) AS total FROM monitors WHERE project=%s GROUP BY state", (p,)
                )
            }
            otlp = dict(
                db.execute(
                    """
                SELECT count(*) spans,
                       count(*) FILTER (WHERE status_code=2) errors,
                       count(DISTINCT service) services,
                       percentile_cont(.50) WITHIN GROUP (ORDER BY (end_ns-start_ns)/1e6) p50_ms,
                       percentile_cont(.95) WITHIN GROUP (ORDER BY (end_ns-start_ns)/1e6) p95_ms,
                       percentile_cont(.99) WITHIN GROUP (ORDER BY (end_ns-start_ns)/1e6) p99_ms
                FROM otlp_spans WHERE project=%s AND start_ns BETWEEN %s AND %s
                  AND (%s='' OR service=%s) AND (%s='' OR env=%s)
            """,
                    (p, start_ns, end_ns, span_service, span_service, span_env, span_env),
                ).fetchone()
            )
            otlp["rps"] = otlp["spans"] / window if window > 0 else 0.0
            previous_otlp = dict(
                db.execute(
                    """
                SELECT count(*) spans,
                       count(*) FILTER (WHERE status_code=2) errors,
                       percentile_cont(.95) WITHIN GROUP (ORDER BY (end_ns-start_ns)/1e6) p95_ms
                FROM otlp_spans WHERE project=%s AND start_ns BETWEEN %s AND %s
                  AND (%s='' OR service=%s) AND (%s='' OR env=%s)
            """,
                    (p, prev_start_ns, start_ns, span_service, span_service, span_env, span_env),
                ).fetchone()
            )
            otlp_hist_rows = db.execute(
                """
                SELECT least(39, greatest(0, floor(((start_ns/1e9)-%s)/%s)::int)) bucket,
                       count(*) total,
                       count(*) FILTER (WHERE status_code=2) errors,
                       percentile_cont(.95) WITHIN GROUP (ORDER BY (end_ns-start_ns)/1e6) p95_ms
                FROM otlp_spans WHERE project=%s AND start_ns BETWEEN %s AND %s
                  AND (%s='' OR service=%s) AND (%s='' OR env=%s)
                GROUP BY bucket
            """,
                (start, width, p, start_ns, end_ns, span_service, span_service, span_env, span_env),
            ).fetchall()
            otlp_by_bucket = {r["bucket"]: r for r in otlp_hist_rows}
            otlp_histogram = [
                {
                    "timestamp": start + i * width,
                    "total": otlp_by_bucket[i]["total"] if i in otlp_by_bucket else 0,
                    "errors": otlp_by_bucket[i]["errors"] if i in otlp_by_bucket else 0,
                    "p95_ms": otlp_by_bucket[i]["p95_ms"] if i in otlp_by_bucket else None,
                }
                for i in range(40)
            ]
            ai_summary = dict(
                db.execute(
                    f"""
                SELECT count(*) runs,
                       count(*) FILTER (WHERE ai_execution_status(status,span_errors)='error') errors,
                       sum(tokens)::bigint tokens,
                       sum(cost_usd) cost_usd
                FROM ai_trace_rollups WHERE {where}
            """,
                    values,
                ).fetchone()
            )
            data_summary = dict(
                db.execute(
                    """
                WITH latest AS (
                    SELECT DISTINCT ON (asset_id) asset_id, state
                    FROM data_observations WHERE project=%s ORDER BY asset_id, measured_at DESC, id DESC
                )
                SELECT count(a.id) assets,
                       count(*) FILTER (WHERE l.state='ok') healthy,
                       count(*) FILTER (WHERE l.state IN ('alert','error')) alerts
                FROM data_assets a LEFT JOIN latest l ON l.asset_id=a.id WHERE a.project=%s
            """,
                    (p, p),
                ).fetchone()
            )
            facets = {
                key: [
                    r[key]
                    for r in db.execute(
                        f"SELECT {key} FROM (SELECT {key} FROM logs WHERE project=%s UNION SELECT {key} FROM metrics WHERE project=%s UNION SELECT {key} FROM otlp_spans WHERE project=%s) AS sources WHERE {key}<>'' ORDER BY {key}",
                        (p, p, p),
                    )
                ]
                for key in ("service", "env")
            }
        return {
            "summary": summary,
            "previous": previous,
            "monitor_states": monitor_states,
            "otlp": otlp,
            "previous_otlp": previous_otlp,
            "otlp_histogram": otlp_histogram,
            "ai": ai_summary,
            "data": data_summary,
            "services": services,
            "histogram": histogram,
            "facets": facets,
        }

    def metrics(self, p, params):
        where, values, start, end = self.scope(p, params)
        with self.connect() as db:
            names = [
                dict(r)
                for r in db.execute(
                    "SELECT name,kind,count(*) samples FROM metrics WHERE project=%s GROUP BY name,kind ORDER BY name",
                    (p,),
                )
            ]
            name = params.get("name") or (names[0]["name"] if names else "")
            kind = next((r["kind"] for r in names if r["name"] == name), "gauge")
            aggregation = "sum" if kind == "count" else "avg"
            width = max(1, (end - start) / 60)
            rows = db.execute(
                f"SELECT floor((timestamp-%s)/%s)::int bucket,{aggregation}(value) value,count(*) samples FROM metrics WHERE {where} AND name=%s GROUP BY bucket ORDER BY bucket",
                [start, width] + values + [name],
            )
            points = [
                {"timestamp": start + r["bucket"] * width, "value": r["value"], "samples": r["samples"]} for r in rows
            ]
        return {
            "names": names,
            "name": name,
            "kind": kind,
            "aggregation": aggregation,
            "bucket_seconds": width,
            "points": points,
        }

    def dashboards(self, p):
        with self.connect() as db:
            return [
                dict(r) for r in db.execute("SELECT * FROM dashboards WHERE project=%s ORDER BY created_at DESC", (p,))
            ]

    def dashboard_frames(self, p, params):
        ident = text(params.get("id"), "dashboard", limit=32)
        position = integer(int(params.get("panel", "-1")), "panel position", 0, 11)
        with self.connect() as db:
            row = db.execute("SELECT panels FROM dashboards WHERE project=%s AND id=%s", (p, ident)).fetchone()
        items = (row["panels"] or []) if row else []
        if position >= len(items) or items[position]["type"] != "table":
            raise AccessError(404, "Table panel not found in this project")
        return self.data.panel_frames(p, items[position])

    def dashboard_panels(self, p, x):
        panels = x.get("panels")
        if panels is not None:
            if not isinstance(panels, list) or not 1 <= len(panels) <= 12:
                raise Invalid("Provide 1 to 12 panels")
            normalized = []
            for panel in panels:
                if not isinstance(panel, dict) or panel.get("type") not in (
                    "metrics",
                    "otlp",
                    "logs",
                    "traces",
                    "table",
                ):
                    raise Invalid("Invalid panel type")
                kind = panel["type"]
                name = text(panel.get("name"), "metric", limit=200) if kind in ("metrics", "otlp") else ""
                item = {"type": kind, "name": name, "title": text(panel.get("title"), "panel", kind, 150)}
                if kind == "table":
                    item.update(
                        asset_id=text(panel.get("asset_id"), "asset", limit=32), mapping=mapping(panel.get("mapping"))
                    )
                    self.data.panel_source(p, item["asset_id"], item["mapping"])
                normalized.append(item)
            panels = normalized
        return panels

    def create_dashboard(self, p, x):
        panels = self.dashboard_panels(p, x)
        item = {
            "id": uuid.uuid4().hex,
            "project": p,
            "title": text(x.get("title"), "title", limit=150),
            "metric": text(x.get("metric"), "metric", limit=200) if panels is None else "",
            "created_at": time.time(),
        }
        with self.connect() as db:
            db.execute(
                """INSERT INTO dashboards(id,project,title,metric,created_at,panels)
                VALUES(%(id)s,%(project)s,%(title)s,%(metric)s,%(created_at)s,%(panels)s)""",
                {**item, "panels": Jsonb(panels) if panels else None},
            )
        if panels:
            item["panels"] = panels
        return item

    def update_dashboard(self, p, x, actor=None):
        ident = text(x.get("id"), "dashboard", limit=32)
        title = text(x.get("title"), "title", limit=150)
        panels = self.dashboard_panels(p, x)
        if panels is None:
            raise Invalid("Provide 1 to 12 panels")
        with self.connect() as db:
            row = db.execute(
                "UPDATE dashboards SET title=%s,metric='',panels=%s WHERE project=%s AND id=%s RETURNING *",
                (title, Jsonb(panels), p, ident),
            ).fetchone()
            if not row:
                raise AccessError(404, "Dashboard not found in this project")
            audit(db, p, actor, "dashboard.update", ident)
        return dict(row)

    def delete_dashboard(self, p, x, actor=None):
        ident = text(x.get("id"), "dashboard", limit=32)
        with self.connect() as db:
            if not db.execute("DELETE FROM dashboards WHERE project=%s AND id=%s", (p, ident)).rowcount:
                raise AccessError(404, "Dashboard not found in this project")
            audit(db, p, actor, "dashboard.delete", ident)
        return {"id": ident, "deleted": True}

    def monitors(self, p, ident=None):
        self.evaluate(p)
        with self.connect() as db:
            items = db.execute(
                """
                SELECT m.*,coalesce(latest.evidence,to_jsonb(legacy)) evidence,
                    coalesce(latest.evidence->>'trace_id',legacy.trace_id) trace_id,
                    coalesce(latest.evidence->>'span_id',legacy.span_id) span_id
                FROM monitors m LEFT JOIN LATERAL (
                    SELECT evidence FROM evaluations WHERE monitor_id=m.id AND project=m.project
                    ORDER BY timestamp DESC,id DESC LIMIT 1
                ) latest ON true LEFT JOIN LATERAL (
                    SELECT s.trace_id,s.span_id FROM otlp_spans s
                    WHERE latest.evidence IS NULL AND m.metric=%s AND m.state='ALERT'
                      AND s.project=m.project AND s.status_code=2
                      AND s.start_ns BETWEEN ((m.evaluated_at-m.window_seconds)*1e9)::bigint
                                         AND (m.evaluated_at*1e9)::bigint
                      AND (m.service='' OR m.service=s.service) AND (m.env='' OR m.env=s.env)
                    ORDER BY s.start_ns DESC,s.span_id LIMIT 1
                ) legacy ON true WHERE m.project=%s AND (%s::text IS NULL OR m.id=%s) ORDER BY m.title
            """,
                (OTLP_ERRORS, p, ident, ident),
            ).fetchall()
            history = db.execute(
                """
                SELECT e.*,m.title FROM evaluations e JOIN monitors m ON m.id=e.monitor_id
                WHERE e.project=%s AND (%s::text IS NULL OR e.monitor_id=%s)
                ORDER BY timestamp DESC,id DESC LIMIT 50
            """,
                (p, ident, ident),
            ).fetchall()
            return {"items": items, "history": history}

    def monitor_fields(self, x):
        op = x.get("op", ">")
        if op not in OPS:
            raise Invalid("invalid operator")
        window = int(number(x.get("window", 300), "window"))
        if not 10 <= window <= 86400:
            raise Invalid("window: de 10 a 86400 segundos")
        config = alert_options(x)
        item = {
            "title": text(x.get("title"), "title", limit=150),
            "metric": text(x.get("metric"), "metric", "@slo" if config["source"] == "slo" else None, 200),
            "service": x.get("service", ""),
            "env": x.get("env", ""),
            "op": op,
            "threshold": number(x.get("threshold"), "threshold"),
            "window": window,
            "configuration": Jsonb(config),
        }
        for field in ("service", "env"):
            if not isinstance(item[field], str) or len(item[field]) > 100:
                raise Invalid(f"Invalid {field}")
        return item

    def create_monitor(self, p, x, actor=None):
        item = {**self.monitor_fields(x), "id": uuid.uuid4().hex, "project": p}
        with self.connect() as db:
            db.execute(
                """INSERT INTO monitors(id,project,title,metric,service,env,op,threshold,window_seconds,state,value,evaluated_at,configuration)
                VALUES(%(id)s,%(project)s,%(title)s,%(metric)s,%(service)s,%(env)s,%(op)s,%(threshold)s,%(window)s,'NO_DATA',NULL,NULL,%(configuration)s)""",
                item,
            )
            audit(db, p, actor, "monitor.create", item["id"])
        self.evaluate(p)
        return next(m for m in self.monitors(p)["items"] if m["id"] == item["id"])

    @staticmethod
    def editable_monitor(db, p, ident):
        row = db.execute(
            "SELECT configuration FROM monitors WHERE project=%s AND id=%s FOR UPDATE", (p, ident)
        ).fetchone()
        if not row:
            raise AccessError(404, "Monitor not found in this project")
        if row["configuration"].get("source") in MANAGED_MONITORS:
            raise AccessError(409, "This monitor is managed by a check; change the source check instead")

    def update_monitor(self, p, x, actor=None):
        ident = text(x.get("id"), "monitor", limit=32)
        item = {**self.monitor_fields(x), "id": ident, "project": p}
        if item["configuration"].obj.get("source") in MANAGED_MONITORS:
            raise Invalid("Invalid monitor source for editing")
        with self.evaluation_lock, self.connect() as db:
            self.editable_monitor(db, p, ident)
            # Keep the current state: the next evaluation records (and notifies) only a real transition.
            db.execute(
                """UPDATE monitors SET title=%(title)s,metric=%(metric)s,service=%(service)s,env=%(env)s,op=%(op)s,
                threshold=%(threshold)s,window_seconds=%(window)s,configuration=%(configuration)s
                WHERE project=%(project)s AND id=%(id)s""",
                item,
            )
            audit(db, p, actor, "monitor.update", ident)
        self.evaluate(p)
        return next(m for m in self.monitors(p)["items"] if m["id"] == ident)

    def delete_monitor(self, p, x, actor=None):
        ident = text(x.get("id"), "monitor", limit=32)
        with self.evaluation_lock, self.connect() as db:
            self.editable_monitor(db, p, ident)
            db.execute("DELETE FROM evaluations WHERE project=%s AND monitor_id=%s", (p, ident))
            db.execute("DELETE FROM monitors WHERE project=%s AND id=%s", (p, ident))
            audit(db, p, actor, "monitor.delete", ident)
        return {"id": ident, "deleted": True}

    def evaluate(self, p=None, now=None):
        now = time.time() if now is None else now
        with self.evaluation_lock, self.connect() as db:
            monitors = db.execute(
                "SELECT * FROM monitors" + (" WHERE project=%s" if p else "") + " ORDER BY id FOR UPDATE",
                (p,) if p else (),
            ).fetchall()
            for m in monitors:
                start = now - m["window_seconds"]
                evidence = {"from": start, "to": now}
                if m["configuration"].get("source") in ("otlp", "slo"):
                    value, evidence = evaluate_otlp(db, m, start, now)
                elif m["configuration"].get("source") == "online":
                    value, evidence = evaluate_online(db, m, start, now)
                elif m["configuration"].get("source") in ("data_quality", "data_job"):
                    value, detail = self.data.evaluate_monitor(db, m, now)
                    evidence.update(detail)
                elif m["configuration"].get("source") == "synthetic":
                    result = db.execute(
                        """SELECT ok,trace_id,span_id,step,timestamp FROM synthetic_results
                        WHERE project=%s AND check_id=%s AND timestamp<=%s
                        ORDER BY timestamp DESC LIMIT 1""",
                        (m["project"], m["configuration"]["check"], now),
                    ).fetchone()
                    value = int(not result["ok"]) if result else None
                    if result:
                        evidence.update(result)
                elif m["metric"] == OTLP_ERRORS:
                    row = db.execute(
                        """
                        SELECT count(*) spans,count(*) FILTER (WHERE status_code=2) errors
                        FROM otlp_spans WHERE project=%s AND start_ns BETWEEN %s AND %s
                        AND (%s='' OR service=%s) AND (%s='' OR env=%s)
                    """,
                        (
                            m["project"],
                            int((now - m["window_seconds"]) * 1e9),
                            int(now * 1e9),
                            m["service"],
                            m["service"],
                            m["env"],
                            m["env"],
                        ),
                    ).fetchone()
                    value = row["errors"] if row["spans"] else None
                    if value:
                        evidence.update(span_evidence(db, m, start, now) or {})
                else:
                    filters = "project=%s AND name=%s AND timestamp>=%s AND timestamp<=%s"
                    values = [m["project"], m["metric"], now - m["window_seconds"], now]
                    for key in ("service", "env"):
                        if m[key]:
                            filters += f" AND {key}=%s"
                            values.append(m[key])
                    row = db.execute(
                        f"SELECT kind,sum(value) total,avg(value) mean FROM metrics WHERE {filters} GROUP BY kind",
                        values,
                    ).fetchone()
                    value = (row["total"] if row["kind"] == "count" else row["mean"]) if row else None
                state = "NO_DATA" if value is None else ("ALERT" if OPS[m["op"]](value, m["threshold"]) else "OK")
                if state != m["state"] or m["evaluated_at"] is None:
                    event = db.execute(
                        """INSERT INTO evaluations(monitor_id,project,timestamp,state,value,evidence)
                        VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (m["id"], m["project"], now, state, value, Jsonb(evidence)),
                    ).fetchone()["id"]
                    self.alerts.enqueue(db, m, event, state, value, now, evidence)
                db.execute(
                    "UPDATE monitors SET state=%s,value=%s,evaluated_at=%s WHERE id=%s", (state, value, now, m["id"])
                )

    def demo(self, p):
        return self.demo_service.ensure(p)


def main():
    parser = argparse.ArgumentParser(description="Datalog local observability")
    parser.add_argument("--port", type=int, default=4310)
    parser.add_argument("--bind", default=os.environ.get("DATALOG_BIND", "127.0.0.1"))
    parser.add_argument(
        "--max-connections",
        type=int,
        default=int(os.environ.get("DATALOG_MAX_CONNECTIONS", "64")),
        help="Requests processed at once before answering 503; open sockets are capped at 4x (default 64)",
    )
    parser.add_argument(
        "--db-pool-size",
        type=int,
        default=int(os.environ.get("DATALOG_DB_POOL_SIZE", "10")),
        help="PostgreSQL connection pool size (default 10)",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL DSN (prefer DATABASE_URL environment variable)",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("DATABASE_URL is required: configure PostgreSQL before starting")
    public_origin = os.environ.get("DATALOG_PUBLIC_ORIGIN", "").rstrip("/") or None
    if public_origin:
        parsed = urlsplit(public_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            parser.error("DATALOG_PUBLIC_ORIGIN must be an HTTPS origin without a path")
    if not 1 <= args.max_connections <= 10000 or not 1 <= args.db_pool_size <= 500:
        parser.error("--max-connections: 1 to 10000; --db-pool-size: 1 to 500")
    store = Store(args.database_url, args.db_pool_size)
    oidc = OIDC(store.connect, public_origin) if os.environ.get("DATALOG_OIDC_ISSUER") else None
    server = DatalogHTTPServer(
        (args.bind, args.port),
        make_handler(store, public_origin, os.environ.get("DATALOG_INTERNAL_METRICS_TOKEN"), oidc),
        args.max_connections,
    )
    stopping = threading.Event()

    def evaluator():
        while not stopping.wait(10):
            try:
                store.workflows.evaluate()
                store.evaluate()
            except Exception as exc:
                print(f"Monitor evaluation failed: {type(exc).__name__}: {exc}", flush=True)

    def deliver():
        while not stopping.wait(2):
            try:
                store.alerts.deliver()
            except Exception as exc:
                print(f"Alert delivery failed: {type(exc).__name__}", flush=True)

    def probe():
        while not stopping.wait(5):
            try:
                store.platform.run_probes()
            except Exception as exc:
                print(f"Synthetic check failed: {type(exc).__name__}", flush=True)

    def retain():
        while not stopping.is_set():
            try:
                store.operational.sweep(sweep(store.connect))
            except Exception as exc:
                store.operational.sweep()
                print(f"Retention failed: {type(exc).__name__}", flush=True)
            stopping.wait(60)

    def quality():
        while not stopping.wait(5):
            try:
                result = store.data.scan()
                if result:
                    store.evaluate(result["project"])
            except Exception as exc:
                print(f"Data quality scan failed: {type(exc).__name__}", flush=True)

    workers = [threading.Thread(target=fn, daemon=True) for fn in (evaluator, deliver, probe, retain, quality)]
    for worker in workers:
        worker.start()

    def _handle_term(_signum, _frame):
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    os_signal.signal(os_signal.SIGTERM, _handle_term)
    print(f"Datalog → http://{args.bind}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        server.server_close()
        for worker in workers:
            worker.join(timeout=2)
        store.close()


if __name__ == "__main__":
    main()
