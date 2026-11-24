"""Project policy for persisted telemetry attributes and payloads."""

import re

SAFE = {
    "service.name",
    "service.namespace",
    "service.version",
    "deployment.environment",
    "deployment.environment.name",
    "telemetry.sdk.name",
    "telemetry.sdk.version",
    "telemetry.sdk.language",
    "http.request.method",
    "http.response.status_code",
    "http.route",
    "rpc.system",
    "rpc.service",
    "rpc.method",
    "db.system.name",
    "db.operation.name",
    "error.type",
    "log.record.uid",
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.system",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "openinference.span.kind",
    "llm.provider",
    "llm.system",
    "llm.model_name",
    "llm.token_count.prompt",
    "llm.token_count.completion",
    "llm.cost.total",
    "datalog.cost.usd",
    "datalog.cost.source",
    "datalog.recovered",
    "datalog.execution.version",
    "datalog.prompt.name",
    "datalog.prompt.version",
    "datalog.gen_ai.streaming",
    "datalog.gen_ai.ttft_ms",
    "datalog.gen_ai.first_token_ns",
    "gen_ai.response.time_to_first_chunk",
    "datalog.gateway.attempt",
    "datalog.gateway.policy",
    "datalog.gateway.retrieval",
    "host.id",
    "host.name",
    "k8s.namespace.name",
    "k8s.pod.name",
    "k8s.pod.uid",
    "k8s.container.name",
    "container.id",
    "tool.name",
}
SECRETS = re.compile(
    r"authorization|(?:^|[._-])(?:cookie|password|passwd|secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|credential|session[_-]?token)(?:$|[._-])",
    re.I,
)


class Privacy:
    def __init__(self, policy):
        self.capture = policy["capture_content"]
        self.allowed = SAFE.union(policy["allowed_attributes"])

    def strip(self, attributes):
        for index in range(len(attributes) - 1, -1, -1):
            attr = attributes[index]
            if SECRETS.search(attr.key) or (not self.capture and attr.key not in self.allowed):
                del attributes[index]
            else:
                self.clean_value(attr.value)

    def clean_value(self, value):
        kind = value.WhichOneof("value")
        if kind == "kvlist_value":
            self.strip(value.kvlist_value.values)
        if kind == "array_value":
            for item in value.array_value.values:
                self.clean_value(item)

    def clean_json(self, value, only_allowed=False):
        if isinstance(value, dict):
            return {
                key: self.clean_json(child, only_allowed)
                for key, child in value.items()
                if not SECRETS.search(key) and (not only_allowed or key in self.allowed)
            }
        if isinstance(value, list):
            return [self.clean_json(child, only_allowed) for child in value]
        return value

    def span(self, span):
        self.strip(span.attributes)
        for event in span.events:
            self.strip(event.attributes)
        for link in span.links:
            self.strip(link.attributes)
        if not self.capture:
            span.ClearField("trace_state")
            for link in span.links:
                link.ClearField("trace_state")
            if span.HasField("status"):
                span.status.ClearField("message")

    def ai_snapshot(self, body):
        body = self.clean_json(body)
        for trace in body if isinstance(body, list) else [body]:
            if not isinstance(trace, dict):
                continue
            spans = trace.get("spans")
            for part in [trace] + (spans if isinstance(spans, list) else []):
                if not isinstance(part, dict):
                    continue
                if not self.capture:
                    for key in ("input", "output", "user_id"):
                        part.pop(key, None)
                    if "metadata" in part:
                        part["metadata"] = self.clean_json(part["metadata"], only_allowed=True)
        return body
