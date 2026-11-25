"""Execute operator-configured model adapters and record all three OTLP signals."""

import hashlib
import json
import math
import os
import time
import uuid

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from psycopg.types.json import Jsonb

from datalog.auth.access import AccessError
from datalog.core.alerts import configured_targets, post_json
from datalog.auth.privacy import Privacy
from datalog.core.validation import Invalid, text


def set_attr(attrs, key, value):
    fields = {str: "string_value", bool: "bool_value", int: "int_value", float: "double_value"}
    if type(value) in fields:
        setattr(attrs.add(key=key).value, fields[type(value)], value)


def stable_id(*parts):
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).digest()


class ModelExecution:
    def __init__(self, store):
        self.store = store
        self.targets = configured_targets("DATALOG_MODEL_ADAPTERS", "url")
        for name, target in self.targets.items():
            text(target.get("model"), "model", limit=200)
            text(target.get("provider"), "provedor", limit=100)
            projects = target.get("projects", [])
            if not isinstance(projects, list) or any(not isinstance(p, str) for p in projects):
                raise ValueError(f"Invalid projects for {name}")

    def choices(self, p):
        return [
            key
            for key, config in self.targets.items()
            if p in config.get("projects", []) or "*" in config.get("projects", [])
        ]

    def select(self, p, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 4 or any(not isinstance(i, str) for i in ids):
            raise Invalid("Selecione de 1 a 4 adaptadores")
        if len(set(ids)) != len(ids) or any(i not in self.choices(p) for i in ids):
            raise AccessError(403, "Adapter unavailable in this project")
        return [self.targets[i] for i in ids]

    @staticmethod
    def adapter(target, request, key):
        headers = {"Idempotency-Key": key}
        if target.get("token_env"):
            token = os.environ.get(target["token_env"])
            if not token:
                raise AccessError(503, "Adapter credential unavailable")
            headers["Authorization"] = "Bearer " + token
        with post_json(target["url"], {**request, "model": target["model"]}, headers) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("Response exceeds 256 KiB")
        result = json.loads(raw, parse_constant=lambda _: None)
        if not isinstance(result, dict) or "output" not in result:
            raise ValueError("The adapter must return output")
        return result

    def run(self, p, body, stable=None, *, replay=None):
        choices = body.get("adapters")
        configs = self.select(p, choices)
        service = text(body.get("service"), "service", "datalog-agent", 120)
        env = text(body.get("env"), "environment", "production", 80)
        execution_version = text(body.get("execution_version"), "execution version", "interactive", 120)
        policy = text(body.get("policy"), "policy", "ordered-fallback", 120)
        prompt = body.get("prompt")
        model_input = body.get("input")
        evidence = []
        if "retrieval" in body:
            rows, evidence = self.store.data.retrieve(p, body["retrieval"], choices)
            if rows:
                model_input = {"request": model_input, "retrieved_data": rows}
        trace_id = stable_id(p, stable)[:16] if stable else uuid.uuid4().bytes
        root_id = stable_id(trace_id.hex(), "root")[:8]
        traces, logs, metrics = ExportTraceServiceRequest(), ExportLogsServiceRequest(), ExportMetricsServiceRequest()
        trace_resource, log_resource, metric_resource = (
            traces.resource_spans.add(),
            logs.resource_logs.add(),
            metrics.resource_metrics.add(),
        )
        for group in (trace_resource, log_resource, metric_resource):
            set_attr(group.resource.attributes, "service.name", service)
            set_attr(group.resource.attributes, "deployment.environment.name", env)
        scope = trace_resource.scope_spans.add()
        log_scope = log_resource.scope_logs.add()
        metric_scope = metric_resource.scope_metrics.add()
        meter = metric_scope.metrics.add(name="datalog.gen_ai.request.duration", unit="ms")
        meter.histogram.aggregation_temporality = 1
        start = time.time_ns()
        root = scope.spans.add(
            trace_id=trace_id, span_id=root_id, name="agent.execute", kind=2, start_time_unix_nano=start
        )
        set_attr(root.attributes, "gen_ai.operation.name", "invoke_agent")
        set_attr(root.attributes, "datalog.execution.version", execution_version)
        set_attr(root.attributes, "datalog.gateway.policy", policy)
        if replay:
            root.links.add(trace_id=bytes.fromhex(replay[0]), span_id=bytes.fromhex(replay[1]))
        attempts, winner = [], None
        for i, (alias, target) in enumerate(zip(choices, configs)):
            span_id = stable_id(trace_id.hex(), i)[:8]
            begin = time.time_ns()
            span = scope.spans.add(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=root_id,
                name="gen_ai.chat",
                kind=3,
                start_time_unix_nano=begin,
            )
            for key, value in {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": target["provider"],
                "gen_ai.request.model": target["model"],
                "datalog.execution.version": execution_version,
                "datalog.gateway.attempt": i + 1,
                "datalog.gateway.policy": policy,
            }.items():
                set_attr(span.attributes, key, value)
            if evidence:
                set_attr(span.attributes, "datalog.gateway.retrieval", True)
            if prompt:
                set_attr(span.attributes, "datalog.prompt.name", prompt["name"])
                set_attr(span.attributes, "datalog.prompt.version", prompt["version"])
            error = None
            try:
                request = {"input": model_input, "prompt": prompt["content"] if prompt else None}
                if replay:
                    request.update(prompt=replay[2], parameters=replay[3])
                    span.links.add(trace_id=bytes.fromhex(replay[0]), span_id=bytes.fromhex(replay[1]))
                    for key, value in replay[3].items():
                        set_attr(span.attributes, "gen_ai.request." + key, value)
                result = self.adapter(target, request, f"{trace_id.hex()}-{i}")
                winner = result
            except (OSError, ValueError, AccessError) as exc:
                error = type(exc).__name__
                result = {}
                set_attr(span.attributes, "error.type", error)
            end = max(time.time_ns(), begin + 1)
            span.end_time_unix_nano = end
            span.status.code = 2 if error else 1
            for field, key in (
                ("input_tokens", "gen_ai.usage.input_tokens"),
                ("output_tokens", "gen_ai.usage.output_tokens"),
                ("cost_usd", "datalog.cost.usd"),
                ("ttft_ms", "datalog.gen_ai.ttft_ms"),
            ):
                value = result.get(field)
                if type(value) in (float, int) and 0 <= value <= 2**53 - 1 and math.isfinite(value):
                    if field.endswith("_tokens") and type(value) is not int:
                        continue
                    set_attr(span.attributes, key, value)
            if type(result.get("streaming")) is bool:
                set_attr(span.attributes, "datalog.gen_ai.streaming", result["streaming"])
            record = log_scope.log_records.add(
                trace_id=trace_id,
                span_id=span_id,
                time_unix_nano=end,
                event_name="gen_ai.attempt.failed" if error else "gen_ai.attempt.ok",
                severity_number=17 if error else 9,
            )
            set_attr(record.attributes, "log.record.uid", f"{trace_id.hex()}:{i}")
            if error:
                set_attr(record.attributes, "error.type", error)
            duration = (end - begin) / 1e6
            attempts.append({"adapter": alias, "span_id": span_id.hex(), "duration_ms": duration, "error": error})
            if not error:
                break
        root.end_time_unix_nano = max(time.time_ns(), start + 1)
        root.status.code = 1 if winner is not None else 2
        if len(attempts) > 1 and winner is not None:
            for span in list(scope.spans)[1:-1]:
                set_attr(span.attributes, "datalog.recovered", True)
            set_attr(root.attributes, "datalog.recovered", True)
        duration = (root.end_time_unix_nano - start) / 1e6
        bounds = [1, 5, 10, 25, 100, 250, 1000, 5000]
        exemplified = next((a for a in attempts if a["error"]), attempts[-1])
        point = meter.histogram.data_points.add(
            start_time_unix_nano=start,
            time_unix_nano=root.end_time_unix_nano,
            count=1,
            sum=duration,
            explicit_bounds=bounds,
            bucket_counts=[int(i == sum(duration > b for b in bounds)) for i in range(len(bounds) + 1)],
        )
        point.exemplars.add(
            trace_id=trace_id,
            span_id=bytes.fromhex(exemplified["span_id"]),
            time_unix_nano=root.end_time_unix_nano,
            as_double=duration,
        )
        with self.store.connect() as db:
            privacy = Privacy(db.execute("SELECT * FROM projects WHERE id=%s", (p,)).fetchone())
        if privacy.capture:
            captured = {"input": body.get("input")}
            if winner is not None:
                captured["output"] = winner["output"]
            for name, value in captured.items():
                try:
                    content = json.dumps(privacy.clean_json(value), ensure_ascii=False, allow_nan=False)
                except (ValueError, RecursionError):
                    continue
                if len(content.encode()) <= 16000:
                    for span in list(scope.spans)[1:] if name == "input" else [scope.spans[-1]]:
                        set_attr(span.attributes, "datalog.gen_ai." + name, content)
            if replay and replay[2]:
                set_attr(scope.spans[-1].attributes, "datalog.replay.prompt", replay[2])

        def bind_sources(db):
            db.execute(
                """INSERT INTO data_agent_sources VALUES(%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""",
                (p, trace_id.hex(), attempts[-1]["span_id"], Jsonb(evidence)),
            )

        linked = bool(evidence and winner is not None)
        self.store.tracing.ingest(p, traces, privacy, after_insert=bind_sources if linked else None)
        self.store.signals.logs(p, logs, privacy)
        self.store.signals.metrics(p, metrics, privacy)
        self.store.evaluate(p)
        return {
            "trace_id": trace_id.hex(),
            "span_id": attempts[-1]["span_id"],
            "attempts": attempts,
            "status": "ok" if winner is not None else "error",
            "output": winner["output"] if winner is not None else None,
            "duration_ms": duration,
            **({"data_sources": evidence} if linked else {}),
        }
