"""Lossless OTLP metric points and correlation-compatible OTLP logs."""

import hashlib
import time
from collections import Counter

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceResponse

from datalog.signals.otlp import MAX_NORMALIZED, MAX_NS, MAX_SPAN, MAX_SPANS, WireError, json_document, resource_value
from datalog.core.validation import Invalid
from psycopg.types.json import Jsonb


def lock_logs(db, project):
    # A cursor sees logs in commit order within a project, including concurrent exporters.
    db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("logs:" + project,))


def log_attributes(attributes):
    types = {"stringValue": str, "intValue": int, "doubleValue": float, "boolValue": bool}
    return {
        attribute["key"]: types[kind](value)
        for attribute in attributes
        for kind, value in attribute.get("value", {}).items()
        if kind in types and not (kind == "doubleValue" and isinstance(value, str))
    }


def response(rows, rejected, response_type, rejection_field):
    diagnostic = "; ".join(f"{reason} ({count})" for reason, count in rejected.items())
    if rejected and not rows:
        raise WireError(400, diagnostic)
    reply = response_type()
    if rejected:
        setattr(reply.partial_success, rejection_field, sum(rejected.values()))
        reply.partial_success.error_message = diagnostic
    return reply.SerializeToString()


def environments(resource):
    attributes = resource.attributes
    service = resource_value(attributes, "service.name") or "unknown_service"
    env = resource_value(attributes, "deployment.environment.name") or resource_value(
        attributes, "deployment.environment"
    )
    return service[:200], env[:100]


def limited_rows(items, build):
    rows, rejected, budget = [], Counter(), 0
    for index, item in enumerate(items):
        if index >= MAX_SPANS:
            rejected["Request exceeds 2000 records"] += 1
            continue
        try:
            row, size = build(*item)
            if size > MAX_SPAN or budget + size > MAX_NORMALIZED:
                raise Invalid("Normalized telemetry exceeds storage budget")
            rows.append(row)
            budget += size
        except Invalid as exc:
            rejected[str(exc)] += 1
    return rows, rejected


def log_items(request):
    for group in request.resource_logs:
        for scope in group.scope_logs:
            for log in scope.log_records:
                yield group, scope, log


def metric_items(request):
    for group in request.resource_metrics:
        for scope in group.scope_metrics:
            for metric in scope.metrics:
                kind = metric.WhichOneof("data")
                if not kind:
                    yield group, scope, metric, kind, None
                    continue
                for point in getattr(metric, kind).data_points:
                    yield group, scope, metric, kind, point


class SignalStore:
    def __init__(self, connect, operational=None):
        self.connect = connect
        self.operational = operational

    def report(self, signal, rows, inserted, rejected, wire_project, latest=None):
        if self.operational:
            self.operational.ingest(
                signal, len(rows), inserted, sum(rejected.values()), latest, wire_project=wire_project
            )

    def logs(self, p, request, privacy, *, wire=False):
        received = time.time_ns()

        def build(group, scope, record):
            event_time = record.time_unix_nano or record.observed_time_unix_nano or received
            if not 0 < event_time <= MAX_NS:
                raise Invalid("Invalid log timestamp")
            trace_id = record.trace_id.hex() if record.trace_id else None
            span_id = record.span_id.hex() if record.span_id else None
            if (trace_id and (len(trace_id) != 32 or not any(record.trace_id))) or (
                span_id and (not trace_id or len(span_id) != 16 or not any(record.span_id))
            ):
                raise Invalid("Invalid log correlation IDs")
            uid = resource_value(record.attributes, "log.record.uid")
            if uid:
                identity = b"uid\0" + uid.encode()
            else:
                identity = b"record\0" + b"".join(
                    len(part).to_bytes(4, "big") + part
                    for part in (
                        group.resource.SerializeToString(deterministic=True),
                        scope.scope.SerializeToString(deterministic=True),
                        record.SerializeToString(deterministic=True),
                    )
                )
            severity = record.severity_number
            if severity >= 21:
                level = "critical"
            elif severity >= 17:
                level = "error"
            elif severity >= 13:
                level = "warn"
            elif 1 <= severity < 9:
                level = "debug"
            else:
                level = "info"
            privacy.strip(record.attributes)
            if not privacy.capture:
                record.ClearField("body")
            elif record.HasField("body"):
                privacy.clean_value(record.body)
            privacy.strip(group.resource.attributes)
            privacy.strip(scope.scope.attributes)
            service, env = environments(group.resource)
            raw = MessageToDict(record)
            if trace_id:
                raw["traceId"] = trace_id
            if span_id:
                raw["spanId"] = span_id
            raw["resource"] = MessageToDict(group.resource)
            raw["scope"] = MessageToDict(scope.scope)
            raw["resourceSchemaUrl"] = group.schema_url
            raw["scopeSchemaUrl"] = scope.schema_url
            body = raw.get("body")
            message = record.body.string_value or (
                json_document(body) if body else record.event_name or "OpenTelemetry event"
            )
            document = json_document(raw)
            if "\0" in message:
                raise Invalid("NUL in log")
            return (
                p,
                event_time / 1e9,
                received / 1e9,
                service,
                env,
                level,
                message[:64000],
                trace_id,
                span_id,
                hashlib.sha256(identity).digest(),
                document,
                Jsonb(log_attributes(raw.get("attributes", []))),
            ), len(document)

        # Filtering resource/scope must not mutate the original group, used for deduplication.
        def normalize(group, scope, record):
            header = type(group)(resource=group.resource, schema_url=group.schema_url)
            instrumentation = type(scope)(scope=scope.scope, schema_url=scope.schema_url)
            return build(header, instrumentation, record)

        rows, rejected = limited_rows(log_items(request), normalize)
        if not rows and rejected:
            self.report("logs", rows, 0, rejected, p if wire else None)
        payload = response(rows, rejected, ExportLogsServiceResponse, "rejected_log_records")
        inserted = 0
        if rows:
            with self.connect() as db:
                lock_logs(db, p)
                cursor = db.cursor()
                cursor.executemany(
                    """
                    INSERT INTO logs(project,timestamp,ingested_at,service,env,level,message,trace_id,span_id,otlp_id,raw,tags)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT(project,otlp_id) WHERE otlp_id IS NOT NULL DO NOTHING
                """,
                    sorted(rows, key=lambda row: row[9]),
                )
                inserted = cursor.rowcount
        self.report("logs", rows, inserted, rejected, p if wire else None, max((row[1] for row in rows), default=None))
        return payload

    def metrics(self, p, request, privacy, *, wire=False):
        def build(group, scope, metric, kind, point):
            if not metric.name.strip() or not kind or not 0 < point.time_unix_nano <= MAX_NS:
                raise Invalid("Invalid metric name, kind or timestamp")
            if point.start_time_unix_nano > point.time_unix_nano:
                raise Invalid("Invalid metric start timestamp")
            container = getattr(metric, kind)
            temporality = getattr(container, "aggregation_temporality", None)
            monotonic = container.is_monotonic if kind == "sum" else None
            identity = {
                "name": metric.name,
                "unit": metric.unit,
                "type": kind,
                "temporality": temporality,
                "monotonic": monotonic,
                "resource": sorted(
                    (a.key, a.SerializeToString(deterministic=True).hex()) for a in group.resource.attributes
                ),
                "scope": scope.scope.SerializeToString(deterministic=True).hex(),
                "schema": [group.schema_url, scope.schema_url],
                "attributes": sorted((a.key, a.SerializeToString(deterministic=True).hex()) for a in point.attributes),
            }
            stream_id = hashlib.sha256(json_document(identity).encode()).digest()
            point_id = hashlib.sha256(
                json_document({**identity, "time": point.time_unix_nano, "start": point.start_time_unix_nano}).encode()
            ).digest()
            resource = type(group.resource)()
            resource.CopyFrom(group.resource)
            instrumentation = type(scope.scope)()
            instrumentation.CopyFrom(scope.scope)
            privacy.strip(resource.attributes)
            privacy.strip(instrumentation.attributes)
            privacy.strip(point.attributes)
            for exemplar in getattr(point, "exemplars", []):
                privacy.strip(exemplar.filtered_attributes)
            service, env = environments(resource)
            resource_data = json_document({"schemaUrl": group.schema_url, **MessageToDict(resource)})
            scope_data = json_document({"schemaUrl": scope.schema_url, **MessageToDict(instrumentation)})
            descriptor = json_document(
                {"unit": metric.unit, "description": metric.description if privacy.capture else ""}
            )
            document = MessageToDict(point)
            for exemplar, item in zip(getattr(point, "exemplars", []), document.get("exemplars", [])):
                if exemplar.trace_id:
                    item["traceId"] = exemplar.trace_id.hex()
                if exemplar.span_id:
                    item["spanId"] = exemplar.span_id.hex()
            data = json_document(document)
            size = sum(map(len, [resource_data, scope_data, descriptor, data, metric.name]))
            if "\0" in metric.name:
                raise Invalid("NUL in metric name")
            return (
                p,
                point_id,
                metric.name,
                kind,
                service,
                env,
                point.time_unix_nano,
                temporality,
                monotonic,
                resource_data,
                scope_data,
                descriptor,
                data,
                stream_id,
            ), size

        rows, rejected = limited_rows(metric_items(request), build)
        if not rows and rejected:
            self.report("metrics", rows, 0, rejected, p if wire else None)
        payload = response(rows, rejected, ExportMetricsServiceResponse, "rejected_data_points")
        inserted = 0
        if rows:
            with self.connect() as db:
                cursor = db.cursor()
                cursor.executemany(
                    """
                    INSERT INTO otlp_metrics VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s)
                    ON CONFLICT(project,point_id) DO NOTHING
                """,
                    sorted(rows, key=lambda row: row[1]),
                )
                inserted = cursor.rowcount
        self.report(
            "metrics", rows, inserted, rejected, p if wire else None, max((row[6] / 1e9 for row in rows), default=None)
        )
        return payload

    def metric_points(self, p, params, start, end):
        clauses = ["project=%s", "time_ns BETWEEN %s AND %s"]
        values = [p, int(start * 1e9), min(int(end * 1e9), MAX_NS)]
        for key in ("name", "service", "env"):
            if params.get(key):
                clauses.append(f"{key}=%s")
                values.append(params[key])
        limit = max(1, min(200, int(params.get("limit", 50))))
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT name,kind,service,env,time_ns::text,temporality,monotonic,descriptor,point
                FROM otlp_metrics WHERE {' AND '.join(clauses)}
                ORDER BY otlp_metrics.time_ns DESC,point_id LIMIT %s
            """,
                values + [limit],
            ).fetchall()
        return {"items": rows}
