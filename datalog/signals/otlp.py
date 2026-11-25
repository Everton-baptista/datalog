"""OTLP/HTTP (Protobuf or JSON) trace receiver and read model for the Trace Explorer."""

import base64
import binascii
import gzip
import io
import json
import re
import sys
import zlib
from collections import Counter

from google.protobuf.json_format import MessageToDict, MessageToJson, ParseDict, ParseError
from google.protobuf.message import DecodeError
from google.rpc.status_pb2 import Status
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from datalog.core.validation import Invalid, attribute_filters

PROTOBUF = "application/x-protobuf"
JSON = "application/json"
# OTLP/JSON encodes these bytes fields as hex, while protobuf JSON mapping expects base64.
HEX_FIELDS = {"traceId", "spanId", "parentSpanId", "trace_id", "span_id", "parent_span_id"}
MAX_BODY = 4 * 1024 * 1024
MAX_SPANS = 2000
MAX_NORMALIZED = 16 * 1024 * 1024
# Shape limits checked right after decoding, before any per-item work.
MAX_RESOURCES, MAX_SCOPES, MAX_ATTRIBUTES = 1000, 2000, 128
GROUPS = {"resource_spans": "scope_spans", "resource_logs": "scope_logs", "resource_metrics": "scope_metrics"}


def check_shape(request):
    """Reject requests whose resource/scope structure would amplify normalization work."""
    field = next(name for name in GROUPS if hasattr(request, name))
    resources = getattr(request, field)
    if len(resources) > MAX_RESOURCES:
        raise WireError(400, f"OTLP request exceeds {MAX_RESOURCES} resources")
    scopes = 0
    for resource in resources:
        if len(resource.resource.attributes) > MAX_ATTRIBUTES:
            raise WireError(400, f"OTLP resource exceeds {MAX_ATTRIBUTES} attributes")
        groups = getattr(resource, GROUPS[field])
        scopes += len(groups)
        if scopes > MAX_SCOPES:
            raise WireError(400, f"OTLP request exceeds {MAX_SCOPES} scopes")
        for group in groups:
            if len(group.scope.attributes) > MAX_ATTRIBUTES:
                raise WireError(400, f"OTLP scope exceeds {MAX_ATTRIBUTES} attributes")
    return request


MAX_SPAN = 1024 * 1024
MAX_NS = (1 << 63) - 1
PAGE_SIZE = 50
DETAIL_LIMIT = 500


class WireError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def wire_format(headers):
    """Return the OTLP encoding named by Content-Type: PROTOBUF or JSON."""
    kind = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if kind not in {PROTOBUF, JSON}:
        raise WireError(415, f"Use Content-Type: {PROTOBUF} or {JSON}")
    return kind


def hex_ids(value):
    if isinstance(value, list):
        return [hex_ids(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key in HEX_FIELDS and isinstance(item, str):
            try:
                item = base64.b64encode(bytes.fromhex(item)).decode()
            except ValueError as exc:
                raise WireError(400, f"OTLP/JSON {key} must be hexadecimal") from exc
        out[key] = hex_ids(item)
    return out


def read_request(headers, stream, message_type=ExportTraceServiceRequest, decoded=None):
    """Decode an OTLP body; ``decoded`` (a list) receives the decompressed size for quota accounting."""
    kind = wire_format(headers)
    encoding = headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip"}:
        raise WireError(415, "Use Content-Encoding: gzip or identity")
    try:
        length = int(headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise WireError(400, "Invalid Content-Length") from exc
    if headers.get("Transfer-Encoding") or length < 0:
        raise WireError(400, "Send a body with Content-Length")
    if length > MAX_BODY:
        raise WireError(413, "OTLP request exceeds 4 MiB")
    body = stream.read(length)
    if len(body) != length:
        raise WireError(400, "Incomplete OTLP request body")
    try:
        if encoding == "gzip":
            if not body:
                raise WireError(400, "Missing gzip body")
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as reader:
                body = reader.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise WireError(413, "Decompressed OTLP request exceeds 4 MiB")
        if decoded is not None:
            decoded.append(len(body))
        if kind == PROTOBUF:
            return check_shape(message_type.FromString(body))
        if not body.strip():
            return message_type()
        document = json.loads(body)
        if not isinstance(document, dict):
            raise WireError(400, "OTLP/JSON body must be an object")
        # The OTLP spec asks receivers to ignore unknown fields for forward compatibility.
        return check_shape(ParseDict(hex_ids(document), message_type(), ignore_unknown_fields=True))
    except WireError:
        raise
    except (DecodeError, EOFError, OSError, zlib.error) as exc:
        raise WireError(400, "Invalid OTLP Protobuf or gzip body") from exc
    except (ValueError, ParseError, binascii.Error, RecursionError) as exc:
        raise WireError(400, "Invalid OTLP/JSON body") from exc


def response_type(request_type):
    module = sys.modules[request_type.__module__]
    return getattr(module, request_type.__name__.replace("Request", "Response"))


def encode_response(payload, request_type, kind):
    """Re-encode a serialized Export*ServiceResponse in the client's wire format."""
    if kind == PROTOBUF:
        return payload
    message = response_type(request_type).FromString(payload)
    return MessageToJson(message, indent=None).encode()


def encode_status(message, kind):
    if kind == JSON:
        return json.dumps({"message": message}).encode()
    return error_response(message)


def error_response(message):
    return Status(message=message).SerializeToString()


def checked_id(value, size):
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{size}}}", value) or not int(value, 16):
        raise Invalid(f"Invalid ID: use {size} hexadecimal digits and a non-zero value")
    return value.lower()


def json_document(value):
    # PostgreSQL JSONB does not represent NUL, even when escaped.
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str) and "\0" in item:
            raise Invalid("NUL in span or metadata")
    return json.dumps(value, separators=(",", ":"))


def resource_value(attributes, name):
    return next((item.value.string_value.strip() for item in attributes if item.key == name), "")


def span_row(p, span, service, env, resource, scope):
    trace_id, span_id, parent_id = span.trace_id, span.span_id, span.parent_span_id
    if len(trace_id) != 16 or not any(trace_id) or len(span_id) != 8 or not any(span_id):
        raise Invalid("Invalid trace or span ID")
    if parent_id and (len(parent_id) != 8 or not any(parent_id) or parent_id == span_id):
        raise Invalid("Invalid parent span ID")
    if not 0 < span.start_time_unix_nano <= span.end_time_unix_nano <= MAX_NS:
        raise Invalid("Invalid or unsupported span timestamps")
    if not span.name.strip():
        raise Invalid("Missing span name")
    payload = MessageToDict(span)
    for key in ("traceId", "spanId", "parentSpanId", "name", "startTimeUnixNano", "endTimeUnixNano"):
        payload.pop(key, None)
    for link, raw in zip(span.links, payload.get("links", [])):
        if link.trace_id:
            raw["traceId"] = link.trace_id.hex()
        if link.span_id:
            raw["spanId"] = link.span_id.hex()
    document = json_document(payload)
    size = len(resource) + len(scope) + len(document) + len(span.name.encode("utf-8"))
    if size > MAX_SPAN:
        raise Invalid("Normalized span exceeds 1 MiB")
    if "\0" in span.name:
        raise Invalid("NUL in span name")
    return (
        p,
        trace_id.hex(),
        span_id.hex(),
        parent_id.hex() if parent_id else None,
        span.name,
        service,
        env,
        span.kind,
        span.status.code,
        span.start_time_unix_nano,
        span.end_time_unix_nano,
        resource,
        scope,
        document,
    ), size


class TraceStore:
    def __init__(self, connect, operational=None):
        self.connect = connect
        self.operational = operational

    def ingest(self, p, request, privacy=None, *, wire=False, after_insert=None):
        rows, rejected, seen, size = [], Counter(), 0, 0
        for rs in request.resource_spans:
            attrs = rs.resource.attributes
            if privacy:
                privacy.strip(attrs)
            service = (resource_value(attrs, "service.name") or "unknown_service")[:200]
            env = (
                resource_value(attrs, "deployment.environment.name") or resource_value(attrs, "deployment.environment")
            )[:100]
            try:
                resource = json_document({"schemaUrl": rs.schema_url, **MessageToDict(rs.resource)})
            except Invalid:
                rejected["Invalid resource metadata"] += sum(len(ss.spans) for ss in rs.scope_spans)
                continue
            for ss in rs.scope_spans:
                if privacy:
                    privacy.strip(ss.scope.attributes)
                try:
                    scope = json_document({"schemaUrl": ss.schema_url, **MessageToDict(ss.scope)})
                except Invalid:
                    rejected["Invalid scope metadata"] += len(ss.spans)
                    continue
                for span in ss.spans:
                    seen += 1
                    if seen > MAX_SPANS:
                        rejected["Request exceeds 2000 spans"] += 1
                        continue
                    try:
                        if privacy:
                            privacy.span(span)
                        row, span_size = span_row(p, span, service, env, resource, scope)
                        if size + span_size > MAX_NORMALIZED:
                            raise Invalid("Request exceeds 16 MiB after normalization")
                        rows.append(row)
                        size += span_size
                    except Invalid as exc:
                        rejected[str(exc)] += 1
        diagnostic = "; ".join(f"{reason} ({count})" for reason, count in rejected.items())
        if rejected and not rows:
            if self.operational:
                self.operational.ingest("traces", 0, 0, sum(rejected.values()), wire_project=p if wire else None)
            raise WireError(400, diagnostic)
        inserted = 0
        if rows:
            # Consistent key order across concurrent exports, including overlapping batches.
            rows.sort(key=lambda row: row[:3])
            with self.connect() as db:
                cursor = db.cursor()
                cursor.executemany(
                    """
                    INSERT INTO otlp_spans(project,trace_id,span_id,parent_span_id,name,service,env,
                        kind,status_code,start_ns,end_ns,resource,scope,payload)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
                    ON CONFLICT(project,trace_id,span_id) DO NOTHING
                """,
                    rows,
                )
                inserted = cursor.rowcount
                if after_insert:
                    after_insert(db)
        if self.operational:
            self.operational.ingest(
                "traces",
                len(rows),
                inserted,
                sum(rejected.values()),
                max((row[10] / 1e9 for row in rows), default=None),
                wire_project=p if wire else None,
            )
        response = ExportTraceServiceResponse()
        if rejected:
            response.partial_success.rejected_spans = sum(rejected.values())
            response.partial_success.error_message = diagnostic
        return response.SerializeToString()

    def traces(self, p, params, start, end):
        clauses = ["project=%s", "start_ns BETWEEN %s AND %s"]
        values = [p, int(start * 10**9), min(int(end * 10**9), MAX_NS)]
        for key in ("service", "env"):
            if params.get(key):
                clauses.append(f"{key}=%s")
                values.append(params[key])
        if params.get("attribute"):
            key = params["attribute"]
            if len(key) > 150 or len(params.get("value", "")) > 500:
                raise Invalid("Attribute filter too long")
            condition = "a->>'key'=%s"
            values.append(key)
            if "value" in params:
                condition += " AND coalesce(a->'value'->>'stringValue',a->'value'->>'intValue',a->'value'->>'boolValue',a->'value'->>'doubleValue')=%s"
                values.append(params["value"])
            clauses.append(
                f"EXISTS (SELECT 1 FROM jsonb_array_elements(coalesce(payload->'attributes','[]')) a WHERE {condition})"
            )
        for key, value in attribute_filters(params.get("attrs")).items():
            variants = {"stringValue": str, "boolValue": bool, "intValue": int, "doubleValue": float}
            kinds = [kind for kind, cls in variants.items() if type(value) is cls]
            if type(value) in (int, float):
                condition = (
                    "a->'value' ?| %s AND coalesce(a->'value'->>'intValue',a->'value'->>'doubleValue')::numeric=%s"
                )
                args = [["intValue", "doubleValue"], value]
            else:
                condition = "a->'value'->>%s=%s"
                args = [kinds[0], str(value).lower() if type(value) is bool else value]
            clauses.append(
                f"EXISTS (SELECT 1 FROM jsonb_array_elements(payload->'attributes') a WHERE a->>'key'=%s AND {condition})"
            )
            values.extend([key, *args])
        if "min_ms" in params:
            duration = float(params["min_ms"])
            if not 0 <= duration <= MAX_NS / 10**6:
                raise Invalid("invalid min_ms")
            clauses.append("end_ns-start_ns>=%s")
            values.append(int(duration * 10**6))
        if "status" in params:
            if params["status"] not in ("0", "1", "2"):
                raise Invalid("invalid status")
            clauses.append("status_code=%s")
            values.append(int(params["status"]))
        if params.get("operation"):
            clauses.append("name=%s")
            values.append(params["operation"])
        offset = max(0, min(10000, int(params.get("offset", 0))))
        with self.connect() as db:
            rows = db.execute(
                f"""
                WITH candidates AS (
                    SELECT trace_id, max(start_ns) matched_at FROM otlp_spans
                    WHERE {' AND '.join(clauses)} GROUP BY trace_id
                    ORDER BY matched_at DESC, trace_id LIMIT %s OFFSET %s
                )
                SELECT c.trace_id, left(head.name, 500) name, head.service, head.env,
                    head.parent_span_id IS NULL AS has_root, rollup.*
                FROM candidates c
                CROSS JOIN LATERAL (
                    SELECT name, service, env, parent_span_id FROM otlp_spans
                    WHERE project=%s AND trace_id=c.trace_id
                    ORDER BY (parent_span_id IS NOT NULL), start_ns, span_id LIMIT 1
                ) head
                CROSS JOIN LATERAL (
                    SELECT min(start_ns)::text start_ns, max(end_ns)::text end_ns,
                        count(*) span_count, count(*) FILTER (WHERE status_code=2) error_count,
                        count(DISTINCT service) service_count
                    FROM otlp_spans WHERE project=%s AND trace_id=c.trace_id
                ) rollup
                ORDER BY c.matched_at DESC, c.trace_id
            """,
                values + [PAGE_SIZE + 1, offset, p, p],
            ).fetchall()
            summary = db.execute(
                f"""
                WITH per_trace AS (
                    SELECT trace_id, min(start_ns) start_ns, max(end_ns) end_ns,
                           bool_or(status_code=2) has_error
                    FROM otlp_spans WHERE {' AND '.join(clauses)} GROUP BY trace_id
                )
                SELECT count(*) total,
                       count(*) FILTER (WHERE has_error) errors,
                       percentile_cont(.50) WITHIN GROUP (ORDER BY (end_ns-start_ns)/1e6) p50_ms,
                       percentile_cont(.95) WITHIN GROUP (ORDER BY (end_ns-start_ns)/1e6) p95_ms,
                       percentile_cont(.99) WITHIN GROUP (ORDER BY (end_ns-start_ns)/1e6) p99_ms
                FROM per_trace
            """,
                values,
            ).fetchone()
            width = max(1.0, (end - start) / 40)
            hist_rows = db.execute(
                f"""
                WITH per_trace AS (
                    SELECT trace_id, min(start_ns) start_ns, max(end_ns) end_ns,
                           bool_or(status_code=2) has_error
                    FROM otlp_spans WHERE {' AND '.join(clauses)} GROUP BY trace_id
                )
                SELECT least(39, greatest(0, floor(((start_ns/1e9)-%s)/%s)::int)) bucket,
                       count(*) total,
                       count(*) FILTER (WHERE has_error) errors,
                       percentile_cont(.95) WITHIN GROUP (ORDER BY (end_ns-start_ns)/1e6) p95_ms
                FROM per_trace GROUP BY bucket
            """,
                values + [start, width],
            ).fetchall()
        by_bucket = {r["bucket"]: r for r in hist_rows}
        histogram = [
            {
                "timestamp": start + i * width,
                "total": by_bucket[i]["total"] if i in by_bucket else 0,
                "errors": by_bucket[i]["errors"] if i in by_bucket else 0,
                "p95_ms": by_bucket[i]["p95_ms"] if i in by_bucket else None,
            }
            for i in range(40)
        ]
        return {
            "items": rows[:PAGE_SIZE],
            "offset": offset,
            "limit": PAGE_SIZE,
            "has_more": len(rows) > PAGE_SIZE and offset < 10000,
            "summary": summary,
            "histogram": histogram,
        }

    def detail(self, p, value):
        trace_id = checked_id(value, 32)
        with self.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            summary = db.execute(
                """
                SELECT min(start_ns)::text start_ns, max(end_ns)::text end_ns, count(*) span_count,
                    count(*) FILTER (WHERE status_code=2) error_count
                FROM otlp_spans WHERE project=%s AND trace_id=%s
            """,
                (p, trace_id),
            ).fetchone()
            if not summary["span_count"]:
                return None
            spans = db.execute(
                """
                SELECT span_id, parent_span_id, left(name, 500) name, service, env, kind,
                    status_code, start_ns::text, end_ns::text
                FROM otlp_spans WHERE project=%s AND trace_id=%s
                ORDER BY otlp_spans.start_ns, span_id LIMIT %s
            """,
                (p, trace_id, DETAIL_LIMIT),
            ).fetchall()
            logs = db.execute(
                """
                SELECT id, timestamp, service, env, level, span_id, left(message, 500) message
                FROM logs WHERE project=%s AND trace_id IS NOT NULL AND lower(trace_id)=%s
                ORDER BY timestamp DESC, id DESC LIMIT 51
            """,
                (p, trace_id),
            ).fetchall()
        return {"trace_id": trace_id, **summary, "spans": spans, "logs": logs[:50], "more_logs": len(logs) > 50}

    def span(self, p, trace_id, span_id):
        ids = (p, checked_id(trace_id, 32), checked_id(span_id, 16))
        with self.connect() as db:
            span = db.execute(
                """
                SELECT span_id, parent_span_id, name, service, env, kind, status_code,
                    start_ns::text, end_ns::text, resource, scope, payload
                FROM otlp_spans WHERE project=%s AND trace_id=%s AND span_id=%s
            """,
                ids,
            ).fetchone()
            if span:
                lineage = db.execute(
                    """SELECT sources FROM data_agent_sources
                    WHERE project=%s AND trace_id=%s AND span_id=%s""",
                    ids,
                ).fetchone()
                if lineage:
                    span["data_sources"] = lineage["sources"]
                from datalog.agents.agent_runs import project_span, with_price

                span["ai"] = project_span(span)
                if span["ai"]:
                    span["ai"] = with_price(db, p, span["ai"], int(span["start_ns"]) / 1e9)
                    span["evaluations"] = db.execute(
                        """
                        SELECT evaluator,version,value,outcome,measured_at,cost_usd,attempts,failed_attempts,failure
                        FROM online_scores
                        WHERE project=%s AND trace_id=%s AND span_id=%s ORDER BY measured_at DESC LIMIT 50
                    """,
                        ids,
                    ).fetchall()
                from datalog.signals.trace_media import TraceMedia

                span["media"] = TraceMedia.list(db, ids)
                span["logs"] = db.execute(
                    """
                    SELECT id,timestamp,service,level,left(message,500) message FROM logs
                    WHERE project=%s AND lower(trace_id)=%s AND span_id=%s ORDER BY timestamp DESC LIMIT 50
                """,
                    ids,
                ).fetchall()
            return span

    def services(self, p, params, start, end):
        service, env = params.get("service", ""), params.get("env", "")
        bounds = (p, int(start * 1e9), min(int(end * 1e9), MAX_NS), service, service, env, env)
        with self.connect() as db:
            items = db.execute(
                """
                WITH samples AS (
                    SELECT service,trace_id,span_id,status_code,start_ns,end_ns
                    FROM otlp_spans WHERE project=%s AND start_ns BETWEEN %s AND %s
                    AND (%s='' OR service=%s) AND (%s='' OR env=%s)
                ), summary AS (
                    SELECT service,count(*) spans,count(*) FILTER (WHERE status_code=2) errors,
                        percentile_cont(.50) WITHIN GROUP(ORDER BY (end_ns-start_ns)/1e6) p50_ms,
                        percentile_cont(.95) WITHIN GROUP(ORDER BY (end_ns-start_ns)/1e6) p95_ms,
                        percentile_cont(.99) WITHIN GROUP(ORDER BY (end_ns-start_ns)/1e6) p99_ms
                    FROM samples GROUP BY service ORDER BY errors DESC,spans DESC,service LIMIT 100
                ), failed AS (
                    SELECT DISTINCT ON(service) service,trace_id,span_id FROM samples
                    WHERE status_code=2 ORDER BY service,start_ns DESC,span_id
                )
                SELECT summary.*,failed.trace_id,failed.span_id
                FROM summary LEFT JOIN failed USING(service) ORDER BY errors DESC,spans DESC,service
            """,
                bounds,
            ).fetchall()
            edges = db.execute(
                """
                SELECT parent.service source,child.service target,count(*) spans,
                    count(*) FILTER(WHERE child.status_code=2) errors,
                    percentile_cont(.95) WITHIN GROUP(ORDER BY (child.end_ns-child.start_ns)/1e6) p95_ms,
                    (array_agg(child.trace_id ORDER BY (child.status_code=2) DESC,child.start_ns DESC))[1] trace_id,
                    (array_agg(child.span_id ORDER BY (child.status_code=2) DESC,child.start_ns DESC))[1] span_id
                FROM otlp_spans child JOIN otlp_spans parent
                  ON parent.project=child.project AND parent.trace_id=child.trace_id
                  AND parent.span_id=child.parent_span_id
                WHERE child.project=%s AND child.start_ns BETWEEN %s AND %s
                  AND (%s='' OR child.service=%s OR parent.service=%s)
                  AND (%s='' OR child.env=%s)
                  AND parent.service<>child.service
                GROUP BY parent.service,child.service ORDER BY spans DESC,source,target LIMIT 100
            """,
                (*bounds[:3], service, service, service, env, env),
            ).fetchall()
            operations = db.execute(
                """
                SELECT service,name operation,count(*) requests,
                    count(*) FILTER(WHERE status_code=2) errors,
                    count(*)/%s::double precision rate,
                    percentile_cont(.50) WITHIN GROUP(ORDER BY (end_ns-start_ns)/1e6) p50_ms,
                    percentile_cont(.95) WITHIN GROUP(ORDER BY (end_ns-start_ns)/1e6) p95_ms,
                    percentile_cont(.99) WITHIN GROUP(ORDER BY (end_ns-start_ns)/1e6) p99_ms
                FROM otlp_spans WHERE kind=2 AND project=%s AND start_ns BETWEEN %s AND %s
                AND (%s='' OR service=%s) AND (%s='' OR env=%s)
                GROUP BY service,name ORDER BY errors DESC,requests DESC,service,name LIMIT 100
            """,
                (end - start, *bounds),
            ).fetchall()
        for item in items:
            item["rate"] = item["spans"] / (end - start)
        for edge in edges:
            edge["rate"] = edge["spans"] / (end - start)
        return {"items": items, "edges": edges, "operations": operations}
